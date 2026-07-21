"""
INTERCEPT MCP Server

Full-access MCP server exposing the INTERCEPT Signal Intelligence platform
to Claude Code and Claude Desktop. Provides 106 tools across 19 domains:
system management, signal decoding, tracking, scanning, counter-surveillance,
satellite, mesh networking, and multi-agent orchestration.

Configure via environment variable:
    INTERCEPT_URL  (default: http://localhost:5000)

Install in Claude Desktop (claude_desktop_config.json):
    {
        "mcpServers": {
            "intercept": {
                "command": "python",
                "args": ["path/to/intercept/mcp_server/intercept_mcp.py"],
                "env": {"INTERCEPT_URL": "http://localhost:5000"}
            }
        }
    }

Install in Claude Code:
    claude mcp add intercept -- python mcp_server/intercept_mcp.py
"""

import json
import os
import logging
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("intercept_mcp")

BASE_URL = os.environ.get("INTERCEPT_URL", "http://localhost:5000")

mcp = FastMCP(
    "INTERCEPT",
    instructions=(
        "Control an INTERCEPT Signal Intelligence platform — manage SDR hardware, "
        "run signal decoders (pager, ADS-B, AIS, ACARS, APRS, 433MHz, SubGHz), "
        "scan WiFi/Bluetooth, track aircraft/vessels/satellites, perform TSCM "
        "counter-surveillance sweeps, operate Meshtastic mesh networks, and "
        "orchestrate remote collection agents."
    ),
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _get(path: str, params: dict | None = None, timeout: float = 15.0) -> Any:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout) as client:
        resp = await client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, payload: dict | None = None, timeout: float = 15.0) -> Any:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout) as client:
        resp = await client.post(path, json=payload or {})
        resp.raise_for_status()
        return resp.json()


async def _put(path: str, payload: dict | None = None, timeout: float = 15.0) -> Any:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout) as client:
        resp = await client.put(path, json=payload or {})
        resp.raise_for_status()
        return resp.json()


async def _delete(path: str, timeout: float = 15.0) -> Any:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=timeout) as client:
        resp = await client.delete(path)
        resp.raise_for_status()
        return resp.json()


async def _read_sse(path: str, duration_seconds: float = 5.0, max_events: int = 100) -> list[dict]:
    """Connect to an SSE endpoint and collect events for a duration."""
    events = []
    deadline = time.time() + duration_seconds
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=None) as client:
            async with client.stream("GET", path, headers={"Accept": "text/event-stream"}) as resp:
                async for line in resp.aiter_lines():
                    if time.time() > deadline or len(events) >= max_events:
                        break
                    line = line.strip()
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str and data_str != ":keepalive":
                            try:
                                events.append(json.loads(data_str))
                            except json.JSONDecodeError:
                                events.append({"raw": data_str})
    except httpx.ReadTimeout:
        pass
    except Exception as e:
        logger.debug(f"SSE read error on {path}: {e}")
    return events


def _fmt(data: Any) -> str:
    """Format response data as JSON string."""
    if isinstance(data, str):
        return data
    return json.dumps(data, indent=2, default=str)


# ===================================================================
# 1. CORE — SYSTEM & DEVICE MANAGEMENT (8 tools)
# ===================================================================

@mcp.tool()
async def get_devices() -> str:
    """List all connected SDR hardware (RTL-SDR, HackRF, LimeSDR, USRP, Airspy, SDRPlay, BladeRF, HydraSDR).
    Returns device index, name, serial, type, and capabilities (frequency range, gain range, sample rates)."""
    return _fmt(await _get("/devices"))


@mcp.tool()
async def get_device_status() -> str:
    """List SDR devices with their current usage status — shows which device is claimed by which decoder mode.
    Use this before starting a decoder to find an available device."""
    return _fmt(await _get("/devices/status"))


@mcp.tool()
async def get_dependencies() -> str:
    """Check which external tools are installed (rtl_fm, dump1090, rtl_433, acarsdec, AIS-catcher, SatDump, etc.).
    Returns availability status for each tool. Use to verify prerequisites before starting a decoder."""
    return _fmt(await _get("/dependencies"))


@mcp.tool()
async def health_check() -> str:
    """Comprehensive system health check — SDR devices, tool availability, disk space, running modes, database status.
    Call this first to understand the full system state."""
    return _fmt(await _get("/health"))


@mcp.tool()
async def killall() -> str:
    """Emergency stop — kill all running decoder processes. Use when the system is in a bad state or
    you need to free all SDR devices."""
    return _fmt(await _post("/killall"))


@mcp.tool()
async def get_settings() -> str:
    """Read all application settings (observer location, default gains, frequencies, API keys, etc.)."""
    return _fmt(await _get("/settings/"))


@mcp.tool()
async def set_setting(key: str, value: str) -> str:
    """Update a single application setting.
    Common keys: observer_latitude, observer_longitude, observer_elevation, default_gain, default_device."""
    return _fmt(await _put(f"/settings/{key}", {"value": value}))


@mcp.tool()
async def get_system_metrics() -> str:
    """Get system resource metrics — CPU usage, memory, disk space, SDR device utilization, active mode count."""
    return _fmt(await _get("/system/metrics"))


# ===================================================================
# 2. PAGER DECODING (4 tools)
# ===================================================================

