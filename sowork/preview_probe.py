"""Disposable selector probe. Run with -I -S; no client, credential resolution or generation."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace


def run(request):
    home = Path(request["home"]).resolve()
    source = Path(request["source"]).resolve()
    libraries = [Path(p).resolve() for p in request["libraries"]]
    provider_paths = [Path(p).resolve() for p in request["provider_directories"]]
    roots = [home, source, *libraries, *provider_paths,
             Path(sys.base_prefix).resolve(), Path(__file__).resolve().parent]
    blocked = set()

    def inside(path, directories):
        return any(path == root or path.is_relative_to(root) for root in directories)

    def guard(event, args):
        denied = False
        if event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn", "os.posix_spawn")) or event in ("os.system", "os.fork"):
            denied = True
        elif event == "open" and not isinstance(args[0], int):
            path = Path(os.fsdecode(args[0])).resolve()
            mode, flags = args[1:3]
            write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int)
                and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            denied = (not inside(path, [home] if write else roots)
                or path.name in (".env", ".op.env", "auth.json", ".credentials.json"))
        elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.utime"):
            denied = not inside(Path(os.fsdecode(args[0])).resolve(), [home])
        elif event in ("os.rename", "os.link", "os.symlink"):
            denied = any(not inside(Path(os.fsdecode(p)).resolve(), [home]) for p in args[:2])
        if denied:
            blocked.add(event.split(".")[0])
            raise PermissionError("preview_probe_side_effect_blocked")

    sys.dont_write_bytecode = True
    sys.path[:0] = [str(source), *map(str, libraries)]
    sys.addaudithook(guard)
    # Provider import diagnostics and arbitrary exception strings never enter CLI output.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from gateway.config import StreamingConfig
        from providers import get_provider_profile
        from hermes_cli.auth import PROVIDER_REGISTRY
        from agent.turn_api_call import _should_stream
        stream = StreamingConfig.from_dict(request["streaming"])
        configuration_ready = stream.enabled_for(request["platform_streaming"]) and stream.transport in ("auto", "draft")
        results = []
        issues = [] if configuration_ready else ["preview_streaming_configuration_blocked"]
        for route in request["routes"]:
            row = {k: route[k] for k in ("kind", "thread_id", "model", "provider")}
            if "channel_id" in route:
                row["channel_id"] = route["channel_id"]
            row.update(status="unknown", reason="provider_or_model_unresolved", streaming_allowed_by_core=None)
            if route["provider"] and route["model"]:
                try:
                    profile = get_provider_profile(route["provider"]) or PROVIDER_REGISTRY.get(route["provider"])
                    if profile is None:
                        raise LookupError()
                    base = (route["acp_scheme"] + "://preview-check" if route["acp_scheme"]
                            else str(getattr(profile, "base_url", "") or ""))
                    agent = SimpleNamespace(provider=route["provider"], model=route["model"], base_url=base,
                        api_mode=getattr(profile, "api_mode", "chat_completions"), _disable_streaming=route["model_streaming_disabled"],
                        _has_stream_consumers=lambda: True, client=None)
                    allowed = _should_stream(agent)
                    row["streaming_allowed_by_core"] = bool(allowed)
                    declaration = getattr(profile, "supports_streaming", None)
                    row["provider_declares_streaming"] = declaration if type(declaration) is bool else None
                    if not configuration_ready:
                        reason = "preview_streaming_configuration_blocked"
                    elif route["model_streaming_disabled"]:
                        reason = "model_streaming_disabled"
                    elif base.lower().startswith(("acp://", "acp+tcp://")):
                        reason = "acp_streaming_blocked"
                    elif not allowed and profile.auth_type == "external_process" and declaration is not True:
                        reason = "provider_streaming_not_declared"
                    elif not allowed:
                        reason = "core_rejects_provider_streaming"
                    else:
                        reason = "core_allows_streaming"
                    row.update(status="compatible" if configuration_ready and allowed else "blocked", reason=reason)
                except Exception:
                    row["reason"] = "preview_provider_check_unavailable"
            if row["status"] != "compatible":
                issues.append(row["reason"])
            results.append(row)
        selector = Path(sys.modules[_should_stream.__module__].__file__)
        return {"routes": results, "configuration_ready": bool(configuration_ready),
                "core_selector_sha256": hashlib.sha256(selector.read_bytes()).hexdigest(),
                "import_side_effects_blocked": sorted(blocked), "issues": sorted(set(issues))}


if __name__ == "__main__":
    try:
        result = run(json.load(sys.stdin))
    except Exception:
        result = {"error": "preview_probe_unavailable"}
    print(json.dumps(result))
