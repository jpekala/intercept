"""
Spectral baseline tracking for TSCM RF monitoring.

Stores per-frequency-bin power levels as numpy .npz array files and detects:
- New transmitters (power in bins that were previously at noise floor)
- Significant power changes (bins deviating from historical mean)
- Disappeared transmitters (bins that went quiet)

Performance: vectorized numpy operations with np.searchsorted alignment
give ~20-60x speedup over the previous SQLite row-per-bin approach.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("intercept.tscm.spectral_baseline")

_HAS_NUMPY = True
try:
    import numpy as np
except ImportError:
    _HAS_NUMPY = False

_SPECTRAL_DIR = Path(__file__).parent.parent.parent / "instance" / "spectral"


@dataclass
class SpectralAnomaly:
    """A detected anomaly in the RF spectrum."""
    frequency_mhz: float
    anomaly_type: str  # "new_transmitter", "power_increase", "power_decrease", "disappeared"
    current_power: float
    baseline_power: float | None
    delta_db: float
    band: str
    severity: str  # "low", "medium", "high", "critical"
    confidence: float  # 0.0-1.0

    def to_dict(self) -> dict:
        return {
            "frequency_mhz": round(self.frequency_mhz, 4),
            "anomaly_type": self.anomaly_type,
            "current_power": round(self.current_power, 1),
            "baseline_power": round(self.baseline_power, 1) if self.baseline_power is not None else None,
            "delta_db": round(self.delta_db, 1),
            "band": self.band,
            "severity": self.severity,
            "confidence": round(self.confidence, 2),
        }


@dataclass
class SpectralSnapshot:
    """A snapshot of spectrum power levels at a point in time."""
    timestamp: float
    bins: dict[str, float]  # freq_key -> power_dBm
    noise_floors: dict[str, float]  # band -> noise_floor_dBm
    sweep_id: int | None = None

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "bin_count": len(self.bins),
            "bands": list(self.noise_floors.keys()),
            "sweep_id": self.sweep_id,
        }


@dataclass
class BaselineArrays:
    """Numpy arrays holding spectral baseline data, sorted by frequency."""
    freqs: Any       # float64 sorted frequencies (MHz)
    mean: Any        # float64 power means (dBm)
    min_p: Any       # float64 power minimums
    max_p: Any       # float64 power maximums
    stdev: Any       # float64 power std deviations
    m2: Any          # float64 Welford M2 accumulators
    count: Any       # int32 sample counts
    band_ids: Any    # uint8 band enum indices

    @property
    def size(self) -> int:
        return len(self.freqs)


BAND_NAMES = [
    "HF", "VHF-Low", "FM", "VHF", "UHF-Low", "UHF",
    "UHF-High", "L-Band", "GPS/GNSS", "S-Band/WiFi",
    "S-Band", "C-Band/WiFi", "SHF",
]
_BAND_TO_ID = {b: i for i, b in enumerate(BAND_NAMES)}


class SpectralStore:
    """Stores spectral baseline metadata in SQLite, bin data as numpy .npz files."""

    def __init__(self):
        _SPECTRAL_DIR.mkdir(parents=True, exist_ok=True)
        self._ensure_tables()

    def _ensure_tables(self):
        from utils.database import get_db
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tscm_spectral_baselines (
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
                CREATE TABLE IF NOT EXISTS tscm_spectral_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    baseline_id INTEGER NOT NULL,
                    sweep_id INTEGER,
                    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    bin_count INTEGER DEFAULT 0,
                    noise_floors TEXT,
                    FOREIGN KEY (baseline_id) REFERENCES tscm_spectral_baselines(id)
                )
            """)

    def _npz_path(self, baseline_id: int) -> Path:
        return _SPECTRAL_DIR / f"baseline_{baseline_id}.npz"

    def _save_arrays(self, baseline_id: int, arrays: BaselineArrays) -> None:
        """Atomically save baseline arrays to .npz file."""
        target = self._npz_path(baseline_id)
        # Suffix must end in .npz so np.savez_compressed doesn't append one
        fd, tmp = tempfile.mkstemp(dir=_SPECTRAL_DIR, suffix=".tmp.npz")
        os.close(fd)
        try:
            np.savez_compressed(
                tmp,
                freqs=arrays.freqs,
                mean=arrays.mean,
                min_p=arrays.min_p,
                max_p=arrays.max_p,
                stdev=arrays.stdev,
                m2=arrays.m2,
                count=arrays.count,
                band_ids=arrays.band_ids,
            )
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _load_arrays(self, baseline_id: int) -> BaselineArrays | None:
        path = self._npz_path(baseline_id)
        if not path.exists():
            return self._migrate_from_sqlite(baseline_id)
        try:
            with np.load(path) as data:
                return BaselineArrays(
                    freqs=data["freqs"],
                    mean=data["mean"],
                    min_p=data["min_p"],
                    max_p=data["max_p"],
                    stdev=data["stdev"],
                    m2=data["m2"],
                    count=data["count"],
                    band_ids=data["band_ids"],
                )
        except Exception as e:
            logger.warning(f"Failed to load spectral arrays for baseline {baseline_id}: {e}")
            return None

    def _migrate_from_sqlite(self, baseline_id: int) -> BaselineArrays | None:
        """One-time migration: convert legacy tscm_spectral_bins rows to .npz."""
        try:
            from utils.database import get_db
            with get_db() as conn:
                try:
                    rows = conn.execute(
                        "SELECT frequency_mhz, band, power_mean, power_min, power_max, power_stdev, sample_count "
                        "FROM tscm_spectral_bins WHERE baseline_id = ? ORDER BY frequency_mhz",
                        (baseline_id,),
                    ).fetchall()
                except Exception:
                    return None

            if not rows:
                return None

            freqs = np.array([r["frequency_mhz"] for r in rows], dtype=np.float64)
            mean = np.array([r["power_mean"] for r in rows], dtype=np.float64)
            min_p = np.array([r["power_min"] or r["power_mean"] for r in rows], dtype=np.float64)
            max_p = np.array([r["power_max"] or r["power_mean"] for r in rows], dtype=np.float64)
            stdev = np.array([r["power_stdev"] or 0.0 for r in rows], dtype=np.float64)
            count = np.array([r["sample_count"] or 1 for r in rows], dtype=np.int32)
            m2 = stdev ** 2 * count.astype(np.float64)
            band_ids = np.array(
                [_BAND_TO_ID.get(r["band"] or _freq_to_band(r["frequency_mhz"]), 0) for r in rows],
                dtype=np.uint8,
            )

            arrays = BaselineArrays(
                freqs=freqs, mean=mean, min_p=min_p, max_p=max_p,
                stdev=stdev, m2=m2, count=count, band_ids=band_ids,
            )
            self._save_arrays(baseline_id, arrays)
            logger.info(f"Migrated spectral baseline {baseline_id}: {len(rows)} bins from SQLite to .npz")
            return arrays
        except Exception as e:
            logger.debug(f"SQLite migration not available for baseline {baseline_id}: {e}")
            return None

    def create_baseline(self, name: str, description: str = "") -> int:
        from utils.database import get_db
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO tscm_spectral_baselines (name, description) VALUES (?, ?)",
                (name, description),
            )
            return cursor.lastrowid

    def activate_baseline(self, baseline_id: int) -> None:
        from utils.database import get_db
        with get_db() as conn:
            conn.execute("UPDATE tscm_spectral_baselines SET is_active = 0")
            conn.execute(
                "UPDATE tscm_spectral_baselines SET is_active = 1 WHERE id = ?",
                (baseline_id,),
            )

    def get_active_baseline_id(self) -> int | None:
        from utils.database import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT id FROM tscm_spectral_baselines WHERE is_active = 1"
            ).fetchone()
            return row["id"] if row else None

    def list_baselines(self) -> list[dict]:
        from utils.database import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM tscm_spectral_baselines ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_baseline(self, baseline_id: int) -> dict | None:
        from utils.database import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM tscm_spectral_baselines WHERE id = ?",
                (baseline_id,),
            ).fetchone()
            return dict(row) if row else None

    def delete_baseline(self, baseline_id: int) -> None:
        from utils.database import get_db
        npz = self._npz_path(baseline_id)
        if npz.exists():
            try:
                npz.unlink()
            except OSError as e:
                logger.warning(f"Could not delete {npz}: {e}")
        with get_db() as conn:
            conn.execute("DELETE FROM tscm_spectral_snapshots WHERE baseline_id = ?", (baseline_id,))
            conn.execute("DELETE FROM tscm_spectral_baselines WHERE id = ?", (baseline_id,))
            # Clean up legacy SQLite bins table if it exists
            try:
                conn.execute("DELETE FROM tscm_spectral_bins WHERE baseline_id = ?", (baseline_id,))
            except Exception:
                pass

    def ingest_snapshot(self, baseline_id: int, snapshot: SpectralSnapshot) -> None:
        """Ingest a spectral snapshot using vectorized Welford's update."""
        if not _HAS_NUMPY:
            logger.warning("numpy not available, skipping spectral ingestion")
            return

        from utils.database import get_db

        # Log snapshot metadata to SQLite
        with get_db() as conn:
            conn.execute(
                "INSERT INTO tscm_spectral_snapshots (baseline_id, sweep_id, bin_count, noise_floors) VALUES (?, ?, ?, ?)",
                (baseline_id, snapshot.sweep_id, len(snapshot.bins), json.dumps(snapshot.noise_floors)),
            )

        if not snapshot.bins:
            return

        # Convert snapshot to sorted numpy arrays
        snap_freqs_list = sorted(snapshot.bins.keys(), key=float)
        snap_freqs = np.array([float(k) for k in snap_freqs_list], dtype=np.float64)
        snap_power = np.array([snapshot.bins[k] for k in snap_freqs_list], dtype=np.float64)
        snap_bands = np.array(
            [_BAND_TO_ID.get(_freq_to_band(f), 0) for f in snap_freqs], dtype=np.uint8,
        )

        existing = self._load_arrays(baseline_id)

        if existing is None or existing.size == 0:
            # First snapshot: initialize arrays directly
            arrays = BaselineArrays(
                freqs=snap_freqs,
                mean=snap_power.copy(),
                min_p=snap_power.copy(),
                max_p=snap_power.copy(),
                stdev=np.zeros_like(snap_power),
                m2=np.zeros_like(snap_power),
                count=np.ones(len(snap_freqs), dtype=np.int32),
                band_ids=snap_bands,
            )
        else:
            # Find which snapshot bins already exist in baseline
            idx = np.searchsorted(existing.freqs, snap_freqs)
            idx = np.clip(idx, 0, existing.size - 1)
            matched = np.abs(existing.freqs[idx] - snap_freqs) < 1e-6

            # Update existing bins (vectorized Welford's)
            match_idx = idx[matched]
            match_power = snap_power[matched]
            n = existing.count[match_idx].astype(np.float64)
            old_mean = existing.mean[match_idx]
            new_n = n + 1.0
            delta = match_power - old_mean
            new_mean = old_mean + delta / new_n
            delta2 = match_power - new_mean
            new_m2 = existing.m2[match_idx] + delta * delta2

            existing.mean[match_idx] = new_mean
            existing.min_p[match_idx] = np.minimum(existing.min_p[match_idx], match_power)
            existing.max_p[match_idx] = np.maximum(existing.max_p[match_idx], match_power)
            existing.m2[match_idx] = new_m2
            existing.count[match_idx] = new_n.astype(np.int32)
            existing.stdev[match_idx] = np.where(
                new_n > 1,
                np.sqrt(new_m2 / new_n),
                0.0,
            )

            # New bins not in baseline
            new_mask = ~matched
            if np.any(new_mask):
                new_freqs = snap_freqs[new_mask]
                new_power = snap_power[new_mask]
                new_bands = snap_bands[new_mask]

                # Merge sorted arrays
                all_freqs = np.concatenate([existing.freqs, new_freqs])
                all_mean = np.concatenate([existing.mean, new_power])
                all_min = np.concatenate([existing.min_p, new_power])
                all_max = np.concatenate([existing.max_p, new_power])
                all_stdev = np.concatenate([existing.stdev, np.zeros(len(new_freqs))])
                all_m2 = np.concatenate([existing.m2, np.zeros(len(new_freqs))])
                all_count = np.concatenate([existing.count, np.ones(len(new_freqs), dtype=np.int32)])
                all_bands = np.concatenate([existing.band_ids, new_bands])

                order = np.argsort(all_freqs)
                existing = BaselineArrays(
                    freqs=all_freqs[order],
                    mean=all_mean[order],
                    min_p=all_min[order],
                    max_p=all_max[order],
                    stdev=all_stdev[order],
                    m2=all_m2[order],
                    count=all_count[order],
                    band_ids=all_bands[order],
                )

            arrays = existing

        self._save_arrays(baseline_id, arrays)

        # Update metadata
        bands_seen = list(snapshot.noise_floors.keys())
        with get_db() as conn:
            conn.execute(
                """UPDATE tscm_spectral_baselines
                   SET snapshot_count = snapshot_count + 1,
                       bin_count = ?,
                       bands = ?
                   WHERE id = ?""",
                (arrays.size, json.dumps(bands_seen), baseline_id),
            )

    def get_bins(self, baseline_id: int) -> dict[str, dict]:
        """Get spectral bins as a dict, for backward compatibility with watch daemon."""
        if not _HAS_NUMPY:
            return {}
        arrays = self._load_arrays(baseline_id)
        if arrays is None:
            return {}
        result = {}
        for i in range(arrays.size):
            freq_key = f"{arrays.freqs[i]:.4f}"
            result[freq_key] = {
                "freq_key": freq_key,
                "frequency_mhz": float(arrays.freqs[i]),
                "band": BAND_NAMES[arrays.band_ids[i]] if arrays.band_ids[i] < len(BAND_NAMES) else "unknown",
                "power_mean": float(arrays.mean[i]),
                "power_min": float(arrays.min_p[i]),
                "power_max": float(arrays.max_p[i]),
                "power_stdev": float(arrays.stdev[i]),
                "sample_count": int(arrays.count[i]),
            }
        return result

    def get_arrays(self, baseline_id: int) -> BaselineArrays | None:
        """Get raw numpy arrays for vectorized comparison."""
        if not _HAS_NUMPY:
            return None
        return self._load_arrays(baseline_id)


