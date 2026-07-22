"""
Spectral baseline tracking for TSCM RF monitoring.

Stores per-frequency-bin power levels over time and detects:
- New transmitters (power in bins that were previously at noise floor)
- Significant power changes (bins deviating from historical mean)
- Disappeared transmitters (bins that went quiet)
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("intercept.tscm.spectral_baseline")


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


class SpectralStore:
    """Stores and retrieves spectral baseline data in SQLite."""

    def __init__(self):
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
                CREATE TABLE IF NOT EXISTS tscm_spectral_bins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    baseline_id INTEGER NOT NULL,
                    freq_key TEXT NOT NULL,
                    frequency_mhz REAL NOT NULL,
                    band TEXT,
                    power_mean REAL NOT NULL,
                    power_min REAL,
                    power_max REAL,
                    power_stdev REAL,
                    sample_count INTEGER DEFAULT 1,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (baseline_id) REFERENCES tscm_spectral_baselines(id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_spectral_bins_baseline_freq
                ON tscm_spectral_bins(baseline_id, freq_key)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tscm_spectral_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    baseline_id INTEGER NOT NULL,
                    sweep_id INTEGER,
                    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    bin_data TEXT NOT NULL,
                    noise_floors TEXT,
                    FOREIGN KEY (baseline_id) REFERENCES tscm_spectral_baselines(id)
                )
            """)

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
        with get_db() as conn:
            conn.execute("DELETE FROM tscm_spectral_bins WHERE baseline_id = ?", (baseline_id,))
            conn.execute("DELETE FROM tscm_spectral_snapshots WHERE baseline_id = ?", (baseline_id,))
            conn.execute("DELETE FROM tscm_spectral_baselines WHERE id = ?", (baseline_id,))

    def ingest_snapshot(self, baseline_id: int, snapshot: SpectralSnapshot) -> None:
        """Ingest a spectral snapshot, updating per-bin running statistics."""
        from utils.database import get_db
        with get_db() as conn:
            conn.execute(
                "INSERT INTO tscm_spectral_snapshots (baseline_id, sweep_id, bin_data, noise_floors) VALUES (?, ?, ?, ?)",
                (baseline_id, snapshot.sweep_id, json.dumps(snapshot.bins), json.dumps(snapshot.noise_floors)),
            )

            existing = {}
            rows = conn.execute(
                "SELECT freq_key, power_mean, power_min, power_max, power_stdev, sample_count FROM tscm_spectral_bins WHERE baseline_id = ?",
                (baseline_id,),
            ).fetchall()
            for r in rows:
                existing[r["freq_key"]] = dict(r)

            for freq_key, power in snapshot.bins.items():
                freq_mhz = float(freq_key)
                band = _freq_to_band(freq_mhz)

                if freq_key in existing:
                    old = existing[freq_key]
                    n = old["sample_count"]
                    old_mean = old["power_mean"]
                    new_mean = old_mean + (power - old_mean) / (n + 1)
                    # Welford's online variance
                    old_stdev = old["power_stdev"] or 0.0
                    old_var = old_stdev ** 2 * n
                    new_var = old_var + (power - old_mean) * (power - new_mean)
                    new_stdev = (new_var / (n + 1)) ** 0.5 if n + 1 > 1 else 0.0

                    conn.execute(
                        """UPDATE tscm_spectral_bins
                           SET power_mean = ?, power_min = MIN(power_min, ?),
                               power_max = MAX(power_max, ?), power_stdev = ?,
                               sample_count = ?, last_updated = CURRENT_TIMESTAMP
                           WHERE baseline_id = ? AND freq_key = ?""",
                        (new_mean, power, power, new_stdev, n + 1, baseline_id, freq_key),
                    )
                else:
                    conn.execute(
                        """INSERT INTO tscm_spectral_bins
                           (baseline_id, freq_key, frequency_mhz, band, power_mean, power_min, power_max, power_stdev, sample_count)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1)""",
                        (baseline_id, freq_key, freq_mhz, band, power, power, power),
                    )

            conn.execute(
                """UPDATE tscm_spectral_baselines
                   SET snapshot_count = snapshot_count + 1,
                       bin_count = (SELECT COUNT(*) FROM tscm_spectral_bins WHERE baseline_id = ?),
                       bands = ?
                   WHERE id = ?""",
                (baseline_id, json.dumps(list(snapshot.noise_floors.keys())), baseline_id),
            )

    def get_bins(self, baseline_id: int) -> dict[str, dict]:
        """Get all spectral bins for a baseline, keyed by freq_key."""
        from utils.database import get_db
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM tscm_spectral_bins WHERE baseline_id = ?",
                (baseline_id,),
            ).fetchall()
            return {r["freq_key"]: dict(r) for r in rows}


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
        """Compare a snapshot against a spectral baseline."""
        baseline_bins = self.store.get_bins(baseline_id)
        if not baseline_bins:
            logger.info("No spectral baseline bins to compare against")
            return []

        anomalies: list[SpectralAnomaly] = []

        for freq_key, current_power in snapshot.bins.items():
            freq_mhz = float(freq_key)
            band = _freq_to_band(freq_mhz)
            noise_floor = snapshot.noise_floors.get(band, -100.0)

            if freq_key in baseline_bins:
                bl = baseline_bins[freq_key]
                bl_mean = bl["power_mean"]
                bl_stdev = bl["power_stdev"] or 3.0
                delta = current_power - bl_mean

                if delta > self.POWER_CHANGE_THRESHOLD:
                    sigma = delta / max(bl_stdev, 1.0)
                    anomalies.append(SpectralAnomaly(
                        frequency_mhz=freq_mhz,
                        anomaly_type="power_increase",
                        current_power=current_power,
                        baseline_power=bl_mean,
                        delta_db=delta,
                        band=band,
                        severity=_severity_from_delta(delta),
                        confidence=min(1.0, sigma / 5.0),
                    ))
                elif delta < self.DISAPPEAR_THRESHOLD and current_power < noise_floor + 3:
                    anomalies.append(SpectralAnomaly(
                        frequency_mhz=freq_mhz,
                        anomaly_type="disappeared",
                        current_power=current_power,
                        baseline_power=bl_mean,
                        delta_db=delta,
                        band=band,
                        severity="low",
                        confidence=min(1.0, abs(delta) / 20.0),
                    ))
            else:
                above_noise = current_power - noise_floor
                if above_noise >= self.NEW_TRANSMITTER_THRESHOLD:
                    anomalies.append(SpectralAnomaly(
                        frequency_mhz=freq_mhz,
                        anomaly_type="new_transmitter",
                        current_power=current_power,
                        baseline_power=None,
                        delta_db=above_noise,
                        band=band,
                        severity=_severity_from_delta(above_noise),
                        confidence=min(1.0, above_noise / 20.0),
                    ))

        for freq_key, bl in baseline_bins.items():
            if freq_key not in snapshot.bins and bl["power_mean"] > -80:
                freq_mhz = float(freq_key)
                band = bl.get("band", _freq_to_band(freq_mhz))
                anomalies.append(SpectralAnomaly(
                    frequency_mhz=freq_mhz,
                    anomaly_type="disappeared",
                    current_power=-120.0,
                    baseline_power=bl["power_mean"],
                    delta_db=-120.0 - bl["power_mean"],
                    band=band,
                    severity="low",
                    confidence=0.7,
                ))

        anomalies.sort(key=lambda a: abs(a.delta_db), reverse=True)
        return anomalies


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
