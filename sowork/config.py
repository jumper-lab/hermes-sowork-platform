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
class Conversation:
    channel_id: str
    type: str
    allow_from: tuple[str, ...]
    require_mention: bool = False

    @property
    def chat_type(self):
        return "dm" if self.type == "dm" else "group"

    @property
    def wire_type(self):
        return "dm" if self.type in ("dm", "group_dm") else self.type


def identifiers(values):
    if (not isinstance(values, (list, tuple)) or not values
            or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", v) or v == "*" for v in values)
            or len(set(values)) != len(values)):
        raise ValueError("Expected distinct explicit SoWork IDs; wildcards are not supported")
    return tuple(values)


@dataclass(frozen=True)
class Settings:
    subscription_id: str
    workspace_id: str
    app_user_id: str
    allowed_user_id: str = ""
    channel_id: str = ""
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
    channels: tuple[Conversation, ...] = ()

    @property
    def conversations(self):
        return self.channels or (Conversation(self.channel_id, "dm", (self.allowed_user_id,)),)

    @property
    def allowed_users(self):
        return tuple(sorted({user for route in self.conversations for user in route.allow_from}))

    def conversation(self, channel_id):
        return next((route for route in self.conversations if route.channel_id == channel_id), None)

    def allowlist_matches(self, extra):
        values = extra.get("allow_from")
        return (isinstance(values, list) and len(values) == len(self.allowed_users)
                and all(isinstance(v, str) for v in values) and set(values) == set(self.allowed_users))

    @classmethod
    def from_extra(cls, extra):
        fields = ("subscription_id", "workspace_id", "app_user_id")
        values = {key: extra.get(key, "") for key in fields}
        if not all(isinstance(v, str) and v and v != "*" for v in values.values()):
            raise ValueError("SoWork requires an explicit subscription, workspace, app and confirmed human/DM")
        values.update({key: extra.get(key, "") for key in ("allowed_user_id", "channel_id")})
        channels = []
        if "channels" in extra:
            raw = extra["channels"]
            if not isinstance(raw, dict) or not raw or len(raw) > 100:
                raise ValueError("SoWork channels must be an explicit nonempty mapping (at most 100)")
            for channel, rule in raw.items():
                identifiers([channel])
                if (not isinstance(rule, dict) or not {"type", "allow_from"}.issubset(rule)
                        or set(rule) - {"type", "allow_from", "require_mention"}
                        or rule["type"] not in ("dm", "group_dm", "public", "private")):
                    raise ValueError("Each SoWork channel needs a type and an explicit allow_from list")
                users = identifiers(rule["allow_from"])
                if rule["type"] == "dm" and len(users) != 1:
                    raise ValueError("A one-to-one DM must bind exactly one human")
                require_mention = rule.get("require_mention", False)
                if type(require_mention) is not bool or (rule["type"] == "dm" and require_mention):
                    raise ValueError("require_mention must be a boolean for a channel or group DM")
                channels.append(Conversation(channel, rule["type"], users, require_mention))
        legacy = values["allowed_user_id"], values["channel_id"]
        if any(legacy) or not channels:
            identifiers(list(legacy))
            if channels and Conversation(legacy[1], "dm", (legacy[0],)) not in channels:
                raise ValueError("The existing primary DM must remain explicitly bound")
        all_users = {user for route in channels for user in route.allow_from} if channels else {legacy[0]}
        if values["app_user_id"] in all_users:
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
        result = cls(**values, credential_env=dict(names), max_attachment_bytes=limit, channels=tuple(channels), **flags)
        if channels and not result.allowlist_matches(extra):
            raise ValueError("Global allow_from must equal the union of configured channel users")
        group_users = {user for route in channels if route.chat_type == "group" for user in route.allow_from}
        if group_users and set(identifiers(extra.get("group_allow_from"))) != group_users:
            raise ValueError("group_allow_from must equal the union of configured group/channel users")
        prompts = extra.get("channel_prompts", {})
        if (not isinstance(prompts, dict) or any(not result.conversation(key) or not isinstance(value, str)
                for key, value in prompts.items())):
            raise ValueError("channel_prompts must map configured channel IDs to instruction strings")
        return result
