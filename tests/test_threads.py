"""Private thread routing through native session keys, ACL, media and cron delivery."""
import asyncio
from dataclasses import replace
import importlib
import json
import time

import httpx
import pytest

from test_contracts import adapter, credentials, event, identity, no_sleep  # noqa: F401
from test_media_feedback import MediaAPI, rich  # noqa: F401
from test_proactive import execution, write_config

ROOT_A = "00000000-0000-4000-8000-000000000001"
ROOT_B = "00000000-0000-4000-8000-000000000002"
THREAD_EVENT = "chat.thread_reply_created"


def parent(root):
    return {"id": root, "channel": {"id": "dm", "type": "dm"}, "sender": {"id": "app"},
            "text": "farol azul" if root == ROOT_A else "ponte vermelha", "createdAt": int(time.time()) - 10}


def reply(root=ROOT_A, mid="reply-a", eid="thread-a", *, kind=THREAD_EVENT, text="What was the object?"):
    raw = event(mid=mid, eid=eid, type=kind)
    raw["data"].update(threadRootId=root, channel={"id": "dm", "type": "dm"})
    raw["data"]["message"].update(parentId=root, text=text)
    if kind == THREAD_EVENT:
        for key in ("conversationType", "memberCount", "channelId"):
            raw["data"].pop(key)
    return raw


class ThreadAPI(MediaAPI):
    def __init__(self):
        super().__init__()
        self.parents = {r: parent(r) for r in (ROOT_A, ROOT_B)}
        self.conversation = {"id": "dm", "type": "direct", "participants": [{"id": "human"}]}

    def __call__(self, request):
        path = request.url.path.removeprefix("/public")
        if path == "/v1/app":
            self.requests.append(request)
            data = identity(); data["installation"]["consentedScopes"] += ["chat.history", "events.subscribe:" + THREAD_EVENT]
            return httpx.Response(200, json=data)
        if path == "/v1/chat/dms":
            self.requests.append(request)
            return httpx.Response(200, json={"conversations": [self.conversation]})
        if path.endswith("/replies"):
            self.requests.append(request)
            root = path.split("/")[-2]
            return httpx.Response(200, json={"parent": self.parents.get(root), "messages": []})
        if path == "/v1/subscriptions":
            self.requests.append(request)
            return httpx.Response(200, json={"webhooks": [{"id": "sub", "officeId": "workspace", "events": ["chat.direct_message_created", THREAD_EVENT], "creator": {"id": "app"}}]})
        return super().__call__(request)

    def accept(self, request):
        result = super().accept(request)
        root = json.loads(request.content).get("parentId")
        if root:
            result["parentId"] = root
        return result


@pytest.fixture
def threaded(rich):
    adp, _, runner = rich
    api = ThreadAPI()
    adp.client = type(adp.client)(credentials(), transport=httpx.MockTransport(api), storage_transport=httpx.MockTransport(api.storage), sleep=no_sleep)
    adp.client.identity = identity()
    adp.settings = replace(adp.settings, threads_enabled=True, proactive_enabled=True)
    adp.config.extra.update(threads_enabled=True, proactive_enabled=True, media_enabled=True, reactions_enabled=True, progress_enabled=True)
    return adp, api, runner


async def drained(adp, count):
    for _ in range(300):
        summary = adp.store.summary()["inbox"]
        if (sum(summary.get(k, 0) for k in ("complete", "failed", "rejected", "cancelled")) >= count
                and not adp._admissions and not any(not task.done() for task in adp._background_tasks)):
            return
        await asyncio.sleep(.01)
    pytest.fail("Thread processing did not drain")


async def test_native_thread_sessions_context_and_same_thread_egress(threaded):
    adp, api, _ = threaded
    seen = []
    async def handler(inbound):
        seen.append((adp._event_session_key(inbound), inbound.source.thread_id, inbound.channel_context))
        return "received " + str(inbound.source.thread_id)
    adp.set_message_handler(handler)
    for n, raw in enumerate([reply(), reply(ROOT_B, "reply-b", "thread-b"), event(mid="main", eid="main")]):
        await adp.ingest(str(n), raw)
    await drained(adp, 3)
    assert len({key for key, _, _ in seen}) == 3
    contexts = {root: context for _, root, context in seen}
    assert "farol azul" in contexts[ROOT_A] and "ponte vermelha" not in contexts[ROOT_A]
    assert "ponte vermelha" in contexts[ROOT_B] and "farol azul" not in contexts[ROOT_B]
    assert "farol azul" not in contexts[None] and "ponte vermelha" not in contexts[None]
    assert {body.get("parentId") for body, _ in api.deliveries.values()} == {None, ROOT_A, ROOT_B}
    await adp.disconnect()


