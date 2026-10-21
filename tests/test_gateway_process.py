"""Full Hermes process + installed plugin + real loopback REST/SSE/model calls."""
from pathlib import Path
import json
import shutil
import tempfile
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
import hermes_yaml as yaml

from e2e_support import SoWorkServer
from tests.e2e.core.platforms._helpers import Director, GatewayUnderTest
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
from tests.fakes.platforms._standin import wait_until

pytestmark = pytest.mark.spawns_gateway_lookalike


@pytest.fixture(scope="module")
def rig():
    # Short path for the native gateway's Unix sockets; no operator HOME/config.
    with tempfile.TemporaryDirectory(prefix="sw-process-") as directory:
        root = Path(directory)
        shim = root / "shim";shim.mkdir()
        shutil.copyfile(Path(__file__).with_name("e2e_sitecustomize.py"), shim / "sitecustomize.py")
        api = SoWorkServer().start()
        director = Director()
        llm = FakeLLMServer(director);llm.start()
        extra = {"subscription_id": "sub", "workspace_id": "workspace", "app_user_id": "app",
                 "allowed_user_id": "human", "channel_id": "dm", "allow_from": ["human"],
                 "threads_enabled": True, "preview_enabled": True, "progress_enabled": True}
        config = {"plugins": {"enabled": ["sowork-platform"]},
                  "platforms": {"sowork": {"enabled": True, "extra": extra}, "slack": {"enabled": False}, "telegram": {"enabled": False}},
                  "streaming": {"enabled": True, "transport": "auto", "min_interval": .05, "buffer_threshold": 1},
                  "display": {"platforms": {"sowork": {"streaming": True}}, "tool_progress": "off"}}
        gw = GatewayUnderTest(root / "gateway", llm_base_url=llm.base_url, config=config,
            env={"SOWORK_USERNAME": "test-client", "SOWORK_CREDENTIAL": "test-secret", "SOWORK_INSTALLATION_ID": "install",
                 "SOWORK_TEST_API": api.url, "PYTHONPATH_PREPEND": str(shim),
                 "HERMES_GATEWAY_LOCK_DIR": str(root / "locks")}, ready=lambda: bool(api.stream_cursors))
        plugins = gw.hermes_home / "plugins";plugins.mkdir(exist_ok=True)
        (plugins / "sowork-platform").symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
        result = SimpleNamespace(api=api, gw=gw, director=director)
        try:
            gw.start()
            yield result
        finally:
            gw.stop();api.close();llm.stop()


def send(rig, token, *, root=None, text=None, expected=None):
    event = rig.api.message(text or f"[in:{token}]", root=root)
    wait_until(lambda: any((expected or "reply to " + token) in m["body"].get("text", "") for m in rig.api.visible()),
               f"SoWork response {token}", timeout=45, on_timeout=rig.gw.tail)
    return event


def test_diagnostics_verify_real_gateway_profile_and_legacy_snapshot(rig, monkeypatch):
    from sowork.diagnostics import gateway_summary
    home = rig.gw.hermes_home
    path = home / "gateway_state.json"
    snapshot = json.loads(path.read_text())
    snapshot.pop("hermes_home", None)  # Native operational releases omit this field.
    read_text = Path.read_text
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", lambda target, *a, **k:
                      json.dumps(snapshot) if target == path else read_text(target, *a, **k))
        report = gateway_summary(home, time.time())
        assert report["fresh"] and report["state"] == "connected"
        snapshot["hermes_home"] = str(home.parent / "foreign")
        assert gateway_summary(home, time.time())["state"] == "unknown"


