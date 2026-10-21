import asyncio
import importlib
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

from sowork.client import Client, DM_EVENT, REQUIRED_SCOPES, SoWorkError
from sowork.config import Credentials
from sowork.state import ReconciliationRequired, Store


def event(mid="message", eid="event", **overrides):
    result = {"id": eid, "type": DM_EVENT, "createdAt": time.time(), "installationId": "install", "officeId": "workspace", "data": {
        "channelId": "dm", "conversationType": "direct", "memberCount": 2,
        "message": {"id": mid, "text": "neutral test", "channel": {"id": "dm", "type": "dm"}, "sender": {"id": "human"}},
    }}
    result.update(overrides)
    return result


def credentials():
    return Credentials("test-client", "test-secret", "install")


def identity():
    return {"installation": {"id": "install", "state": "active", "consentedScopes": list(REQUIRED_SCOPES)}, "group": {"id": "workspace"}, "appUser": {"id": "app"}}


class API:
    def __init__(self):
        self.requests, self.deliveries = [], {}
        self.grants = 0
        self.failures = []

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path.removeprefix("/public")
        assert not any("behalf" in h.lower() for h in request.headers)
        if path == "/oauth/token":
            self.grants += 1
            return httpx.Response(200, json={"access_token": f"test-token-{self.grants}", "expires_in": 3600})
        if path == "/v1/app":
            return httpx.Response(200, json=identity())
        if path == "/v1/subscriptions":
            return httpx.Response(200, json={"webhooks": [{"id": "sub", "officeId": "workspace", "events": [DM_EVENT], "creator": {"id": "app"}}]})
        if path.endswith("/messages"):
            if self.failures:
                failure = self.failures.pop(0)
                if failure == "timeout":
                    # Server accepted the write; client never received receipt.
                    self.accept(request)
                    raise httpx.ReadTimeout("simulated", request=request)
                return httpx.Response(failure, headers={"Retry-After": "0"}, json={"error": {"code": "simulated"}})
            return httpx.Response(200, json=self.accept(request))
        if path == "/v1/events":
            return httpx.Response(200, json={"items": [], "hasMore": False})
        if path == "/v1/events/stream":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=": heartbeat\n\nevent: goodbye\n\n")
        raise AssertionError(path)

    def accept(self, request):
        key = request.headers["Idempotency-Key"]
        body = json.loads(request.content)
        if key in self.deliveries:
            assert self.deliveries[key][0] == body
        else:
            self.deliveries[key] = (body, {"id": f"receipt-{len(self.deliveries)}", "sender": {"id": "app"}, "channel": {"id": "dm"}})
        return self.deliveries[key][1]


async def no_sleep(_):
    pass


@pytest.fixture
def adapter(plugin_home):
    from gateway.config import PlatformConfig
    from gateway.platform_registry import platform_registry
    config = PlatformConfig(enabled=True, extra={"subscription_id": "sub", "workspace_id": "workspace", "app_user_id": "app", "allowed_user_id": "human", "channel_id": "dm", "allow_from": ["human"]})
    result = platform_registry.create_adapter("sowork", config)
    assert result is not None
    api = API()
    native = importlib.import_module(type(result).__module__)
    result.client = native.Client(credentials(), transport=httpx.MockTransport(api), sleep=no_sleep)
    result.store = native.Store(plugin_home / "state.sqlite3", result.scope)
    # Exercise the actual gateway authorization chain, not an always-true stub.
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={result.platform: config})
    runner.adapters = {result.platform: result}
    runner._profile_adapters = {}
    runner._voice_mode = {}
    result.gateway_runner = runner
    result.set_authorization_check(runner._make_adapter_auth_check(result.platform))
    return result, api, runner


@pytest.mark.parametrize("authorized", [True, False])
async def test_native_cold_start_waits_for_publication_before_acl_and_recovery(adapter, authorized):
    adp, api, runner = adapter
    runner.adapters.clear()  # Actual native startup connects before publishing.
    if not authorized:
        adp.config.extra["allow_from"] = ["another-human"]
    adp.store.prepare_response("previous-turn", 0, "dm", ["pending response"])
    assert adp._is_sender_authorized("human", "dm", "dm") is False
    try:
        assert await adp.connect()
        await asyncio.sleep(.1)
        assert not api.deliveries
        assert not any("/events" in str(r.url) for r in api.requests)
        # Use Hermes' real publication seam instead of pre-populating the map.
        runner._publish_primary_adapter(adp.platform, adp)
        for _ in range(100):
            if api.deliveries or adp.has_fatal_error:
                break
            await asyncio.sleep(.01)
        if authorized:
            assert len(api.deliveries) == 1
            assert adp._is_sender_authorized("human", "dm", "dm") is True
            assert not adp._is_sender_authorized("intruder", "dm", "dm")
            assert not adp.has_fatal_error
        else:
            assert adp.has_fatal_error
            assert not api.deliveries
            assert not any("/events" in str(r.url) for r in api.requests)
    finally:
        await adp.disconnect()