def test_native_persistent_sessions_and_reset_are_isolated_per_thread(threaded):
    adp, _, runner = threaded
    from gateway.session import SessionStore
    store = SessionStore(adp._home / "sessions", runner.config)
    sources = [adp.normalize(raw).source for raw in (reply(), reply(ROOT_B), event())]
    entries = [store.get_or_create_session(source) for source in sources]
    identifiers = [entry.session_id for entry in entries]
    assert len(set(identifiers)) == 3
    reset = store.reset_session(entries[0].session_key)
    assert reset.session_id != identifiers[0]
    assert store.get_or_create_session(sources[1]).session_id == identifiers[1]
    assert store.get_or_create_session(sources[2]).session_id == identifiers[2]


@pytest.mark.parametrize("participants", [["human"], ["human", "app"], ["app"], ["human", "other"], ["human", "human"]])
async def test_dm_membership_accepts_peer_only_or_full_list_and_no_other_shape(threaded, participants):
    adp, api, _ = threaded
    api.conversation["participants"] = [{"id": p} for p in participants]
    native = importlib.import_module(type(adp).__module__)
    if participants in (["human"], ["human", "app"]):
        await native.threads.verify_dm(adp)
        assert adp._thread_dm_verified
    else:
        with pytest.raises(native.SoWorkError):await native.threads.verify_dm(adp)


async def test_duplicate_dm_and_thread_events_invoke_agent_once(threaded):
    adp, api, _ = threaded
    seen = []
    async def handler(inbound):
        seen.append(inbound.message_id)
        return "one response"
    adp.set_message_handler(handler)
    await adp.ingest("1", reply())
    await adp.ingest("2", reply(eid="dm-duplicate", kind="chat.direct_message_created"))
    await drained(adp, 1)
    assert seen == ["reply-a"] and len(api.deliveries) == 1
    assert adp.store.cursor == "2"
    await adp.disconnect()


@pytest.mark.parametrize("case", ["other_human", "other_dm", "group", "parent_disagrees", "bad_root", "missing_channel", "disabled"])
async def test_disallowed_thread_events_do_not_reach_network_or_agent(threaded, case):
    adp, api, _ = threaded
    raw = reply()
    if case == "other_human":raw["data"]["message"]["sender"]["id"] = "other"
    if case == "other_dm":raw["data"]["message"]["channel"]["id"] = "other"
    if case == "group":raw["data"]["message"]["channel"]["type"] = "public"
    if case == "parent_disagrees":raw["data"]["message"]["parentId"] = ROOT_B
    if case == "bad_root":raw["data"]["threadRootId"] = raw["data"]["message"]["parentId"] = "../../invalid"
    if case == "missing_channel":raw["data"]["channel"] = None
    if case == "disabled":adp.settings = replace(adp.settings, threads_enabled=False)
    assert adp.verdict(raw) != "accepted"
    await adp.ingest("ignored", raw)
    assert not api.requests and not adp._admissions


@pytest.mark.parametrize("case", ["foreign_parent", "foreign_author", "nested_parent", "group_membership"])
async def test_authenticated_root_and_membership_are_checked_before_agent(threaded, case):
    adp, api, _ = threaded
    if case == "foreign_parent":api.parents[ROOT_A]["channel"]["id"] = "other-dm"
    if case == "foreign_author":api.parents[ROOT_A]["sender"]["id"] = "other-human"
    if case == "nested_parent":api.parents[ROOT_A]["parentId"] = ROOT_B
    if case == "group_membership":api.conversation["participants"].append({"id": "third"})
    async def handler(_):pytest.fail("Unverified root reached agent")
    adp.set_message_handler(handler)
    await adp.ingest("bad", reply())
    await drained(adp, 1)
    assert not api.deliveries and not api.storage_requests and not api.visuals


async def test_thread_files_and_private_progress_remain_in_thread(threaded):
    adp, api, _ = threaded
    file = adp._home / "cache/thread.txt"; file.parent.mkdir(exist_ok=True); file.write_text("neutral thread file\n")
    async def handler(inbound):
        await adp.send_document("dm", str(file))
        await adp.send_typing("dm")
        return "file delivered"
    adp.set_message_handler(handler)
    await adp.ingest("media", reply())
    await drained(adp, 1)
    assert len(api.deliveries) == 2
    assert all(body["parentId"] == ROOT_A for body, _ in api.deliveries.values())
    previews = [json.loads(r.content) for r in api.requests if "ephemeral-messages" in r.url.path and r.method == "POST"]
    assert previews and all(x["parentId"] == ROOT_A for x in previews)
    assert not api.visuals
    await adp.disconnect()


@pytest.mark.parametrize("command", ["/reset", "/approve", "/cancel", "/stop"])
async def test_thread_commands_keep_native_text_and_routing(threaded, command):
    adp, api, _ = threaded
    observed = []
    async def handler(inbound):
        observed.append((inbound.text, inbound.source.thread_id))
        return "command response"
    adp.set_message_handler(handler)
    await adp.ingest("control", reply(text=command))
    await drained(adp, 1)
    assert observed == [(command, ROOT_A)]
    assert all(body["parentId"] == ROOT_A for body, _ in api.deliveries.values())
    await adp.disconnect()


