"""Exercise native authorization, media processing and status lifecycle, without credentials."""
import asyncio
from dataclasses import replace
import importlib
import json
from pathlib import Path
import time

import httpx
import pytest

from sowork.state import Store
from test_contracts import API, adapter, credentials, event, identity, no_sleep


class MediaAPI(API):
    def __init__(self):
        super().__init__()
        self.visuals = {}
        self.reactions = set()
        self.slots = []
        self.storage_requests = []
        self.file_bytes = b"neutral attachment content\n"
        self.download_url = "https://storage.example.org/input?signature=private"
        self.visual_failure = None

    def __call__(self, request):
        path = request.url.path.removeprefix("/public")
        if path.endswith("/attachments") and request.method == "POST":
            self.requests.append(request)
            body = json.loads(request.content)
            slot = {"channelId": "dm", "attachment": {"key": "file-" + str(len(self.slots)), **body},
                    "upload": {"url": "https://storage.example.org/upload", "fields": {"policy": "private", "Content-Type": body["mime"]},
                               "expiresAt": time.time() + 3600}}
            self.slots.append(slot)
            return httpx.Response(200, json=slot)
        if "/attachments/" in path:
            self.requests.append(request)
            return httpx.Response(200, json={"url": self.download_url, "expiresAt": int(time.time()) + 300})
        if "/reactions" in path:
            self.requests.append(request)
            item = (path.split("/")[4], json.loads(request.content)["type"] if request.method == "POST" else path.rsplit("/", 1)[1])
            if request.method == "POST":
                self.reactions.add(item)
            else:
                self.reactions.discard(item)
            return httpx.Response(200, json={"count": 1})
        if "ephemeral-messages" in path:
            self.requests.append(request)
            if self.visual_failure:
                return httpx.Response(self.visual_failure, json={"error": {"code": "simulated"}})
            if request.method == "POST":
                key = request.headers["Idempotency-Key"]
                self.visuals.setdefault(key, {"id": "progress-" + str(len(self.visuals)), "senderId": "app", "channelId": "dm", **json.loads(request.content)})
                return httpx.Response(200, json=self.visuals[key])
            mid = path.rsplit("/", 1)[1]
            if request.method == "DELETE":
                self.visuals = {k: v for k, v in self.visuals.items() if v["id"] != mid}
                return httpx.Response(204)
            value = next((v for v in self.visuals.values() if v["id"] == mid), None)
            if value is None:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            value.update(json.loads(request.content))
            return httpx.Response(200, json=value)
        return super().__call__(request)

    def storage(self, request):
        self.storage_requests.append(request)
        assert "authorization" not in request.headers
        assert "idempotency-key" not in request.headers
        if request.method == "POST":
            assert request.content.index(b'name="policy"') < request.content.index(b'name="file"')
            return httpx.Response(204)
        return httpx.Response(200, content=self.file_bytes)


@pytest.fixture
def rich(adapter):
    adp, _, runner = adapter
    api = MediaAPI()
    adp.client = type(adp.client)(credentials(), transport=httpx.MockTransport(api), storage_transport=httpx.MockTransport(api.storage), sleep=no_sleep)
    adp.client.identity = identity()
    adp.settings = replace(adp.settings, media_enabled=True, reactions_enabled=True, progress_enabled=True)
    return adp, api, runner


def attachment(api):
    return {"index": 0, "name": "neutral.txt", "mime": "text/plain", "size": len(api.file_bytes)}


def api_error(adp):
    return importlib.import_module(type(adp).__module__).SoWorkError


async def wait_complete(adp):
    for _ in range(200):
        if adp.store.summary()["inbox"].get("complete"):
            return
        await asyncio.sleep(.01)
    pytest.fail("Native background processing did not finish")


async def test_recover_legacy_null_visual_preserves_delivery_history(rich):
    adp, api, _ = rich
    adp.store.ingest("keep-cursor", event(), "unbound_sender")
    before = adp.store.summary()
    cursor = adp.store.cursor
    with adp.store.db:
        adp.store.db.execute("INSERT INTO visuals VALUES (NULL,NULL,?)", (time.time(),))
    await adp._recover_visuals()
    assert not adp.store.unfinished_visuals()
    assert adp.store.cursor == cursor and adp.store.summary() == before
    assert not api.requests


async def test_internal_turn_without_message_id_creates_no_visual(rich):
    adp, api, _ = rich
    inbound = adp.normalize(event())
    inbound.message_id = None
    await adp.on_processing_start(inbound)
    assert not adp.store.unfinished_visuals() and not adp._feedback
    assert not api.requests