@pytest.mark.parametrize("path", ["/v1/events", "/v1/events/stream"])
@pytest.mark.parametrize("fault", ["temporary", "deleted_subscription", "changed_owner", "revoked_installation", "explicit_not_found"])
async def test_event_route_404_rechecks_binding_before_resuming(adapter, path, fault):
    adp, api, _ = adapter
    failed = False
    recovered = asyncio.Event()
    calls_after_failure = []

    def transport(request):
        nonlocal failed
        route = request.url.path.removeprefix("/public")
        if failed:
            calls_after_failure.append(route)
            if route == "/v1/subscriptions" and fault == "deleted_subscription":
                return httpx.Response(200, json={"webhooks": []})
            if route == "/v1/subscriptions" and fault == "changed_owner":
                response = api(request).json()
                response["webhooks"][0]["creator"]["id"] = "another-app"
                return httpx.Response(200, json=response)
            if route == "/v1/app" and fault == "revoked_installation":
                response = identity()
                response["installation"]["state"] = "revoked"
                return httpx.Response(200, json=response)
            if route == path:
                recovered.set()
        if route == path and not failed:
            failed = True
            if fault == "explicit_not_found":
                return httpx.Response(404, json={"error": {"code": "subscription_not_found"}})
            return httpx.Response(404, text="Not Found")
        return api(request)

    await adp.client.http.aclose()
    adp.client.http = httpx.AsyncClient(base_url="https://api.sowork.com/public", transport=httpx.MockTransport(transport))
    adp.store.ingest("opaque/previous", event(), "accepted")
    adp.store.finish("message", "complete")
    try:
        assert await adp.connect()
        if fault == "temporary":
            await asyncio.wait_for(recovered.wait(), 10)
            assert calls_after_failure.index("/v1/app") < calls_after_failure.index(path)
            assert calls_after_failure.index("/v1/subscriptions") < calls_after_failure.index(path)
            assert not adp.has_fatal_error
        else:
            await asyncio.wait_for(asyncio.shield(adp._receiver), 10)
            assert adp.has_fatal_error
            assert not recovered.is_set()
        assert adp.store.cursor == "opaque/previous"
        assert adp.store.db.execute("SELECT status FROM inbox WHERE message_id='message'").fetchone()[0] == "complete"
    finally:
        await adp.disconnect()


@pytest.mark.parametrize("endpoint,operation", [("/oauth/token", None), ("/v1/events", "poll"), ("/v1/events/stream", "stream")])
async def test_event_error_context_never_labels_token_errors_as_receive_errors(endpoint, operation):
    api = API()
    def transport(request):
        if request.url.path.removeprefix("/public") == endpoint:
            return httpx.Response(404, text="Not Found")
        return api(request)
    client = Client(credentials(), transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SoWorkError) as caught:
            if endpoint == "/v1/events/stream":
                async for _ in client.stream_once("sub", "opaque/cursor"):
                    pass
            else:
                await client.poll("sub", "opaque/cursor")
        assert caught.value.operation == operation
        assert not caught.value.retryable  # Only the bound receiver may recover.
        assert "opaque/cursor" not in str(caught.value)
    finally:
        await client.close()


@pytest.mark.parametrize("text,expected", [
    (r"/model claude-opus-5-5\[1m\] --provider test", "/model claude-opus-5-5[1m] --provider test"),
    ("/model claude-opus-5-5[1m] --provider test", "/model claude-opus-5-5[1m] --provider test"),
    (r"Explain \[1m\]", r"Explain \[1m\]"),
    (r"/model path\with\letters", r"/model path\with\letters"),
])
def test_native_model_command_receives_literal_model_brackets(adapter, text, expected):
    adp, _, _ = adapter
    raw = event()
    raw["data"]["message"]["text"] = text
    normalized = adp.normalize(raw)
    assert normalized.text == expected
    assert raw["data"]["message"]["text"] == text


