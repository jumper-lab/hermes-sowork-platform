"""Non-secret YAML settings and scoped credential lookup."""
from dataclasses import dataclass, field
import re


DEFAULT_CREDENTIAL_ENV = {
    "client_id": "SOWORK_USERNAME",
    "client_secret": "SOWORK_CREDENTIAL",
    "installation_id": "SOWORK_INSTALLATION_ID",
}


@dataclass(frozen=True)
class Credentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    installation_id: str

    @classmethod
    def from_scope(cls, names=None, *, home=None):
        # A scoped miss NEVER falls back to another profile's environment.
        from pathlib import Path
        from agent.secret_scope import current_secret_scope, current_secret_scope_home, get_secret
        from hermes_constants import get_hermes_home

        names = names or DEFAULT_CREDENTIAL_ENV
        active_home = get_hermes_home().resolve()
        scope_home = current_secret_scope_home()
        if ((home is not None and Path(home).resolve() != active_home)
                or (scope_home is not None and Path(scope_home).resolve() != active_home)):
            raise ValueError("SoWork credentials require the active Hermes profile")
        scope = current_secret_scope()
        # A bound miss is authoritative even on Hermes versions that allow a
        # single-profile environment fallback. All values use the public route.
        values = {key: get_secret(names[key], "") if scope is None or names[key] in scope else ""
                  for key in DEFAULT_CREDENTIAL_ENV}
        if not all(values.values()):
            raise ValueError("Missing scoped SoWork credentials")
        return cls(**values)


@dataclass(frozen=True)
class Settings:
    subscription_id: str
    workspace_id: str
    app_user_id: str
    allowed_user_id: str
    channel_id: str
    credential_env: dict = field(default_factory=lambda: DEFAULT_CREDENTIAL_ENV.copy())
    reconnect_seconds: float = 2
    max_event_age_seconds: int = 3600
    media_enabled: bool = False
    reactions_enabled: bool = False
    progress_enabled: bool = False
    proactive_enabled: bool = False
    threads_enabled: bool = False
    preview_enabled: bool = False
    reaction_probe_enabled: bool = False
    max_attachment_bytes: int = 25 * 1024 * 1024

    @classmethod
    def from_extra(cls, extra):
        fields = ("subscription_id", "workspace_id", "app_user_id", "allowed_user_id", "channel_id")
        values = {key: extra.get(key, "") for key in fields}
        if not all(isinstance(v, str) and v and v != "*" for v in values.values()):
            raise ValueError("SoWork requires an explicit subscription, workspace, app and confirmed human/DM")
        if values["app_user_id"] == values["allowed_user_id"]:
            raise ValueError("App and human identities must differ")
        names = extra.get("credential_env", DEFAULT_CREDENTIAL_ENV)
        if set(names) != set(DEFAULT_CREDENTIAL_ENV) or not all(
            isinstance(v, str) and re.fullmatch(r"[A-Z][A-Z0-9_]+", v)
            for v in names.values()
        ):
            raise ValueError("Invalid credential environment mapping")
        flags = {}
        for key in ("media_enabled", "reactions_enabled", "progress_enabled", "proactive_enabled", "threads_enabled", "preview_enabled", "reaction_probe_enabled"):
            value = extra.get(key, False)
            if not isinstance(value, bool):
                raise ValueError(key + " must be a YAML boolean")
            flags[key] = value
        limit = extra.get("max_attachment_bytes", 25 * 1024 * 1024)
        if type(limit) is not int or not 10 <= limit <= 100 * 1024 * 1024:
            raise ValueError("max_attachment_bytes must be between 10 and 104857600")
        return cls(**values, credential_env=dict(names), max_attachment_bytes=limit, **flags)