@pytest.mark.parametrize("turn", [None, "", 42])
def test_visual_journal_requires_a_message_id(rich, turn):
    adp, _, _ = rich
    with pytest.raises(ValueError):adp.store.begin_visual(turn)


@pytest.mark.parametrize("text", ["", "Read this attachment"])
async def test_real_native_media_event_and_same_session(rich, text):
    adp, api, runner = rich
    raw = event()
    raw["data"]["message"].update(text=text, attachments=[attachment(api)])
    seen = []
    async def handler(inbound):
        assert runner._is_user_authorized(inbound.source)
        assert inbound.media_types == ["text/plain"], inbound.text
        assert inbound.media_text_inlined == [False]
        assert Path(inbound.media_urls[0]).read_bytes() == api.file_bytes
        seen.append(inbound.source.chat_id)
        return "attachment received"
    adp.set_message_handler(handler)
    await adp.ingest("cursor", raw)
    await wait_complete(adp)
    await asyncio.sleep(.05)
    assert seen == ["dm"]
    assert len(api.deliveries) == 1
    await adp.ingest("duplicate", raw)
    assert seen == ["dm"]
    assert len(api.storage_requests) == 1
    await adp.disconnect()


async def test_current_dm_capabilities_reach_native_turn_and_txt_delivery(rich, tmp_path):
    adp, api, runner = rich
    file = tmp_path / "LAGOA-742.txt"
    file.write_bytes(b"LAGOA-742\n")
    raw = event()
    raw["data"]["message"]["text"] = "Crie um TXT com LAGOA-742 e envie como anexo."

    async def handler(inbound):
        # Current transport facts must reach existing conversations without rewriting
        # a cached system prompt, the human's text, or previous transcript messages.
        assert inbound.text == raw["data"]["message"]["text"]
        prepared = runner._prefix_inbound_sender_context(inbound, inbound.source, inbound.text)
        assert "MEDIA:/absolute/path/to/file" in prepared
        assert prepared.endswith("[New message]\n" + inbound.text)
        return "Arquivo de teste.\nMEDIA:" + str(file)

    adp.set_message_handler(handler)
    await adp.ingest("txt-cursor", raw)
    await wait_complete(adp)
    await asyncio.sleep(.05)
    assert len(api.slots) == len(api.storage_requests) == 1
    assert api.slots[0]["attachment"]["name"] == file.name
    assert api.slots[0]["attachment"]["size"] == len(file.read_bytes())
    assert sum(bool(body.get("attachments")) for body, _ in api.deliveries.values()) == 1
    assert all("MEDIA:" not in body.get("text", "") for body, _ in api.deliveries.values())
    await adp.ingest("txt-duplicate", raw)
    assert len(api.slots) == 1
    await adp.disconnect()


@pytest.mark.parametrize("text", ["/stop", "/reset", "/approve", "/voice on"])
def test_transport_context_keeps_native_controls_unchanged(rich, text):
    adp, _, _ = rich
    raw = event(); raw["data"]["message"]["text"] = text
    inbound = adp.normalize(raw)
    assert inbound.text == text and inbound.is_command()
    assert not inbound.channel_context


def test_disabled_media_is_not_advertised(adapter):
    adp, _, _ = adapter
    assert not adp.normalize(event()).channel_context


async def test_media_rejected_before_network_for_other_human_or_channel(rich):
    adp, api, _ = rich
    for field, value in [("sender", {"id": "intruder"}), ("channel", {"id": "another", "type": "dm"})]:
        raw = event(eid=field)
        raw["data"]["message"].update(attachments=[attachment(api)], **{field: value})
        await adp.ingest("cursor-" + field, raw)
    assert not api.requests
    assert not api.storage_requests
    await adp.disconnect()


async def test_attachment_size_failure_is_visible_and_retains_text(rich):
    adp, api, _ = rich
    raw = event()
    raw["data"]["message"]["attachments"] = [{**attachment(api), "size": adp.settings.max_attachment_bytes + 1}]
    inbound = adp.normalize(raw)
    await adp.on_processing_start(inbound)
    assert "limite" in inbound.text and "neutral test" in inbound.text
    assert not inbound.media_urls
    assert not api.storage_requests
    await adp.disconnect()


