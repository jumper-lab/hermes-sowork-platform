"""Hermes discovery entry: passive probes and no core schema additions."""
from dataclasses import fields
import importlib.util

from .config import Credentials, Settings


def requirements():
    return all(importlib.util.find_spec(name) is not None for name in ("httpx", "fcntl"))


def configured(config):
    try:
        settings = Settings.from_extra(config.extra)
        Credentials.from_scope(settings.credential_env)
        return True
    except (ValueError, RuntimeError, KeyError):
        return False


def build_adapter(config):
    from .runtime import require_supported_runtime
    require_supported_runtime()
    from .adapter import SoWorkAdapter
    return SoWorkAdapter(config)


def register(ctx):
    from gateway.platform_registry import PlatformEntry
    from .proactive import standalone_send, parse_target, validate_target
    from .cli import setup_parser, dispatch
    ctx.register_cli_command(name="sowork", help="SoWork configuration and diagnostics",
                             setup_fn=setup_parser, handler_fn=dispatch)
    optional = {}
    supported = {field.name for field in fields(PlatformEntry)}
    if "trusted_inbound" in supported:
        optional["trusted_inbound"] = False
    if "display_tier" in supported:
        optional["display_tier"] = "minimal"
    ctx.register_platform(
        name="sowork", label="SoWork", adapter_factory=build_adapter,
        check_fn=requirements, validate_config=configured, is_connected=configured,
        # The adapter persists and splits the complete payload itself. Native pre-splitting
        # would lose ordinals for identical adjacent chunks on delivery retry.
        max_message_length=0, standalone_sender_fn=standalone_send,
        parse_target_ref_fn=parse_target, validate_target_ref_fn=validate_target,
        allow_update_command=False, emoji="💬",
        platform_hint=(
            "This is a private SoWork direct message. Incoming attachments use normal Hermes "
            "media handling; when enabled, outgoing MEDIA files are uploaded natively. "
            "An optional private temporary preview shows generated text and activity. Send a final response; "
            "permanent message editing and interactive buttons are unavailable. "
            "Text slash commands and approvals use the normal Hermes gateway."
        ),
        **optional,
    )
