"""Conversation ACLs, native prompt/session routing and durable delivery boundaries."""
import asyncio
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from sowork import routing, threads, admin, proactive
from sowork.client import CHANNEL_EVENT, DM_EVENT, THREAD_EVENT, SoWorkError
from sowork.config import Settings
from test_contracts import adapter, credentials, event, identity, no_sleep  # noqa: F401
from test_media_feedback import MediaAPI
from test_proactive import execution


def extra():
    return {"subscription_id": "sub", "workspace_id": "workspace", "app_user_id": "app",
            "allowed_user_id": "human", "channel_id": "dm", "allow_from": ["human", "colleague"],
            "group_allow_from": ["human", "colleague"], "threads_enabled": True,
            "media_enabled": True, "progress_enabled": True, "preview_enabled": True, "proactive_enabled": True,
            "channels": {"dm": {"type": "dm", "allow_from": ["human"]},
                         "dm2": {"type": "dm", "allow_from": ["colleague"]},
                         "client-a": {"type": "private", "allow_from": ["human"]},
                         "client-b": {"type": "public", "allow_from": ["colleague"]},
                         "team-dm": {"type": "group_dm", "allow_from": ["human", "colleague"]}},
            "channel_prompts": {"client-a": "Context: client A, Flow ID 100.",
                                "client-b": "Context: client B, Flow ID 200."}}


def inbound(channel="client-a", user="human", *, root=None, mid=None):
    kind = extra()["channels"][channel]["type"]
    wire = "dm" if kind in ("dm", "group_dm") else kind
    raw = event(mid=mid or str(uuid4()), eid=str(uuid4()), type=DM_EVENT if wire == "dm" else CHANNEL_EVENT)
    message = raw["data"]["message"]
    message.update(channel={"id": channel, "type": wire}, sender={"id": user})
    if wire != "dm":
        raw["data"] = {"message": message, "channel": message["channel"], "threadRootId": None}
    else:
        raw["data"].update(channelId=channel, conversationType="group" if kind == "group_dm" else "direct",
                           memberCount=3 if kind == "group_dm" else 2)
    if root:
        raw["type"] = THREAD_EVENT
        raw["data"].update(threadRootId=root, channel=message["channel"])
        message["parentId"] = root
    return raw


class MultiAPI(MediaAPI):
    def __init__(self):
        super().__init__()
        self.parent = None
        self.events = [DM_EVENT, CHANNEL_EVENT, THREAD_EVENT]
        self.scope_override = None
        self.group_pages = None
        self.groups = [{"id": "client-a", "type": "private"}, {"id": "client-b", "type": "public"}]
        self.dms = [{"id": "dm", "type": "direct", "participants": [{"id": "human"}]},
                    {"id": "dm2", "type": "direct", "participants": [{"id": "colleague"}]},
                    {"id": "team-dm", "type": "group", "participants": [{"id": "human"}, {"id": "colleague"}]}]

    def __call__(self, request):
        path = request.url.path.removeprefix("/public")
        if path == "/v1/app":
            self.requests.append(request)
            data = identity();data["installation"]["consentedScopes"] = (self.scope_override if self.scope_override is not None
                else list(routing.scopes(Settings.from_extra(extra()))))
            return httpx.Response(200, json=data)
        if path == "/v1/chat/channels":
            self.requests.append(request)
            return httpx.Response(200, json=self.group_pages[request.url.params.get("before")]
                if self.group_pages else {"channels": self.groups})
        if path == "/v1/chat/dms":
            self.requests.append(request)
            return httpx.Response(200, json={"conversations": self.dms})
        if path == "/v1/subscriptions":
            self.requests.append(request)
            return httpx.Response(200, json={"webhooks": [{"id": "sub", "officeId": "workspace",
                "events": self.events, "creator": {"id": "app"}}]})
        if path.endswith("/replies"):
            self.requests.append(request)
            return httpx.Response(200, json={"parent": self.parent, "messages": []})
        response = super().__call__(request)
        if path.endswith(("/attachments", "/ephemeral-messages")) and request.method == "POST":
            data = response.json();data["channelId"] = path.split("/")[-2]
            return httpx.Response(response.status_code, json=data)
        return response

    def accept(self, request):
        result = super().accept(request)
        result["channel"]["id"] = request.url.path.split("/")[-2]
        body = json.loads(request.content)
        if body.get("parentId"):
            result["parentId"] = body["parentId"]
        return result


@pytest.fixture
def multi(adapter):
    adp, _, runner = adapter
    adp.config.extra = extra()
    # Plugin modules are loaded under Hermes' discovery namespace.
    adp.settings = type(adp.settings).from_extra(adp.config.extra)
    api = MultiAPI()
    adp.client = type(adp.client)(credentials(), transport=httpx.MockTransport(api),
        storage_transport=httpx.MockTransport(api.storage), sleep=no_sleep)
    adp.client.identity = identity()
    return adp, api, runner


@pytest.mark.parametrize("change", ["wildcard_channel", "wildcard_user", "empty_channels", "missing_group_acl",
                                     "global_mismatch", "two_dm_users", "unknown_type", "unknown_prompt", "lost_primary"])