@pytest.mark.parametrize("url", ["http://storage.example.org/file", "https://127.0.0.1/a", "https://169.254.169.254/a", "https://metadata.google.internal/a", "https://user:pass@storage.example.org/a"])
async def test_signed_download_refuses_unsafe_destinations(rich, url):
    adp, api, _ = rich
    api.download_url = url
    with pytest.raises(api_error(adp), match="unsafe_storage_url"):
        await adp.client.download_attachment("message", 0, len(api.file_bytes), 1000)
    assert not api.storage_requests
    await adp.disconnect()


async def test_signed_download_does_not_follow_redirect_or_exceed_bound(rich):
    adp, api, _ = rich
    adp.client._storage_transport = httpx.MockTransport(lambda _: httpx.Response(302, headers={"Location": "https://127.0.0.1/private"}))
    with pytest.raises(api_error(adp), match="attachment_download_failed"):
        await adp.client.download_attachment("message", 0, 20, 100)
    await adp.client._storage.aclose()
    adp.client._storage = None
    adp.client._storage_transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"a" * 21))
    with pytest.raises(api_error(adp), match="attachment_too_large"):
        await adp.client.download_attachment("message", 0, 20, 100)
    await adp.disconnect()


@pytest.mark.parametrize("kind,extension", [("document", ".txt"), ("voice", ".wav"), ("image_file", ".png"), ("video", ".mp4")])
async def test_native_outbound_upload_caption_and_receipt(rich, tmp_path, kind, extension):
    adp, api, _ = rich
    file = tmp_path / ("neutral" + extension)
    file.write_bytes(b"neutral test file bytes")
    await adp.on_processing_start(adp.normalize(event()))
    sender = getattr(adp, "send_" + kind)
    args = {"document": "file_path", "voice": "audio_path", "image_file": "image_path", "video": "video_path"}
    result = await sender("dm", **{args[kind]: str(file)}, caption="neutral caption")
    assert result.success
    assert len(api.slots) == 1
    body, receipt = next(iter(api.deliveries.values()))
    assert body["text"] == "neutral caption"
    assert body["attachments"][0]["size"] == file.stat().st_size
    assert result.message_id == receipt["id"]
    assert not (await sender("another", **{args[kind]: str(file)})).success
    assert len(api.slots) == 1
    await adp.disconnect()


async def test_media_send_timeout_reuses_attachment_body_and_key(rich, tmp_path):
    adp, api, _ = rich
    file = tmp_path / "neutral.txt"; file.write_bytes(b"neutral test content")
    api.failures = ["timeout"]
    await adp.on_processing_start(adp.normalize(event()))
    assert (await adp.send_document("dm", str(file))).success
    assert len(api.slots) == len(api.deliveries) == 1
    posts = [r for r in api.requests if r.url.path.endswith("/messages")]
    assert len(posts) == 2 and posts[0].content == posts[1].content
    assert posts[0].headers["Idempotency-Key"] == posts[1].headers["Idempotency-Key"]
    await adp.disconnect()


async def test_upload_not_sent_until_storage_accepts(rich, tmp_path):
    adp, api, _ = rich
    adp.client._storage_transport = httpx.MockTransport(lambda _: httpx.Response(403, text="private signed policy rejected"))
    file = tmp_path / "neutral.txt"; file.write_bytes(b"neutral test content")
    await adp.on_processing_start(adp.normalize(event()))
    result = await adp.send_document("dm", str(file))
    assert not result.success and result.error == "attachment_upload_failed"
    assert not api.deliveries
    assert not adp.store.pending_sends()
    await adp.disconnect()


async def test_feedback_updates_one_private_message_and_cleans_up(rich):
    adp, api, _ = rich
    from gateway.platforms.base import ProcessingOutcome
    inbound = adp.normalize(event())
    await adp.on_processing_start(inbound)
    await adp._feedback["message"].task
    assert len(api.visuals) == 1 and not api.deliveries
    adp.set_status_text("dm", "Lendo um arquivo…")
    await adp.send_typing("dm")
    await adp.send_typing("dm")
    assert len(api.visuals) == 1
    assert next(iter(api.visuals.values()))["text"] == "⏳ Lendo um arquivo…"
    assert (await adp.send("dm", "permanent final")).success
    await adp.on_processing_complete(inbound, ProcessingOutcome.SUCCESS)
    assert not api.visuals
    assert ("message", "eyes") not in api.reactions
    assert ("message", "white_check_mark") in api.reactions
    assert not adp.store.unfinished_visuals()
    assert not (await adp.send("dm", "late response")).success
    assert len(api.deliveries) == 1
    await adp.disconnect()


