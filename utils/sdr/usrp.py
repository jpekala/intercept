"""
USRP command builder implementation.

Uses SoapySDR (via the UHD driver / SoapyUHD bridge) for all signal
processing tasks.  The same rx_fm / rx_sdr / rtl_433 / AIS-catcher
toolchain used by HackRF applies here because they all speak SoapySDR.

Tested targets:
  - Ettus USRP B200mini  (AD9364, 1 TX/RX, 70 MHz-6 GHz, 56 MHz BW, USB 3.0)
  - Ettus USRP B200       (AD9364, 1 TX/RX, 70 MHz-6 GHz, 56 MHz BW, USB 3.0)
  - Ettus USRP B210       (AD9361, 2 TX/RX, 70 MHz-6 GHz, 56 MHz BW, USB 3.0)
  - Ettus USRP N200/N210  (external daughterboard, GigE)

Requires:
  - uhd (libuhd-dev, uhd-host) — UHD driver and uhd_find_devices
  - UHD firmware images    — run 'uhd_images_downloader --types b2xx'
  - SoapySDR + SoapyUHD    — bridge for rx_fm / rx_sdr / readsb / rtl_433

If uhd_find_devices fails with 'Could not find path for image', set
UHD_IMAGES_DIR to the directory containing usrp_b200_fw.hex (typically
/usr/share/uhd/<version>/images on Debian/Ubuntu).
"""

from __future__ import annotations

from utils.dependencies import get_tool_path

from .base import CommandBuilder, SDRCapabilities, SDRDevice, SDRType


class USRPCommandBuilder(CommandBuilder):
    """USRP command builder using SoapySDR / UHD."""

    # B200-family specs (AD9364): 70 MHz-6 GHz, 76 dB gain, 61.44 MS/s max.
    # The freq_min is set to 1 MHz to accommodate N200/N210 with wideband
    # daughterboards; B200/B200mini actual minimum is 70 MHz.
    CAPABILITIES = SDRCapabilities(
        sdr_type=SDRType.USRP,
        freq_min_mhz=1.0,
        freq_max_mhz=6000.0,
        gain_min=0.0,
        gain_max=76.0,
        sample_rates=[1000000, 2000000, 4000000, 8000000, 16000000, 25000000, 40000000, 61440000],
        supports_bias_t=False,
        supports_ppm=False,
        tx_capable=True,
        supports_iq_capture=True,
    )

    def _build_device_string(self, device: SDRDevice) -> str:
        """Build SoapySDR device string for USRP."""
        if device.serial and device.serial not in ("N/A", "Unknown"):
            return f"driver=uhd,serial={device.serial}"
        return "driver=uhd"

    def build_fm_demod_command(
        self,
        device: SDRDevice,
        frequency_mhz: float,
        sample_rate: int = 22050,
        gain: float | None = None,
        ppm: int | None = None,
        modulation: str = "fm",
        squelch: int | None = None,
        bias_t: bool = False,
    ) -> list[str]:
        device_str = self._build_device_string(device)
        rx_fm_path = get_tool_path("rx_fm") or "rx_fm"
        cmd = [
            rx_fm_path,
            "-d",
            device_str,
            "-f",
            f"{frequency_mhz}M",
            "-M",
            modulation,
            "-s",
            str(sample_rate),
        ]
        if gain is not None and gain > 0:
            cmd.extend(["-g", str(int(gain))])
        if squelch is not None and squelch > 0:
            cmd.extend(["-l", str(squelch)])
        cmd.append("-")
        return cmd

    def build_adsb_command(self, device: SDRDevice, gain: float | None = None, bias_t: bool = False) -> list[str]:
        device_str = self._build_device_string(device)
        cmd = ["readsb", "--net", "--device-type", "soapysdr", "--device", device_str, "--quiet"]
        if gain is not None:
            cmd.extend(["--gain", str(int(gain))])
        return cmd

    def build_ism_command(
        self,
        device: SDRDevice,
        frequency_mhz: float = 433.92,
        gain: float | None = None,
        ppm: int | None = None,
        bias_t: bool = False,
    ) -> list[str]:
        device_str = self._build_device_string(device)
        cmd = ["rtl_433", "-d", device_str, "-f", f"{frequency_mhz}M", "-F", "json"]
        if gain is not None and gain > 0:
            cmd.extend(["-g", str(int(gain))])
        return cmd

    def build_ais_command(
        self,
        device: SDRDevice,
        gain: float | None = None,
        bias_t: bool = False,
        tcp_port: int = 10110,
        udp_host: str | None = None,
        udp_port: int | None = None,
    ) -> list[str]:
        device_str = self._build_device_string(device)
        cmd = [
            "AIS-catcher",
            "-d",
            f"soapysdr -d {device_str}",
            "-S",
            str(tcp_port),
            "-o",
            "5",
            "-q",
        ]
        if gain is not None and gain > 0:
            cmd.extend(["-gr", "tuner", str(int(gain))])
        if udp_host and udp_port:
            cmd.extend(["-u", udp_host, str(udp_port)])
        return cmd

    def build_iq_capture_command(
        self,
        device: SDRDevice,
        frequency_mhz: float,
        sample_rate: int = 2048000,
        gain: float | None = None,
        ppm: int | None = None,
        bias_t: bool = False,
        output_format: str = "cu8",
    ) -> list[str]:
        device_str = self._build_device_string(device)
        freq_hz = int(frequency_mhz * 1e6)
        rx_sdr_path = get_tool_path("rx_sdr") or "rx_sdr"
        cmd = [
            rx_sdr_path,
            "-d",
            device_str,
            "-f",
            str(freq_hz),
            "-s",
            str(sample_rate),
            "-F",
            "CU8",
        ]
        if gain is not None and gain > 0:
            cmd.extend(["-g", str(int(gain))])
        cmd.append("-")
        return cmd

    def get_capabilities(self) -> SDRCapabilities:
        return self.CAPABILITIES

    @classmethod
    def get_sdr_type(cls) -> SDRType:
        return SDRType.USRP
