"""SoWork attachment transport through Hermes' existing media cache and event contract."""
from __future__ import annotations

import mimetypes
from pathlib import Path
import re

from .client import SoWorkError


def display_name(name):
    return re.sub(r"[^\w. -]", "_", str(name)).strip(" .")[:200] or "attachment.bin"


def valid_attachments(attachments):
    return (isinstance(attachments, list) and len(attachments) <= 10
            and all(isinstance(a, dict) and type(a.get("index")) is int and a["index"] >= 0
                    and isinstance(a.get("name"), str) and bool(a["name"])
                    and isinstance(a.get("mime"), str) and bool(a["mime"])
                    and type(a.get("size")) is int and a["size"] >= 0 for a in attachments)
            and len({a["index"] for a in attachments}) == len(attachments))


async def receive_media(adapter, event):
    """Called only after both DM identity admission and native authorization."""
    from gateway.platforms.base import cache_media_bytes_async
    from gateway.platforms.event import MessageType

    attachments = (event.raw_message or {}).get("attachments", [])
    if not attachments or event.is_command():
        return
    notices = []
    for attachment in attachments:
        name = display_name(attachment["name"])
        size = attachment["size"]
        if not 10 <= size <= adapter.settings.max_attachment_bytes:
            notices.append(f"O anexo '{name}' está fora do limite de tamanho permitido.")
            continue
        try:
            data = await adapter.client.download_attachment(event.message_id, attachment["index"], size,
                                                             adapter.settings.max_attachment_bytes)
            cached = await cache_media_bytes_async(data, filename=name, mime_type=attachment["mime"])
            if cached is None:
                raise SoWorkError(400, "invalid_image")
            event.media_urls.append(cached.path)
            event.media_types.append(cached.media_type)
            event.media_text_inlined.append(False)
        except (SoWorkError, OSError, ValueError) as error:
            # Never put transport errors, URLs, policy fields or credentials in the prompt.
            code = error.code if isinstance(error, SoWorkError) else "attachment_processing_failed"
            notices.append(f"Não foi possível abrir o anexo '{name}' ({code}).")
    if notices:
        event.text = "\n".join(notices) + ("\n\n" + event.text if event.text else "")
    if event.media_types:
        first = event.media_types[0]
        event.message_type = next((kind for prefix, kind in (
            ("image/", MessageType.PHOTO), ("audio/", MessageType.VOICE), ("video/", MessageType.VIDEO)
        ) if first.startswith(prefix)), MessageType.DOCUMENT)


def read_outbound(path, maximum, name=None):
    """Bounded local read after the adapter's native media-path authorization."""
    file = Path(path)
    if not file.is_file() or not 10 <= file.stat().st_size <= maximum:
        raise SoWorkError(400, "attachment_size_or_path_invalid")
    with file.open("rb") as stream:
        data = stream.read(maximum + 1)
    if not 10 <= len(data) <= maximum:
        raise SoWorkError(413, "attachment_too_large")
    filename = display_name(name or file.name)
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return filename, mime, data