async def test_root_attachment_is_hydrated_without_fetching_another_thread(threaded):
    adp, api, _ = threaded
    api.parents[ROOT_A]["attachments"] = [{"index":0,"name":"root.txt","mime":"text/plain","size":len(api.file_bytes)}]
    seen = []
    async def handler(inbound):
        from pathlib import Path
        seen.extend(Path(p).read_bytes() for p in inbound.media_urls)
        return "root file read"
    adp.set_message_handler(handler)
    await adp.ingest("root-file", reply())
    await drained(adp, 1)
    assert api.file_bytes in seen
    assert not any(ROOT_B in str(r.url) for r in api.requests)
    await adp.disconnect()


async def test_standalone_thread_delivery_reuses_live_receipts(threaded, monkeypatch):
    adp, api, _ = threaded
    import hashlib
    native = importlib.import_module(type(adp).__module__)
    path = adp._home / "sowork" / (hashlib.sha256(adp.scope.encode()).hexdigest()[:24] + ".sqlite3")
    adp.store.close(); adp.store = native.Store(path, adp.scope)
    adp.store.remember_thread(parent(ROOT_A))
    write_config(adp)
    file = adp._home / "cache/standalone.txt"; file.parent.mkdir(exist_ok=True); file.write_text("neutral thread result\n")
    http_client = httpx.AsyncClient
    import tools.url_safety as url_safety
    def fake_http(**kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda request: api.storage(request) if request.url.host == "storage.example.org" else api(request))
        return http_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", fake_http)
    monkeypatch.setattr(url_safety, "create_ssrf_safe_async_client", fake_http)
    from gateway.platform_registry import platform_registry
    sender = platform_registry.get("sowork").standalone_sender_fn
    with execution():
        result = await sender(adp.config, "dm", "standalone thread", thread_id=ROOT_A, media_files=[(str(file), False)])
        assert result.get("success"), result
        assert (await adp.send("dm", "standalone thread", metadata={"thread_id":ROOT_A})).success
        assert (await adp.send_document("dm", str(file), metadata={"thread_id":ROOT_A})).success
    assert len(api.deliveries) == 2 and len(api.slots) == 1
    assert all(body["parentId"] == ROOT_A for body, _ in api.deliveries.values())
    assert not (adp._home / "gateway_state.json").exists()


async def test_proactive_delivery_and_worker_queue_preserve_thread(threaded):
    adp, api, _ = threaded
    for root in (ROOT_A, ROOT_B):adp.store.remember_thread(parent(root))
    write_config(adp)
    with execution():
        for root in (ROOT_A, ROOT_B, None):
            result = await adp.send("dm", "same text", metadata={"thread_id": root} if root else None)
            assert result.success
        assert (await adp.send("dm", "same text", metadata={"thread_id": ROOT_A})).success
    assert len(api.deliveries) == 3
    from cron import delivery_queue, scheduler
    job = {"id": "job-1", "execution_id": "thread-worker", "name": "Thread worker", "deliver": "sowork:dm:" + ROOT_A}
    delivery_queue.enqueue("thread-worker", job, "queued thread result")
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, {adp.platform: adp}, asyncio.get_running_loop()) == 1
    assert delivery_queue.get_status("thread-worker")["status"] == "delivered"
    assert list(api.deliveries.values())[-1][0]["parentId"] == ROOT_A


async def test_thread_outbox_recovery_keeps_parent_and_key(threaded):
    adp, api, _ = threaded
    adp.store.remember_thread(parent(ROOT_A))
    api.failures = ["timeout"] * 4
    with execution():
        assert not (await adp.send("dm", "recover thread", metadata={"thread_id": ROOT_A})).success
    native = importlib.import_module(type(adp).__module__)
    path = adp.store.path; adp.store.close(); adp.store = native.Store(path, adp.scope)
    api.failures.clear()
    await adp._recover_outbox()
    assert len(api.deliveries) == 1
    assert next(iter(api.deliveries.values()))[0]["parentId"] == ROOT_A


async def test_stop_only_cancels_admission_in_the_same_thread(threaded):
    adp, api, _ = threaded
    downloading = asyncio.Event()
    async def slow(*args):
        downloading.set()
        await asyncio.Event().wait()
    adp.client.download_attachment = slow
    calls = []
    async def handler(inbound):
        calls.append((inbound.source.thread_id, inbound.text))
        return "okay"
    adp.set_message_handler(handler)
    raw = reply(); raw["data"]["message"]["attachments"] = [{"index":0,"name":"slow.txt","mime":"text/plain","size":20}]
    await adp.ingest("slow-a", raw)
    await asyncio.wait_for(downloading.wait(), 2)
    await adp.ingest("waiting-b", reply(ROOT_B, "reply-b", "thread-b"))
    await adp.ingest("stop-a", reply(ROOT_A, "stop", "stop", text="/stop"))
    await drained(adp, 3)
    assert any(root == ROOT_B for root, _ in calls)
    assert adp.store.summary()["inbox"].get("cancelled") == 1
    await adp.disconnect()
