"""Exercise the real inbox/ACL/outbox path; an emoji never enters the agent."""
import asyncio
from dataclasses import replace
import importlib
import json
from uuid import UUID

import httpx
import pytest

from test_contracts import adapter, event, API, identity  # noqa: F401
from sowork.client import SoWorkError


class ProbeAPI(API):
    def __init__(self):
        super().__init__()
        self.reads, self.deleted = [], []
        self.groups = [[]]
        self.mutate = None
        self.fail_read = None
        self.progress = "00000000-0000-4000-8000-000000000099"

    def accept(self, request):
        result = super().accept(request)
        result.update(id=str(UUID(int=len(self.deliveries))), channel={"id": "dm", "type": "dm"})
        return result

    def __call__(self, request):
        path = request.url.path.removeprefix("/public")
        if path == "/v1/app":
            data = identity();data["installation"]["consentedScopes"].append("chat.history")
            return httpx.Response(200, json=data)
        if path == "/v1/chat/dms":
            return httpx.Response(200, json={"conversations": [{"id": "dm", "type": "direct", "participants": [{"id": "human"}]}]})
        if path.endswith("/replies"):
            self.requests.append(request);self.reads.append(request)
            assert dict(request.url.params) == {"limit": "1"}
            if self.fail_read:
                return httpx.Response(self.fail_read, headers={"Retry-After": "30"}, json={"error": {"code": "simulated"}})
            message = {"id": path.split("/")[-2], "channel": {"id": "dm", "type": "dm"},
                       "sender": {"id": "app"}, "reactions": self.groups.pop(0) if len(self.groups) > 1 else self.groups[0]}
            if self.mutate:self.mutate(message)
            return httpx.Response(200, json={"parent": message, "messages": []})
        if path.endswith("/ephemeral-messages"):
            self.requests.append(request)
            return httpx.Response(200, json={"id": self.progress, "channelId": "dm", "visibleToUserId": "human", "senderId": "app"})
        if path == "/v1/chat/ephemeral-messages/" + self.progress:
            self.requests.append(request);self.deleted.append(path)
            assert request.method == "DELETE"
            return httpx.Response(204)
        return super().__call__(request)


def reaction(user="human", kind="eyes"):
    return [{"type": kind, "count": 1, "users": [{"id": user}]}]


@pytest.fixture
async def probe(adapter):
    adp, old_api, runner = adapter
    api = ProbeAPI()
    await adp.client.http.aclose()
    adp.client.http = httpx.AsyncClient(base_url="https://api.sowork.com/public/", transport=httpx.MockTransport(api))
    adp.client.identity = identity()
    adp.client.identity["installation"]["consentedScopes"].append("chat.history")
    adp.settings = replace(adp.settings, reaction_probe_enabled=True)
    adp.config.extra["reaction_probe_enabled"] = True
    adp._running = True
    def forbidden(*a, **k):raise AssertionError("Probe must never call the agent")
    adp.set_message_handler(forbidden)
    clock = [0.0]
    adp._reaction_probe.clock = lambda: clock[0]
    async def sleep(seconds):
        clock[0] += seconds
        await asyncio.sleep(0)
    adp._reaction_probe.sleep = sleep
    yield adp, api, runner, clock
    await adp._reaction_probe.close()
    await adp.client.close()
    adp.store.close()


async def command(adp, text="/reaction-test", number=1):
    raw = event(mid=str(UUID(int=1000 + number)), eid="command-" + str(number))
    raw["data"]["message"]["text"] = text
    await adp.ingest("cursor-" + str(number), raw)
    return raw


def last(adp):
    native = importlib.import_module(type(adp._reaction_probe).__module__)
    return native.snapshot(adp.store)


async def test_matching_human_can_only_remove_own_visual_once_through_native_ingest(probe):
    adp, api, runner, clock = probe
    api.groups = [[], reaction()]
    raw = await command(adp)
    await adp._reaction_probe.task
    assert last(adp)["state"] == "observed" and last(adp)["reads"] == len(api.reads) == 2
    assert len(api.deliveries) == 1 and len(api.deleted) == 1
    assert not adp.store.unfinished_visuals()
    # Duplicate delivery cannot make a second challenge/action.
    await adp.ingest("duplicate", raw)
    assert len(api.deliveries) == 1 and len(api.deleted) == 1
    await command(adp, "/reaction-status", 2)
    body = list(api.deliveries.values())[-1][0]["text"]
    assert "observed" in body and "no command" in body
    assert all(r.method == "GET" or r.url.path.endswith("/messages") or "ephemeral-messages" in r.url.path
               or r.url.path.endswith("/oauth/token") for r in api.requests)


async def test_no_reaction_observation_never_exceeds_twelve_real_gets_or_two_minutes(probe):
    adp, api, runner, clock = probe
    await command(adp)
    await adp._reaction_probe.task
    assert last(adp)["state"] == "expired" and len(api.reads) == 12
    assert clock[0] <= 120 and len(api.deliveries) == 1
    assert len({str(r.url) for r in api.reads}) == 1


@pytest.mark.parametrize("groups", [reaction("app"), reaction("outsider"), reaction("human", "+1"), reaction("human", "approve")])
async def test_other_people_app_or_other_emoji_do_not_trigger(probe, groups):
    adp, api, runner, clock = probe
    api.groups = [[], groups]
    await command(adp);await adp._reaction_probe.task
    assert last(adp)["state"] == "expired" and len(api.reads) == 12


