"""Local preview checks. Provider imports run only inside a guarded disposable process."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

from .admin import AdminError, binding, managed_config_present, read_config
from .diagnostics import readonly_uri

PROBE_TIMEOUT = 15
MAX_ROUTES = 64
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@+\[\]\\-]{0,159}\Z")
PROVIDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")


def public_name(value, pattern):
    return value if isinstance(value, str) and pattern.fullmatch(value) and not value.startswith("op://") else None


def route(model, *, kind, thread_id=None, override=None):
    base = dict(model) if isinstance(model, dict) else {"default": model}
    override = override or {}
    provider = public_name(override.get("provider") or base.get("provider"), PROVIDER)
    name = public_name(override.get("model") or base.get("default"), MODEL)
    # Only ACP scheme markers affect the selector; endpoint credentials never leave this process.
    endpoint = override.get("base_url") or base.get("base_url") or ""
    scheme = (endpoint.lower().split(":", 1)[0] if isinstance(endpoint, str)
              and endpoint.lower().startswith(("acp://", "acp+tcp://")) else "")
    return {"kind": kind, "thread_id": thread_id, "model": name, "provider": provider,
            "acp_scheme": scheme if scheme in ("acp", "acp+tcp") else None,
            "model_streaming_disabled": str(base.get("streaming", "true")).strip().lower() in ("false", "0", "no", "off")}


def session_routes(home, settings, model):
    path = home / "state.db"
    if not path.exists():
        return [], ["preview_session_routes_unavailable"]
    try:
        with closing(sqlite3.connect(readonly_uri(path), uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            # Select public routing fields only, never entry_json, prompts, messages or model_config.
            rows = db.execute("""SELECT session_key,
                CASE WHEN json_valid(entry_json) THEN json_extract(entry_json,'$.origin.platform') END,
                CASE WHEN json_valid(entry_json) THEN json_extract(entry_json,'$.origin.chat_id') END,
                CASE WHEN json_valid(entry_json) THEN json_extract(entry_json,'$.origin.user_id') END,
                CASE WHEN json_valid(entry_json) THEN json_extract(entry_json,'$.origin.thread_id') END,
                CASE WHEN json_valid(entry_json) THEN json_extract(entry_json,'$.model_override.model') END,
                CASE WHEN json_valid(entry_json) THEN json_extract(entry_json,'$.model_override.provider') END,
                CASE WHEN json_valid(entry_json) THEN json_extract(entry_json,'$.model_override.base_url') END
                FROM gateway_routing WHERE scope=? AND instr(session_key,?)>0
                ORDER BY session_key LIMIT ?""",
                (str(home / "sessions"), ":sowork:dm:" + settings.channel_id, MAX_ROUTES + 1)).fetchall()
    except (AdminError, sqlite3.Error, OSError, ValueError):
        return [], ["preview_session_routes_unavailable"]
    if len(rows) > MAX_ROUTES:
        return [], ["preview_session_route_limit"]
    result, issues = [], []
    for key, platform, channel, user, thread, name, provider, endpoint in rows:
        # A different DM sharing an ID prefix is not this binding's conversation.
        if channel != settings.channel_id and channel is not None:
            continue
        if (platform != "sowork" or channel != settings.channel_id or user != settings.allowed_user_id
                or (thread is not None and not public_name(thread, IDENTIFIER))):
            issues.append("preview_session_binding_unconfirmed")
            continue
        if thread and not settings.threads_enabled:
            continue
        result.append(route(model, kind="thread" if thread else "dm", thread_id=thread,
            override={"model": name, "provider": provider, "base_url": endpoint}))
    return result, sorted(set(issues))


def provider_directories(home):
    from hermes_yaml import safe_load
    result = []
    for parent in (home / "plugins", home / "plugins/model-providers"):
        for directory in sorted(parent.glob("*")):
            manifest = directory / "plugin.yaml"
            if manifest.is_file() and (directory / "__init__.py").is_file():
                try:
                    data = safe_load(manifest.read_text())
                    if isinstance(data, dict) and data.get("kind") == "model-provider":
                        result.append(directory.resolve())
                except Exception:
                    raise AdminError("preview_provider_manifest_invalid") from None
    return result


def probe(home, config, routes):
    import hermes_constants
    source = Path(hermes_constants.__file__).resolve().parent
    libraries = [str(Path(p).resolve()) for p in sys.path if p and Path(p).is_dir()
                 and ("site-packages" in Path(p).parts or "dist-packages" in Path(p).parts)]
    with tempfile.TemporaryDirectory(prefix="sowork-preview-check-") as directory:
        temporary = Path(directory)
        plugins = temporary / "plugins";plugins.mkdir()
        providers = provider_directories(home)
        # Use this profile's installed code, bound to a disposable home with no auth/config state.
        for index, path in enumerate(providers):
            (plugins / ("provider-" + str(index))).symlink_to(path, target_is_directory=True)
        (temporary / "config.yaml").write_text('{"plugins":{"enabled":[]}}\n')
        stream = config.get("streaming") or (config.get("gateway") or {}).get("streaming") or {}
        display = config.get("display", {}).get("platforms", {}).get("sowork", {})
        request = {"source": str(source), "libraries": libraries, "home": directory,
            "provider_directories": [str(p) for p in providers], "routes": routes,
            "streaming": {k: stream[k] for k in ("enabled", "transport", "mode") if k in stream},
            "platform_streaming": display.get("streaming")}
        environment = {"HOME": directory, "HERMES_HOME": directory, "PATH": "/usr/bin:/bin",
                       "LANG": "C.UTF-8", "TZ": "UTC", "HERMES_DISABLE_LAZY_INSTALLS": "1"}
        try:
            result = subprocess.run([sys.executable, "-I", "-S", str(Path(__file__).with_name("preview_probe.py"))],
                input=json.dumps(request), text=True, capture_output=True, env=environment,
                cwd=directory, timeout=PROBE_TIMEOUT, check=False)
            data = json.loads(result.stdout)
            if result.returncode or not isinstance(data, dict) or len(data.get("routes", [])) != len(routes):
                raise ValueError()
            return data
        except subprocess.TimeoutExpired:
            raise AdminError("preview_probe_timeout") from None
        except (ValueError, OSError):
            raise AdminError("preview_probe_unavailable") from None


def check_preview(home):
    config, sha = read_config(home)
    settings = binding(config)
    report = {"status": "unknown", "configured": settings.preview_enabled, "config_sha256": sha,
              "local_only": True, "credentials_resolved": False, "generation_tested": False,
              "live_agent_state_checked": False, "routes": [], "issues": []}
    if managed_config_present():
        report["issues"] = ["managed_config_requires_native_review"]
        return report
    if not settings.preview_enabled:
        report["status"] = "disabled"
        return report
    model = config.get("model") or {}
    routes = [route(model, kind="profile_default")]
    sessions, session_issues = session_routes(home, settings, model)
    issues = list(session_issues)
    routes.extend(sessions)
    try:
        result = probe(home, config, routes)
        report.update(result)
        issues += result.get("issues", [])
    except AdminError as error:
        issues.append(str(error))
    if hashlib.sha256((home / "config.yaml").read_bytes()).hexdigest() != sha:
        issues.append("config_changed_review_again")
    current_sessions, current_issues = session_routes(home, settings, model)
    if sessions != current_sessions or session_issues != current_issues:
        issues.append("preview_session_routes_changed_review_again")
    report["issues"] = sorted(set(issues))
    states = [r["status"] for r in report["routes"]]
    report["status"] = ("blocked" if "blocked" in states or report.get("configuration_ready") is False else "unknown" if report["issues"]
                        or "unknown" in states or not states else "compatible")
    return report