@mcp.tool()
async def start_pager(
    frequency: str = "929.6125",
    gain: str = "40",
    device: str = "0",
    sdr_type: str = "rtlsdr",
    squelch: int = 0,
    ppm: str = "0",
    protocols: list[str] | None = None,
    bias_t: bool = False,
) -> str:
    """Start POCSAG/FLEX pager decoder. Decodes pager messages on the specified frequency.
    Common US frequencies: 929.6125, 931.9375. Protocols: POCSAG512, POCSAG1200, POCSAG2400, FLEX."""
    payload: dict[str, Any] = {
        "frequency": frequency,
        "gain": gain,
        "device": device,
        "sdr_type": sdr_type,
        "squelch": squelch,
        "ppm": ppm,
        "bias_t": bias_t,
    }
    if protocols:
        payload["protocols"] = protocols
    return _fmt(await _post("/start", payload))


@mcp.tool()
async def stop_pager() -> str:
    """Stop the running pager decoder."""
    return _fmt(await _post("/stop"))


@mcp.tool()
async def get_pager_status() -> str:
    """Check if the pager decoder is running, and whether logging is enabled."""
    return _fmt(await _get("/status"))


@mcp.tool()
async def read_pager_messages(duration_seconds: float = 5.0, max_events: int = 50) -> str:
    """Read decoded pager messages from the live stream for the specified duration (max 30s).
    Returns POCSAG/FLEX messages with timestamp, address, function code, and message text."""
    duration_seconds = min(duration_seconds, 30.0)
    events = await _read_sse("/stream", duration_seconds, max_events)
    return _fmt({"message_count": len(events), "messages": events})


# ===================================================================
# 3. 433 MHz SENSORS (4 tools)
# ===================================================================

@mcp.tool()
async def start_sensor(
    frequency: str = "433.92",
    gain: str = "40",
    device: str = "0",
    sdr_type: str = "rtlsdr",
    ppm: str = "0",
    bias_t: bool = False,
) -> str:
    """Start 433 MHz sensor decoder (rtl_433). Decodes temperature sensors, weather stations,
    tire pressure monitors, doorbell buttons, motion detectors, and other ISM-band IoT devices."""
    return _fmt(await _post("/start_sensor", {
        "frequency": frequency, "gain": gain, "device": device,
        "sdr_type": sdr_type, "ppm": ppm, "bias_t": bias_t,
    }))


@mcp.tool()
async def stop_sensor() -> str:
    """Stop the 433 MHz sensor decoder."""
    return _fmt(await _post("/stop_sensor"))


@mcp.tool()
async def get_sensor_status() -> str:
    """Check if the sensor decoder is running."""
    return _fmt(await _get("/sensor/status"))


@mcp.tool()
async def read_sensor_data(duration_seconds: float = 5.0, max_events: int = 50) -> str:
    """Read decoded sensor events from the live stream. Returns device model, ID,
    temperature, humidity, battery status, and other sensor-specific fields."""
    duration_seconds = min(duration_seconds, 30.0)
    events = await _read_sse("/stream_sensor", duration_seconds, max_events)
    return _fmt({"event_count": len(events), "events": events})


# ===================================================================
# 4. ADS-B AIRCRAFT TRACKING (8 tools)
# ===================================================================

@mcp.tool()
async def start_adsb(
    gain: str = "40",
    device: str = "0",
    sdr_type: str = "rtlsdr",
    bias_t: bool = False,
) -> str:
    """Start ADS-B aircraft tracking on 1090 MHz. Decodes Mode-S transponder signals to track
    aircraft position, altitude, speed, callsign, and squawk code."""
    return _fmt(await _post("/adsb/start", {
        "gain": gain, "device": device, "sdr_type": sdr_type, "bias_t": bias_t,
    }))


@mcp.tool()
async def stop_adsb() -> str:
    """Stop ADS-B aircraft tracking."""
    return _fmt(await _post("/adsb/stop"))


@mcp.tool()
async def get_adsb_status() -> str:
    """Get ADS-B tracker status — running state, aircraft count, message statistics, active device."""
    return _fmt(await _get("/adsb/status"))


@mcp.tool()
async def get_aircraft(icao: str | None = None, military_only: bool = False) -> str:
    """Get currently tracked aircraft. Optionally filter by ICAO hex code or military-only.
    Returns ICAO, callsign, altitude, speed, heading, lat/lon, squawk, aircraft type."""
    params: dict[str, str] = {}
    if icao:
        params["icao"] = icao
    if military_only:
        params["military"] = "true"
    return _fmt(await _get("/adsb/aircraft", params))


@mcp.tool()
async def get_aircraft_messages(icao: str) -> str:
    """Get correlated ACARS/VDL2 datalink messages for a specific aircraft by ICAO hex code.
    Useful for cross-referencing flight communications with position data."""
    return _fmt(await _get(f"/adsb/aircraft/{icao}/messages"))


@mcp.tool()
async def get_adsb_history(
    since_minutes: int | None = None,
    limit: int = 100,
    search: str | None = None,
) -> str:
    """Query historical ADS-B data. Search by callsign, ICAO, or aircraft type.
    Requires PostgreSQL history profile to be active."""
    params: dict[str, Any] = {"limit": limit}
    if since_minutes:
        params["since_minutes"] = since_minutes
    if search:
        params["search"] = search
    return _fmt(await _get("/adsb/history/aircraft", params))


@mcp.tool()
async def get_adsb_history_summary(since_minutes: int = 60) -> str:
    """Get a summary of historical ADS-B data — message count, aircraft count, time range."""
    return _fmt(await _get("/adsb/history/summary", {"since_minutes": since_minutes}))