def test_explicit_routes_reject_ambiguous_or_widened_bindings(change):
    data = extra()
    if change == "wildcard_channel":data["channels"]["*"] = data["channels"].pop("client-a")
    if change == "wildcard_user":data["channels"]["client-a"]["allow_from"] = ["*"]
    if change == "empty_channels":data["channels"] = {}
    if change == "missing_group_acl":data.pop("group_allow_from")
    if change == "global_mismatch":data["allow_from"].append("intruder")
    if change == "two_dm_users":data["channels"]["dm"]["allow_from"].append("colleague")
    if change == "unknown_type":data["channels"]["client-a"]["type"] = "meeting"
    if change == "unknown_prompt":data["channel_prompts"]["unconfigured"] = "instructions"
    if change == "lost_primary":data["channels"].pop("dm")
    with pytest.raises(ValueError):Settings.from_extra(data)


async def test_channel_only_configuration_needs_no_primary_dm(multi):
    adp, api, _ = multi
    data = extra()
    for key in ("allowed_user_id", "channel_id"):data.pop(key)
    data["channels"] = {k:v for k,v in data["channels"].items() if k.startswith("client-")}
    settings = Settings.from_extra(data)
    assert "chat.dm" not in routing.scopes(settings)
    assert routing.event_types(settings) == [CHANNEL_EVENT, THREAD_EVENT]
    await routing.verify_conversations(SimpleNamespace(settings=settings, client=adp.client))
    adp.config.extra = data
    adp.settings = type(adp.settings).from_extra(data)
    api.events = [CHANNEL_EVENT, THREAD_EVENT]
    await adp._verify_binding()
    assert not any(r.url.path.endswith("/dms") for r in api.requests)


@pytest.mark.parametrize("channel,user", [("dm", "human"), ("dm2", "colleague"), ("client-a", "human"),
                                          ("client-b", "colleague"), ("team-dm", "human"), ("team-dm", "colleague")])
async def test_correct_native_source_acl_and_prompt(multi, channel, user):
    adp, _, runner = multi
    raw = inbound(channel, user)
    assert adp.verdict(raw) == "accepted"
    before = copy.deepcopy(raw)
    message = adp.normalize(raw)
    assert raw == before
    assert message.source.chat_type == ("dm" if channel in ("dm", "dm2") else "group")
    assert runner._is_user_authorized(message.source)
    assert message.channel_prompt == extra()["channel_prompts"].get(channel)
    assert message.source.user_id == user and message.source.chat_id == channel


@pytest.mark.parametrize("case", ["cross_user", "cross_channel", "wrong_type", "wrong_envelope", "other_workspace", "self"])
def test_other_channels_or_senders_never_enter_agent(multi, case):
    adp, _, _ = multi
    raw = inbound()
    if case == "cross_user":raw["data"]["message"]["sender"]["id"] = "colleague"
    if case == "cross_channel":raw["data"]["message"]["channel"]["id"] = "outside"
    if case == "wrong_type":raw["data"]["message"]["channel"]["type"] = "public"
    if case == "wrong_envelope":raw["data"]["channel"] = {"id": "client-b", "type": "private"}
    if case == "other_workspace":raw["officeId"] = "other"
    if case == "self":raw["data"]["message"]["sender"]["id"] = "app"
    assert adp.verdict(raw) != "accepted"


@pytest.mark.parametrize("fault", ["missing", "archived", "type_changed", "dm_peer_changed"])
async def test_startup_verifies_app_membership_without_creating_anything(multi, fault):
    adp, api, _ = multi
    if fault == "missing":api.groups.pop()
    if fault == "archived":api.groups[0]["archivedAt"] = 42
    if fault == "type_changed":api.groups[0]["type"] = "public"
    if fault == "dm_peer_changed":api.dms[1]["participants"] = [{"id": "stranger"}]
    with pytest.raises(SoWorkError):await routing.verify_conversations(adp)
    assert all(r.method == "GET" for r in api.requests if not r.url.path.endswith("/oauth/token"))


async def test_readonly_connection_and_diagnostics_support_all_routes(multi):
    adp, api, _ = multi
    report = await admin.verify_connection(adp.client, adp.settings)
    assert report["connection"] == "verified"
    assert all(r.method in ("GET", "POST") for r in api.requests)
    assert [r.url.path for r in api.requests if r.method == "POST"] == ["/public/oauth/token"]
    config = {"platforms": {"sowork": adp.config.to_dict()}}
    assert set(admin.binding(config).allowed_users) == {"human", "colleague"}
    summary = admin.config_summary(config, adp.settings)
    assert len(summary["channels"]) == 5 and "Flow ID" not in json.dumps(summary)


