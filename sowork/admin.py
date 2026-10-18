"""Profile-local administration. Importing this module never resolves secrets."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import time
from types import SimpleNamespace

from .config import Settings

FEATURES = ("media", "reactions", "progress", "proactive", "threads", "preview", "reaction_probe")
EDITABLE = ("media", "reactions", "progress", "preview", "reaction_probe")


class AdminError(ValueError):
    """A fixed, safe code; never include config contents or upstream exceptions."""


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def managed_config_present():
    from hermes_cli.managed_scope import get_managed_dir
    directory = get_managed_dir()
    return directory is not None and (directory / "config.yaml").exists()


def read_config(home):
    from hermes_yaml import safe_load
    try:
        path = home / "config.yaml"
        if path.is_symlink():
            raise AdminError("config_symlink")
        raw = path.read_bytes()
        config = safe_load(raw.decode())
        if not isinstance(config, dict):
            raise AdminError("config_invalid")
        return config, hashlib.sha256(raw).hexdigest()
    except AdminError:
        raise
    except Exception:
        raise AdminError("config_unreadable_or_invalid") from None


def binding(config):
    try:
        platform = config["platforms"]["sowork"]
        settings = Settings.from_extra(platform["extra"])
        if (platform.get("enabled") is not True
                or platform["extra"].get("allow_from") != [settings.allowed_user_id]):
            raise AdminError("single_human_binding_required")
        home = platform.get("home_channel")
        if home and (home.get("chat_id") != settings.channel_id or home.get("thread_id")
                     or home.get("user_id") not in (None, settings.allowed_user_id)):
            raise AdminError("home_target_mismatch")
        return settings
    except AdminError:
        raise
    except Exception:
        raise AdminError("sowork_binding_invalid") from None


def config_summary(config, settings):
    display = config.get("display", {}).get("platforms", {}).get("sowork", {})
    return {
        "binding": {"workspace_id": settings.workspace_id, "app_user_id": settings.app_user_id,
                    "allowed_user_ids": [settings.allowed_user_id], "channel_id": settings.channel_id,
                    "subscription_id": settings.subscription_id},
        "features": {name: getattr(settings, name + "_enabled") for name in FEATURES},
        "preview_available_locally": bool(settings.preview_enabled and preview_compatible(config)
            and display.get("streaming") is True),
        "max_attachment_bytes": settings.max_attachment_bytes,
        "credential_source": "Hermes active profile",
        "credentials_resolved": False,
    }


def preview_compatible(config):
    # Conservative raw-file inspection. Importing gateway.config also discovers
    # provider plugins in some Hermes revisions, so it is not an offline read.
    stream = config.get("streaming") or (config.get("gateway") or {}).get("streaming") or {}
    transport = stream.get("transport", "auto")
    return stream.get("enabled") is True and (transport is True or transport in ("auto", "draft"))


def configure(home, feature, enabled, *, apply=False, expected_sha=None):
    """Review by default. Only resource flags and the SoWork preview override may change."""
    if feature not in EDITABLE or type(enabled) is not bool:
        raise AdminError("unsupported_setting")
    if managed_config_present():
        raise AdminError("managed_config_requires_native_review")
    original, sha = read_config(home)
    binding(original)
    candidate = copy.deepcopy(original)
    candidate["platforms"]["sowork"]["extra"][feature + "_enabled"] = enabled
    if feature == "preview":
        display = candidate.setdefault("display", {}).setdefault("platforms", {}).setdefault("sowork", {})
        if enabled and (not preview_compatible(original)
                        or display.get("interim_assistant_messages") or display.get("show_reasoning")):
            raise AdminError("preview_requires_existing_safe_global_streaming")
        display["streaming"] = enabled
    binding(candidate)
    changed = candidate != original
    result = {"config_sha256": sha, "setting": feature, "enabled": enabled,
              "changed": changed, "applied": False, "restart_required": changed,
              "allowlist_preserved": True, "other_channels_preserved": True}
    if not apply:
        return result
    if not expected_sha or expected_sha != sha or digest(home / "config.yaml") != sha:
        raise AdminError("config_changed_review_again")
    if not changed:
        return result
    from hermes_cli.config import validate_config_structure, atomic_config_write
    if validate_config_structure(candidate) != validate_config_structure(original):
        raise AdminError("candidate_introduces_native_config_issues")
    backup = home / ("config.yaml.bak-sowork-admin-" + str(time.time_ns()))
    # Exclusive creation; config backups may contain unrelated secrets, so always private.
    with backup.open("xb") as file:
        os.chmod(backup, 0o600)
        file.write((home / "config.yaml").read_bytes())
    if digest(home / "config.yaml") != sha:
        raise AdminError("config_changed_review_again")
    atomic_config_write(home / "config.yaml", candidate)
    after, new_sha = read_config(home)
    if after != candidate:
        raise AdminError("config_write_verification_failed")
    return {**result, "applied": True, "config_sha256": new_sha, "backup": str(backup)}


def fetch_credentials(home, config, settings):
    """Read only Hermes' public secret route for the already-active profile.

    Hermes owns 1Password resolution and bootstrap. An offline --home argument
    never authorizes loading credentials from that directory.
    """
    from .config import Credentials
    try:
        return Credentials.from_scope(settings.credential_env, home=home)
    except Exception:
        raise AdminError("active_profile_credentials_required") from None


async def check_connection(home):
    if managed_config_present():
        raise AdminError("managed_config_requires_native_review")
    config, sha = read_config(home)
    settings = binding(config)
    credentials = fetch_credentials(home, config, settings)
    if digest(home / "config.yaml") != sha:
        raise AdminError("config_changed_review_again")
    from .client import Client, SoWorkError, THREAD_EVENT
    from .threads import events, verify_dm
    client = Client(credentials)
    try:
        await client.verify_identity(workspace_id=settings.workspace_id, app_user_id=settings.app_user_id)
        if settings.reaction_probe_enabled and "chat.history" not in client.identity["installation"]["consentedScopes"]:
            raise AdminError("reaction_probe_scope_missing")
        if settings.threads_enabled and not {"chat.history", "events.subscribe:" + THREAD_EVENT}.issubset(
                set(client.identity["installation"]["consentedScopes"])):
            raise AdminError("thread_scopes_missing")
        subscriptions = await client.subscriptions()
        sub = next((s for s in subscriptions if s["id"] == settings.subscription_id), None)
        if (not sub or sub.get("url") or sub.get("disabledAt")
                or sub.get("officeId") != settings.workspace_id
                or sorted(sub.get("events", [])) != sorted(events(settings))
                or sub.get("creator", {}).get("id") != settings.app_user_id):
            raise AdminError("subscription_mismatch")
        await verify_dm(SimpleNamespace(client=client, settings=settings, _thread_dm_verified=False))
        return {"connection": "verified", "identity_verified": True, "subscription_verified": True,
                "dm_membership_verified": True, "allowed_user_ids": [settings.allowed_user_id],
                "installation_id": credentials.installation_id,
                "credential_source": "Hermes active profile", "config_sha256": sha,
                "messages_sent": 0, "receiver_started": False}
    except AdminError:
        raise
    except SoWorkError as error:
        # Upstream errors may contain arbitrary response text. Only expose HTTP status.
        raise AdminError("connection_failed_http_" + str(error.status)) from None
    except Exception:
        raise AdminError("connection_check_failed") from None
    finally:
        await client.close()