@mcp.tool()
async def export_aircraft(format: str = "json") -> str:
    """Export current aircraft data. Format: 'json' or 'csv'."""
    return _fmt(await _get("/export/aircraft", {"format": format}))


# ===================================================================
# 5. AIS VESSEL TRACKING (5 tools)
# ===================================================================

@mcp.tool()
async def start_ais(
    gain: str = "40",
    device: str = "0",
    sdr_type: str = "rtlsdr",
    bias_t: bool = False,
) -> str:
    """Start AIS vessel tracking on 161.975/162.025 MHz. Decodes ship transponder signals
    for vessel name, MMSI, position, course, speed, destination, and vessel type."""
    return _fmt(await _post("/ais/start", {
        "gain": gain, "device": device, "sdr_type": sdr_type, "bias_t": bias_t,
    }))


@mcp.tool()
async def stop_ais() -> str:
    """Stop AIS vessel tracking."""
    return _fmt(await _post("/ais/stop"))


@mcp.tool()
async def get_ais_status() -> str:
    """Get AIS tracker status — running state, vessel count, message statistics."""
    return _fmt(await _get("/ais/status"))


@mcp.tool()
async def get_vessels(mmsi: str | None = None) -> str:
    """Get currently tracked vessels. Optionally filter by MMSI number.
    Returns MMSI, name, callsign, vessel type, position, course, speed, destination."""
    params = {"mmsi": mmsi} if mmsi else None
    return _fmt(await _get("/ais/vessels", params))


@mcp.tool()
async def read_ais_stream(duration_seconds: float = 5.0, max_events: int = 50) -> str:
    """Read live AIS messages for the specified duration. Returns decoded vessel position reports,
    voyage data, and safety messages."""
    duration_seconds = min(duration_seconds, 30.0)
    events = await _read_sse("/ais/stream", duration_seconds, max_events)
    return _fmt({"event_count": len(events), "events": events})


# ===================================================================
# 6. WIFI SCANNING (8 tools)
# ===================================================================

@mcp.tool()
async def get_wifi_capabilities() -> str:
    """Get WiFi scanning capabilities — available interfaces, monitor mode support,
    supported bands (2.4/5 GHz), and whether running as root."""
    return _fmt(await _get("/wifi/v2/capabilities"))


@mcp.tool()
async def start_wifi_scan(
    interface: str | None = None,
    band: str = "all",
    channel: int | None = None,
) -> str:
    """Start WiFi network scanning. Requires a monitor-mode capable interface.
    Band: '2.4', '5', or 'all'. Optionally lock to a specific channel."""
    payload: dict[str, Any] = {"band": band}
    if interface:
        payload["interface"] = interface
    if channel:
        payload["channel"] = channel
    return _fmt(await _post("/wifi/v2/scan/start", payload))


@mcp.tool()
async def stop_wifi_scan() -> str:
    """Stop WiFi scanning and return interface to managed mode."""
    return _fmt(await _post("/wifi/v2/scan/stop"))


@mcp.tool()
async def get_wifi_networks(
    band: str | None = None,
    security: str | None = None,
    min_rssi: int | None = None,
    sort: str = "last_seen",
) -> str:
    """Get discovered WiFi networks. Filter by band (2.4/5), security type, or minimum signal strength.
    Returns BSSID, SSID, channel, encryption, signal strength, and connected clients."""
    params: dict[str, Any] = {"sort": sort, "format": "full"}
    if band:
        params["band"] = band
    if security:
        params["security"] = security
    if min_rssi is not None:
        params["min_rssi"] = min_rssi
    return _fmt(await _get("/wifi/v2/networks", params))


@mcp.tool()
async def get_wifi_clients(
    associated: bool | None = None,
    bssid: str | None = None,
    min_rssi: int | None = None,
) -> str:
    """Get discovered WiFi clients. Filter by association status, connected AP (BSSID),
    or minimum signal. Returns MAC, vendor, RSSI, associated network, and probe requests."""
    params: dict[str, Any] = {}
    if associated is not None:
        params["associated"] = str(associated).lower()
    if bssid:
        params["bssid"] = bssid
    if min_rssi is not None:
        params["min_rssi"] = min_rssi
    return _fmt(await _get("/wifi/v2/clients", params))


@mcp.tool()
async def get_wifi_probes(ssid: str | None = None, limit: int = 100) -> str:
    """Get WiFi probe requests — devices broadcasting SSIDs they're looking for.
    Reveals device travel history and preferred networks."""
    params: dict[str, Any] = {"limit": limit}
    if ssid:
        params["ssid"] = ssid
    return _fmt(await _get("/wifi/v2/probes", params))


@mcp.tool()
async def get_wifi_channels() -> str:
    """Get WiFi channel utilization analysis with congestion scores and recommendations
    for least-congested channels per band."""
    return _fmt(await _get("/wifi/v2/channels"))


@mcp.tool()
async def export_wifi(format: str = "json") -> str:
    """Export WiFi scan data. Format: 'json' or 'csv'."""
    return _fmt(await _get("/export/wifi", {"format": format}))


# ===================================================================
# 7. BLUETOOTH SCANNING (8 tools)
# ===================================================================

@mcp.tool()
async def get_bt_capabilities() -> str:
    """Get Bluetooth scanning capabilities — available adapters, backend (DBus/BlueZ/Bleak/hcitool),
    BLE support, and whether running as root."""
    return _fmt(await _get("/api/bluetooth/capabilities"))


