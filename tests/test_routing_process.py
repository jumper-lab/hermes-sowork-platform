"""Real installed plugin + Hermes gateway + loopback SSE/HTTP/model, across channels/users."""
from pathlib import Path
import copy
import shutil
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.onboarding import BUSY_INPUT_FLAG
from e2e_support import SoWorkServer, CHANNEL_EVENT
from test_routing import extra
from tests.e2e.core.platforms._helpers import Director, GatewayUnderTest
from tests.fakes.fake_llm_provider import FakeLLMServer, Text
from tests.fakes.platforms._standin import wait_until

pytestmark = pytest.mark.spawns_gateway_lookalike


@pytest.fixture(scope="module")
def rig():
    with tempfile.TemporaryDirectory(prefix="sw-routes-") as directory:
        root = Path(directory)
        shim = root / "shim";shim.mkdir()
        shutil.copyfile(Path(__file__).with_name("e2e_sitecustomize.py"), shim / "sitecustomize.py")
        api = SoWorkServer().start()
        api.subscriptions["sub"]["events"].append(CHANNEL_EVENT)
        api.channels = [{"id": "client-a", "type": "private"}, {"id": "client-b", "type": "public"}]
        api.conversations = [{"id": "dm", "type": "direct", "participants": [{"id": "human"}]},
            {"id": "dm2", "type": "direct", "participants": [{"id": "colleague"}]},
            {"id": "team-dm", "type": "group", "participants": [{"id": "human"}, {"id": "colleague"}]}]
        settings = extra()
        settings["channels"]["client-a"]["allow_from"].append("colleague")
        director = Director();llm = FakeLLMServer(director);llm.start()
        config = {"plugins": {"enabled": ["sowork-platform"]},
            # Native busy-input onboarding persists this flag on the first overlap.
            # Start past that hint so byte equality still detects plugin config writes.
            "onboarding": {"seen": {BUSY_INPUT_FLAG: True}},
            "platforms": {"sowork": {"enabled": True, "extra": settings},
                          "slack": {"enabled": False}, "telegram": {"enabled": False}},
            "streaming": {"enabled": True, "transport": "auto", "min_interval": .05, "buffer_threshold": 1},
            "display": {"platforms": {"sowork": {"streaming": True}}, "tool_progress": "off"}}
        gw = GatewayUnderTest(root/"gateway", llm_base_url=llm.base_url, config=config,
            env={"SOWORK_USERNAME": "test-client", "SOWORK_CREDENTIAL": "test-secret", "SOWORK_INSTALLATION_ID": "install",
                 "SOWORK_TEST_API": api.url, "PYTHONPATH_PREPEND": str(shim), "HERMES_GATEWAY_LOCK_DIR": str(root/"locks")},
            ready=lambda: bool(api.stream_cursors))
        plugins = gw.hermes_home/"plugins";plugins.mkdir(exist_ok=True)
        (plugins/"sowork-platform").symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
        initial = (gw.hermes_home/"config.yaml").read_bytes()
        try:
            gw.start()
            yield SimpleNamespace(api=api, gw=gw, director=director)
            assert (gw.hermes_home/"config.yaml").read_bytes() == initial
        finally:
            gw.stop();api.close();llm.stop()


def send(rig, token, *, channel="client-a", user="human", kind="private", root=None):
    event = rig.api.message("[in:"+token+"]", channel=channel, sender=user, kind=kind, root=root)
    wait_until(lambda: any("reply to "+token in m["body"].get("text", "") for m in rig.api.visible()),
               "reply "+token, timeout=45, on_timeout=rig.gw.tail)
    result = next(m for m in rig.api.visible() if "reply to "+token in m["body"].get("text", ""))
    assert result["channel"]["id"] == channel
    assert result["body"].get("parentId") == root
    return event


def system(rig, token):
    return "\n".join(str(m.get("content", "")) for m in rig.director.requests[token][0]["messages"] if m["role"] == "system")


def test_real_channels_users_prompts_sessions_and_unlisted_senders(rig):
    api, director = rig.api, rig.director
    send(rig, "route-a")
    send(rig, "route-b", channel="client-b", user="colleague", kind="public")
    send(rig, "route-a-colleague", user="colleague")
    send(rig, "route-a-next")
    assert "Flow ID 100" in system(rig, "route-a") and "Flow ID 200" not in system(rig, "route-a")
    assert "Flow ID 200" in system(rig, "route-b") and "Flow ID 100" not in system(rig, "route-b")
    assert system(rig, "route-a") == system(rig, "route-a-next")
    history = str(director.requests["route-a-colleague"][0]["messages"])
    assert "[in:route-a]" not in history  # Native default group sessions isolate people.
    history = str(director.requests["route-a-next"][0]["messages"])
    assert "[in:route-a]" in history and "[in:route-b]" not in history and "[in:route-a-colleague]" not in history
    send(rig, "second-dm", channel="dm2", user="colleague", kind="dm")
    send(rig, "group-dm", channel="team-dm", user="colleague", kind="group_dm")
    api.message("[in:unlisted-user]", channel="client-a", kind="private", sender="intruder")
    api.message("[in:wrong-route-user]", channel="client-b", kind="public", sender="human")
    api.message("[in:unlisted-channel]", channel="outside", kind="public")
    send(rig, "acl-barrier")
    assert all(director.turns.get(token, 0) == 0 for token in ("unlisted-user", "wrong-route-user", "unlisted-channel"))


