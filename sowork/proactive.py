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
from . import routing


def enabled(config, settings):
    return settings.proactive_enabled and routing.active(config, settings)


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
        return enabled(config, settings) and settings.conversation(chat_id) is not None
    except (KeyError, TypeError, ValueError, OSError):
        return False


def operation(home, metadata=None, *, host, channel_id=None, thread_id=None, multi_channel=False):
    """Only executor-bound identity is accepted; payload/metadata cannot mint it."""
    from agent.secret_scope import current_secret_scope_home
    from hermes_cli.profiles import current_profile_name
    from tools.approval_context import get_current_session_key
    from .hermes_compat import tool_call_id, queued_execution

    home = Path(home).resolve()
    scoped_home = current_secret_scope_home()
    if get_hermes_home().resolve() != home or (scoped_home and Path(scoped_home).resolve() != home):
        raise SoWorkError(403, "proactive_profile_mismatch")
    execution = host.current_cron_execution()
    if execution and execution.profile != current_profile_name("default"):
        raise SoWorkError(403, "proactive_execution_profile_mismatch")
    call_id = tool_call_id()
    if call_id:
        session = get_current_session_key("")
        if not session:
            raise SoWorkError(403, "proactive_session_identity_required")
        identity = ["tool", session, call_id]
    elif execution and execution.execution_id and execution.job_id:
        identity = ["cron", execution.job_id, execution.execution_id]
    else:
        queued = queued_execution(home, metadata)
        if not queued or not any(t.get("platform") == "sowork" and t.get("chat_id") == channel_id
                                 and (t.get("thread_id") or None) == thread_id for t in queued[2]):
            raise SoWorkError(403, "proactive_execution_identity_required")
        identity = ["cron", queued[0], queued[1]]
    import json
    if multi_channel and channel_id:
        identity += ["channel", channel_id]
    if thread_id:
        identity += ["thread", thread_id]
    return "proactive_" + hashlib.sha256(json.dumps([str(home), *identity]).encode()).hexdigest()


def authorized(adapter, chat_id, metadata=None, reply_to=None):
    from .threads import destination
    root = destination(adapter, metadata, chat_id)
    if (not adapter.client or not adapter.store or not enabled(adapter.config, adapter.settings)
            or adapter.settings.conversation(chat_id) is None
            or (reply_to and reply_to != root) or (metadata or {}).get("parentId")):
        raise SoWorkError(403, "proactive_destination_denied")
    if not routing.target_authorized(adapter, chat_id):
        raise SoWorkError(403, "gateway_acl_denied")
    return operation(adapter._home, metadata, host=adapter.host, channel_id=chat_id, thread_id=root,
                     multi_channel=bool(adapter.settings.channels and chat_id != adapter.settings.channel_id))


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


async def standalone_send(config, chat_id, message, *, host, thread_id=None, media_files=None, force_document=False):
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
                or settings.conversation(chat_id) is None or (thread_id and not settings.threads_enabled)):
            raise SoWorkError(403, "proactive_destination_denied")
        operation(get_hermes_home(), host=host, channel_id=chat_id, thread_id=thread_id,
                  multi_channel=bool(settings.channels and chat_id != settings.channel_id))  # Preserve primary-DM delivery keys.
        adapter = SoWorkAdapter(config, host=host)
        adapter.set_authorization_check(lambda user, kind, chat: (
            settings.conversation(chat) is not None and user in settings.conversation(chat).allow_from
            and kind == settings.conversation(chat).chat_type
            and enabled(current, settings)))
        name = hashlib.sha256(adapter.scope.encode()).hexdigest()[:24] + ".sqlite3"
        adapter.store = Store(adapter._home / "sowork" / name, adapter.scope)
        metadata = {"thread_id": thread_id} if thread_id else None
        if thread_id:
            from .threads import destination, verify_dm
            destination(adapter, metadata, chat_id)
        from .client import Client
        adapter.client = Client(adapter.credentials)
        await adapter.client.verify_identity(workspace_id=settings.workspace_id, app_user_id=settings.app_user_id,
                                             required_scopes=routing.scopes(settings))
        if settings.channels:
            await routing.verify_conversations(adapter)
        elif thread_id:
            await verify_dm(adapter)
        # Match native live send_message caption placement; cron always sends text and files separately.
        from .hermes_compat import tool_call_id, queued_execution
        from tools.send_message_tool import _media_caption_split, _DEFAULT_CAPTION_LIMIT
        caption, text = (_media_caption_split(message, media_files or [], max_caption_len=_DEFAULT_CAPTION_LIMIT)
                         if tool_call_id() else (None, message))
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