@mcp.tool()
async def start_bt_scan(
    mode: str = "auto",
    transport: str = "auto",
    rssi_threshold: int = -100,
    adapter_id: str | None = None,
) -> str:
    """Start Bluetooth scan. Mode: 'auto', 'dbus', 'bleak', 'hcitool', 'bluetoothctl', 'ubertooth'.
    Transport: 'auto', 'bredr' (Classic), 'le' (BLE). Detects AirTags, Tiles, SmartTags automatically."""
    payload: dict[str, Any] = {
        "mode": mode, "transport": transport, "rssi_threshold": rssi_threshold,
    }
    if adapter_id:
        payload["adapter_id"] = adapter_id
    return _fmt(await _post("/api/bluetooth/scan/start", payload))


@mcp.tool()
async def stop_bt_scan() -> str:
    """Stop Bluetooth scanning."""
    return _fmt(await _post("/api/bluetooth/scan/stop"))


@mcp.tool()
async def get_bt_devices(
    sort: str = "last_seen",
    min_rssi: int | None = None,
    protocol: str | None = None,
    max_age: float = 300,
) -> str:
    """Get discovered Bluetooth devices. Filter by protocol ('ble'/'classic'), signal strength,
    and age. Returns address, name, RSSI, manufacturer, device type, and heuristic classification."""
    params: dict[str, Any] = {"sort": sort, "max_age": max_age}
    if min_rssi is not None:
        params["min_rssi"] = min_rssi
    if protocol:
        params["protocol"] = protocol
    return _fmt(await _get("/api/bluetooth/devices", params))


@mcp.tool()
async def get_bt_trackers(
    min_confidence: str = "medium",
    max_age: float = 300,
) -> str:
    """Get detected Bluetooth trackers (AirTag, Tile, SmartTag, Galaxy SmartTag).
    Returns tracker type, confidence level, risk assessment, and investigation guidance.
    Confidence: 'high', 'medium', 'low'."""
    return _fmt(await _get("/api/bluetooth/trackers", {
        "min_confidence": min_confidence, "max_age": max_age, "include_risk": "true",
    }))


@mcp.tool()
async def get_bt_diagnostics() -> str:
    """Get Bluetooth scan diagnostics — backend info, scan duration, device counts by type,
    error rates, and performance metrics."""
    return _fmt(await _get("/api/bluetooth/diagnostics"))


@mcp.tool()
async def set_bt_baseline() -> str:
    """Set current Bluetooth devices as the known baseline. New devices appearing after
    this point will be flagged as 'new' in subsequent scans."""
    return _fmt(await _post("/api/bluetooth/baseline/set"))


@mcp.tool()
async def export_bluetooth(format: str = "json") -> str:
    """Export Bluetooth scan data. Format: 'json' or 'csv'."""
    return _fmt(await _get("/export/bluetooth", {"format": format}))


# ===================================================================
# 8. ACARS / VDL2 AVIATION DATA (6 tools)
# ===================================================================

@mcp.tool()
async def start_acars(
    device: str = "0",
    gain: str = "40",
    sdr_type: str = "rtlsdr",
    frequencies: str | None = None,
    bias_t: bool = False,
) -> str:
    """Start ACARS aviation datalink decoder. Decodes aircraft text messages on VHF frequencies.
    Default NA frequencies: 131.550, 131.525, 131.725, 130.025, 129.125.
    Pass comma-separated frequencies to override (e.g. '131.550,131.525')."""
    payload: dict[str, Any] = {
        "device": device, "gain": gain, "sdr_type": sdr_type, "bias_t": bias_t,
    }
    if frequencies:
        payload["frequencies"] = frequencies
    return _fmt(await _post("/acars/start", payload))


@mcp.tool()
async def stop_acars() -> str:
    """Stop ACARS decoder."""
    return _fmt(await _post("/acars/stop"))


@mcp.tool()
async def get_acars_messages(limit: int = 50) -> str:
    """Get recent ACARS messages. Returns flight ID, registration, label, message text,
    and frequency. Limit: 1-200."""
    return _fmt(await _get("/acars/messages", {"limit": min(limit, 200)}))


@mcp.tool()
async def start_vdl2(
    device: str = "0",
    gain: str = "40",
    sdr_type: str = "rtlsdr",
    frequencies: str | None = None,
    bias_t: bool = False,
) -> str:
    """Start VDL Mode 2 aviation datalink decoder. Higher-bandwidth digital successor to ACARS.
    Carries CPDLC (controller-pilot datalink), ADS-C (automatic position reports), and ACARS-over-AVLC."""
    payload: dict[str, Any] = {
        "device": device, "gain": gain, "sdr_type": sdr_type, "bias_t": bias_t,
    }
    if frequencies:
        payload["frequencies"] = frequencies
    return _fmt(await _post("/vdl2/start", payload))


@mcp.tool()
async def stop_vdl2() -> str:
    """Stop VDL2 decoder."""
    return _fmt(await _post("/vdl2/stop"))


@mcp.tool()
async def get_vdl2_messages(limit: int = 50) -> str:
    """Get recent VDL2 messages. Returns decoded CPDLC, ADS-C, and ACARS-over-AVLC messages
    with flight context."""
    return _fmt(await _get("/vdl2/messages", {"limit": min(limit, 200)}))


# ===================================================================
# 9. APRS PACKET RADIO (5 tools)
# ===================================================================