def test_real_threads_inherit_prompt_and_share_only_their_own_history(rig):
    api = rig.api
    a, b = str(uuid4()), str(uuid4())
    for root, channel, kind in ((a,"client-a","private"),(b,"client-b","public")):
        api.parents[root] = {"id": root, "text": "neutral root", "channel": {"id": channel, "type": kind}, "sender": {"id": "human"}}
    send(rig, "thread-a-human", root=a)
    send(rig, "thread-a-colleague", user="colleague", root=a)
    send(rig, "thread-b-colleague", channel="client-b", user="colleague", kind="public", root=b)
    messages = str(rig.director.requests["thread-a-colleague"][0]["messages"])
    assert "[in:thread-a-human]" in messages
    assert "Flow ID 100" in system(rig, "thread-a-colleague") and "Flow ID 200" not in messages
    assert "Flow ID 200" in system(rig, "thread-b-colleague")
    assert "[in:thread-a-human]" not in str(rig.director.requests["thread-b-colleague"][0]["messages"])


def test_real_private_preview_and_stop_do_not_interrupt_another_channel(rig):
    api, gw = rig.api, rig.gw
    root = str(uuid4())
    api.parents[root] = {"id": root, "text": "neutral root", "channel": {"id": "client-b", "type": "public"}, "sender": {"id": "colleague"}}
    rig.director.script("slow-route-b", Text("private-preview-route-b "*800, chunk_chars=30, delay_per_chunk=.2))
    api.message("[in:slow-route-b]", channel="client-b", sender="colleague", kind="public", root=root)
    wait_until(lambda: any(p.get("parentId")==root and "private-preview-route-b" in p.get("text", "") for p in api.visuals()),
               "channel B private preview", timeout=45, on_timeout=gw.tail)
    assert all(p.get("visibleToUserId")=="colleague" for p in api.visuals() if p.get("parentId")==root)
    send(rig, "other-channel-while-b-runs")
    api.message("/stop", channel="client-b", sender="colleague", kind="public", root=root)
    wait_until(lambda: any("stopped" in m["body"].get("text", "").lower() and m["body"].get("parentId")==root
                          and m["channel"]["id"]=="client-b" for m in api.visible()), "B stopped", timeout=30, on_timeout=gw.tail)
    wait_until(lambda: not any(p.get("parentId")==root for p in api.visuals()), "B preview removed", timeout=15, on_timeout=gw.tail)
    send(rig, "b-resumed", channel="client-b", user="colleague", kind="public", root=root)
    assert not any("private-preview-route-b" in m["body"].get("text", "") for m in api.visible())


def test_real_restart_keeps_routes_and_deduplication(rig):
    first = send(rig, "route-before-restart")
    old = len(rig.api.poll_cursors)
    rig.gw.stop();rig.api.replay(first);rig.gw.start()
    wait_until(lambda: len(rig.api.poll_cursors)>old, "route catch-up", timeout=30, on_timeout=rig.gw.tail)
    assert rig.api.poll_cursors[-1] is not None
    send(rig, "route-after-restart", channel="client-b", user="colleague", kind="public")
    assert rig.director.turns["route-before-restart"] == 1
    assert sum("reply to route-before-restart" in m["body"].get("text", "") for m in rig.api.visible()) == 1


def test_real_busy_followup_keeps_routes_and_config(rig):
    root = str(uuid4())
    rig.api.parents[root] = {"id": root, "text": "busy root", "channel": {"id": "client-a", "type": "private"}, "sender": {"id": "human"}}
    rig.director.script("busy-route", Text("busy-route-preview "*800, chunk_chars=30, delay_per_chunk=.2))
    rig.api.message("[in:busy-route]", channel="client-a", kind="private", root=root)
    wait_until(lambda: any(p.get("parentId")==root and "busy-route-preview" in p.get("text", "") for p in rig.api.visuals()),
               "active turn before busy followup", timeout=45, on_timeout=rig.gw.tail)
    send(rig, "busy-followup", root=root)