def test_durable_ingest_dedupe_and_claim_barrier(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path, "install:sub")
    assert store.ingest("opaque-z", event(), "accepted")
    assert not store.ingest("opaque-a", event(), "accepted")
    assert not store.ingest("opaque-b", event(eid="another"), "accepted")
    assert store.cursor == "opaque-b"  # never lexically compare opaque IDs
    assert len(store.queued()) == 1
    assert store.claim("message")
    assert not store.claim("message")
    store.close()
    store = Store(path, "install:sub")
    assert store.recover_claims() == 1
    assert not store.queued()  # a crash cannot rerun tools
    assert store.summary()["inbox"] == {"uncertain": 1}
    with pytest.raises(ValueError):
        store.ingest("rollback", event(mid=None, eid="bad"), "accepted")
    assert store.cursor == "opaque-b"
    store.observed(now=1)
    with pytest.raises(ReconciliationRequired):
        store.check_retention(now=72 * 3600 + 1)
    store.close()


def test_immutable_chunks_survive_restart(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path, "install:sub")
    rows = store.prepare_response("turn", 0, "dm", ["same", "same"])
    assert rows[0]["idempotency_key"] != rows[1]["idempotency_key"]
    store.sent(rows[0]["logical_id"], {"id": "receipt"})
    store.close()
    store = Store(path, "install:sub")
    assert store.pending_sends()[0]["idempotency_key"] == rows[1]["idempotency_key"]
    assert store.prepare_response("turn", 0, "dm", ["same", "same"])[0]["status"] == "sent"
    with pytest.raises(ValueError):
        store.prepare_response("turn", 0, "dm", ["changed"])
    store.close()
    with pytest.raises(ValueError):
        Store(path, "another:scope")


@pytest.mark.parametrize("failure", [401, 429, 503, "timeout"])
async def test_authenticated_retry_keeps_body_and_key(failure):
    api = API()
    api.failures = [failure]
    client = Client(credentials(), transport=httpx.MockTransport(api), sleep=no_sleep)
    receipt = await client.send("dm", {"text": "hello"}, "hermes_test_stable_key")
    assert receipt["id"] == "receipt-0"
    assert len(api.deliveries) == 1
    posts = [r for r in api.requests if r.url.path.endswith("/messages")]
    assert len(posts) == 2
    assert posts[0].content == posts[1].content
    assert posts[0].headers["Idempotency-Key"] == posts[1].headers["Idempotency-Key"]
    assert api.grants == (2 if failure == 401 else 1)
    await client.close()


async def test_forbidden_rereads_grant_without_widening_it():
    api = API()
    api.failures = [403]
    client = Client(credentials(), transport=httpx.MockTransport(api), sleep=no_sleep)
    with pytest.raises(SoWorkError) as error:
        await client.send("dm", {"text": "hello"}, "hermes_test_stable_key")
    assert error.value.status == 403
    assert not api.deliveries
    assert [r.method for r in api.requests] == ["POST", "POST", "GET"]
    await client.close()


async def test_sse_cursor_and_goodbye_reconnect_contract():
    requests = []
    def transport(request):
        requests.append(request)
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 3600})
        stream = ': heartbeat\n\nid: opaque/next\nevent: message\ndata: ' + json.dumps(event()) + '\n\nevent: goodbye\ndata: {}\n\n'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=stream)
    client = Client(credentials(), transport=httpx.MockTransport(transport))
    frames = [frame async for frame in client.stream_once("sub", "opaque/previous")]
    assert [f.log_id for f in frames if f.event] == ["opaque/next"]
    assert frames[-1].goodbye
    assert requests[-1].headers["Last-Event-ID"] == "opaque/previous"
    assert requests[-1].url.params["subscriptionId"] == "sub"
    await client.close()


async def test_native_adapter_acl_session_dedupe_and_delivery(adapter):
    adp, api, runner = adapter
    from gateway.session import build_session_key
    assert not adp.authorization_is_upstream
    assert not adp.enforces_own_access_policy
    assert adp._is_sender_authorized("human", "dm", "dm")
    assert not adp._is_sender_authorized("intruder", "dm", "dm")
    calls = []
    async def handler(inbound):
        assert runner._is_user_authorized(inbound.source)
        calls.append(build_session_key(inbound.source))
        return "native final"
    adp.set_message_handler(handler)
    for raw in [event(), event(), event(eid="other-type", type="chat.mentioned"), event(eid="thread", type="chat.thread_reply"), event(eid="same-message")]:
        await adp.ingest("cursor-" + raw["id"], raw)
    for _ in range(100):
        if adp.store.summary()["inbox"] == {"complete": 1}:
            break
        await asyncio.sleep(.01)
    assert len(calls) == 1
    assert len(api.deliveries) == 1
    assert adp.store.summary() == {"inbox": {"complete": 1}, "outbox": {"sent": 1}}
    followup = event(mid="followup", eid="followup")
    assert build_session_key(adp.normalize(followup).source) == calls[0]
    assert not (await adp.send("other-dm", "no")).success
    await adp.disconnect()