def test_real_gateway_dm_acl_thread_routes_retry_and_reconnect(rig):
    api, gw, director = rig.api, rig.gw, rig.director
    api.fail_after_accept = True
    first = send(rig, "first")
    assert director.turns["first"] == 1
    assert sum("first" in m["body"].get("text", "") for m in api.visible()) == 1
    api.replay(first)
    api.message("[in:intruder]", sender="another-human")
    send(rig, "barrier")
    assert director.turns.get("intruder", 0) == 0 and director.turns["first"] == 1
    root = str(uuid4())
    api.parents[root] = {"id": root, "text": "neutral root", "channel": {"id": "dm", "type": "dm"}, "sender": {"id": "human"}}
    send(rig, "thread", root=root)
    assert next(m for m in api.visible() if "thread" in m["body"].get("text", ""))["body"]["parentId"] == root
    old = len(api.stream_cursors);api.generation += 1
    wait_until(lambda: len(api.stream_cursors)>old, "SSE reconnect", timeout=15, on_timeout=gw.tail)
    assert api.stream_cursors[-1] is not None
    send(rig, "after-reconnect")
    assert director.turns["first"] == 1
    cfg = yaml.safe_load((gw.hermes_home / "config.yaml").read_text())
    assert cfg["platforms"]["sowork"]["extra"]["allow_from"] == ["human"]
    assert cfg["platforms"]["slack"] == {"enabled": False}
    assert cfg["platforms"]["telegram"] == {"enabled": False}


def test_real_gateway_tool_result_and_process_restart_keep_dedupe(rig):
    api, gw, director = rig.api, rig.gw, rig.director
    fixture = gw.hermes_home / "cache" / "scratch" / "local-code.txt"
    fixture.parent.mkdir(parents=True, exist_ok=True);fixture.write_text("SOWORK-LOCAL-742\n")
    director.script("tool", ToolCall("read_file", {"path": str(fixture)}), Text("tool-complete"))
    event = send(rig, "tool", expected="tool-complete")
    if not any("SOWORK-LOCAL-742" in str(m.get("content", "")) for b in director.requests["tool"] for m in b["messages"] if m["role"] == "tool"):
        pytest.fail("WIRE=" + repr([[{k: m[k] for k in ("role", "content", "tool_calls") if k in m} for m in b["messages"][-2:]] for b in director.requests["tool"]]) + "\n" + gw.tail(1500))
    gw.stop()
    api.replay(event)
    polls = len(api.poll_cursors)
    gw.start()
    wait_until(lambda: len(api.poll_cursors)>polls, "restart catch-up", timeout=30, on_timeout=gw.tail)
    assert api.poll_cursors[-1] is not None
    send(rig, "after-restart")
    assert director.turns["tool"] == 1
    assert sum("tool-complete" in m["body"].get("text", "") for m in api.visible()) == 1


@pytest.mark.parametrize("path", ["/v1/events", "/v1/events/stream"])
def test_real_gateway_recovers_generic_event_404_without_replay(rig, path):
    api = rig.api
    before = send(rig, "before-404-" + path.rsplit("/", 1)[-1])
    with api.lock:
        api.get_failures[path] = [(404, "api_error")]
        api.generation += 1
    wait_until(lambda: not api.get_failures[path], "receive 404", timeout=15, on_timeout=rig.gw.tail)
    api.replay(before)
    token = "after-404-" + path.rsplit("/", 1)[-1]
    send(rig, token)
    assert rig.director.turns[token] == 1
    assert rig.director.turns["before-404-" + path.rsplit("/", 1)[-1]] == 1
    assert all(cursor is not None for cursor in api.poll_cursors[1:])


def test_real_gateway_stop_retires_thread_preview_without_partial_final(rig):
    api, gw, director = rig.api, rig.gw, rig.director
    root = str(uuid4())
    api.parents[root] = {"id": root, "text": "stop test", "channel": {"id": "dm", "type": "dm"}, "sender": {"id": "human"}}
    director.script("slow", Text("slow-partial " * 800, chunk_chars=30, delay_per_chunk=.2))
    api.message("[in:slow]", root=root)
    wait_until(lambda: any(p.get("parentId")==root and "slow-partial" in p.get("text", "") for p in api.visuals()), "thread preview", timeout=45, on_timeout=gw.tail)
    api.message("/stop", root=root)
    wait_until(lambda: any("stopped" in m["body"].get("text", "").lower() and m["body"].get("parentId")==root for m in api.visible()), "stop confirmation", timeout=30, on_timeout=gw.tail)
    wait_until(lambda: not any(p.get("parentId")==root for p in api.visuals()), "preview retired", timeout=15, on_timeout=gw.tail)
    send(rig, "stop-barrier", root=root)
    assert not any("slow-partial" in m["body"].get("text", "") for m in api.visible())