async def test_failed_preview_never_blocks_final(rich):
    adp, api, _ = rich
    api.visual_failure = 403
    await adp.on_processing_start(adp.normalize(event()))
    await adp._feedback["message"].task
    assert (await adp.send("dm", "final despite preview failure")).success
    assert len(api.deliveries) == 1
    await adp.disconnect()


async def test_restart_cleans_visuals_without_replaying_agent(rich):
    adp, api, _ = rich
    adp.store.begin_visual("previous")
    mid = await adp.client.create_progress("dm", "human", "hermes_previous_progress")
    adp.store.remember_visual("previous", mid)
    await adp.client.reaction("previous", "eyes")
    await adp._recover_visuals()
    assert not api.visuals and not api.reactions
    assert not adp.store.unfinished_visuals()
    assert not api.deliveries
    await adp.disconnect()


async def test_stop_bypasses_slow_attachment_download(rich):
    adp, api, _ = rich
    downloading = asyncio.Event()
    async def slow_storage(request):
        downloading.set()
        await asyncio.Event().wait()
    adp.client._storage_transport = httpx.MockTransport(slow_storage)
    calls = []
    async def handler(inbound):
        calls.append(inbound.text)
        return "Stopped"
    adp.set_message_handler(handler)
    raw = event(); raw["data"]["message"]["attachments"] = [attachment(api)]
    await adp.ingest("input", raw)
    await asyncio.wait_for(downloading.wait(), 2)
    control = event(mid="stop", eid="stop"); control["data"]["message"]["text"] = "/stop"
    await asyncio.wait_for(adp.ingest("stop-cursor", control), 1)
    await wait_complete(adp)
    assert calls == ["/stop"]
    assert adp.store.summary()["inbox"] == {"cancelled": 1, "complete": 1}
    await adp.disconnect()


async def test_native_pending_merge_keeps_each_attachment(rich):
    adp, api, _ = rich
    started = asyncio.Event()
    release = asyncio.Event()
    observed = []
    async def handler(inbound):
        if inbound.message_id == "opening":
            started.set()
            await release.wait()
            return "opening complete"
        observed.append(list(inbound.media_urls))
        return "files received"
    adp.set_message_handler(handler)
    await adp.ingest("opening", event(mid="opening", eid="opening"))
    await started.wait()
    # Queue mode leaves the first turn running while file events are hydrated and merged.
    adp._busy_text_mode = "queue"
    for n in (1, 2):
        raw = event(mid="file" + str(n), eid="file" + str(n))
        raw["data"]["message"].update(text="", attachments=[attachment(api)])
        await adp.ingest("file-cursor-" + str(n), raw)
    for _ in range(200):
        if not adp._admissions:
            break
        await asyncio.sleep(.01)
    release.set()
    for _ in range(200):
        if adp.store.summary()["inbox"] == {"complete": 3}:
            break
        await asyncio.sleep(.01)
    assert len(observed) == 1 and len(observed[0]) == 2
    assert adp.store.summary()["inbox"] == {"complete": 3}
    await adp.disconnect()


async def test_live_status_uses_native_verb_lane_without_arguments(rich):
    adp, api, _ = rich
    from gateway.run_turn_runner import TurnRunner
    from types import SimpleNamespace
    ctx = SimpleNamespace(_live_status_adapter=adp, _live_status_mode="verb", source=SimpleNamespace(chat_id="dm"), _run_still_current=lambda: True)
    worker = object.__new__(TurnRunner); worker._ctx = ctx
    worker._progress_live_status("tool.started", "read_file", {"path": "/private/secret-token-xyz"})
    assert adp._status_text.get("dm")
    assert "secret-token" not in adp._status_text["dm"]
    await adp.disconnect()


def test_additive_state_upgrade_preserves_existing_outbox(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path, "install:sub")
    body = {"text": "existing response"}
    original = store.prepare_send("turn:0", "turn", "dm", body)
    # Model a pre-feature DB by dropping only the new, unused tables.
    store.db.executescript("DROP TABLE uploads; DROP TABLE visuals;")
    store.close()
    store = Store(path, "install:sub")
    row = store.pending_sends()[0]
    assert row["idempotency_key"] == original["idempotency_key"]
    assert json.loads(row["body"]) == body
    assert not store.unfinished_visuals()
    assert store.upload("file", "fingerprint") is None
    with pytest.raises(ValueError):
        store.upload("file", "changed-fingerprint")
    store.close()