class SpectralDeltaEngine:
    """Compares a current spectral snapshot against a stored baseline."""

    NEW_TRANSMITTER_THRESHOLD = 10.0  # dB above noise floor to count as new
    POWER_CHANGE_THRESHOLD = 6.0     # dB change to flag
    DISAPPEAR_THRESHOLD = -10.0      # dB drop to flag as disappeared

    def __init__(self, store: SpectralStore):
        self.store = store

    def compare(
        self,
        snapshot: SpectralSnapshot,
        baseline_id: int,
    ) -> list[SpectralAnomaly]:
        """Compare a snapshot against a spectral baseline using vectorized ops."""
        if not _HAS_NUMPY:
            return []

        arrays = self.store.get_arrays(baseline_id)
        if arrays is None or arrays.size == 0:
            logger.info("No spectral baseline bins to compare against")
            return []

        # Convert snapshot to sorted arrays (may be empty)
        snap_keys = sorted(snapshot.bins.keys(), key=float) if snapshot.bins else []
        snap_freqs = np.array([float(k) for k in snap_keys], dtype=np.float64) if snap_keys else np.array([], dtype=np.float64)
        snap_power = np.array([snapshot.bins[k] for k in snap_keys], dtype=np.float64) if snap_keys else np.array([], dtype=np.float64)

        # Build band -> noise_floor lookup for snapshot frequencies
        snap_noise = np.full(len(snap_freqs), -100.0, dtype=np.float64)
        for i, f in enumerate(snap_freqs):
            band = _freq_to_band(float(f))
            snap_noise[i] = snapshot.noise_floors.get(band, -100.0)

        anomalies: list[SpectralAnomaly] = []

        # Align snapshot bins to baseline using searchsorted
        idx = np.searchsorted(arrays.freqs, snap_freqs)
        idx_clipped = np.clip(idx, 0, arrays.size - 1)
        matched = np.abs(arrays.freqs[idx_clipped] - snap_freqs) < 1e-6

        # === Matched bins: check for power changes ===
        if np.any(matched):
            m_idx = idx_clipped[matched]
            m_power = snap_power[matched]
            m_freqs = snap_freqs[matched]

            bl_mean = arrays.mean[m_idx]
            bl_stdev = arrays.stdev[m_idx]
            bl_stdev_safe = np.maximum(bl_stdev, 1.0)
            delta = m_power - bl_mean

            # Power increases
            inc_mask = delta > self.POWER_CHANGE_THRESHOLD
            if np.any(inc_mask):
                inc_delta = delta[inc_mask]
                inc_sigma = inc_delta / bl_stdev_safe[inc_mask]
                for j in range(int(inc_mask.sum())):
                    ii = np.nonzero(inc_mask)[0][j]
                    freq = float(m_freqs[ii])
                    anomalies.append(SpectralAnomaly(
                        frequency_mhz=freq,
                        anomaly_type="power_increase",
                        current_power=float(m_power[ii]),
                        baseline_power=float(bl_mean[ii]),
                        delta_db=float(inc_delta[j]),
                        band=_freq_to_band(freq),
                        severity=_severity_from_delta(float(inc_delta[j])),
                        confidence=min(1.0, float(inc_sigma[j]) / 5.0),
                    ))

            # Disappeared (in-place)
            m_noise = snap_noise[matched]
            dis_mask = (delta < self.DISAPPEAR_THRESHOLD) & (m_power < m_noise + 3)
            if np.any(dis_mask):
                for ii in np.nonzero(dis_mask)[0]:
                    freq = float(m_freqs[ii])
                    anomalies.append(SpectralAnomaly(
                        frequency_mhz=freq,
                        anomaly_type="disappeared",
                        current_power=float(m_power[ii]),
                        baseline_power=float(bl_mean[ii]),
                        delta_db=float(delta[ii]),
                        band=_freq_to_band(freq),
                        severity="low",
                        confidence=min(1.0, abs(float(delta[ii])) / 20.0),
                    ))

        # === Unmatched snapshot bins: new transmitters ===
        new_mask = ~matched
        if np.any(new_mask):
            new_freqs = snap_freqs[new_mask]
            new_power = snap_power[new_mask]
            new_noise = snap_noise[new_mask]
            above_noise = new_power - new_noise
            transmitter_mask = above_noise >= self.NEW_TRANSMITTER_THRESHOLD

            if np.any(transmitter_mask):
                for ii in np.nonzero(transmitter_mask)[0]:
                    freq = float(new_freqs[ii])
                    an = float(above_noise[ii])
                    anomalies.append(SpectralAnomaly(
                        frequency_mhz=freq,
                        anomaly_type="new_transmitter",
                        current_power=float(new_power[ii]),
                        baseline_power=None,
                        delta_db=an,
                        band=_freq_to_band(freq),
                        severity=_severity_from_delta(an),
                        confidence=min(1.0, an / 20.0),
                    ))

        # === Baseline bins not in snapshot: disappeared strong signals ===
        # Vectorized: find baseline bins with no match in snapshot
        bl_idx = np.searchsorted(snap_freqs, arrays.freqs)
        bl_idx_clipped = np.clip(bl_idx, 0, len(snap_freqs) - 1)
        if len(snap_freqs) > 0:
            bl_not_in_snap = np.abs(snap_freqs[bl_idx_clipped] - arrays.freqs) > 1e-6
        else:
            bl_not_in_snap = np.ones(arrays.size, dtype=bool)
        strong_mask = bl_not_in_snap & (arrays.mean > -80)

        if np.any(strong_mask):
            for ii in np.nonzero(strong_mask)[0]:
                freq = float(arrays.freqs[ii])
                bl_power = float(arrays.mean[ii])
                band = BAND_NAMES[arrays.band_ids[ii]] if arrays.band_ids[ii] < len(BAND_NAMES) else _freq_to_band(freq)
                anomalies.append(SpectralAnomaly(
                    frequency_mhz=freq,
                    anomaly_type="disappeared",
                    current_power=-120.0,
                    baseline_power=bl_power,
                    delta_db=-120.0 - bl_power,
                    band=band,
                    severity="low",
                    confidence=0.7,
                ))

        anomalies.sort(key=lambda a: abs(a.delta_db), reverse=True)
        return anomalies