@mcp.tool()
async def start_aprs(
    device: str = "0",
    gain: str = "40",
    sdr_type: str = "rtlsdr",
    region: str = "north_america",
    frequency: str | None = None,
    bias_t: bool = False,
) -> str:
    """Start APRS packet radio decoder. Receives amateur radio position reports, weather data,
    telemetry, and messages. Region sets the frequency: north_america=144.390, europe=144.800,
    australia=145.175, japan=144.660, etc. Override with frequency parameter."""
    payload: dict[str, Any] = {
        "device": device, "gain": gain, "sdr_type": sdr_type,
        "region": region, "bias_t": bias_t,
    }
    if frequency:
        payload["frequency"] = frequency
    return _fmt(await _post("/aprs/start", payload))


@mcp.tool()
async def stop_aprs() -> str:
    """Stop APRS decoder."""
    return _fmt(await _post("/aprs/stop"))


@mcp.tool()
async def get_aprs_status() -> str:
    """Get APRS decoder status — running state, packet count, station count, last packet time."""
    return _fmt(await _get("/aprs/status"))


@mcp.tool()
async def get_aprs_stations() -> str:
    """Get decoded APRS stations with position, symbol, status text, weather data,
    and path information."""
    return _fmt(await _get("/aprs/stations"))


@mcp.tool()
async def export_aprs(format: str = "json") -> str:
    """Export APRS station data. Format: 'json' or 'csv'."""
    return _fmt(await _get("/aprs/export", {"format": format}))


# ===================================================================
# 10. RTLAMR UTILITY METERS (3 tools)
# ===================================================================

@mcp.tool()
async def start_rtlamr(
    frequency: str = "912.0",
    gain: str = "40",
    device: str = "0",
    msgtype: str = "scm",
    filterid: str | None = None,
    unique: bool = True,
) -> str:
    """Start utility meter decoder (rtlamr). Reads smart meter transmissions for electric,
    gas, and water consumption. Message types: scm, scm+, idm, netidm, r900, r900bcd.
    Optionally filter by meter ID."""
    payload: dict[str, Any] = {
        "frequency": frequency, "gain": gain, "device": device,
        "msgtype": msgtype, "unique": unique,
    }
    if filterid:
        payload["filterid"] = filterid
    return _fmt(await _post("/start_rtlamr", payload))


@mcp.tool()
async def stop_rtlamr() -> str:
    """Stop utility meter decoder."""
    return _fmt(await _post("/stop_rtlamr"))


@mcp.tool()
async def read_rtlamr_data(duration_seconds: float = 10.0, max_events: int = 50) -> str:
    """Read decoded meter readings from the live stream. Returns meter ID, type,
    consumption value, and signal metadata."""
    duration_seconds = min(duration_seconds, 30.0)
    events = await _read_sse("/stream_rtlamr", duration_seconds, max_events)
    return _fmt({"event_count": len(events), "events": events})


# ===================================================================
# 11. SUBGHZ ANALYSIS (5 tools)
# ===================================================================

@mcp.tool()
async def start_subghz_decode(
    frequency_hz: int = 433920000,
    sample_rate: int = 2000000,
    lna_gain: int = 32,
    vga_gain: int = 20,
    decode_profile: str = "weather",
    device_serial: str | None = None,
) -> str:
    """Start SubGHz protocol decoder (requires HackRF). Decode 433/868/915 MHz devices:
    weather stations, keyfobs, garage doors, tire sensors, doorbells.
    Profile: 'weather' (common sensors) or 'all' (full protocol set)."""
    payload: dict[str, Any] = {
        "frequency_hz": frequency_hz, "sample_rate": sample_rate,
        "lna_gain": lna_gain, "vga_gain": vga_gain, "decode_profile": decode_profile,
    }
    if device_serial:
        payload["device_serial"] = device_serial
    return _fmt(await _post("/subghz/decode/start", payload))


@mcp.tool()
async def stop_subghz_decode() -> str:
    """Stop SubGHz protocol decoder."""
    return _fmt(await _post("/subghz/decode/stop"))


@mcp.tool()
async def get_subghz_status() -> str:
    """Get SubGHz module status — active mode (rx/decode/tx/sweep), frequency, capture count."""
    return _fmt(await _get("/subghz/status"))


@mcp.tool()
async def get_subghz_captures() -> str:
    """Get list of captured SubGHz signals with metadata — frequency, timestamp, duration,
    protocol matches, and signal characteristics."""
    return _fmt(await _get("/subghz/captures"))


@mcp.tool()
async def get_subghz_presets() -> str:
    """Get SubGHz frequency presets (ISM 433/868/915, weather, automotive, home automation)
    and available sample rates."""
    return _fmt(await _get("/subghz/presets"))


# ===================================================================
# 12. SIGNAL IDENTIFICATION (1 tool)
# ===================================================================

@mcp.tool()
async def signal_identify(
    frequency_mhz: float | None = None,
    bandwidth_khz: float | None = None,
    modulation: str | None = None,
    description: str | None = None,
) -> str:
    """Identify an unknown signal by its characteristics. Searches the SigIdWiki database.
    Provide any combination of: frequency (MHz), bandwidth (kHz), modulation type,
    or a text description of what you're hearing/seeing."""
    payload: dict[str, Any] = {}
    if frequency_mhz is not None:
        payload["frequency_mhz"] = frequency_mhz
    if bandwidth_khz is not None:
        payload["bandwidth_khz"] = bandwidth_khz
    if modulation:
        payload["modulation"] = modulation
    if description:
        payload["description"] = description
    return _fmt(await _post("/signalid/match", payload))


# ===================================================================
# 13. TSCM COUNTER-SURVEILLANCE (8 tools)
# ===================================================================

