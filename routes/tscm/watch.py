"""
TSCM RF Watch & Spectral Baseline Routes

Handles:
  /tscm/watch/*          - Continuous RF monitoring daemon
  /tscm/spectral/*       - Spectral baseline management
"""

from __future__ import annotations

import logging

from flask import jsonify, request

from routes.tscm import _emit_event, tscm_bp
from utils.tscm.rf_watch import start_watch, stop_watch, watch_status, get_watch_daemon
from utils.tscm.spectral_baseline import (
    SpectralDeltaEngine,
    SpectralStore,
    build_snapshot_from_rf_signals,
)

logger = logging.getLogger("intercept.tscm.watch")


# =============================================================================
# Continuous Watch Endpoints
# =============================================================================


@tscm_bp.route("/watch/start", methods=["POST"])
def start_rf_watch():
    """Start the continuous RF watch daemon."""
    data = request.get_json(silent=True) or {}
    device_args = data.get("device_args", "")
    gain = float(data.get("gain", 40))

    bands = data.get("bands")
    band_tuples = None
    if bands:
        band_tuples = []
        for b in bands:
            band_tuples.append((
                int(b["start_hz"]),
                int(b["end_hz"]),
                b.get("name", "custom"),
            ))

    result = start_watch(device_args, band_tuples, gain)

    if result.get("status") == "started":
        _emit_event("watch_started", {
            "device_args": device_args,
            "band_count": len(band_tuples or []),
        })

    return jsonify(result)


@tscm_bp.route("/watch/stop", methods=["POST"])
def stop_rf_watch():
    """Stop the continuous RF watch daemon."""
    result = stop_watch()
    if result.get("status") == "stopped":
        _emit_event("watch_stopped", result.get("stats", {}))
    return jsonify(result)


@tscm_bp.route("/watch/status", methods=["GET"])
def get_rf_watch_status():
    """Get the current watch daemon status."""
    return jsonify(watch_status())


@tscm_bp.route("/watch/waterfall", methods=["GET"])
def get_watch_waterfall():
    """Get recent waterfall data."""
    seconds = int(request.args.get("seconds", 300))
    daemon = get_watch_daemon()
    if not daemon:
        return jsonify({"frames": [], "summary": {"frame_count": 0}})
    return jsonify({
        "frames": daemon.waterfall.get_recent(seconds),
        "summary": daemon.waterfall.get_summary(),
    })


# =============================================================================
# Spectral Baseline Endpoints
# =============================================================================


@tscm_bp.route("/spectral/baselines", methods=["GET"])
def list_spectral_baselines():
    """List all spectral baselines."""
    store = SpectralStore()
    return jsonify({"baselines": store.list_baselines()})


@tscm_bp.route("/spectral/baselines", methods=["POST"])
def create_spectral_baseline():
    """Create a new spectral baseline."""
    data = request.get_json(silent=True) or {}
    name = data.get("name", "Spectral Baseline")
    description = data.get("description", "")
    store = SpectralStore()
    bl_id = store.create_baseline(name, description)
    if data.get("activate", False):
        store.activate_baseline(bl_id)
    return jsonify({"id": bl_id, "name": name, "status": "created"})


@tscm_bp.route("/spectral/baselines/<int:baseline_id>", methods=["GET"])
def get_spectral_baseline(baseline_id):
    """Get a spectral baseline with its bin summary."""
    store = SpectralStore()
    bl = store.get_baseline(baseline_id)
    if not bl:
        return jsonify({"error": "Spectral baseline not found"}), 404
    bins = store.get_bins(baseline_id)
    bands = {}
    for b in bins.values():
        band = b.get("band", "unknown")
        if band not in bands:
            bands[band] = {"count": 0, "avg_power": 0, "signals": []}
        bands[band]["count"] += 1
        bands[band]["avg_power"] += b["power_mean"]
    for band in bands.values():
        if band["count"] > 0:
            band["avg_power"] = round(band["avg_power"] / band["count"], 1)
    return jsonify({**bl, "bin_count": len(bins), "band_summary": bands})


@tscm_bp.route("/spectral/baselines/<int:baseline_id>", methods=["DELETE"])
def delete_spectral_baseline(baseline_id):
    """Delete a spectral baseline."""
    store = SpectralStore()
    store.delete_baseline(baseline_id)
    return jsonify({"status": "deleted"})


@tscm_bp.route("/spectral/baselines/<int:baseline_id>/activate", methods=["POST"])
def activate_spectral_baseline(baseline_id):
    """Set a spectral baseline as active."""
    store = SpectralStore()
    store.activate_baseline(baseline_id)
    return jsonify({"status": "activated", "baseline_id": baseline_id})


@tscm_bp.route("/spectral/baselines/<int:baseline_id>/ingest", methods=["POST"])
def ingest_spectral_data(baseline_id):
    """Manually ingest RF signal data into a spectral baseline."""
    data = request.get_json(silent=True) or {}
    rf_signals = data.get("rf_signals", [])
    sweep_id = data.get("sweep_id")

    if not rf_signals:
        return jsonify({"error": "No rf_signals provided"}), 400

    store = SpectralStore()
    snapshot = build_snapshot_from_rf_signals(rf_signals, sweep_id)
    store.ingest_snapshot(baseline_id, snapshot)
    return jsonify({
        "status": "ingested",
        "bins_in_snapshot": len(snapshot.bins),
        "bands": list(snapshot.noise_floors.keys()),
    })


@tscm_bp.route("/spectral/compare", methods=["POST"])
def compare_spectral():
    """Compare RF signals against the active spectral baseline."""
    data = request.get_json(silent=True) or {}
    rf_signals = data.get("rf_signals", [])
    baseline_id = data.get("baseline_id")

    store = SpectralStore()
    if baseline_id is None:
        baseline_id = store.get_active_baseline_id()
    if baseline_id is None:
        return jsonify({"error": "No spectral baseline active"}), 404

    if not rf_signals:
        return jsonify({"error": "No rf_signals provided"}), 400

    snapshot = build_snapshot_from_rf_signals(rf_signals)
    engine = SpectralDeltaEngine(store)
    anomalies = engine.compare(snapshot, baseline_id)

    return jsonify({
        "baseline_id": baseline_id,
        "anomaly_count": len(anomalies),
        "anomalies": [a.to_dict() for a in anomalies],
        "severity_counts": {
            sev: sum(1 for a in anomalies if a.severity == sev)
            for sev in ("critical", "high", "medium", "low")
        },
    })
