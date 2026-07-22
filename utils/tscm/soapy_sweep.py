"""
SoapySDR-based RF power scanner for TSCM sweeps.

Provides power spectrum scanning for SDR devices that use SoapySDR
(USRP, LimeSDR, Airspy, BladeRF, etc.) when rtl_power or hackrf_sweep
are not available.

Uses numpy FFT for power spectrum computation with SoapySDR streaming.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("intercept.tscm.soapy_sweep")

_HAS_DEPS = True
try:
    import numpy as np
    import SoapySDR
except ImportError:
    _HAS_DEPS = False


def is_available() -> bool:
    """Check if SoapySDR and numpy are importable."""
    return _HAS_DEPS


def scan_bands(
    device_args: str,
    bands: list[tuple[int, int, int, str]],
    gain: float = 40.0,
    stop_check: Any = None,
) -> list[dict]:
    """
    Scan multiple frequency bands and return detected signals.

    Opens the SoapySDR device once, sweeps all bands, then closes.

    Args:
        device_args: SoapySDR device string (e.g. "driver=uhd,serial=01ABCDE")
        bands: List of (start_hz, end_hz, bin_size_hz, band_name) tuples
        gain: Receiver gain in dB
        stop_check: Optional callable returning True to abort early

    Returns:
        List of signal dicts with frequency, power, noise_floor, etc.
    """
    if not _HAS_DEPS:
        logger.error("SoapySDR or numpy not available")
        return []

    if stop_check is None:
        stop_check = lambda: False

    all_signals: list[dict] = []

    device = None
    stream = None
    try:
        device = SoapySDR.Device(device_args)

        max_rate = 2_000_000.0
        try:
            rates = device.listSampleRates(SoapySDR.SOAPY_SDR_RX, 0)
            if rates:
                max_rate = max(rates)
        except Exception:
            pass

        for start_hz, end_hz, bin_size_hz, band_name in bands:
            if stop_check():
                break

            band_signals = _scan_single_band(
                device, start_hz, end_hz, bin_size_hz, band_name, gain, max_rate, stop_check,
            )
            all_signals.extend(band_signals)

    except Exception as e:
        logger.error(f"SoapySDR sweep error: {e}")
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

    return all_signals


def _scan_single_band(
    device: Any,
    start_hz: int,
    end_hz: int,
    bin_size_hz: int,
    band_name: str,
    gain: float,
    max_sample_rate: float,
    stop_check: Any,
) -> list[dict]:
    """Scan a single frequency band using stepped FFT captures."""
    bandwidth = end_hz - start_hz
    if bandwidth <= 0:
        return []

    sample_rate = min(max_sample_rate, max(bandwidth * 1.25, 1_000_000))
    sample_rate = min(sample_rate, 56_000_000)

    usable_bw = sample_rate * 0.8
    steps = max(1, int(np.ceil(bandwidth / usable_bw)))
    step_size = bandwidth / steps

    fft_size = max(256, int(sample_rate / max(bin_size_hz, 1000)))
    fft_size = int(2 ** np.ceil(np.log2(fft_size)))
    fft_size = min(fft_size, 65536)

    logger.info(
        f"Scanning {band_name} ({start_hz / 1e6:.1f}-{end_hz / 1e6:.1f} MHz) "
        f"rate={sample_rate / 1e6:.1f}M, {steps} step(s), fft={fft_size}"
    )

    try:
        device.setSampleRate(SoapySDR.SOAPY_SDR_RX, 0, sample_rate)
        device.setGain(SoapySDR.SOAPY_SDR_RX, 0, gain)
    except Exception as e:
        logger.warning(f"Failed to configure device for {band_name}: {e}")
        return []

    stream = None
    signals: list[dict] = []
    try:
        stream = device.setupStream(SoapySDR.SOAPY_SDR_RX, SoapySDR.SOAPY_SDR_CF32)
        device.activateStream(stream)

        window = np.hanning(fft_size).astype(np.float32)
        buff = np.zeros(fft_size, dtype=np.complex64)

        for step in range(steps):
            if stop_check():
                break

            center_freq = start_hz + step_size * (step + 0.5)
            device.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, center_freq)

            # Flush one buffer to let PLL settle
            device.readStream(stream, [buff], fft_size, timeoutUs=500_000)

            # Capture
            sr = device.readStream(stream, [buff], fft_size, timeoutUs=1_000_000)
            if sr.ret <= 0:
                logger.warning(f"SoapySDR read returned {sr.ret} for {band_name} step {step}")
                continue

            samples = buff[: sr.ret]
            if len(samples) < 64:
                continue

            actual_window = window[: len(samples)] if len(samples) < fft_size else window
            spectrum = np.fft.fftshift(np.fft.fft(samples * actual_window))
            power_db = 20.0 * np.log10(np.abs(spectrum) + 1e-12)

            freqs = np.fft.fftshift(
                np.fft.fftfreq(len(samples), 1.0 / sample_rate)
            )
            freqs += center_freq

            mask = (freqs >= start_hz) & (freqs <= end_hz)
            freqs = freqs[mask]
            power_db = power_db[mask]

            if len(power_db) == 0:
                continue

            noise_floor = float(np.median(power_db))
            threshold = noise_floor + 6.0

            for freq_hz, pwr in zip(freqs, power_db):
                if pwr > threshold and pwr > -90:
                    signals.append(
                        {
                            "frequency": float(freq_hz) / 1e6,
                            "frequency_hz": float(freq_hz),
                            "power": float(pwr),
                            "band": band_name,
                            "noise_floor": noise_floor,
                            "signal_strength": float(pwr - noise_floor),
                        }
                    )

    except Exception as e:
        logger.error(f"SoapySDR band scan error ({band_name}): {e}")
    finally:
        if stream is not None:
            try:
                device.deactivateStream(stream)
                device.closeStream(stream)
            except Exception:
                pass

    return signals
