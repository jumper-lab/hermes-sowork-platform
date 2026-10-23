"""Explicit conversation routes. No channel discovery changes membership or access."""
import re

from markdown_it import MarkdownIt

from .client import DM_EVENT, THREAD_EVENT, CHANNEL_EVENT, MENTION_EVENT, SoWorkError
from .config import Settings

_MARKDOWN = MarkdownIt("commonmark", {"html": False})


def mentioned(text, app_user_id):
    """A real app-targeted Markdown link outside code, images and quotations."""
    quoted = 0
    for token in _MARKDOWN.parse(text):
        if token.type == "blockquote_open":
            quoted += 1
        elif token.type == "blockquote_close":
            quoted -= 1
        elif token.type == "inline" and not quoted:
            if any(child.type == "link_open" and child.attrGet("href") == "user:" + app_user_id
                   for child in token.children or ()):
                return True
    return False


def command_text(text, app_user_id):
    """Let the normal native command parser see `@Bot /stop` as `/stop`."""
    pattern = r"^\s*\[(?:\\.|[^\]\\\r\n])*\]\(user:" + re.escape(app_user_id) + r"\)\s*"
    stripped = re.sub(pattern, "", text, count=1)
    return stripped if stripped.startswith("/") else text


def active(config, settings):
    try:
        current = Settings.from_extra(config.extra)
        return (config.enabled is True and settings.allowlist_matches(config.extra)
                and [(c.channel_id, c.type, c.allow_from, c.require_mention) for c in current.conversations]
                == [(c.channel_id, c.type, c.allow_from, c.require_mention) for c in settings.conversations]
                and (current.workspace_id, current.app_user_id, current.subscription_id)
                == (settings.workspace_id, settings.app_user_id, settings.subscription_id))
    except (TypeError, ValueError, KeyError):
        return False


def event_types(settings):
    result = []
    if any(c.type in ("dm", "group_dm") for c in settings.conversations):
        result.append(DM_EVENT)
    if any(c.type in ("public", "private") and not c.require_mention for c in settings.conversations):
        result.append(CHANNEL_EVENT)
    if any(c.type in ("public", "private") and c.require_mention for c in settings.conversations):
        result.append(MENTION_EVENT)
    if settings.threads_enabled:
        result.append(THREAD_EVENT)
    return result


def scopes(settings):
    required = {"chat.write", *("events.subscribe:" + event for event in event_types(settings))}
    if any(c.wire_type == "dm" for c in settings.conversations):
        required.add("chat.dm")
    if settings.threads_enabled or settings.reaction_probe_enabled or any(c.wire_type != "dm" for c in settings.conversations):
        required.add("chat.history")
    return required


async def listing(client, path, key, wanted):
    """Bounded, GET-only membership verification; fail closed on an incomplete listing."""
    found, cursors = {}, set()
    query = {"limit": 100}
    for _ in range(10):
        page = await client.request("GET", path, params=query)
        for row in page.get(key, []):
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise SoWorkError(502, "invalid_conversation_listing")
            if row["id"] in wanted:
                if row["id"] in found:
                    raise SoWorkError(502, "ambiguous_conversation_listing")
                found[row["id"]] = row
        if set(found) == set(wanted):
            return found
        cursor = page.get("nextCursor")
        if not cursor:
            break
        if not isinstance(cursor, str) or cursor in cursors:
            break
        cursors.add(cursor)
        query["before"] = cursor
    raise SoWorkError(403, "configured_conversation_unavailable")


async def verify_conversations(adapter):
    settings, client = adapter.settings, adapter.client
    if not settings.channels:
        from .threads import verify_dm
        await verify_dm(adapter)
        return
    dms = {c.channel_id for c in settings.conversations if c.wire_type == "dm"}
    groups = {c.channel_id for c in settings.conversations if c.wire_type != "dm"}
    found = {}
    if dms:
        found.update(await listing(client, "/v1/chat/dms", "conversations", dms))
    if groups:
        found.update(await listing(client, "/v1/chat/channels", "channels", groups))
    for route in settings.conversations:
        row = found[route.channel_id]
        if route.wire_type != "dm":
            if row.get("type") != route.type or row.get("archivedAt"):
                raise SoWorkError(403, "channel_membership_mismatch")
            continue
        people = row.get("participants", [])
        if not isinstance(people, list) or any(not isinstance(p, dict) or not isinstance(p.get("id"), str) for p in people):
            raise SoWorkError(403, "dm_membership_mismatch")
        ids = {p["id"] for p in people}
        peers = ids - {settings.app_user_id}
        if len(ids) != len(people) or not set(route.allow_from).issubset(peers):
            raise SoWorkError(403, "dm_membership_mismatch")
        if route.type == "dm" and (row.get("type") != "direct" or peers != set(route.allow_from)):
            raise SoWorkError(403, "dm_membership_mismatch")
        if route.type == "group_dm" and (row.get("type") != "group" or len(peers) < 2):
            raise SoWorkError(403, "dm_membership_mismatch")


def target_authorized(adapter, chat_id, user_id=None):
    route = adapter.settings.conversation(chat_id)
    if not route or not active(adapter.config, adapter.settings):
        return False
    users = (user_id,) if user_id is not None else route.allow_from
    return any(user in route.allow_from and adapter._is_sender_authorized(user, route.chat_type, chat_id) is True for user in users)