@mcp.tool()
async def start_tscm_sweep(
    sweep_type: str = "standard",
    wifi: bool = True,
    bluetooth: bool = True,
    rf: bool = True,
    baseline_id: int | None = None,
    wifi_interface: str | None = None,
    bt_interface: str | None = None,
    sdr_device: int | None = None,
) -> str:
    """Start a TSCM counter-surveillance sweep. Scans WiFi, Bluetooth, and RF spectrum
    for surveillance devices, hidden transmitters, and anomalous signals.
    Sweep types: 'standard', 'quick', 'thorough', 'custom'.
    Optionally compare against a baseline to detect new/changed devices."""
    payload: dict[str, Any] = {
        "sweep_type": sweep_type, "wifi": wifi, "bluetooth": bluetooth, "rf": rf,
    }
    if baseline_id is not None:
        payload["baseline_id"] = baseline_id
    if wifi_interface:
        payload["wifi_interface"] = wifi_interface
    if bt_interface:
        payload["bt_interface"] = bt_interface
    if sdr_device is not None:
        payload["sdr_device"] = sdr_device
    return _fmt(await _post("/tscm/sweep/start", payload))


@mcp.tool()
async def stop_tscm_sweep() -> str:
    """Stop the current TSCM sweep."""
    return _fmt(await _post("/tscm/sweep/stop"))


@mcp.tool()
async def get_tscm_threats(
    severity: str | None = None,
    limit: int = 100,
) -> str:
    """Get detected TSCM threats with severity rating, signal characteristics,
    and recommended countermeasures. Filter by severity: 'critical', 'high', 'medium', 'low'."""
    params: dict[str, Any] = {"limit": limit}
    if severity:
        params["severity"] = severity
    return _fmt(await _get("/tscm/threats", params))


@mcp.tool()
async def get_tscm_findings() -> str:
    """Get all TSCM analysis findings — cross-protocol device correlations,
    MAC randomization clusters, behavioral anomalies, and risk-scored devices."""
    return _fmt(await _get("/tscm/findings"))


@mcp.tool()
async def set_tscm_baseline(name: str | None = None, location: str | None = None) -> str:
    """Record the current RF/WiFi/Bluetooth environment as a 'known good' baseline.
    Future sweeps can compare against this to detect new or changed devices."""
    payload: dict[str, Any] = {}
    if name:
        payload["name"] = name
    if location:
        payload["location"] = location
    return _fmt(await _post("/tscm/baseline/record", payload))


@mcp.tool()
async def get_tscm_report() -> str:
    """Generate a comprehensive TSCM report — executive summary, threat inventory,
    device census, RF environment analysis, and recommended actions."""
    return _fmt(await _get("/tscm/report"))


@mcp.tool()
async def get_tscm_capabilities() -> str:
    """Get available TSCM capabilities — supported sweep modes, available WiFi interfaces,
    Bluetooth adapters, SDR devices, and whether running with root privileges."""
    return _fmt(await _get("/tscm/capabilities"))


@mcp.tool()
async def start_tscm_meeting(name: str | None = None, location: str | None = None) -> str:
    """Start meeting security mode — continuous TSCM monitoring during a sensitive meeting.
    Automatically detects new devices appearing, unusual RF activity, and potential recording devices."""
    payload: dict[str, Any] = {}
    if name:
        payload["name"] = name
    if location:
        payload["location"] = location
    return _fmt(await _post("/tscm/meeting/start-tracked", payload))


# ===================================================================
# 14. SATELLITE TRACKING (5 tools)
# ===================================================================

@mcp.tool()
async def predict_passes(
    latitude: float | None = None,
    longitude: float | None = None,
    hours: int = 24,
    min_elevation: float = 10.0,
    satellites: list[str] | None = None,
) -> str:
    """Predict satellite passes visible from a location. Default satellites: ISS, METEOR-M2-3, METEOR-M2-4.
    Returns pass start/end times, max elevation, azimuth, and duration."""
    payload: dict[str, Any] = {"hours": hours, "minEl": min_elevation}
    if latitude is not None:
        payload["latitude"] = latitude
    if longitude is not None:
        payload["longitude"] = longitude
    if satellites:
        payload["satellites"] = satellites
    return _fmt(await _post("/satellite/predict", payload))


