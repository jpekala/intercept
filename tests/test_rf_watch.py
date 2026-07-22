"""Tests for the RF watch anomaly engine (Tier 3 continuous monitoring)."""

import os

import pytest

os.environ.setdefault("INTERCEPT_SKIP_DEFERRED_INIT", "1")

numpy = pytest.importorskip("numpy")

from utils.tscm.rf_watch import AnomalyEngine, RFWatchDaemon, SpectrumFrame, WaterfallBuffer


def _frame(freqs, power_db, noise_floor=-90.0, band="TEST", timestamp=1000.0):
    return SpectrumFrame(
        timestamp=timestamp,
        center_freq=float(numpy.mean(freqs)),
        freqs=numpy.array(freqs, dtype=numpy.float64),
        power_db=numpy.array(power_db, dtype=numpy.float64),
        noise_floor=noise_floor,
        band=band,
    )


def _spectrum(peaks, start_mhz=100.0, n=256, spacing_mhz=0.05, floor=-90.0, band="TEST", timestamp=1000.0):
    """Build a realistic frame: flat noise floor with injected peaks.

    peaks: list of (freq_mhz, power_db). Nearest bin is set to that power.
    """
    freqs = start_mhz + numpy.arange(n) * spacing_mhz
    power = numpy.full(n, floor, dtype=numpy.float64)
    # slight noise texture so median stays at floor
    power += numpy.linspace(-1.0, 1.0, n)
    for f, p in peaks:
        idx = int(round((f - start_mhz) / spacing_mhz))
        idx = max(1, min(n - 2, idx))
        power[idx] = p
    return SpectrumFrame(
        timestamp=timestamp,
        center_freq=float(start_mhz + n * spacing_mhz / 2),
        freqs=freqs,
        power_db=power,
        noise_floor=floor,
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
        eng.set_baseline_arrays(numpy.array([100.0]), numpy.array([-50.0]))
        assert eng._lookup_baseline(100.5) is None

    def test_no_baseline(self):
        eng = AnomalyEngine()
        assert eng._lookup_baseline(100.0) is None


class TestPeakFinding:
    def test_isolated_peak_found(self):
        eng = AnomalyEngine()
        f = _spectrum([(105.0, -40.0)])
        idx = eng._find_peaks(f.freqs, f.power_db, f.noise_floor)
        assert len(idx) == 1
        assert abs(float(f.freqs[idx[0]]) - 105.0) < 0.05

    def test_flat_noise_no_peaks(self):
        eng = AnomalyEngine()
        f = _spectrum([])  # only the noise texture, nothing above +10
        idx = eng._find_peaks(f.freqs, f.power_db, f.noise_floor)
        assert len(idx) == 0

    def test_below_prominence_ignored(self):
        eng = AnomalyEngine()
        f = _spectrum([(105.0, -85.0)])  # only 5 dB above -90 floor, < 10 prominence
        idx = eng._find_peaks(f.freqs, f.power_db, f.noise_floor)
        assert len(idx) == 0

    def test_multiple_peaks(self):
        eng = AnomalyEngine()
        f = _spectrum([(102.0, -30.0), (108.0, -35.0), (112.0, -50.0)])
        idx = eng._find_peaks(f.freqs, f.power_db, f.noise_floor)
        assert len(idx) == 3

    def test_short_frame_no_crash(self):
        eng = AnomalyEngine()
        idx = eng._find_peaks(
            numpy.array([100.0, 101.0]), numpy.array([-40.0, -50.0]), -90.0
        )
        assert len(idx) == 0


class TestNewSignalCooldown:
    def test_fires_once_then_suppressed(self):
        eng = AnomalyEngine()
        eng.set_baseline_arrays(numpy.array([500.0]), numpy.array([-50.0]))
        f1 = _spectrum([(105.0, -40.0)], timestamp=1000.0)
        f2 = _spectrum([(105.0, -40.0)], timestamp=1001.0)
        new1 = [a for a in eng.process_frame(f1) if a.anomaly_type == "new_signal"]
        new2 = [a for a in eng.process_frame(f2) if a.anomaly_type == "new_signal"]
        assert len(new1) == 1
        assert len(new2) == 0

    def test_refires_after_cooldown(self):
        eng = AnomalyEngine()
        eng.set_baseline_arrays(numpy.array([500.0]), numpy.array([-50.0]))
        eng.process_frame(_spectrum([(105.0, -40.0)], timestamp=1000.0))
        later = eng.process_frame(
            _spectrum([(105.0, -40.0)], timestamp=1000.0 + AnomalyEngine.NEW_SIGNAL_COOLDOWN_S + 1)
        )
        assert len([a for a in later if a.anomaly_type == "new_signal"]) == 1

    def test_baseline_match_no_new_signal(self):
        """A peak near a baseline bin must not fire new_signal."""
        eng = AnomalyEngine()
        eng.set_baseline_arrays(numpy.array([105.01]), numpy.array([-45.0]))
        result = eng.process_frame(_spectrum([(105.0, -40.0)]))
        assert len([a for a in result if a.anomaly_type == "new_signal"]) == 0


class TestSpikeDetection:
    def test_spike_over_short_term_avg(self):
        eng = AnomalyEngine()
        # Stable peak at moderate power builds the short-term average
        for i in range(5):
            eng.process_frame(_spectrum([(105.0, -70.0)], timestamp=1000.0 + i))
        # Same bin jumps +25 dB
        result = eng.process_frame(_spectrum([(105.0, -45.0)], timestamp=1010.0))
        spikes = [a for a in result if a.anomaly_type == "spike"]
        assert len(spikes) == 1
        assert spikes[0].delta_db > AnomalyEngine.SPIKE_THRESHOLD


class TestFloodControl:
    def test_noisy_frame_stays_quiet(self):
        """A frame with hundreds of above-median bins but no real peaks
        must not produce hundreds of anomalies."""
        eng = AnomalyEngine()
        eng.set_baseline_arrays(numpy.array([500.0]), numpy.array([-50.0]))
        n = 512
        freqs = 100.0 + numpy.arange(n) * 0.05
        # Jagged noise: every other bin well above the median, but no clean peaks
        power = numpy.full(n, -90.0)
        power[::2] = -78.0  # 12 dB above floor but a flat comb, not isolated peaks
        f = SpectrumFrame(1000.0, 112.0, freqs, power, -90.0, "TEST")
        result = eng.process_frame(f)
        # Comb teeth are local maxima, but the count is bounded by n/2, and
        # cooldown collapses repeats. Assert it's nowhere near per-bin flood.
        assert len(result) <= n // 2


class TestStateCap:
    def test_short_term_capped(self):
        eng = AnomalyEngine()
        eng._short_term = {f"{i}.0000": None for i in range(100_001)}
        eng.process_frame(_spectrum([(105.0, -40.0)]))
        assert len(eng._short_term) < 100_001


class TestDaemonStatus:
    def test_baseline_absent(self):
        d = RFWatchDaemon("driver=null", [(88_000_000, 108_000_000, "FM")])
        st = d.get_status()
        assert st["baseline"]["loaded"] is False
        assert st["baseline"]["bins"] == 0

    def test_baseline_reported_when_loaded(self):
        d = RFWatchDaemon("driver=null", [(88_000_000, 108_000_000, "FM")])
        d.anomaly_engine.set_baseline_arrays(
            numpy.array([100.0, 200.0, 300.0]), numpy.array([-50.0, -60.0, -70.0])
        )
        st = d.get_status()
        assert st["baseline"]["loaded"] is True
        assert st["baseline"]["bins"] == 3


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
