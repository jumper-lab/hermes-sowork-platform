"""Real native discovery, ACL, execution context, scheduler and out-of-process sender seams."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import importlib
import json
import time

import httpx
import pytest
import hermes_yaml as yaml

from test_contracts import adapter  # noqa: F401
from test_media_feedback import rich  # noqa: F401


@contextmanager
def execution(occurrence="run-1"):
    from cron.execution_identity import enter_cron_execution, exit_cron_execution
    token = enter_cron_execution({"id": "job-1", "name": "Neutral probe"}, occurrence, {"source": "direct"})
    try:
        yield
    finally:
        exit_cron_execution(token)


def enable(adp):
    adp.config.extra["proactive_enabled"] = True
    adp.settings = replace(adp.settings, proactive_enabled=True)


def write_config(adp):
    path = adp._home / "config.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["platforms"] = {"sowork": adp.config.to_dict()}
    cfg["cron"] = {"wrap_response": False}
    path.write_text(yaml.safe_dump(cfg))


def test_native_cron_origin_capture_is_enabled_only_for_the_pinned_surface(adapter):
    adp, _, _ = adapter
    from gateway.session_context import set_session_vars, clear_session_vars
    from tools.cronjob_job_args import _origin_from_env
    assert adp.supports_async_delivery is False
    enable(adp)
    tokens = set_session_vars(platform="sowork", chat_id="dm", user_id="human", async_delivery=adp.supports_async_delivery)
    try:
        origin = _origin_from_env("5m")
        assert origin["platform"] == "sowork" and origin["chat_id"] == "dm" and origin["user_id"] == "human"
    finally:
        clear_session_vars(tokens)


def test_native_target_resolution_restricts_every_destination_to_the_pinned_dm(adapter):
    adp, _, _ = adapter
    enable(adp)
    write_config(adp)
    from tools.send_message_targets import resolve_send_target
    assert resolve_send_target("sowork", "dm") == ("dm", None, None)
    for destination in ("another", "dm:thread", "*", "human"):
        assert resolve_send_target("sowork", destination)[2]


async def test_no_inbound_turn_native_cron_identity_retry_and_distinct_occurrences(adapter):
    adp, api, _ = adapter
    enable(adp)
    with execution():
        first = await adp.send("dm", "neutral scheduled result")
        second = await adp.send("dm", "neutral scheduled result")
    assert first.success and second.message_id == first.message_id
    assert len(api.deliveries) == 1
    with execution("run-2"):
        assert (await adp.send("dm", "neutral scheduled result")).success
    assert len(api.deliveries) == 2
    assert not adp._receiver


@pytest.mark.parametrize("case", ["disabled", "no_identity", "other_dm", "thread", "widened_acl", "native_acl", "foreign_home"])
async def test_proactive_denials_happen_before_network(adapter, monkeypatch, case):
    adp, api, runner = adapter
    enable(adp)
    chat, metadata = "dm", None
    if case == "disabled":
        adp.settings = replace(adp.settings, proactive_enabled=False)
    if case == "other_dm":
        chat = "another-dm"
    if case == "thread":
        metadata = {"thread_id": "unapproved-thread"}
    if case == "widened_acl":
        adp.config.extra["allow_from"].append("another-user")
    if case == "native_acl":
        runner.adapters.clear()
    with execution():
        if case == "foreign_home":
            monkeypatch.setenv("HERMES_HOME", str(adp._home.parent / "other-profile"))
        if case != "no_identity":
            result = await adp.send(chat, "must not send", metadata=metadata)
    if case == "no_identity":
        result = await adp.send(chat, "must not send", metadata={"job_id": "forged", "execution_id": "forged"})
    assert not result.success
    assert api.requests == []


async def test_immutable_complete_batch_and_chunk_ordinals(adapter):
    adp, api, _ = adapter
    enable(adp)
    adp.MAX_MESSAGE_LENGTH = 20
    text = ("x" * 20) * 3
    expected = adp.truncate_message(text, adp.MAX_MESSAGE_LENGTH)
    with execution():
        assert (await adp.send("dm", text)).success
        assert (await adp.send("dm", text)).success
        assert not (await adp.send("dm", "x" * 20)).success
    assert [body[0]["text"] for body in api.deliveries.values()] == expected
    identical = adp.store.prepare_messages("identical", 0, "dm", [{"text": "same"}] * 2)
    assert identical[0]["idempotency_key"] != identical[1]["idempotency_key"]


async def test_uncertain_acceptance_uses_persisted_key_after_sender_restart(adapter):
    adp, api, _ = adapter
    enable(adp)
    # Exhaust the bounded client retries after the server has accepted the first write.
    api.failures = ["timeout"] * 5
    with execution():
        first = await adp.send("dm", "uncertain neutral response")
        assert not first.success
        native = importlib.import_module(type(adp).__module__)
        path = adp.store.path
        adp.store.close()
        adp.store = native.Store(path, adp.scope)
        api.failures.clear()
        assert (await adp.send("dm", "uncertain neutral response")).success
    assert len(api.deliveries) == 1


async def test_expired_uncertain_send_is_not_reposted(adapter):
    adp, api, _ = adapter
    enable(adp)
    native = importlib.import_module(type(adp).__module__)
    with execution():
        identity = native.proactive.operation(adp._home)
        adp.store.prepare_messages(identity, "text", "dm", [{"text": "old uncertain response"}])
        with adp.store.db:
            adp.store.db.execute("UPDATE outbox SET created=?", (time.time() - 3601,))
        assert not (await adp.send("dm", "old uncertain response")).success
    assert not api.requests


async def test_native_tool_executor_identity_works_without_an_inbound_turn(adapter):
    adp, api, _ = adapter
    enable(adp)
    from tools.approval_context import (
        set_current_observability_context, reset_current_observability_context,
        set_current_session_key, reset_current_session_key)
    session = set_current_session_key("sowork:dm:human")
    ids = set_current_observability_context(tool_call_id="call-1", session_id="session-1")
    try:
        assert (await adp.send("dm", "tool notification")).success
        assert (await adp.send("dm", "tool notification")).success
    finally:
        reset_current_observability_context(ids)
        reset_current_session_key(session)
    assert len(api.deliveries) == 1


async def test_proactive_media_uses_same_descriptor_after_restart(rich):
    adp, api, _ = rich
    enable(adp)
    file = adp._home / "cache" / "neutral.txt"
    file.parent.mkdir(exist_ok=True)
    file.write_text("neutral file content\n")
    with execution():
        assert (await adp.send_document("dm", str(file))).success
        assert (await adp.send_document("dm", str(file))).success
        file.write_text("changed file content\n")
        assert not (await adp.send_document("dm", str(file))).success
    assert len(api.slots) == len(api.deliveries) == 1


async def test_native_standalone_and_live_share_receipts_without_gateway_state_writes(rich, monkeypatch):
    adp, api, _ = rich
    enable(adp)
    write_config(adp)
    native = importlib.import_module(type(adp).__module__)
    http_client = httpx.AsyncClient
    def fake_http(**kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda request: api.storage(request) if request.url.host == "storage.example.org" else api(request))
        return http_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", fake_http)
    # Use the canonical DB location shared by the real standalone worker and gateway.
    import hashlib
    path = adp._home / "sowork" / (hashlib.sha256(adp.scope.encode()).hexdigest()[:24] + ".sqlite3")
    adp.store.close()
    adp.store = native.Store(path, adp.scope)
    entry = __import__("gateway.platform_registry", fromlist=["platform_registry"]).platform_registry.get("sowork")
    with execution():
        assert (await adp.send("dm", "standalone parity")).success
        result = await entry.standalone_sender_fn(adp.config, "dm", "standalone parity")
    assert result["success"]
    assert len(api.deliveries) == 1


async def test_real_native_worker_delivery_queue_attests_identity_on_gateway_loop(adapter):
    adp, api, _ = adapter
    enable(adp)
    write_config(adp)
    from cron import delivery_queue, scheduler
    job = {"id": "job-1", "execution_id": "worker-1", "name": "Queued neutral probe", "deliver": "sowork:dm"}
    delivery_queue.enqueue("worker-1", job, "queued scheduled delivery")
    loop = asyncio.get_running_loop()
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, {adp.platform: adp}, loop) == 1
    assert delivery_queue.get_status("worker-1")["status"] == "delivered"
    assert len(api.deliveries) == 1
    assert await asyncio.to_thread(scheduler.drain_delivery_queue, {adp.platform: adp}, loop) == 0
    assert len(api.deliveries) == 1
    assert not (adp._home / "gateway_state.json").exists()
    assert not adp._receiver


async def test_native_scheduler_delivery_on_gateway_loop_and_retry(adapter):
    adp, api, _ = adapter
    enable(adp)
    write_config(adp)
    from cron.scheduler_delivery import _deliver_result
    job = {"id": "job-1", "name": "Neutral probe", "deliver": "sowork:dm", "origin": {"platform": "sowork", "chat_id": "dm", "user_id": "human"}}
    loop = asyncio.get_running_loop()
    with execution():
        error = await asyncio.to_thread(_deliver_result, job, "scheduled delivery", {adp.platform: adp}, loop)
        assert error is None
        assert await asyncio.to_thread(_deliver_result, job, "scheduled delivery", {adp.platform: adp}, loop) is None
    assert len(api.deliveries) == 1
