"""
Continuous RF watch daemon for TSCM monitoring.

Streams from a SoapySDR device, runs rolling FFT, maintains a waterfall
buffer, and fires anomaly alerts when the spectrum deviates from the
spectral baseline.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("intercept.tscm.rf_watch")

_HAS_DEPS = True
try:
    import numpy as np
except ImportError:
    _HAS_DEPS = False

try:
    import SoapySDR as _SoapySDR
except ImportError:
    _SoapySDR = None


@dataclass
class SpectrumFrame:
    """One FFT frame from the watch daemon."""
    timestamp: float
    center_freq: float
    freqs: Any  # numpy array of frequency values in MHz
    power_db: Any  # numpy array of power in dBm
    noise_floor: float
    band: str


@dataclass
class WatchAnomaly:
    """An anomaly detected by the continuous watch engine."""
    timestamp: float
    frequency_mhz: float
    anomaly_type: str  # "spike", "new_signal", "drift", "burst"
    power_db: float
    baseline_power: float | None
    delta_db: float
    duration_s: float
    band: str
    severity: str

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "frequency_mhz": round(self.frequency_mhz, 4),
            "anomaly_type": self.anomaly_type,
            "power_db": round(self.power_db, 1),
            "baseline_power": round(self.baseline_power, 1) if self.baseline_power is not None else None,
            "delta_db": round(self.delta_db, 1),
            "duration_s": round(self.duration_s, 1),
            "band": self.band,
            "severity": self.severity,
        }


class WaterfallBuffer:
    """Rolling buffer of spectrum frames for waterfall display and history."""

    def __init__(self, max_frames: int = 3600):
        self._frames: deque[dict] = deque(maxlen=max_frames)
        self._lock = threading.Lock()

    def add_frame(self, frame: SpectrumFrame) -> None:
        with self._lock:
            self._frames.append({
                "timestamp": frame.timestamp,
                "center_freq": frame.center_freq,
                "noise_floor": frame.noise_floor,
                "band": frame.band,
                "peak_power": float(frame.power_db.max()) if len(frame.power_db) > 0 else -120,
                "signal_count": int((frame.power_db > frame.noise_floor + 6).sum()) if len(frame.power_db) > 0 else 0,
            })

    def get_recent(self, seconds: int = 300) -> list[dict]:
        cutoff = time.time() - seconds
        with self._lock:
            return [f for f in self._frames if f["timestamp"] > cutoff]

    def get_summary(self) -> dict:
        with self._lock:
            if not self._frames:
                return {"frame_count": 0, "duration_s": 0}
            oldest = self._frames[0]["timestamp"]
            newest = self._frames[-1]["timestamp"]
            return {
                "frame_count": len(self._frames),
                "duration_s": round(newest - oldest, 1),
                "oldest": oldest,
                "newest": newest,
            }


class AnomalyEngine:
    """Detects anomalies in streaming spectrum data by comparing against
    a running short-term average and the spectral baseline.

    Baseline lookup uses numpy arrays with np.searchsorted for O(log n)
    per-frame comparison instead of dict lookups per bin.
    """

    SPIKE_THRESHOLD = 10.0      # dB above short-term avg
    NEW_SIGNAL_THRESHOLD = 8.0  # dB above noise floor for previously empty bin
    BURST_MIN_DURATION = 0.5    # seconds
    BURST_MAX_DURATION = 30.0   # seconds

    def __init__(self):
        self._short_term: dict[str, deque] = {}  # freq_key -> deque of (timestamp, power)
        self._active_bursts: dict[str, dict] = {}  # freq_key -> {start, peak_power, band}
        self._baseline_freqs: Any = None   # sorted numpy array of baseline frequencies
        self._baseline_means: Any = None   # numpy array of baseline power means
        self._lock = threading.Lock()
        self._anomaly_callbacks: list[Callable] = []

    def set_baseline(self, bins: dict[str, dict]) -> None:
        """Load baseline from dict (backward compatible)."""
        with self._lock:
            if not _HAS_DEPS or not bins:
                self._baseline_freqs = None
                self._baseline_means = None
                return
            sorted_keys = sorted(bins.keys(), key=float)
            self._baseline_freqs = np.array([float(k) for k in sorted_keys], dtype=np.float64)
            self._baseline_means = np.array([bins[k]["power_mean"] for k in sorted_keys], dtype=np.float64)

    def set_baseline_arrays(self, freqs: Any, means: Any) -> None:
        """Load baseline directly from numpy arrays (fast path)."""
        with self._lock:
            self._baseline_freqs = freqs
            self._baseline_means = means

    def on_anomaly(self, callback: Callable[[WatchAnomaly], None]) -> None:
        self._anomaly_callbacks.append(callback)

    def _lookup_baseline(self, freq_mhz: float) -> float | None:
        """O(log n) baseline lookup using searchsorted."""
        if self._baseline_freqs is None:
            return None
        idx = np.searchsorted(self._baseline_freqs, freq_mhz)
        if idx < len(self._baseline_freqs) and abs(self._baseline_freqs[idx] - freq_mhz) < 1e-6:
            return float(self._baseline_means[idx])
        if idx > 0 and abs(self._baseline_freqs[idx - 1] - freq_mhz) < 1e-6:
            return float(self._baseline_means[idx - 1])
        return None

    def process_frame(self, frame: SpectrumFrame) -> list[WatchAnomaly]:
        if not _HAS_DEPS:
            return []

        anomalies: list[WatchAnomaly] = []
        now = frame.timestamp

        with self._lock:
            has_baseline = self._baseline_freqs is not None

            for i in range(len(frame.freqs)):
                freq_mhz = float(frame.freqs[i])
                power = float(frame.power_db[i])
                freq_key = f"{freq_mhz:.4f}"

                if freq_key not in self._short_term:
                    self._short_term[freq_key] = deque(maxlen=60)
                self._short_term[freq_key].append((now, power))

                samples = self._short_term[freq_key]
                if len(samples) >= 3:
                    avg = sum(p for _, p in samples) / len(samples)
                else:
                    avg = power

                bl_power = self._lookup_baseline(freq_mhz) if has_baseline else None

                # Spike detection: sudden jump above short-term average
                if power - avg > self.SPIKE_THRESHOLD:
                    delta = power - avg
                    a = WatchAnomaly(
                        timestamp=now,
                        frequency_mhz=freq_mhz,
                        anomaly_type="spike",
                        power_db=power,
                        baseline_power=bl_power,
                        delta_db=delta,
                        duration_s=0,
                        band=frame.band,
                        severity=_watch_severity(delta),
                    )
                    anomalies.append(a)

                # New signal detection (not in baseline, above noise)
                above_noise = power - frame.noise_floor
                if has_baseline and bl_power is None and above_noise > self.NEW_SIGNAL_THRESHOLD:
                    a = WatchAnomaly(
                        timestamp=now,
                        frequency_mhz=freq_mhz,
                        anomaly_type="new_signal",
                        power_db=power,
                        baseline_power=None,
                        delta_db=above_noise,
                        duration_s=0,
                        band=frame.band,
                        severity=_watch_severity(above_noise),
                    )
                    anomalies.append(a)

                # Burst tracking (signal that appears and disappears)
                is_active = above_noise > 6.0
                if is_active:
                    if freq_key not in self._active_bursts:
                        self._active_bursts[freq_key] = {
                            "start": now, "peak_power": power, "band": frame.band,
                        }
                    else:
                        if power > self._active_bursts[freq_key]["peak_power"]:
                            self._active_bursts[freq_key]["peak_power"] = power
                elif freq_key in self._active_bursts:
                    burst = self._active_bursts.pop(freq_key)
                    duration = now - burst["start"]
                    if self.BURST_MIN_DURATION <= duration <= self.BURST_MAX_DURATION:
                        a = WatchAnomaly(
                            timestamp=now,
                            frequency_mhz=freq_mhz,
                            anomaly_type="burst",
                            power_db=burst["peak_power"],
                            baseline_power=bl_power,
                            delta_db=burst["peak_power"] - frame.noise_floor,
                            duration_s=duration,
                            band=frame.band,
                            severity="medium" if duration < 5 else "high",
                        )
                        anomalies.append(a)

        for a in anomalies:
            for cb in self._anomaly_callbacks:
                try:
                    cb(a)
                except Exception as e:
                    logger.debug(f"Anomaly callback error: {e}")

        return anomalies


class RFWatchDaemon:
    """Continuous RF streaming daemon using SoapySDR."""

    def __init__(
        self,
        device_args: str,
        bands: list[tuple[int, int, str]],
        gain: float = 40.0,
        fft_size: int = 4096,
        dwell_time: float = 1.0,
    ):
        self.device_args = device_args
        self.bands = bands  # [(start_hz, end_hz, band_name), ...]
        self.gain = gain
        self.fft_size = fft_size
        self.dwell_time = dwell_time

        self.waterfall = WaterfallBuffer()
        self.anomaly_engine = AnomalyEngine()

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
        self._status: dict[str, Any] = {"state": "stopped"}
        self._stats = {
            "frames_processed": 0,
            "anomalies_detected": 0,
            "started_at": None,
            "current_band": None,
        }

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._running:
            return False
        if not _HAS_DEPS or _SoapySDR is None:
            logger.error("SoapySDR or numpy not available for RF watch")
            return False

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="rf-watch")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        self._running = False
        self._status = {"state": "stopped"}

    def get_status(self) -> dict:
        return {
            **self._status,
            "stats": self._stats.copy(),
            "waterfall": self.waterfall.get_summary(),
        }

    def _run(self) -> None:
        self._running = True
        self._stats["started_at"] = time.time()
        self._status = {"state": "starting"}

        device = None
        stream = None
        try:
            device = _SoapySDR.Device(self.device_args)
            sample_rate = 2_000_000.0
            try:
                rates = device.listSampleRates(_SoapySDR.SOAPY_SDR_RX, 0)
                if rates:
                    sample_rate = min(max(rates), 56_000_000)
            except Exception:
                pass

            device.setSampleRate(_SoapySDR.SOAPY_SDR_RX, 0, sample_rate)
            device.setGain(_SoapySDR.SOAPY_SDR_RX, 0, self.gain)

            stream = device.setupStream(_SoapySDR.SOAPY_SDR_RX, _SoapySDR.SOAPY_SDR_CF32)
            device.activateStream(stream)

            window = np.hanning(self.fft_size).astype(np.float32)
            buff = np.zeros(self.fft_size, dtype=np.complex64)

            self._status = {"state": "running"}
            logger.info(f"RF watch started: {len(self.bands)} bands, rate={sample_rate/1e6:.1f}M")

            while not self._stop_event.is_set():
                for start_hz, end_hz, band_name in self.bands:
                    if self._stop_event.is_set():
                        break

                    self._stats["current_band"] = band_name
                    bandwidth = end_hz - start_hz
                    usable_bw = sample_rate * 0.8
                    steps = max(1, int(np.ceil(bandwidth / usable_bw)))
                    step_size = bandwidth / steps

                    for step in range(steps):
                        if self._stop_event.is_set():
                            break

                        center_freq = start_hz + step_size * (step + 0.5)
                        device.setFrequency(_SoapySDR.SOAPY_SDR_RX, 0, center_freq)

                        # Flush + capture
                        device.readStream(stream, [buff], self.fft_size, timeoutUs=500_000)
                        sr = device.readStream(stream, [buff], self.fft_size, timeoutUs=1_000_000)
                        if sr.ret <= 0:
                            continue

                        samples = buff[:sr.ret]
                        if len(samples) < 64:
                            continue

                        w = window[:len(samples)] if len(samples) < self.fft_size else window
                        spectrum = np.fft.fftshift(np.fft.fft(samples * w))
                        power_db = 20.0 * np.log10(np.abs(spectrum) + 1e-12)
                        freqs = np.fft.fftshift(np.fft.fftfreq(len(samples), 1.0 / sample_rate))
                        freqs = (freqs + center_freq) / 1e6  # to MHz

                        mask = (freqs >= start_hz / 1e6) & (freqs <= end_hz / 1e6)
                        freqs = freqs[mask]
                        power_db = power_db[mask]

                        if len(power_db) == 0:
                            continue

                        noise_floor = float(np.median(power_db))

                        frame = SpectrumFrame(
                            timestamp=time.time(),
                            center_freq=center_freq / 1e6,
                            freqs=freqs,
                            power_db=power_db,
                            noise_floor=noise_floor,
                            band=band_name,
                        )

                        self.waterfall.add_frame(frame)
                        anomalies = self.anomaly_engine.process_frame(frame)
                        self._stats["frames_processed"] += 1
                        self._stats["anomalies_detected"] += len(anomalies)

                    # Dwell between bands
                    if not self._stop_event.is_set():
                        self._stop_event.wait(self.dwell_time)

        except Exception as e:
            logger.error(f"RF watch error: {e}")
            self._status = {"state": "error", "error": str(e)}
        finally:
            if stream is not None:
                try:
                    device.deactivateStream(stream)
                    device.closeStream(stream)
                except Exception:
                    pass
            if device is not None:
                try:
                    del device
                except Exception:
                    pass
            self._running = False
            if self._status.get("state") != "error":
                self._status = {"state": "stopped"}
            logger.info("RF watch stopped")


# Singleton daemon instance
_daemon: RFWatchDaemon | None = None
_daemon_lock = threading.Lock()


def get_watch_daemon() -> RFWatchDaemon | None:
    return _daemon


def start_watch(
    device_args: str,
    bands: list[tuple[int, int, str]] | None = None,
    gain: float = 40.0,
) -> dict:
    """Start the RF watch daemon. Returns status dict."""
    global _daemon

    if bands is None:
        bands = [
            (88_000_000, 108_000_000, "FM"),
            (400_000_000, 470_000_000, "UHF"),
            (2_400_000_000, 2_500_000_000, "WiFi-2.4G"),
        ]

    with _daemon_lock:
        if _daemon and _daemon.running:
            return {"status": "already_running", **_daemon.get_status()}

        _daemon = RFWatchDaemon(device_args, bands, gain)

        # Load spectral baseline if available (prefer numpy arrays for speed)
        try:
            from utils.tscm.spectral_baseline import SpectralStore
            store = SpectralStore()
            bl_id = store.get_active_baseline_id()
            if bl_id:
                arrays = store.get_arrays(bl_id)
                if arrays is not None:
                    _daemon.anomaly_engine.set_baseline_arrays(arrays.freqs, arrays.mean)
                    logger.info(f"Watch daemon loaded spectral baseline {bl_id} ({arrays.size} bins, numpy arrays)")
        except Exception as e:
            logger.debug(f"Could not load spectral baseline: {e}")

        if _daemon.start():
            return {"status": "started", **_daemon.get_status()}
        return {"status": "failed", "error": "Could not start watch daemon"}


def stop_watch() -> dict:
    """Stop the RF watch daemon."""
    global _daemon
    with _daemon_lock:
        if not _daemon or not _daemon.running:
            return {"status": "not_running"}
        _daemon.stop()
        status = _daemon.get_status()
        _daemon = None
        return {"status": "stopped", **status}


def watch_status() -> dict:
    """Get the current watch daemon status."""
    with _daemon_lock:
        if not _daemon:
            return {"state": "stopped", "stats": {}, "waterfall": {"frame_count": 0}}
        return _daemon.get_status()


def _watch_severity(delta_db: float) -> str:
    if delta_db >= 20:
        return "critical"
    elif delta_db >= 15:
        return "high"
    elif delta_db >= 10:
        return "medium"
    return "low"
