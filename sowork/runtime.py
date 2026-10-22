"""Fail before credentials or network on unsupported Hermes installations."""
import importlib.util


MINIMUM_HERMES = "0.21.5"


def missing_contracts():
    missing = []
    for module in ("hermes_yaml", "cron.execution_identity", "cron.scheduler_delivery", "gateway.run_turn_runner"):
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(module)
        except (ImportError, ValueError):
            missing.append(module)
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.platforms import base
    if not callable(getattr(base, "resolve_channel_prompt", None)):
        missing.append("gateway.platforms.base.resolve_channel_prompt")
    for method in ("_event_session_key", "supports_draft_streaming"):
        if not hasattr(BasePlatformAdapter, method):
            missing.append("BasePlatformAdapter." + method)
    if not missing:
        from .hermes_compat import missing_delivery_contracts
        missing.extend(missing_delivery_contracts())
    return missing


def require_supported_runtime():
    missing = missing_contracts()
    if missing:
        raise RuntimeError("SoWork requires Hermes >=" + MINIMUM_HERMES +
                           " with the gateway contracts: " + ", ".join(missing))