async def test_reactive_files_and_previews_cannot_move_to_another_channel(multi):
    adp, api, _ = multi
    attempts = []
    async def handler(message):
        other = "client-b" if message.source.chat_id == "client-a" else "client-a"
        attempts.append(await adp.send(other, "wrong destination"))
        assert not attempts[-1].success
        result = await adp._send_file_data(message.source.chat_id, "neutral.txt", "text/plain", b"ROUTE-742\n")
        assert result.success
        return "correct " + message.source.chat_id
    adp.set_message_handler(handler)
    await adp.ingest("one", inbound())
    await adp.ingest("two", inbound("client-b", "colleague"))
    for _ in range(300):
        if adp.store.summary()["inbox"].get("complete") == 2:break
        await asyncio.sleep(.01)
    assert adp.store.summary()["inbox"] == {"complete": 2}
    assert {r[1]["channel"]["id"] for r in api.deliveries.values()} == {"client-a", "client-b"}
    assert all("wrong destination" not in str(body) for body, _ in api.deliveries.values())
    preview_posts = [r for r in api.requests if r.method == "POST" and r.url.path.endswith("/ephemeral-messages")]
    assert preview_posts
    for request in preview_posts:
        channel = request.url.path.split("/")[-2]
        assert json.loads(request.content)["visibleToUserId"] == ("human" if channel == "client-a" else "colleague")
    await adp.disconnect()


async def test_durable_outbox_rechecks_original_author_and_channel(multi):
    adp, api, _ = multi
    raw = inbound()
    adp.store.ingest("one", raw, "accepted")
    row = adp.store.prepare_response(raw["data"]["message"]["id"], 0, "client-b", ["must not escape"])[0]
    with pytest.raises(Exception, match="outbox_origin_mismatch"):await adp._deliver_row(row)
    assert not api.requests
    adp.config.extra["channels"]["client-a"]["allow_from"] = ["colleague"]
    assert not routing.target_authorized(adp, "client-a", "human")


async def test_same_thread_id_cannot_cross_conversations_and_cron_keys_are_distinct(multi):
    adp, api, _ = multi
    root = str(uuid4())
    message = inbound(root=root)["data"]["message"]
    message.update(id=root);message.pop("parentId")
    adp.store.remember_thread(message)
    assert threads.known(adp, root, "client-a") and not threads.known(adp, root, "client-b")
    with pytest.raises(Exception, match="thread_destination_unverified"):
        threads.destination(adp, {"thread_id": root}, "client-b")
    with execution():
        a = await adp.send("client-a", "same reminder")
        b = await adp.send("client-b", "same reminder")
        again = await adp.send("client-a", "same reminder")
    assert a.success and b.success and a.message_id == again.message_id
    assert len(api.deliveries) == 2


async def test_subscription_or_scope_changes_never_start_a_broader_receiver(multi):
    adp, api, _ = multi
    api.events.remove(CHANNEL_EVENT)
    with pytest.raises(Exception, match="subscription_mismatch"):
        await adp._verify_binding()
    assert not adp._receiver
    assert not any(r.method != "GET" and not r.url.path.endswith("/oauth/token") for r in api.requests)


async def test_primary_dm_delivery_identity_survives_an_explicit_route_mapping(multi):
    adp, _, _ = multi
    with execution():
        before = proactive.operation(adp._home, host=adp.host, channel_id="dm")
        after = proactive.authorized(adp, "dm")
    assert before == after


def test_preview_diagnostics_include_only_configured_channel_users(multi):
    from hermes_state import SessionDB
    from sowork.compatibility import session_routes
    adp, _, _ = multi
    db = SessionDB(adp._home / "state.db")
    for channel,user in (("client-a","human"),("client-b","colleague"),("outside","intruder")):
        data = {"origin": {"platform": "sowork", "chat_id": channel, "user_id": user}}
        db.save_gateway_routing_entry("agent:main:sowork:group:"+channel+":"+user, json.dumps(data), scope=str(adp._home/"sessions"))
    db.close()
    rows, issues = session_routes(adp._home, adp.settings, {"default": "test-model", "provider": "test-provider"})
    assert not issues
    assert {r["channel_id"] for r in rows} == {"client-a", "client-b"}


async def test_missing_channel_scope_stops_before_subscription_or_event_reads(multi):
    adp, api, _ = multi
    api.scope_override = list(routing.scopes(Settings.from_extra(extra())) - {"events.subscribe:"+CHANNEL_EVENT})
    with pytest.raises(Exception, match="identity_or_scope_mismatch"):
        await adp._verify_binding()
    assert all(r.url.path in ("/public/oauth/token", "/public/v1/app") for r in api.requests)


@pytest.mark.parametrize("cycle", [False, True])
async def test_membership_listing_follows_documented_cursor_and_refuses_cycles(multi, cycle):
    adp, api, _ = multi
    api.group_pages = {None: {"channels": [], "nextCursor": "next"},
                       "next": {"channels": [], "nextCursor": "next"} if cycle else {"channels": api.groups}}
    if cycle:
        with pytest.raises(SoWorkError, match="configured_conversation_unavailable"):
            await routing.verify_conversations(adp)
    else:
        await routing.verify_conversations(adp)
    requests = [r for r in api.requests if r.url.path == "/public/v1/chat/channels"]
    assert len(requests) == 2 and requests[1].url.params["before"] == "next"