@mcp.tool()
async def get_satellite_position(
    satellites: list[str] | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> str:
    """Get current real-time position of satellites (lat, lon, altitude, velocity).
    Default: ISS. Include track for ground-track projection."""
    payload: dict[str, Any] = {"includeTrack": True}
    if satellites:
        payload["satellites"] = satellites
    if latitude is not None:
        payload["latitude"] = latitude
    if longitude is not None:
        payload["longitude"] = longitude
    return _fmt(await _post("/satellite/position", payload))


@mcp.tool()
async def get_tracked_satellites() -> str:
    """Get list of satellites being tracked (NORAD ID, name, TLE data, enabled status)."""
    return _fmt(await _get("/satellite/tracked"))


@mcp.tool()
async def add_tracked_satellite(norad_id: str, name: str) -> str:
    """Add a satellite to the tracking list by NORAD catalog ID and name."""
    return _fmt(await _post("/satellite/tracked", {"norad_id": norad_id, "name": name}))


@mcp.tool()
async def update_tle() -> str:
    """Refresh TLE (Two-Line Element) orbital data from CelesTrak. Run before pass predictions
    to ensure accuracy — TLEs go stale after a few days."""
    return _fmt(await _post("/satellite/update-tle"))


# ===================================================================
# 15. WEATHER SATELLITE IMAGERY (5 tools)
# ===================================================================

@mcp.tool()
async def start_weather_sat(
    satellite: str,
    device: int = 0,
    gain: float = 30.0,
    sdr_type: str = "rtlsdr",
    bias_t: bool = False,
) -> str:
    """Start weather satellite decoder (SatDump). Satellite names: 'noaa-15', 'noaa-18', 'noaa-19'
    (APT 137 MHz), 'meteor-m2-3', 'meteor-m2-4' (LRPT 137 MHz). Decodes live satellite imagery."""
    return _fmt(await _post("/weather-sat/start", {
        "satellite": satellite, "device": device, "gain": gain,
        "sdr_type": sdr_type, "bias_t": bias_t,
    }))


@mcp.tool()
async def stop_weather_sat() -> str:
    """Stop weather satellite decoder."""
    return _fmt(await _post("/weather-sat/stop"))


@mcp.tool()
async def get_weather_sat_status() -> str:
    """Get weather satellite decoder status — running state, active satellite, signal metrics,
    decode progress."""
    return _fmt(await _get("/weather-sat/status"))


@mcp.tool()
async def get_weather_images(satellite: str | None = None, limit: int = 20) -> str:
    """Get list of decoded weather satellite images with filenames, timestamps, satellite name,
    and image dimensions. Optionally filter by satellite."""
    params: dict[str, Any] = {"limit": limit}
    if satellite:
        params["satellite"] = satellite
    return _fmt(await _get("/weather-sat/images", params))


@mcp.tool()
async def get_weather_passes(
    latitude: float | None = None,
    longitude: float | None = None,
    hours: int = 24,
    min_elevation: float = 15.0,
) -> str:
    """Predict upcoming weather satellite passes with optimal reception windows.
    Returns pass times, max elevation, and whether it's a northbound or southbound pass."""
    params: dict[str, Any] = {"hours": hours, "min_elevation": min_elevation}
    if latitude is not None:
        params["latitude"] = str(latitude)
    if longitude is not None:
        params["longitude"] = str(longitude)
    return _fmt(await _get("/weather-sat/passes", params))


# ===================================================================
# 16. MESHTASTIC MESH NETWORKING (8 tools)
# ===================================================================

@mcp.tool()
async def meshtastic_start(
    connection_type: str = "serial",
    device: str | None = None,
    hostname: str | None = None,
) -> str:
    """Connect to a Meshtastic LoRa mesh radio. Connection type: 'serial' (USB) or 'tcp' (network).
    For serial, specify the COM port / device path. For TCP, specify the hostname."""
    payload: dict[str, Any] = {"connection_type": connection_type}
    if device:
        payload["device"] = device
    if hostname:
        payload["hostname"] = hostname
    return _fmt(await _post("/meshtastic/start", payload))


@mcp.tool()
async def meshtastic_stop() -> str:
    """Disconnect from Meshtastic device."""
    return _fmt(await _post("/meshtastic/stop"))


@mcp.tool()
async def meshtastic_send(text: str, channel: int = 0, to: str | None = None) -> str:
    """Send a text message on the Meshtastic mesh network. Max 237 characters.
    Channel 0-7 (default 0 = primary). Optionally specify destination node ID for DM."""
    payload: dict[str, Any] = {"text": text[:237], "channel": channel}
    if to:
        payload["to"] = to
    return _fmt(await _post("/meshtastic/send", payload))


@mcp.tool()
async def meshtastic_nodes(with_position: bool = False) -> str:
    """Get all nodes on the Meshtastic mesh — node ID, long/short name, hardware model,
    battery level, SNR, last heard. Optionally include GPS positions."""
    params = {"with_position": "true"} if with_position else None
    return _fmt(await _get("/meshtastic/nodes", params))


@mcp.tool()
async def meshtastic_messages(limit: int | None = None, channel: int | None = None) -> str:
    """Get recent Meshtastic messages. Optionally filter by channel number."""
    params: dict[str, Any] = {}
    if limit:
        params["limit"] = limit
    if channel is not None:
        params["channel"] = channel
    return _fmt(await _get("/meshtastic/messages", params))


@mcp.tool()
async def meshtastic_channels() -> str:
    """Get Meshtastic channel configuration — name, encryption settings, and role for each channel slot (0-7)."""
    return _fmt(await _get("/meshtastic/channels"))


@mcp.tool()
async def meshtastic_ports() -> str:
    """List available serial ports for Meshtastic device connection."""
    return _fmt(await _get("/meshtastic/ports"))


@mcp.tool()
async def meshtastic_topology() -> str:
    """Get mesh network topology graph — nodes, edges (neighbor links), SNR values,
    and hop counts. Useful for understanding network coverage and routing."""
    return _fmt(await _get("/meshtastic/topology"))


# ===================================================================
# 17. LISTENING POST / SCANNER (6 tools)
# ===================================================================

@mcp.tool()
async def start_audio_rx(
    frequency_mhz: float = 100.0,
    modulation: str = "fm",
    gain: str = "40",
    device: str = "0",
    sdr_type: str = "rtlsdr",
    squelch: int = 0,
) -> str:
    """Start the listening post audio receiver. Tunes to a frequency and demodulates audio.
    Modulation: 'fm', 'am', 'usb', 'lsb'. Use for monitoring voice channels, utility stations,
    number stations, or any analog signal."""
    return _fmt(await _post("/receiver/audio/start", {
        "frequency_mhz": frequency_mhz, "modulation": modulation,
        "gain": gain, "device": device, "sdr_type": sdr_type, "squelch": squelch,
    }))


@mcp.tool()
async def stop_audio_rx() -> str:
    """Stop the listening post audio receiver."""
    return _fmt(await _post("/receiver/audio/stop"))


@mcp.tool()
async def start_scanner(
    preset: str | None = None,
    frequencies: list[str] | None = None,
    dwell_time: float = 2.0,
    device: str = "0",
    sdr_type: str = "rtlsdr",
) -> str:
    """Start the frequency scanner. Scans a list of frequencies looking for active transmissions.
    Use a preset name or provide custom frequency list. Dwell time is seconds per frequency."""
    payload: dict[str, Any] = {"dwell_time": dwell_time, "device": device, "sdr_type": sdr_type}
    if preset:
        payload["preset"] = preset
    if frequencies:
        payload["frequencies"] = frequencies
    return _fmt(await _post("/receiver/scanner/start", payload))


@mcp.tool()
async def stop_scanner() -> str:
    """Stop the frequency scanner."""
    return _fmt(await _post("/receiver/scanner/stop"))


@mcp.tool()
async def get_scanner_status() -> str:
    """Get scanner status — running state, current frequency, active hits, scan progress."""
    return _fmt(await _get("/receiver/scanner/status"))


@mcp.tool()
async def get_scanner_presets() -> str:
    """Get available scanner presets — marine VHF, aviation, GMRS/FRS, public safety, railroad, etc."""
    return _fmt(await _get("/receiver/presets"))


# ===================================================================
# 18. REMOTE AGENTS / MULTI-NODE (5 tools)
# ===================================================================

@mcp.tool()
async def list_agents(active_only: bool = True) -> str:
    """List registered remote INTERCEPT collection agents with name, URL, status,
    capabilities, and running modes."""
    return _fmt(await _get("/controller/agents", {
        "active_only": str(active_only).lower(),
    }))


@mcp.tool()
async def agent_start_mode(agent_id: int, mode: str, params: dict | None = None) -> str:
    """Start a decoder mode on a remote agent. Mode: pager, sensor, adsb, ais, acars, aprs,
    wifi, bluetooth, tscm, rtlamr, satellite, listening_post.
    Params are mode-specific (same as local start endpoints)."""
    return _fmt(await _post(f"/controller/agents/{agent_id}/{mode}/start", params or {}))


@mcp.tool()
async def agent_stop_mode(agent_id: int, mode: str) -> str:
    """Stop a decoder mode on a remote agent."""
    return _fmt(await _post(f"/controller/agents/{agent_id}/{mode}/stop"))


@mcp.tool()
async def agent_status(agent_id: int) -> str:
    """Get status of a remote agent — running modes, uptime, device availability."""
    return _fmt(await _get(f"/controller/agents/{agent_id}/status"))


@mcp.tool()
async def agent_health() -> str:
    """Get health status of all registered remote agents at once."""
    return _fmt(await _get("/controller/agents/health"))


# ===================================================================
# 19. COMPOSITE / CONVENIENCE (4 tools)
# ===================================================================

@mcp.tool()
async def get_overview() -> str:
    """Get a full system overview in one call — connected SDR devices, active decoder modes,
    recent TSCM threats, and system health. Use as your first call to understand the current state."""
    results: dict[str, Any] = {}
    for key, path in [
        ("health", "/health"),
        ("devices", "/devices/status"),
        ("system", "/system/metrics"),
    ]:
        try:
            results[key] = await _get(path)
        except Exception as e:
            results[key] = {"error": str(e)}
    return _fmt(results)


@mcp.tool()
async def get_all_active_modes() -> str:
    """Check which decoder modes are currently running across all signal types.
    Returns a compact summary of every mode's running/stopped status."""
    status_endpoints = {
        "pager": "/status",
        "sensor": "/sensor/status",
        "adsb": "/adsb/status",
        "ais": "/ais/status",
        "acars": "/acars/status",
        "vdl2": "/vdl2/status",
        "aprs": "/aprs/status",
        "subghz": "/subghz/status",
        "weather_sat": "/weather-sat/status",
        "meshtastic": "/meshtastic/status",
        "tscm": "/tscm/sweep/status",
    }
    results: dict[str, Any] = {}
    for mode, path in status_endpoints.items():
        try:
            data = await _get(path)
            results[mode] = {
                "running": data.get("running", data.get("tracking_active", False)),
            }
        except Exception:
            results[mode] = {"running": False, "error": "endpoint unavailable"}
    active = [m for m, s in results.items() if s.get("running")]
    return _fmt({"active_modes": active, "active_count": len(active), "details": results})


@mcp.tool()
async def search_spy_stations(
    frequency: float | None = None,
    name: str | None = None,
) -> str:
    """Search the number station / spy station database. Filter by frequency (MHz) or station name.
    Returns station profiles with schedule, modulation, and known associations."""
    params: dict[str, Any] = {}
    if frequency is not None:
        params["frequency"] = frequency
    if name:
        params["name"] = name
    return _fmt(await _get("/spy-stations/stations", params))


@mcp.tool()
async def get_recordings() -> str:
    """List available IQ and audio recordings with filename, duration, frequency, sample rate,
    and file size."""
    return _fmt(await _get("/recordings/list"))


# ===================================================================
# Entry point
# ===================================================================

if __name__ == "__main__":
    mcp.run()
