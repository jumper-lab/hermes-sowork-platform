"""Pinned-DM delivery from native cron executions and correlated model tool calls.

No receiver, gateway-state writer or extra model tool is started by the standalone lane.
The scheduler's execution identity survives worker restarts and delivery fallback.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
import time

from hermes_constants import get_hermes_home

from .client import SoWorkError


def enabled(config, settings):
    return (config.enabled is True and settings.proactive_enabled
            and config.extra.get("allow_from") == [settings.allowed_user_id])


def parse_target(reference):
    if not isinstance(reference, str) or not reference:
        return None
    parts = reference.split(":")
    if len(parts) == 1:
        return reference, None
    if len(parts) == 2 and parts[0]:
        from uuid import UUID
        try:
            if str(UUID(parts[1])) == parts[1]:
                return parts[0], parts[1]
        except ValueError:
            pass
    return None


def validate_target(chat_id):
    # Passive and credential-free; native target resolution invokes this before dispatch.
    import hermes_yaml as yaml
    from gateway.config import PlatformConfig
    from .config import Settings
    try:
        data = yaml.safe_load((get_hermes_home() / "config.yaml").read_text())["platforms"]["sowork"]
        config = PlatformConfig.from_dict(data)
        settings = Settings.from_extra(config.extra)
        return enabled(config, settings) and chat_id == settings.channel_id
    except (KeyError, TypeError, ValueError, OSError):
        return False


def _queued_execution(home, metadata):
    """Attest a restart-safe worker result claimed by THIS native gateway process.

    The native queue drain currently does not bind current_cron_execution(). Read its
    existing ownership fence instead; never accept a model-supplied job/execution ID.
    No queue state is changed here. Ambiguous/unowned/foreign-profile rows fail closed.
    """
    import json
    from cron import delivery_queue
    from cron.scheduler_delivery import _resolve_delivery_targets

    path = delivery_queue.queue_path()
    if path.resolve() != home / "cron" / "deliveries.db" or not path.is_file():
        return None
    with delivery_queue._lock:
        active = tuple(delivery_queue._ACTIVE_DELIVERIES)
    matches = []
    for execution_id in active:
        row = delivery_queue.get_status(execution_id)
        if (not row or row["status"] != "delivering" or row["owner_pid"] != os.getpid()
                or row["owner_process_id"] != delivery_queue._PROCESS_ID):
            continue
        job = json.loads(row["job_json"])
        if job.get("execution_id") != execution_id or ((metadata or {}).get("job_id") not in (None, job["id"])):
            continue
        targets = _resolve_delivery_targets(job, for_failure=bool(row["for_failure"]))
        matches.append((job["id"], execution_id, targets))
    return matches[0] if len(matches) == 1 else None


def operation(home, metadata=None, *, channel_id=None, thread_id=None):
    """Only executor-bound identity is accepted; payload/metadata cannot mint it."""
    from agent.secret_scope import current_secret_scope_home
    from cron.execution_identity import current_cron_execution
    from hermes_cli.profiles import current_profile_name
    from tools.approval_context import get_current_session_key
    from tools.browser_extension_router import current_tool_call_id

    home = Path(home).resolve()
    scoped_home = current_secret_scope_home()
    if get_hermes_home().resolve() != home or (scoped_home and Path(scoped_home).resolve() != home):
        raise SoWorkError(403, "proactive_profile_mismatch")
    execution = current_cron_execution()
    if execution and execution.profile != current_profile_name("default"):
        raise SoWorkError(403, "proactive_execution_profile_mismatch")
    call_id = current_tool_call_id()
    if call_id:
        session = get_current_session_key("")
        if not session:
            raise SoWorkError(403, "proactive_session_identity_required")
        identity = ["tool", session, call_id]
    elif execution and execution.execution_id and execution.job_id:
        identity = ["cron", execution.job_id, execution.execution_id]
    else:
        queued = _queued_execution(home, metadata)
        if not queued or not any(t.get("platform") == "sowork" and t.get("chat_id") == channel_id
                                 and (t.get("thread_id") or None) == thread_id for t in queued[2]):
            raise SoWorkError(403, "proactive_execution_identity_required")
        identity = ["cron", queued[0], queued[1]]
    import json
    if thread_id:
        identity += ["thread", thread_id]
    return "proactive_" + hashlib.sha256(json.dumps([str(home), *identity]).encode()).hexdigest()


def authorized(adapter, chat_id, metadata=None, reply_to=None):
    from .threads import destination
    root = destination(adapter, metadata)
    if (not adapter.client or not adapter.store or not enabled(adapter.config, adapter.settings) or chat_id != adapter.settings.channel_id
            or (reply_to and reply_to != root) or (metadata or {}).get("parentId")):
        raise SoWorkError(403, "proactive_destination_denied")
    if adapter._is_sender_authorized(adapter.settings.allowed_user_id, "dm", chat_id) is not True:
        raise SoWorkError(403, "gateway_acl_denied")
    return operation(adapter._home, metadata, channel_id=chat_id, thread_id=root)


@asynccontextmanager
async def lease(store, identity):
    """Serialize one operation across gateway and cron worker processes, including uploads."""
    import fcntl
    path = store.path.with_name(store.path.name + "." + hashlib.sha256(identity.encode()).hexdigest() + ".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SoWorkError(409, "proactive_delivery_busy")
                await asyncio.sleep(.05)
        yield
    finally:
        os.close(fd)  # Never unlink a lock inode while another process can be waiting.


async def standalone_send(config, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
    """Native PlatformEntry standalone_sender_fn; credentials come from the active profile."""
    from gateway.config import Platform, load_gateway_config
    from .adapter import SoWorkAdapter
    from .config import Settings
    from .state import Store

    adapter = None
    try:
        settings = Settings.from_extra(config.extra)
        # Re-read native profile configuration; stale configs and disabled destinations fail closed.
        current = load_gateway_config().platforms.get(Platform("sowork"))
        if (not current or not enabled(current, Settings.from_extra(current.extra))
                or not enabled(config, settings) or Settings.from_extra(current.extra) != settings
                or chat_id != settings.channel_id or (thread_id and not settings.threads_enabled)):
            raise SoWorkError(403, "proactive_destination_denied")
        operation(get_hermes_home(), channel_id=chat_id, thread_id=thread_id)  # No credential lookup/network before the identity gate.
        adapter = SoWorkAdapter(config)
        adapter.set_authorization_check(lambda user, kind, chat: (
            user == settings.allowed_user_id and kind == "dm" and chat == settings.channel_id
            and enabled(current, settings)))
        name = hashlib.sha256(adapter.scope.encode()).hexdigest()[:24] + ".sqlite3"
        adapter.store = Store(adapter._home / "sowork" / name, adapter.scope)
        metadata = {"thread_id": thread_id} if thread_id else None
        if thread_id:
            from .threads import destination, verify_dm
            destination(adapter, metadata)
        from .client import Client
        adapter.client = Client(adapter.credentials)
        await adapter.client.verify_identity(workspace_id=settings.workspace_id, app_user_id=settings.app_user_id)
        if thread_id:
            await verify_dm(adapter)
        # Match native live send_message caption placement; cron always sends text and files separately.
        from tools.browser_extension_router import current_tool_call_id
        from tools.send_message_tool import _media_caption_split, _DEFAULT_CAPTION_LIMIT
        caption, text = (_media_caption_split(message, media_files or [], max_caption_len=_DEFAULT_CAPTION_LIMIT)
                         if current_tool_call_id() else (None, message))
        result = None
        if text and text.strip():
            result = await adapter.send(chat_id, text, metadata=metadata)
            if not result.success:
                return {"error": result.error}
        for index, descriptor in enumerate(media_files or []):
            result = await adapter.send_document(chat_id, descriptor[0], caption=caption if index == 0 else None, metadata=metadata)
            if not result.success:
                return {"error": result.error}
        if result is None:
            return {"error": "empty_proactive_delivery"}
        return {"success": True, "message_id": result.message_id, "media_delivered": bool(media_files)}
    except (SoWorkError, ValueError, OSError) as error:
        return {"error": error.code if isinstance(error, SoWorkError) else "proactive_delivery_refused"}
    finally:
        if adapter:
            if adapter.client:
                await adapter.client.close()
            if adapter.store:
                adapter.store.close()
