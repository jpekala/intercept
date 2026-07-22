"""Tests for numpy-backed spectral baseline storage and comparison."""

import os
import sys
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Guard against conftest importing the Flask app (fails on Windows due to termios)
os.environ.setdefault("INTERCEPT_SKIP_DEFERRED_INIT", "1")

numpy = pytest.importorskip("numpy")

from utils.tscm.spectral_baseline import (
    BaselineArrays,
    SpectralAccumulator,
    SpectralAnomaly,
    SpectralDeltaEngine,
    SpectralSnapshot,
    SpectralStore,
    _freq_to_band,
    _severity_from_delta,
    build_snapshot_from_rf_signals,
)


@pytest.fixture
def tmp_spectral_dir(tmp_path):
    """Override _SPECTRAL_DIR to a temp directory."""
    with patch("utils.tscm.spectral_baseline._SPECTRAL_DIR", tmp_path):
        yield tmp_path


@pytest.fixture
def mock_db():
    """Mock the database so no real SQLite is needed."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute("""
        CREATE TABLE tscm_spectral_baselines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            snapshot_count INTEGER DEFAULT 0,
            bin_count INTEGER DEFAULT 0,
            bands TEXT,
            is_active BOOLEAN DEFAULT 0,
            description TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE tscm_spectral_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            baseline_id INTEGER NOT NULL,
            sweep_id INTEGER,
            captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            bin_count INTEGER DEFAULT 0,
            noise_floors TEXT
        )
    """)

    class FakeCtx:
        def __enter__(self):
            return conn
        def __exit__(self, *args):
            conn.commit()

    with patch("utils.tscm.spectral_baseline.get_db", return_value=FakeCtx()) as mock:
        # Also need to patch the import inside methods
        with patch.dict("sys.modules", {}):
            with patch("utils.tscm.spectral_baseline.SpectralStore._ensure_tables"):
                yield conn, FakeCtx


@pytest.fixture
def store(tmp_spectral_dir, mock_db):
    """Create a SpectralStore backed by temp dir and in-memory SQLite."""
    conn, FakeCtx = mock_db
    with patch("utils.tscm.spectral_baseline.SpectralStore._ensure_tables"):
        s = SpectralStore.__new__(SpectralStore)
        s._ensure_tables = lambda: None
        # Patch get_db in the module
        import utils.tscm.spectral_baseline as mod
        original_get_db = None
        def fake_get_db():
            return FakeCtx()
        with patch.object(mod, "get_db", fake_get_db, create=True):
            pass
        s._get_db = fake_get_db
    return s, conn, FakeCtx


def _make_store(tmp_path):
    """Helper to create a fully functional store with in-memory SQLite."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE tscm_spectral_baselines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            snapshot_count INTEGER DEFAULT 0,
            bin_count INTEGER DEFAULT 0,
            bands TEXT,
            is_active BOOLEAN DEFAULT 0,
            description TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE tscm_spectral_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            baseline_id INTEGER NOT NULL,
            sweep_id INTEGER,
            captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            bin_count INTEGER DEFAULT 0,
            noise_floors TEXT
        )
    """)
    conn.commit()

    class FakeCtx:
        def __enter__(self):
            return conn
        def __exit__(self, *args):
            conn.commit()

    import utils.tscm.spectral_baseline as mod

    # Inject a module-level get_db so the deferred imports inside methods find it
    orig_spectral_dir = mod._SPECTRAL_DIR
    mod._SPECTRAL_DIR = tmp_path

    fake_db_mod = MagicMock()
    fake_db_mod.get_db = FakeCtx

    # Patch utils.database at import time so `from utils.database import get_db`
    # inside each method resolves to our fake
    orig_db_module = sys.modules.get("utils.database")
    sys.modules["utils.database"] = fake_db_mod

    try:
        with patch.object(SpectralStore, "_ensure_tables", lambda self: None):
            s = SpectralStore()
    finally:
        mod._SPECTRAL_DIR = orig_spectral_dir
        if orig_db_module is not None:
            sys.modules["utils.database"] = orig_db_module
        else:
            sys.modules.pop("utils.database", None)

    # Keep the patches active on the returned store
    s._test_spectral_dir = tmp_path
    s._test_fake_db = fake_db_mod
    s._test_orig_db = orig_db_module

    # Wrap each public method to apply patches during execution
    def _wrap(method_name):
        orig = getattr(SpectralStore, method_name)
        def wrapper(self, *args, **kwargs):
            saved_dir = mod._SPECTRAL_DIR
            saved_db = sys.modules.get("utils.database")
            mod._SPECTRAL_DIR = self._test_spectral_dir
            sys.modules["utils.database"] = self._test_fake_db
            try:
                return orig(self, *args, **kwargs)
            finally:
                mod._SPECTRAL_DIR = saved_dir
                if saved_db is not None:
                    sys.modules["utils.database"] = saved_db
                else:
                    sys.modules.pop("utils.database", None)
        setattr(s, method_name, wrapper.__get__(s))

    for m in ("create_baseline", "activate_baseline", "get_active_baseline_id",
              "list_baselines", "get_baseline", "delete_baseline", "ingest_snapshot",
              "get_bins", "get_arrays"):
        _wrap(m)

    return s


# ─── Unit tests ───


class TestFreqToBand:
    def test_hf(self):
        assert _freq_to_band(14.0) == "HF"

    def test_fm(self):
        assert _freq_to_band(100.0) == "FM"

    def test_uhf(self):
        assert _freq_to_band(450.0) == "UHF"

    def test_wifi(self):
        assert _freq_to_band(2450.0) == "S-Band/WiFi"


class TestSeverityFromDelta:
    def test_low(self):
        assert _severity_from_delta(5.0) == "low"

    def test_medium(self):
        assert _severity_from_delta(12.0) == "medium"

    def test_high(self):
        assert _severity_from_delta(17.0) == "high"

    def test_critical(self):
        assert _severity_from_delta(25.0) == "critical"


class TestBuildSnapshotFromRfSignals:
    def test_basic(self):
        signals = [
            {"frequency": 100.5, "power": -50.0, "band": "FM", "noise_floor": -100},
            {"frequency": 100.5, "power": -45.0, "band": "FM", "noise_floor": -98},
            {"frequency": 200.0, "power": -60.0, "band": "VHF", "noise_floor": -95},
        ]
        snap = build_snapshot_from_rf_signals(signals, sweep_id=1)
        assert snap.sweep_id == 1
        assert len(snap.bins) == 2
        assert snap.bins["100.5000"] == -45.0  # max power kept
        assert "FM" in snap.noise_floors

    def test_empty(self):
        snap = build_snapshot_from_rf_signals([])
        assert len(snap.bins) == 0

    def test_invalid_values_skipped(self):
        signals = [
            {"frequency": "invalid", "power": -50},
            {"frequency": 100.0, "power": "bad"},
            {"frequency": None, "power": -50},
        ]
        snap = build_snapshot_from_rf_signals(signals)
        assert len(snap.bins) == 0


class TestBaselineArrays:
    def test_size(self):
        ba = BaselineArrays(
            freqs=numpy.array([1.0, 2.0, 3.0]),
            mean=numpy.zeros(3),
            min_p=numpy.zeros(3),
            max_p=numpy.zeros(3),
            stdev=numpy.zeros(3),
            m2=numpy.zeros(3),
            count=numpy.ones(3, dtype=numpy.int32),
            band_ids=numpy.zeros(3, dtype=numpy.uint8),
        )
        assert ba.size == 3


class TestSpectralStoreArrayIO:
    def test_save_and_load(self, tmp_path):
        with patch("utils.tscm.spectral_baseline._SPECTRAL_DIR", tmp_path):
            with patch("utils.tscm.spectral_baseline.SpectralStore._ensure_tables"):
                s = SpectralStore.__new__(SpectralStore)

            freqs = numpy.array([100.0, 200.0, 300.0], dtype=numpy.float64)
            arrays = BaselineArrays(
                freqs=freqs,
                mean=numpy.array([-50.0, -60.0, -70.0]),
                min_p=numpy.array([-55.0, -65.0, -75.0]),
                max_p=numpy.array([-45.0, -55.0, -65.0]),
                stdev=numpy.array([2.0, 3.0, 1.0]),
                m2=numpy.array([8.0, 18.0, 2.0]),
                count=numpy.array([3, 3, 3], dtype=numpy.int32),
                band_ids=numpy.array([2, 3, 3], dtype=numpy.uint8),
            )
            s._save_arrays(1, arrays)

            loaded = s._load_arrays(1)
            assert loaded is not None
            assert loaded.size == 3
            numpy.testing.assert_allclose(loaded.freqs, freqs)
            numpy.testing.assert_allclose(loaded.mean, arrays.mean)
            numpy.testing.assert_array_equal(loaded.count, arrays.count)

    def test_load_nonexistent(self, tmp_path):
        with patch("utils.tscm.spectral_baseline._SPECTRAL_DIR", tmp_path):
            with patch("utils.tscm.spectral_baseline.SpectralStore._ensure_tables"):
                s = SpectralStore.__new__(SpectralStore)
            assert s._load_arrays(999) is None


class TestSpectralStoreIngest:
    def test_first_snapshot(self, tmp_path):
        s = _make_store(tmp_path)

        bl_id = s.create_baseline("test")
        snap = SpectralSnapshot(
            timestamp=1000.0,
            bins={"100.0000": -50.0, "200.0000": -60.0},
            noise_floors={"FM": -100.0},
            sweep_id=1,
        )
        s.ingest_snapshot(bl_id, snap)

        arrays = s.get_arrays(bl_id)
        assert arrays is not None
        assert arrays.size == 2
        numpy.testing.assert_allclose(arrays.freqs, [100.0, 200.0])
        numpy.testing.assert_allclose(arrays.mean, [-50.0, -60.0])
        numpy.testing.assert_array_equal(arrays.count, [1, 1])

    def test_welford_update(self, tmp_path):
        s = _make_store(tmp_path)
        bl_id = s.create_baseline("test")

        snap1 = SpectralSnapshot(
            timestamp=1000.0,
            bins={"100.0000": -50.0},
            noise_floors={"FM": -100.0},
        )
        s.ingest_snapshot(bl_id, snap1)

        snap2 = SpectralSnapshot(
            timestamp=1001.0,
            bins={"100.0000": -40.0},
            noise_floors={"FM": -100.0},
        )
        s.ingest_snapshot(bl_id, snap2)

        arrays = s.get_arrays(bl_id)
        assert arrays is not None
        assert arrays.count[0] == 2
        numpy.testing.assert_allclose(arrays.mean[0], -45.0)
        numpy.testing.assert_allclose(arrays.min_p[0], -50.0)
        numpy.testing.assert_allclose(arrays.max_p[0], -40.0)
        assert arrays.stdev[0] > 0

    def test_new_bins_merged(self, tmp_path):
        s = _make_store(tmp_path)
        bl_id = s.create_baseline("test")

        snap1 = SpectralSnapshot(
            timestamp=1000.0,
            bins={"100.0000": -50.0, "300.0000": -70.0},
            noise_floors={"FM": -100.0},
        )
        s.ingest_snapshot(bl_id, snap1)

        snap2 = SpectralSnapshot(
            timestamp=1001.0,
            bins={"200.0000": -60.0},
            noise_floors={"VHF": -95.0},
        )
        s.ingest_snapshot(bl_id, snap2)

        arrays = s.get_arrays(bl_id)
        assert arrays.size == 3
        numpy.testing.assert_allclose(arrays.freqs, [100.0, 200.0, 300.0])


class TestSpectralStoreGetBins:
    def test_backward_compat(self, tmp_path):
        s = _make_store(tmp_path)
        bl_id = s.create_baseline("test")

        snap = SpectralSnapshot(
            timestamp=1000.0,
            bins={"100.0000": -50.0},
            noise_floors={"FM": -100.0},
        )
        s.ingest_snapshot(bl_id, snap)

        bins = s.get_bins(bl_id)
        assert "100.0000" in bins
        assert bins["100.0000"]["power_mean"] == -50.0
        assert bins["100.0000"]["sample_count"] == 1
        assert "band" in bins["100.0000"]


class TestSpectralDeltaEngine:
    def _make_baseline(self, tmp_path, bins_data):
        s = _make_store(tmp_path)
        bl_id = s.create_baseline("test")
        snap = SpectralSnapshot(
            timestamp=1000.0,
            bins=bins_data,
            noise_floors={"FM": -100.0, "VHF": -95.0},
        )
        s.ingest_snapshot(bl_id, snap)
        return s, bl_id

    def test_no_anomalies_same_data(self, tmp_path):
        s, bl_id = self._make_baseline(tmp_path, {"100.0000": -50.0, "200.0000": -60.0})
        engine = SpectralDeltaEngine(s)
        snap = SpectralSnapshot(
            timestamp=2000.0,
            bins={"100.0000": -50.0, "200.0000": -60.0},
            noise_floors={"FM": -100.0},
        )
        anomalies = engine.compare(snap, bl_id)
        assert len(anomalies) == 0

    def test_power_increase(self, tmp_path):
        s, bl_id = self._make_baseline(tmp_path, {"100.0000": -70.0})
        engine = SpectralDeltaEngine(s)
        snap = SpectralSnapshot(
            timestamp=2000.0,
            bins={"100.0000": -50.0},  # +20 dB
            noise_floors={"FM": -100.0},
        )
        anomalies = engine.compare(snap, bl_id)
        power_inc = [a for a in anomalies if a.anomaly_type == "power_increase"]
        assert len(power_inc) == 1
        assert power_inc[0].delta_db == pytest.approx(20.0, abs=0.1)

    def test_new_transmitter(self, tmp_path):
        s, bl_id = self._make_baseline(tmp_path, {"100.0000": -50.0})
        engine = SpectralDeltaEngine(s)
        snap = SpectralSnapshot(
            timestamp=2000.0,
            bins={"100.0000": -50.0, "300.0000": -80.0},
            noise_floors={"FM": -100.0, "VHF": -95.0},
        )
        anomalies = engine.compare(snap, bl_id)
        new_tx = [a for a in anomalies if a.anomaly_type == "new_transmitter"]
        assert len(new_tx) == 1
        assert new_tx[0].frequency_mhz == 300.0

    def test_disappeared_signal(self, tmp_path):
        s, bl_id = self._make_baseline(tmp_path, {"100.0000": -50.0, "200.0000": -60.0})
        engine = SpectralDeltaEngine(s)
        snap = SpectralSnapshot(
            timestamp=2000.0,
            bins={"100.0000": -50.0},
            noise_floors={"FM": -100.0},
        )
        anomalies = engine.compare(snap, bl_id)
        disappeared = [a for a in anomalies if a.anomaly_type == "disappeared"]
        assert len(disappeared) == 1
        assert disappeared[0].frequency_mhz == 200.0

    def test_empty_baseline_no_crash(self, tmp_path):
        s = _make_store(tmp_path)
        bl_id = s.create_baseline("empty")
        engine = SpectralDeltaEngine(s)
        snap = SpectralSnapshot(timestamp=1000.0, bins={"100.0000": -50.0}, noise_floors={})
        assert engine.compare(snap, bl_id) == []

    def test_empty_snapshot_no_crash(self, tmp_path):
        s, bl_id = self._make_baseline(tmp_path, {"100.0000": -50.0})
        engine = SpectralDeltaEngine(s)
        snap = SpectralSnapshot(timestamp=2000.0, bins={}, noise_floors={})
        anomalies = engine.compare(snap, bl_id)
        disappeared = [a for a in anomalies if a.anomaly_type == "disappeared"]
        assert len(disappeared) == 1


class TestSpectralAccumulator:
    def test_accumulate_and_flush(self, tmp_path):
        s = _make_store(tmp_path)
        bl_id = s.create_baseline("test")

        acc = SpectralAccumulator(s, bl_id, flush_interval=0.01)

        freqs = numpy.array([100.0, 200.0], dtype=numpy.float64)
        power1 = numpy.array([-50.0, -60.0], dtype=numpy.float64)
        power2 = numpy.array([-45.0, -55.0], dtype=numpy.float64)

        acc.add_frame(freqs, power1, -100.0, "FM")
        acc.add_frame(freqs, power2, -100.0, "FM")

        import time
        time.sleep(0.02)
        acc.add_frame(freqs, power1, -100.0, "FM")  # triggers flush

        arrays = s.get_arrays(bl_id)
        assert arrays is not None
        assert arrays.size == 2

    def test_flush_empty(self, tmp_path):
        s = _make_store(tmp_path)
        bl_id = s.create_baseline("test")
        acc = SpectralAccumulator(s, bl_id)
        acc.flush()  # should not crash


class TestSpectralAnomaly:
    def test_to_dict(self):
        a = SpectralAnomaly(
            frequency_mhz=100.1234,
            anomaly_type="new_transmitter",
            current_power=-50.0,
            baseline_power=None,
            delta_db=15.0,
            band="FM",
            severity="high",
            confidence=0.75,
        )
        d = a.to_dict()
        assert d["frequency_mhz"] == 100.1234
        assert d["baseline_power"] is None
        assert d["severity"] == "high"
