"""Interactive profile-local onboarding; no model, receiver or service restart."""
from __future__ import annotations

import asyncio
import copy
import getpass
import json
import math
import os
import secrets
import shlex
import sys
import time

from .admin import AdminError, binding, managed_config_present, preview_compatible, read_config, verify_connection, write_config
from .client import Client, DM_EVENT, THREAD_EVENT, SoWorkError
from .config import Settings
from .lock import SubscriptionLock
from .setup_secrets import credentials, require_active_profile


class Console:
    def write(self, text):
        print(text, flush=True)

    def ask(self, text):
        return input(text)

    def reference(self, text):
        # Also avoid echoing an accidentally pasted credential value.
        return getpass.getpass(text)

    def confirm(self, text, *, default=False):
        answer = self.ask(text + (" [Y/n]: " if default else " [y/N]: ")).strip().lower()
        return answer in {"y", "yes"} or (not answer and default)


def label(value):
    return "".join(c for c in str(value) if c.isprintable())[:100]


class Journal:
    def __init__(self, home):
        directory = home / "sowork"
        if directory.is_symlink():
            raise AdminError("setup_state_symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        self.path = directory / "setup.json"
        if self.path.is_symlink():
            raise AdminError("setup_state_symlink")

    def read(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict) or data.get("schema") != 1:
                raise ValueError
            return data
        except (ValueError, OSError):
            raise AdminError("setup_state_requires_review") from None

    def save(self, data):
        temporary = self.path.with_name(".setup-" + secrets.token_hex(12))
        try:
            with temporary.open("x") as file:
                os.chmod(temporary, 0o600)
                json.dump({**data, "schema": 1}, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


def paired_candidate(event, identity, nonce, started, owner):
    """A private fresh nonce nominates a human; it never grants access by itself."""
    try:
        data, message = event["data"], event["data"]["message"]
        stamp = event["createdAt"]
        if (event["type"] != DM_EVENT or event["installationId"] != identity["installation"]["id"]
                or event["officeId"] != identity["group"]["id"]
                or data["conversationType"] != "direct" or data["memberCount"] != 2
                or message["channel"]["type"] != "dm" or data["channelId"] != message["channel"]["id"]
                or message.get("parentId") or message.get("attachments")
                or message.get("text", "").strip() != nonce
                or type(stamp) not in (int, float) or not math.isfinite(stamp)
                or not started - 5 <= stamp <= time.time() + 60
                or not isinstance(message["sender"]["id"], str) or not message["sender"]["id"]
                or message["sender"]["id"] == identity["appUser"]["id"]
                or (owner and message["sender"]["id"] != owner)):
            return None
        return {"allowed_user_id": message["sender"]["id"], "channel_id": message["channel"]["id"],
                "name": label(message["sender"].get("name", "User"))}
    except (KeyError, TypeError, AttributeError):
        return None


async def pair(client, identity, subscription, owner, ui, timeout):
    nonce, started, cursor = "HERMES-PAIR-" + secrets.token_hex(8), time.time(), None
    ui.write("Send this exact code in a private 1:1 DM to " + label(identity["appUser"].get("name", "the app")) + ":")
    ui.write(nonce)
    try:
        async with asyncio.timeout(timeout):
            while True:
                page = await client.poll(subscription, cursor)
                for item in page["items"]:
                    cursor = item["logId"]
                    candidate = paired_candidate(item["event"], identity, nonce, started, owner)
                    if candidate:
                        return candidate
                if page["hasMore"]:
                    if not page["items"]:
                        raise AdminError("setup_invalid_event_page")
                else:
                    await asyncio.sleep(2)
    except TimeoutError:
        raise AdminError("setup_pairing_timed_out_run_setup_again") from None


def subscription_matches(sub, identity, events):
    return (sub.get("officeId") == identity["group"]["id"] and not sub.get("url")
            and not sub.get("disabledAt") and sorted(sub.get("events", [])) == sorted(events)
            and sub.get("creator", {}).get("id") == identity["appUser"]["id"])


async def subscription(client, identity, flags, journal, ui, resume=None):
    events = [DM_EVENT, THREAD_EVENT] if flags["threads_enabled"] else [DM_EVENT]
    scope = {"installation_id": identity["installation"]["id"], "workspace_id": identity["group"]["id"],
             "app_user_id": identity["appUser"]["id"]}
    state = journal.read()
    if state and (state.get("identity") != scope or state.get("events") != events):
        raise AdminError("setup_pending_identity_or_events_changed")
    listed = await client.subscriptions()
    if resume:
        if state and state.get("subscription_id") not in (None, resume):
            raise AdminError("setup_pending_subscription_changed")
        sub = next((s for s in listed if s["id"] == resume), None)
        if not sub or not subscription_matches(sub, identity, events):
            raise AdminError("setup_subscription_mismatch")
        if not ui.confirm("Use subscription " + label(resume) + " exclusively for THIS Hermes profile? Other consumers must not use it."):
            raise AdminError("setup_cancelled")
        state = {"identity": scope, "events": events, "flags": flags, "subscription_id": resume}
    elif state:
        if not state.get("subscription_id"):
            raise AdminError("setup_creation_uncertain_review_subscriptions_then_resume_with_subscription_id")
        sub = next((s for s in listed if s["id"] == state["subscription_id"]), None)
        if not sub or not subscription_matches(sub, identity, events):
            raise AdminError("setup_pending_subscription_requires_review")
    else:
        if not ui.confirm("Create ONE exclusive inbox-only subscription for this profile (no public URL), leaving existing subscriptions unchanged?"):
            raise AdminError("setup_cancelled")
        state = {"identity": scope, "events": events, "flags": flags, "phase": "creation_started"}
        journal.save(state)  # A lost receipt must never cause a blind second POST.
        created = await client.request("POST", "/v1/subscriptions", body={"events": events}, retry=False)
        if not isinstance(created, dict) or not isinstance(created.get("id"), str) or not created["id"]:
            raise AdminError("setup_creation_uncertain_review_subscriptions_then_resume_with_subscription_id")
        state["subscription_id"] = created["id"]
    state["phase"] = "pairing"
    journal.save(state)
    return state


def feature_flags(config, identity, ui, pending):
    if pending:
        return pending["flags"]
    scopes = set(identity["installation"]["consentedScopes"])
    threads = {"chat.history", "events.subscribe:" + THREAD_EVENT}.issubset(scopes)
    threads = threads and ui.confirm("Enable isolated threads in your DM?", default=True)
    preview = preview_compatible(config)
    if preview:
        preview = ui.confirm("Enable private previews when the model/provider supports streaming?", default=True)
    else:
        ui.write("Private previews stay off: global streaming is disabled or incompatible. Other channels will not be changed.")
    return {"threads_enabled": bool(threads), "preview_enabled": bool(preview), "media_enabled": True,
            "reactions_enabled": True, "progress_enabled": True, "proactive_enabled": True,
            "reaction_probe_enabled": False}


def candidate_config(original, state, paired):
    candidate = copy.deepcopy(original)
    extra = {**state["identity"], **state["flags"], "subscription_id": state["subscription_id"],
             "allowed_user_id": paired["allowed_user_id"], "channel_id": paired["channel_id"],
             "allow_from": [paired["allowed_user_id"]]}
    extra.pop("installation_id")  # Credential identity is resolved by Hermes, never YAML.
    candidate.setdefault("platforms", {})["sowork"] = {"enabled": True, "extra": extra,
        "home_channel": {"platform": "sowork", "chat_id": paired["channel_id"], "name": "Private SoWork DM"}}
    enabled = candidate.setdefault("plugins", {}).setdefault("enabled", [])
    if "sowork-platform" not in enabled:
        enabled.append("sowork-platform")
    candidate.setdefault("display", {}).setdefault("platforms", {})["sowork"] = {
        "streaming": state["flags"]["preview_enabled"], "interim_assistant_messages": False,
        "show_reasoning": False, "tool_progress": "off"}
    binding(candidate)
    return candidate


def activation_command(home):
    """Pin the exact home even when the instruction is copied to another shell."""
    from hermes_cli.profiles import get_active_profile_name, profile_root_for_env_home
    name = get_active_profile_name()
    if name == "custom":
        # 'custom' describes an unnamed home; it is not a named profile.
        name = "default"
    root = profile_root_for_env_home(str(home), home)
    target = root if name == "default" else root / "profiles" / name
    if target.resolve() != home:
        raise AdminError("setup_restart_destination_mismatch")
    return ["env", "HERMES_HOME=" + str(home), "hermes", "--profile", name, "gateway", "restart"]


async def run_setup(home, *, ui=None, owner_id=None, resume_subscription=None, client_factory=None, pair_timeout=180):
    from .runtime import require_supported_runtime
    require_active_profile(home)
    require_supported_runtime()
    if owner_id is not None and (not owner_id or owner_id == "*" or not owner_id.isalnum()):
        raise AdminError("setup_requires_one_explicit_human")
    if managed_config_present():
        raise AdminError("managed_config_requires_native_review")
    ui = ui or Console()
    config, _ = read_config(home)
    if "sowork" in config.get("platforms", {}):
        binding(config)  # Never replace a partial or existing identity silently.
        from .admin import check_connection
        return {**await check_connection(home), "already_configured": True, "restart_required": False}
    command = activation_command(home)
    lease = SubscriptionLock("setup:" + str(home))
    if not lease.acquire():
        raise AdminError("setup_already_running_for_profile")
    client = None
    consumer_lease = None
    try:
        from hermes_constants import display_hermes_home
        ui.write("SoWork setup — active profile: " + label(display_hermes_home()))
        journal = Journal(home)
        pending = journal.read()
        credential = credentials(home, ui)
        client = (client_factory or Client)(credential)
        identity = await client.verify_identity()
        api_owner = identity["installation"].get("ownerUserId")
        if owner_id and api_owner and owner_id != api_owner:
            raise AdminError("setup_owner_mismatch")
        owner = api_owner or owner_id
        ui.write("App: " + label(identity["appUser"].get("name", "App")) + " (" + label(identity["appUser"]["id"]) + ")")
        ui.write("Workspace: " + label(identity["group"].get("name", "Workspace")) + " (" + label(identity["group"]["id"]) + ")")
        config, sha = read_config(home)
        if "sowork" in config.get("platforms", {}):
            raise AdminError("config_changed_review_again")
        flags = feature_flags(config, identity, ui, pending)
        names = {"media_enabled": "attachments", "reactions_enabled": "status reactions",
                 "progress_enabled": "private activity indicator", "proactive_enabled": "reminders to your DM",
                 "threads_enabled": "isolated threads", "preview_enabled": "private previews"}
        ui.write("Enabled features: " + ", ".join(name for key, name in names.items() if flags[key]))
        state = await subscription(client, identity, flags, journal, ui, resume_subscription)
        consumer_lease = SubscriptionLock(credential.installation_id + ":" + state["subscription_id"])
        if not consumer_lease.acquire():
            raise AdminError("setup_subscription_has_active_consumer")
        paired = await pair(client, identity, state["subscription_id"], owner, ui, pair_timeout)
        candidate = candidate_config(config, state, paired)
        settings = Settings.from_extra(candidate["platforms"]["sowork"]["extra"])
        await verify_connection(client, settings)
        ui.write("Verified human: " + paired["name"] + " (" + label(paired["allowed_user_id"]) + ")")
        ui.write("Private DM: " + label(paired["channel_id"]))
        ui.write("Only this human will be allowed. Existing channels, model, memory and other profiles stay unchanged.")
        if not ui.confirm("Save this SoWork connection in the active profile?"):
            raise AdminError("setup_cancelled_subscription_retained_run_setup_to_resume")
        written = write_config(home, config, candidate, sha)
        journal.save({**state, "phase": "configured"})
        ui.write("Configured. Activate using the native gateway lifecycle for this profile: " + shlex.join(command))
        return {**written, "configured": True, "restart_required": True, "restart_command": command,
                "allowed_user_ids": [paired["allowed_user_id"]], "channel_id": paired["channel_id"],
                "subscription_id": state["subscription_id"], "features": flags,
                "messages_sent": 0, "receiver_started": False, "other_channels_preserved": True}
    except SoWorkError as error:
        raise AdminError("setup_api_failed_http_" + str(error.status) + "_rerun_setup_to_resume") from None
    finally:
        if client:
            await client.close()
        if consumer_lease:
            consumer_lease.release()
        lease.release()


def interactive(home, args):
    if args.json or not sys.stdin.isatty():
        raise AdminError("setup_requires_interactive_terminal")
    try:
        return asyncio.run(run_setup(home, owner_id=args.owner_id, resume_subscription=args.subscription_id))
    except (EOFError, KeyboardInterrupt):
        raise AdminError("setup_cancelled_run_setup_to_resume") from None
