# INTERCEPT MCP Server

MCP (Model Context Protocol) server that gives Claude full control over the INTERCEPT Signal Intelligence platform — 106 tools across 19 domains.

## Requirements

```bash
pip install mcp[cli] httpx
```

Or from the included requirements file:

```bash
pip install -r mcp_server/requirements.txt
```

## Setup

The MCP server connects to a running INTERCEPT instance over HTTP. Set `INTERCEPT_URL` to point at your server (defaults to `http://localhost:5000`).

### Claude Code

From the INTERCEPT project root:

```bash
claude mcp add intercept -- python mcp_server/intercept_mcp.py
```

To point at a remote INTERCEPT instance:

```bash
claude mcp add intercept -e INTERCEPT_URL=http://10.0.0.5:5000 -- python mcp_server/intercept_mcp.py
```

### Claude Desktop

Add to your Claude Desktop config file:

| OS | Path |
|----|------|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "intercept": {
      "command": "python",
      "args": ["mcp_server/intercept_mcp.py"],
      "cwd": "/path/to/intercept",
      "env": {
        "INTERCEPT_URL": "http://localhost:5000"
      }
    }
  }
}
```

Replace `/path/to/intercept` with the absolute path to your cloned repo.

A copy-paste template is provided in [`claude_desktop_config.example.json`](claude_desktop_config.example.json).

### Docker

If INTERCEPT is running in Docker, use the container's published port:

```json
{
  "mcpServers": {
    "intercept": {
      "command": "python",
      "args": ["mcp_server/intercept_mcp.py"],
      "cwd": "/path/to/intercept",
      "env": {
        "INTERCEPT_URL": "http://localhost:5000"
      }
    }
  }
}
```

## Tool Domains

| Domain | Tools | Examples |
|--------|-------|---------|
| Core / System | 8 | `health_check`, `get_devices`, `killall` |
| Pager (POCSAG/FLEX) | 4 | `start_pager`, `read_pager_messages` |
| 433 MHz Sensors | 4 | `start_sensor`, `read_sensor_data` |
| ADS-B Aircraft | 8 | `start_adsb`, `get_aircraft`, `get_adsb_history` |
| AIS Vessels | 5 | `start_ais`, `get_vessels` |
| WiFi Scanning | 8 | `start_wifi_scan`, `get_wifi_networks`, `get_wifi_probes` |
| Bluetooth Scanning | 8 | `start_bt_scan`, `get_bt_trackers`, `get_bt_devices` |
| ACARS + VDL2 Aviation | 6 | `start_acars`, `get_acars_messages`, `start_vdl2` |
| APRS Packet Radio | 5 | `start_aprs`, `get_aprs_stations` |
| RTLAMR Utility Meters | 3 | `start_rtlamr`, `read_rtlamr_data` |
| SubGHz Analysis | 5 | `start_subghz_decode`, `get_subghz_captures` |
| Signal Identification | 1 | `signal_identify` |
| TSCM Counter-Surveillance | 8 | `start_tscm_sweep`, `get_tscm_threats`, `get_tscm_report` |
| Satellite Tracking | 5 | `predict_passes`, `get_satellite_position` |
| Weather Satellite | 5 | `start_weather_sat`, `get_weather_images` |
| Meshtastic Mesh | 8 | `meshtastic_send`, `meshtastic_nodes`, `meshtastic_topology` |
| Listening Post | 6 | `start_audio_rx`, `start_scanner`, `get_scanner_presets` |
| Remote Agents | 5 | `list_agents`, `agent_start_mode`, `agent_health` |
| Composite | 4 | `get_overview`, `get_all_active_modes`, `search_spy_stations` |

## Usage Examples

Once installed, Claude can control INTERCEPT directly:

- "What SDR hardware is connected?" — calls `get_devices`
- "Start tracking aircraft" — calls `start_adsb`
- "Are there any Bluetooth trackers nearby?" — calls `start_bt_scan` then `get_bt_trackers`
- "Run a TSCM sweep and tell me what you find" — calls `start_tscm_sweep`, waits, calls `get_tscm_threats`
- "When is the next NOAA-19 pass?" — calls `predict_passes`
- "Send 'hello' on the Meshtastic mesh" — calls `meshtastic_send`
- "What's the full system status?" — calls `get_overview`