@pytest.mark.parametrize("change,reason", [
    ({"installationId": "other"}, "wrong_installation"),
    ({"officeId": "other"}, "wrong_workspace"),
    ({"data": None}, "malformed"),
    ({"createdAt": 1}, "stale_requires_review"),
    ({"type": "chat.thread_reply"}, "unsupported_event"),
])
async def test_negative_event_admission(adapter, change, reason):
    adp, _, _ = adapter
    assert adp.verdict(event(**change)) == reason
    await adp.ingest("negative", event(**change))
    assert not adp.store.queued()
    await adp.disconnect()


async def test_human_app_group_and_attachment_boundaries(adapter):
    adp, _, _ = adapter
    for key, value, reason in [("sender", {"id": "app"}, "self_message"), ("sender", {"id": "other"}, "unbound_sender"), ("attachments", [{}], "malformed")]:
        raw = event()
        raw["data"]["message"][key] = value
        assert adp.verdict(raw) == reason
    raw = event()
    raw["data"]["memberCount"] = 3
    assert adp.verdict(raw) == "not_individual_dm"
    await adp.disconnect()


async def test_response_chunks_and_uncertain_receipt_recovery(adapter):
    adp, api, _ = adapter
    adp.MAX_MESSAGE_LENGTH = 100
    await adp.on_processing_start(adp.normalize(event()))
    result = await adp.send("dm", "a" * 170)
    assert result.success and len(result.continuation_message_ids) == 1
    assert len(api.deliveries) == 2
    row = adp.store.prepare_send("crash:0", "crash", "dm", {"text": "crash test"})
    # Crash after server acceptance but before persisting the receipt.
    await adp.client.send("dm", json.loads(row["body"]), row["idempotency_key"])
    await adp._recover_outbox()
    assert len(api.deliveries) == 3
    assert not adp.store.pending_sends()
    await adp.disconnect()


async def test_connect_scoped_lock_and_restart_cursor(adapter):
    adp, _, _ = adapter
    assert await adp.connect()
    await adp.ingest("durable-cursor", event(eid="ignored", type="chat.mentioned"))
    assert adp.store.cursor == "durable-cursor"
    assert adp._platform_lock_identity == "install:sub"
    child_env = dict(os.environ, HERMES_HOME=str(adp.store.db.execute("PRAGMA database_list").fetchone()[2]) + ".other-home")
    child_env["PYTHONPATH"] = os.pathsep.join(sys.path)
    probe = subprocess.run([sys.executable, "-c", "from sowork.lock import SubscriptionLock; lock = SubscriptionLock('install:sub'); print(lock.acquire())"], env=child_env, capture_output=True, text=True, check=True)
    assert probe.stdout.strip() == "False"
    # Rotation does not alter lock identity.
    adp.client._token = "rotated-test-token"
    assert adp.scope == "install:sub"
    await adp.disconnect()


async def test_native_stop_during_processing_finishes_both_inbox_rows(adapter):
    adp, api, _ = adapter
    started = asyncio.Event()
    async def handler(inbound):
        if inbound.text == "/stop":
            return "Stopped"
        started.set()
        await asyncio.Event().wait()
    adp.set_message_handler(handler)
    await adp.ingest("first", event())
    await asyncio.wait_for(started.wait(), 2)
    control = event(mid="stop", eid="stop")
    control["data"]["message"]["text"] = "/stop"
    await adp.ingest("second", control)
    for _ in range(100):
        if adp.store.summary()["inbox"] == {"cancelled": 1, "complete": 1}:
            break
        await asyncio.sleep(.01)
    assert adp.store.summary()["inbox"] == {"cancelled": 1, "complete": 1}
    assert len(api.deliveries) == 1
    await adp.disconnect()


def test_passive_probe_and_profile_secret_isolation(plugin_home, monkeypatch):
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    from sowork.plugin import requirements, configured
    from gateway.config import PlatformConfig
    monkeypatch.setattr(Client, "token", lambda *_: pytest.fail("passive probe called OAuth"))
    assert requirements()
    token = set_secret_scope({})
    try:
        with pytest.raises(ValueError):
            Credentials.from_scope()
        assert not configured(PlatformConfig(extra={}))
    finally:
        reset_secret_scope(token)
