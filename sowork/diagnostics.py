"""Read-only local inspection: no adapter, secrets, network or queue recovery."""
import json
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import time
import tomllib

from .admin import AdminError, binding, config_summary, managed_config_present, read_config


def installed_version():
    try:
        data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
        version = data["project"]["version"]
        return version if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version) else "unknown"
    except (OSError, ValueError, KeyError):
        return "unknown"


def readonly_uri(path):
    if path.is_symlink():
        raise AdminError("state_symlink")
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    if wal.is_symlink() or shm.is_symlink():
        raise AdminError("state_sidecar_symlink")
    if wal.exists() and not shm.exists():
        raise AdminError("state_wal_requires_live_reader")
    return path.as_uri() + ("?mode=ro" if wal.exists() else "?mode=ro&immutable=1")


def database_summary(home, subscription_id, now):
    matches = []
    for path in sorted((home / "sowork").glob("*.sqlite3")):
        if path.is_symlink():
            continue
        # Never create a WAL shared-memory sidecar during inspection. A live store
        # already has one. A checkpointed store can safely use immutable read mode.
        with closing(sqlite3.connect(readonly_uri(path), uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            scope = db.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
            if not scope or not scope[0].endswith(":" + subscription_id):
                continue
            meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('cursor','observed_at')"))
            observed = float(meta["observed_at"]) if meta.get("observed_at") else None
            pending, oldest = db.execute("SELECT count(*),min(created) FROM outbox WHERE status='pending'").fetchone()
            inbox = dict(db.execute("SELECT status,count(*) FROM inbox GROUP BY status"))
            # Status values come from local data, so emit only the known enum.
            inbox = {k: inbox.get(k, 0) for k in ("queued", "claimed", "complete", "failed", "cancelled", "rejected", "uncertain")}
            matches.append({"cursor_present": bool(meta.get("cursor")),
                "observed_age_seconds": max(0, round(now - observed)) if observed else None,
                "pending_deliveries": pending,
                "oldest_pending_age_seconds": max(0, round(now - oldest)) if oldest else None,
                "inbox": inbox, "unfinished_visuals": db.execute("SELECT count(*) FROM visuals").fetchone()[0]})
    if len(matches) > 1:
        raise AdminError("state_scope_ambiguous")
    return matches[0] if matches else None


def gateway_summary(home, now):
    path = home / "gateway_state.json"
    if not path.is_file() or path.is_symlink():
        return {"state": "unknown", "fresh": False}
    try:
        data = json.loads(path.read_text())
        age = max(0, now - path.stat().st_mtime)
        from gateway.status import get_runtime_status_running_pid
        # Native snapshots may omit hermes_home. Hermes verifies process start
        # identity and the requested profile; bare PID existence is insufficient.
        pid = get_runtime_status_running_pid(data, expected_home=home)
        fresh = age <= 120 and pid is not None and pid == int(data["pid"])
        state = data.get("platforms", {}).get("sowork", {}).get("state")
        allowed = {"connected", "disconnected", "connecting", "error", "failed", "fatal"}
        return {"state": state if fresh and state in allowed else "unknown", "fresh": fresh,
                "snapshot_age_seconds": round(age)}
    except (ValueError, KeyError, OSError, TypeError, ImportError):
        return {"state": "unknown", "fresh": False}


def inspect(home, *, now=None):
    now = time.time() if now is None else now
    config, sha = read_config(home)
    settings = binding(config)
    summary = config_summary(config, settings)
    issues = []
    managed = managed_config_present()
    if managed:
        issues.append("managed_config_requires_native_review")
    if settings.preview_enabled and not summary["preview_available_locally"]:
        issues.append("preview_config_inconsistent")
    gateway = gateway_summary(home, now)
    if not gateway["fresh"] or gateway["state"] != "connected":
        issues.append("gateway_status_unconfirmed")
    try:
        state = database_summary(home, settings.subscription_id, now)
    except (AdminError, sqlite3.Error, OSError, ValueError):
        state = None
        issues.append("state_unreadable_or_ambiguous")
    if state is None:
        issues.append("local_state_unavailable")
    else:
        age = state["observed_age_seconds"]
        if age is None or age > 60:
            issues.append("stream_observation_stale")
        if age is not None and age >= 72 * 3600:
            issues.append("retention_reconciliation_required")
        if state["pending_deliveries"]:
            issues.append("pending_delivery_review")
        if (state["oldest_pending_age_seconds"] or 0) >= 3600:
            issues.append("delivery_reconciliation_required")
        if state["inbox"]["failed"] or state["inbox"]["uncertain"]:
            issues.append("inbox_failure_review")
        if state["unfinished_visuals"] and not gateway["fresh"]:
            issues.append("unfinished_visual_review")
    return {"profile": str(home), "installed_plugin_version": installed_version(), "config_sha256": sha, **summary,
            "preview_compatibility": {"status": "not_checked", "configuration_ready": summary["preview_available_locally"],
                                      "command": "check-preview"},
            "gateway": gateway, "state": state, "issues": issues,
            "local_only": True, "raw_config_only": True, "managed_overlay_present": managed,
            "connection_verified": False}