class SpectralAccumulator:
    """Buffers high-frequency watch frames and flushes to store periodically.

    Used by the watch daemon to avoid writing .npz on every frame.
    Accumulates power samples in-memory and flushes every flush_interval seconds.
    """

    def __init__(self, store: SpectralStore, baseline_id: int, flush_interval: float = 10.0):
        self.store = store
        self.baseline_id = baseline_id
        self.flush_interval = flush_interval
        self._lock = threading.Lock()
        self._bins: dict[str, list[float]] = {}  # freq_key -> [power samples]
        self._noise_floors: dict[str, list[float]] = {}
        self._last_flush = time.time()
        self._sample_count = 0

    def add_frame(self, freqs, power_db, noise_floor: float, band: str) -> None:
        """Accumulate a spectrum frame. freqs and power_db are numpy arrays."""
        if not _HAS_NUMPY:
            return
        with self._lock:
            for i in range(len(freqs)):
                freq_key = f"{float(freqs[i]):.4f}"
                if freq_key not in self._bins:
                    self._bins[freq_key] = []
                self._bins[freq_key].append(float(power_db[i]))
            self._noise_floors.setdefault(band, []).append(noise_floor)
            self._sample_count += 1

            if time.time() - self._last_flush >= self.flush_interval:
                self._flush_locked()

    def flush(self) -> None:
        """Force a flush of accumulated data."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._bins:
            self._last_flush = time.time()
            return

        # Take max power per bin across accumulated frames
        bins = {k: max(v) for k, v in self._bins.items()}
        noise_floors = {k: statistics.mean(v) for k, v in self._noise_floors.items()}

        snapshot = SpectralSnapshot(
            timestamp=time.time(),
            bins=bins,
            noise_floors=noise_floors,
        )
        try:
            self.store.ingest_snapshot(self.baseline_id, snapshot)
        except Exception as e:
            logger.debug(f"Spectral accumulator flush failed: {e}")

        self._bins.clear()
        self._noise_floors.clear()
        self._last_flush = time.time()
        self._sample_count = 0


def build_snapshot_from_rf_signals(rf_signals: list[dict], sweep_id: int | None = None) -> SpectralSnapshot:
    """Convert raw RF signal list from a sweep into a SpectralSnapshot."""
    bins: dict[str, float] = {}
    noise_floors: dict[str, list[float]] = {}

    for sig in rf_signals:
        freq = sig.get("frequency") or sig.get("frequency_mhz")
        power = sig.get("power") or sig.get("level")
        if freq is None or power is None:
            continue
        try:
            freq = float(freq)
            power = float(power)
        except (ValueError, TypeError):
            continue

        freq_key = f"{freq:.4f}"
        if freq_key not in bins or power > bins[freq_key]:
            bins[freq_key] = power

        band = sig.get("band", _freq_to_band(freq))
        nf = sig.get("noise_floor")
        if nf is not None:
            try:
                noise_floors.setdefault(band, []).append(float(nf))
            except (ValueError, TypeError):
                pass

    avg_noise = {}
    for band, samples in noise_floors.items():
        avg_noise[band] = statistics.mean(samples)

    return SpectralSnapshot(
        timestamp=time.time(),
        bins=bins,
        noise_floors=avg_noise,
        sweep_id=sweep_id,
    )


def _freq_to_band(freq_mhz: float) -> str:
    if freq_mhz < 30:
        return "HF"
    elif freq_mhz < 88:
        return "VHF-Low"
    elif freq_mhz < 108:
        return "FM"
    elif freq_mhz < 174:
        return "VHF"
    elif freq_mhz < 400:
        return "UHF-Low"
    elif freq_mhz < 512:
        return "UHF"
    elif freq_mhz < 960:
        return "UHF-High"
    elif freq_mhz < 1300:
        return "L-Band"
    elif freq_mhz < 2000:
        return "GPS/GNSS"
    elif freq_mhz < 2500:
        return "S-Band/WiFi"
    elif freq_mhz < 4000:
        return "S-Band"
    elif freq_mhz < 6000:
        return "C-Band/WiFi"
    else:
        return "SHF"


def _severity_from_delta(delta_db: float) -> str:
    if delta_db >= 20:
        return "critical"
    elif delta_db >= 15:
        return "high"
    elif delta_db >= 10:
        return "medium"
    return "low"
