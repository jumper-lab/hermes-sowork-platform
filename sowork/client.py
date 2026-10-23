"""SoWork installation OAuth, REST and SSE. Never logs tokens or response bodies."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import json
import random
import re
import time
import ipaddress
from urllib.parse import quote, urlsplit

import httpx

from .config import Credentials

BASE_URL = "https://api.sowork.com/public"
DM_EVENT = "chat.direct_message_created"
THREAD_EVENT = "chat.thread_reply_created"
CHANNEL_EVENT = "chat.channel_message_created"
MENTION_EVENT = "me.chat_mentioned"
REQUIRED_SCOPES = {"chat.dm", "chat.write", "events.subscribe:" + DM_EVENT}


class SoWorkError(Exception):
    def __init__(self, status=0, code="transport_error", *, retry_after=None, operation=None):
        self.status = status
        self.code = code if re.fullmatch(r"[a-zA-Z0-9_]{1,80}", code) else "api_error"
        self.retry_after = retry_after
        self.retryable = status in {0, 408, 429} or status >= 500
        self.operation = operation if operation in {"poll", "stream"} else None
        context = f" during {self.operation}" if self.operation else ""
        super().__init__(f"SoWork {self.code} (HTTP {status}){context}")


def _error(response, *, operation=None):
    code = "api_error"
    try:
        code = response.json().get("error", {}).get("code", code)
    except (ValueError, AttributeError):
        pass
    after = response.headers.get("Retry-After")
    delay = None
    if after:
        try:
            delay = max(0, float(after))
        except ValueError:
            try:
                delay = max(0, parsedate_to_datetime(after).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    return SoWorkError(response.status_code, str(code), retry_after=delay, operation=operation)


@dataclass(frozen=True)
class Frame:
    log_id: str | None
    event: dict | None
    goodbye: bool = False


class Client:
    def __init__(self, credentials: Credentials, *, transport=None, storage_transport=None, sleep=asyncio.sleep):
        self.credentials = credentials
        self.http = httpx.AsyncClient(
            base_url=BASE_URL, transport=transport, trust_env=False,
            follow_redirects=False, timeout=httpx.Timeout(25, read=90),
        )
        self._token = ""
        self._expires = 0.0
        self._token_lock = asyncio.Lock()
        self.sleep = sleep
        self.identity = None
        self._storage = None
        self._storage_transport = storage_transport

    async def close(self):
        await self.http.aclose()
        if self._storage:
            await self._storage.aclose()
        self._token = ""

    async def token(self):
        async with self._token_lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            try:
                response = await self.http.post("/oauth/token", data={
                    "grant_type": "client_credentials",
                    "client_id": self.credentials.client_id,
                    "client_secret": self.credentials.client_secret,
                    "installation_id": self.credentials.installation_id,
                })
            except httpx.TransportError:
                raise SoWorkError() from None
            if not response.is_success:
                raise _error(response)
            try:
                data = response.json()
                value = data["access_token"]
                expires = float(data["expires_in"])
                if not isinstance(value, str) or not value or expires <= 0:
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                raise SoWorkError(502, "invalid_token_response") from None
            self._token = value
            self._expires = time.monotonic() + max(1, expires - 60)
            return value

    async def request(self, method, path, *, body=None, params=None, key=None, idempotent=False, retry=True):
        if not path.startswith("/v1/") or "?" in path or "#" in path:
            raise ValueError("Expected a documented relative API path")
        retry_safe = method in {"GET", "DELETE"} or bool(key) or idempotent
        attempts = 4 if retry else 1
        refreshed = False
        attempt = 0
        while True:
            token = await self.token()
            headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
            if key:
                if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
                    raise ValueError("Invalid idempotency key")
                headers["Idempotency-Key"] = key
            # No impersonation or caller-supplied headers are accepted.
            try:
                response = await self.http.request(method, path, json=body, params=params, headers=headers)
            except httpx.TransportError:
                error = SoWorkError()
            else:
                if response.is_success:
                    if response.status_code == 204:
                        return None
                    try:
                        return response.json()
                    except ValueError:
                        raise SoWorkError(502, "invalid_json_response") from None
                error = _error(response, operation="poll" if method == "GET" and path == "/v1/events" else None)
                if retry and response.status_code == 401 and not refreshed:
                    if self._token == token:
                        self._token = ""
                    refreshed = True
                    continue
                if retry and response.status_code == 403 and path != "/v1/app":
                    # Observe the current grant. Never enlarge it or create subscriptions.
                    self.identity = await self.request("GET", "/v1/app")
            attempt += 1
            if (not retry_safe and error.status != 429) or not error.retryable or attempt >= attempts:
                raise error
            delay = error.retry_after if error.retry_after is not None else min(2 ** (attempt - 1), 8)
            await self.sleep(delay + random.uniform(0, 0.25))

    async def verify_identity(self, *, workspace_id=None, app_user_id=None, required_scopes=None):
        data = await self.request("GET", "/v1/app")
        try:
            installation = data["installation"]
            valid = (
                installation["id"] == self.credentials.installation_id
                and installation["state"] == "active"
                and (REQUIRED_SCOPES if required_scopes is None else required_scopes).issubset(installation["consentedScopes"])
                and (workspace_id is None or data["group"]["id"] == workspace_id)
                and (app_user_id is None or data["appUser"]["id"] == app_user_id)
            )
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise SoWorkError(403, "identity_or_scope_mismatch")
        self.identity = data
        return data

    async def subscriptions(self):
        data = await self.request("GET", "/v1/subscriptions")
        return data["webhooks"]

    async def poll(self, subscription_id, cursor):
        params = {"subscriptionId": subscription_id, "limit": 100}
        if cursor is not None:
            params["after"] = cursor
        return await self.request("GET", "/v1/events", params=params)

    async def send(self, channel_id, body, key):
        return await self.request("POST", f"/v1/chat/channels/{quote(channel_id, safe='')}/messages", body=body, key=key)

    def _storage_client(self, url):
        # URLs are issued by the authenticated API, never taken from message text.
        # A separate client prevents forwarding OAuth or signed-policy fields to redirects.
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment
                    or parsed.port not in (None, 443) or not host or "." not in host
                    or host.endswith((".local", ".internal", ".localhost"))):
                raise ValueError
            try:
                if not ipaddress.ip_address(host).is_global:
                    raise ValueError
            except ValueError as error:
                if host.replace(".", "").isdigit() or ":" in host:
                    raise ValueError from error
        except (TypeError, ValueError):
            raise SoWorkError(502, "unsafe_storage_url") from None
        if self._storage is None:
            from tools.url_safety import create_ssrf_safe_async_client
            # Explicit transport injection is used only by offline contract tests.
            factory = create_ssrf_safe_async_client if self._storage_transport is None else httpx.AsyncClient
            self._storage = factory(
                transport=self._storage_transport, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(30, read=60),
            )
        return self._storage

    async def download_attachment(self, message_id, index, size, maximum):
        path = f"/v1/chat/messages/{quote(message_id, safe='')}/attachments/{index}"
        for attempt in range(2):
            slot = await self.request("GET", path)
            if not isinstance(slot, dict) or not isinstance(slot.get("expiresAt"), (int, float)):
                raise SoWorkError(502, "invalid_download_slot")
            url = slot.get("url")
            storage = self._storage_client(url)
            try:
                async with storage.stream("GET", url) as response:
                    if response.status_code == 403 and attempt == 0:
                        continue  # Refresh an expired signed URL, never the OAuth grant.
                    if not response.is_success:
                        raise SoWorkError(response.status_code, "attachment_download_failed")
                    if response.headers.get("Content-Length", "").isdigit() and int(response.headers["Content-Length"]) > min(size, maximum):
                        raise SoWorkError(413, "attachment_too_large")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > min(size, maximum):
                            raise SoWorkError(413, "attachment_too_large")
                    if len(data) != size:
                        raise SoWorkError(502, "attachment_size_mismatch")
                    return bytes(data)
            except httpx.TransportError:
                if attempt:
                    raise SoWorkError(0, "attachment_download_failed") from None
        raise SoWorkError(403, "attachment_download_failed")

    async def upload_attachment(self, channel_id, name, mime, data):
        slot = await self.request("POST", f"/v1/chat/channels/{quote(channel_id, safe='')}/attachments",
                                  body={"name": name, "mime": mime, "size": len(data)})
        try:
            attachment, upload = slot["attachment"], slot["upload"]
            if (slot["channelId"] != channel_id or attachment["name"] != name
                    or attachment["size"] != len(data) or not attachment["key"]
                    or not isinstance(attachment["mime"], str) or not attachment["mime"]
                    or upload["expiresAt"] <= time.time()
                    or not isinstance(upload["fields"], dict)
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in upload["fields"].items())):
                raise ValueError
            storage = self._storage_client(upload["url"])
        except (KeyError, TypeError, ValueError):
            raise SoWorkError(502, "invalid_upload_slot") from None
        # httpx writes data fields before the file, as required by the signed policy.
        try:
            response = await storage.post(upload["url"], data=upload["fields"],
                                          files={"file": (name, data, attachment["mime"])})
        except httpx.TransportError:
            raise SoWorkError(0, "attachment_upload_failed") from None
        if response.status_code != 204:
            raise SoWorkError(response.status_code, "attachment_upload_failed")
        return {key: attachment[key] for key in ("key", "name", "mime", "size")}

    async def reaction(self, message_id, emoji, *, remove=False):
        path = f"/v1/chat/messages/{quote(message_id, safe='')}/reactions"
        if remove:
            return await self.request("DELETE", path + "/" + quote(emoji, safe=""))
        return await self.request("POST", path, body={"type": emoji}, idempotent=True)

    async def create_progress(self, channel_id, user_id, key, *, parent_id=None, text="⏳ Working…"):
        result = await self.request("POST", f"/v1/chat/channels/{quote(channel_id, safe='')}/ephemeral-messages",
                                    body={"text": text, "visibleToUserId": user_id,
                                          "expiresInSeconds": 120, **({"parentId": parent_id} if parent_id else {})}, key=key)
        if (not result.get("id") or result.get("channelId") != channel_id
                or result.get("visibleToUserId") != user_id
                or result.get("senderId") != self.identity["appUser"]["id"]):
            raise SoWorkError(502, "progress_receipt_mismatch")
        if parent_id and result.get("parentId") != parent_id:
            raise SoWorkError(502, "progress_thread_mismatch")
        return result["id"]

    async def update_progress(self, message_id, text):
        return await self.request("PATCH", f"/v1/chat/ephemeral-messages/{quote(message_id, safe='')}",
                                  body={"text": text, "expiresInSeconds": 120}, idempotent=True)

    async def delete_progress(self, message_id):
        return await self.request("DELETE", f"/v1/chat/ephemeral-messages/{quote(message_id, safe='')}")

    async def stream_once(self, subscription_id, cursor):
        """Yield one connection's frames; caller persists cursor before reconnect.

        Matches official SDK 0.10.0: frame.id is logId, data is EventEnvelope,
        comments are heartbeats, `goodbye` asks for reconnect. IDs stay opaque.
        """
        token = await self.token()
        headers = {"Authorization": "Bearer " + token, "Accept": "text/event-stream"}
        if cursor is not None:
            headers["Last-Event-ID"] = cursor
        try:
            async with self.http.stream("GET", "/v1/events/stream", params={"subscriptionId": subscription_id}, headers=headers) as response:
                if not response.is_success:
                    await response.aread()
                    if response.status_code == 401 and self._token == token:
                        self._token = ""
                    raise _error(response, operation="stream")
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raise SoWorkError(502, "invalid_stream_content_type")
                yield Frame(None, None)
                log_id, kind, data, size = None, None, [], 0
                async for line in response.aiter_lines():
                    if line.startswith(":"):
                        yield Frame(None, None)
                        continue
                    if line == "":
                        if kind == "goodbye":
                            yield Frame(None, None, goodbye=True)
                            return
                        if data:
                            if not log_id:
                                raise SoWorkError(502, "missing_event_cursor")
                            try:
                                event = json.loads("\n".join(data))
                            except ValueError:
                                raise SoWorkError(502, "invalid_event_json") from None
                            if not isinstance(event, dict):
                                raise SoWorkError(502, "invalid_event_envelope")
                            yield Frame(log_id, event)
                        log_id, kind, data, size = None, None, [], 0
                        continue
                    size += len(line)
                    if size > 1_000_000:
                        raise SoWorkError(502, "event_too_large")
                    key, sep, value = line.partition(":")
                    if not sep:
                        continue
                    value = value[1:] if value.startswith(" ") else value
                    if key == "id" and "\x00" not in value:
                        log_id = value
                    elif key == "event":
                        kind = value
                    elif key == "data":
                        data.append(value)
        except httpx.TransportError:
            raise SoWorkError() from None