@pytest.mark.parametrize("groups,state", [(reaction(), "preexisting"), (None, "unreported"),
    ({}, "unreported"), ([{"type": "eyes", "count": 1}], "unreported")])
async def test_baseline_must_be_authoritative_and_free_of_prior_target_reaction(probe, groups, state):
    adp, api, runner, clock = probe
    api.groups = [groups]
    await command(adp)
    assert last(adp)["state"] == state and last(adp)["reads"] == 1
    assert adp._reaction_probe.task is None and not adp.store.unfinished_visuals()


async def test_omitted_reactions_later_are_unknown_not_a_removal_or_empty_list(probe):
    adp, api, runner, clock = probe
    api.groups = [[], None]
    await command(adp);await adp._reaction_probe.task
    assert last(adp)["state"] == "unreported" and len(api.reads) == 2


@pytest.mark.parametrize("change", ["id", "channel", "sender", "parent"])
async def test_response_cannot_redirect_observation_to_another_message_or_thread(probe, change):
    adp, api, runner, clock = probe
    def mutate(message):
        if change == "id":message["id"] = str(UUID(int=900))
        elif change == "channel":message["channel"] = {"id": "other", "type": "dm"}
        elif change == "sender":message["sender"] = {"id": "human"}
        else:message["parentId"] = str(UUID(int=901))
    api.mutate = mutate
    await command(adp)
    assert last(adp)["state"] == "denied" and len(api.reads) == 1
    assert adp._reaction_probe.task is None


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_message_get_has_no_hidden_retries_or_extra_identity_reprobes(probe, status):
    adp, api, runner, clock = probe
    api.fail_read = status
    await command(adp)
    assert len(api.reads) == 1 and adp._reaction_probe.task is None
    assert not any(r.url.path.endswith("/v1/app") for r in api.requests)
    assert last(adp)["state"] == ("rate_limited" if status == 429 else "denied" if status in (401, 403) else "error")


async def test_acl_change_while_sleeping_closes_without_another_message_get(probe):
    adp, api, runner, clock = probe
    original = adp._reaction_probe.sleep
    async def change_acl(seconds):
        await original(seconds)
        adp.config.extra["allow_from"] = ["outsider"]
    adp._reaction_probe.sleep = change_acl
    await command(adp);await adp._reaction_probe.task
    assert last(adp)["state"] == "denied" and len(api.reads) == 1


async def test_one_active_observation_and_status_does_not_start_an_agent(probe):
    adp, api, runner, clock = probe
    hold = asyncio.Event()
    adp._reaction_probe.sleep = lambda _: hold.wait()
    await command(adp)
    first = last(adp)["message_id"]
    await command(adp, number=2)
    assert last(adp)["message_id"] == first and len(api.reads) == 1
    assert "already being observed" in list(api.deliveries.values())[-1][0]["text"]
    await command(adp, "/reaction-status", 3)
    assert "Waiting" in list(api.deliveries.values())[-1][0]["text"]
    await adp._reaction_probe.close()
    assert last(adp)["state"] == "interrupted"


async def test_disconnect_and_new_instance_never_resume_old_observation(probe):
    adp, api, runner, clock = probe
    hold = asyncio.Event();adp._reaction_probe.sleep = lambda _: hold.wait()
    await command(adp)
    store = adp.store
    await adp._reaction_probe.close()
    count = len(api.reads)
    # A real crash can leave the durable state active even though no task exists.
    adp._reaction_probe.save(state="waiting")
    native = importlib.import_module(type(adp._reaction_probe).__module__)
    recovered = native.ReactionProbe(adp);recovered.recover()
    assert recovered.task is None and native.snapshot(store)["state"] == "interrupted"
    assert len(api.reads) == count and len(api.deleted) == 1


async def test_feature_is_opt_in_and_missing_history_scope_cannot_arm(probe):
    adp, api, runner, clock = probe
    adp.settings = replace(adp.settings, reaction_probe_enabled=False)
    await command(adp)
    assert not api.reads and "disabled" in list(api.deliveries.values())[-1][0]["text"]
    adp.settings = replace(adp.settings, reaction_probe_enabled=True)
    adp.client.identity["installation"]["consentedScopes"].remove("chat.history")
    await command(adp, number=2)
    assert not api.reads and "not confirmed" in list(api.deliveries.values())[-1][0]["text"]


async def test_unauthorized_inbound_cannot_create_a_probe(probe):
    adp, api, runner, clock = probe
    raw = event(mid=str(UUID(int=2000)))
    raw["data"]["message"].update(text="/reaction-test", sender={"id": "outsider"})
    await adp.ingest("intruder", raw)
    assert not api.reads and not api.deliveries and adp._reaction_probe.task is None


@pytest.mark.parametrize("text", ["/reaction\\-test", "/REACTION-TEST"])
async def test_literal_markdown_command_never_falls_through_to_model(probe, text):
    adp, api, runner, clock = probe
    api.groups = [[], reaction()]
    await command(adp, text);await adp._reaction_probe.task
    assert last(adp)["state"] == "observed"
