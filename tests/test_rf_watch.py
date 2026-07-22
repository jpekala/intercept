"""Tests for the RF watch anomaly engine (Tier 3 continuous monitoring)."""

import os

import pytest

os.environ.setdefault("INTERCEPT_SKIP_DEFERRED_INIT", "1")

numpy = pytest.importorskip("numpy")

from utils.tscm.rf_watch import AnomalyEngine, SpectrumFrame, WaterfallBuffer


def _frame(freqs, power_db, noise_floor=-90.0, band="TEST", timestamp=1000.0):
    return SpectrumFrame(
        timestamp=timestamp,
        center_freq=float(numpy.mean(freqs)),
        freqs=numpy.array(freqs, dtype=numpy.float64),
        power_db=numpy.array(power_db, dtype=numpy.float64),
        noise_floor=noise_floor,
        band=band,
    )


class TestBaselineLookup:
    def test_exact_match(self):
        eng = AnomalyEngine()
        eng.set_baseline_arrays(
            numpy.array([100.0, 200.0, 300.0]),
            numpy.array([-50.0, -60.0, -70.0]),
        )
        assert eng._lookup_baseline(200.0) == -60.0

    def test_nearest_within_tolerance(self):
        """Sweep grid vs FFT grid: 38.4375 baseline should match 38.44 query."""
        eng = AnomalyEngine()
        eng.set_baseline_arrays(
            numpy.array([38.4375, 100.0]),
            numpy.array([-40.0, -55.0]),
        )
        assert eng._lookup_baseline(38.44) == -40.0
        assert eng._lookup_baseline(38.40) == -40.0

    def test_outside_tolerance_misses(self):
        eng = AnomalyEngine()
        eng.set_baseline_arrays(
            numpy.array([100.0]),
            numpy.array([-50.0]),
        )
        assert eng._lookup_baseline(100.5) is None

    def test_no_baseline(self):
        eng = AnomalyEngine()
        assert eng._lookup_baseline(100.0) is None


class TestNewSignalCooldown:
    def test_fires_once_then_suppressed(self):
        eng = AnomalyEngine()
        eng.set_baseline_arrays(
            numpy.array([500.0]),  # baseline far from our test signal
            numpy.array([-50.0]),
        )
        freqs = [100.0]
        power = [-60.0]  # 30 dB above -90 noise floor -> new_signal

        first = eng.process_frame(_frame(freqs, power, timestamp=1000.0))
        second = eng.process_frame(_frame(freqs, power, timestamp=1001.0))

        new1 = [a for a in first if a.anomaly_type == "new_signal"]
        new2 = [a for a in second if a.anomaly_type == "new_signal"]
        assert len(new1) == 1
        assert len(new2) == 0

    def test_refires_after_cooldown(self):
        eng = AnomalyEngine()
        eng.set_baseline_arrays(numpy.array([500.0]), numpy.array([-50.0]))
        freqs = [100.0]
        power = [-60.0]

        eng.process_frame(_frame(freqs, power, timestamp=1000.0))
        later = eng.process_frame(
            _frame(freqs, power, timestamp=1000.0 + AnomalyEngine.NEW_SIGNAL_COOLDOWN_S + 1)
        )
        assert len([a for a in later if a.anomaly_type == "new_signal"]) == 1

    def test_baseline_match_no_new_signal(self):
        """A signal near a baseline bin must not fire new_signal."""
        eng = AnomalyEngine()
        eng.set_baseline_arrays(numpy.array([100.01]), numpy.array([-58.0]))
        result = eng.process_frame(_frame([100.0], [-60.0]))
        assert len([a for a in result if a.anomaly_type == "new_signal"]) == 0


class TestSpikeDetection:
    def test_spike_over_short_term_avg(self):
        eng = AnomalyEngine()
        freqs = [100.0]
        # Build a stable short-term average
        for i in range(5):
            eng.process_frame(_frame(freqs, [-80.0], timestamp=1000.0 + i))
        # Sudden +25 dB jump
        result = eng.process_frame(_frame(freqs, [-55.0], timestamp=1010.0))
        spikes = [a for a in result if a.anomaly_type == "spike"]
        assert len(spikes) == 1
        assert spikes[0].delta_db > AnomalyEngine.SPIKE_THRESHOLD


class TestStateCap:
    def test_short_term_capped(self):
        eng = AnomalyEngine()
        eng._short_term = {f"{i}.0000": None for i in range(200_001)}
        eng.process_frame(_frame([100.0], [-80.0]))
        assert len(eng._short_term) < 200_000


class TestWaterfallBuffer:
    def test_add_and_summary(self):
        buf = WaterfallBuffer(max_frames=10)
        buf.add_frame(_frame([100.0, 101.0], [-50.0, -80.0], timestamp=1000.0))
        buf.add_frame(_frame([100.0, 101.0], [-52.0, -79.0], timestamp=1001.0))
        s = buf.get_summary()
        assert s["frame_count"] == 2
        assert s["duration_s"] == 1.0

    def test_max_frames_rolls(self):
        buf = WaterfallBuffer(max_frames=3)
        for i in range(5):
            buf.add_frame(_frame([100.0], [-60.0], timestamp=1000.0 + i))
        assert buf.get_summary()["frame_count"] == 3
