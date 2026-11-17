"""Host API drift must stop startup before any secret lookup or network."""
from types import SimpleNamespace

import pytest

from sowork import hermes_compat, plugin, runtime
from test_contracts import adapter  # noqa: F401
from test_proactive import enable


def test_executor_identity_uses_native_context_without_browser_module(monkeypatch):
    import sys
    from tools.approval_context import set_current_observability_context, reset_current_observability_context
    monkeypatch.setitem(sys.modules, "tools.browser_extension_router", None)
    assert hermes_compat.tool_call_id() == ""
    token = set_current_observability_context(tool_call_id="native-call")
    try:
        assert hermes_compat.tool_call_id() == "native-call"
    finally:
        reset_current_observability_context(token)
    assert hermes_compat.tool_call_id() == ""


def test_missing_executor_identity_stops_before_credentials(monkeypatch):
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_approval_tool_call_id", None)
    assert "tools.approval_context._approval_tool_call_id" in runtime.missing_contracts()
    with pytest.raises(RuntimeError, match="_approval_tool_call_id"):
        plugin.build_adapter(None)


def test_native_caption_split_preserves_long_text_and_media():
    caption, text = hermes_compat.split_caption("caption", [("neutral.png", False)])
    assert caption == "caption" and not text
    message = "x" * 20000
    caption, text = hermes_compat.split_caption(message, [("neutral.png", False)])
    assert text == message and not caption


def test_broken_caption_contract_is_reported(monkeypatch):
    monkeypatch.setattr(hermes_compat, "_caption_helpers", lambda: (lambda: None, 4096))
    assert "native outbound caption helpers" in runtime.missing_contracts()


def test_shared_status_uses_only_the_attested_profile(monkeypatch, tmp_path):
    from gateway import status
    from sowork.diagnostics import gateway_summary
    if not hasattr(status, "profile_platforms_from_multiplexer"):
        pytest.skip("native shared-gateway status projection is unavailable")
    record = {"pid":123,"platforms":{"alpha:sowork":{"state":"connected"},
                                   "beta:sowork":{"state":"fatal"}}}
    monkeypatch.setattr(status,"multiplexer_liveness_for_profile",lambda home:(123,record))
    monkeypatch.setattr(status,"profile_name_for_home",lambda home:home.name)
    monkeypatch.setattr(status,"runtime_status_heartbeat_age_s",lambda runtime:1)
    assert gateway_summary(tmp_path/'alpha',0)['state']=='connected'
    assert gateway_summary(tmp_path/'beta',0)['state']=='fatal'
    assert gateway_summary(tmp_path/'unserved-platform',0)['state']=='unknown'
    monkeypatch.setattr(status,"multiplexer_liveness_for_profile",lambda home:None)
    assert gateway_summary(tmp_path/'alpha',0)=={'state':'unknown','fresh':False}


@pytest.mark.parametrize("name,replacement", [("_ACTIVE_DELIVERIES", None), ("_PROCESS_ID", 1), ("_lock", None), ("get_status", lambda: None)])
def test_changed_queue_contract_is_reported_before_startup(monkeypatch, name, replacement):
    from cron import delivery_queue
    monkeypatch.setattr(delivery_queue, name, replacement)
    assert any(name in issue for issue in runtime.missing_contracts())
    with pytest.raises(RuntimeError, match=name):plugin.build_adapter(None)


def test_changed_target_resolver_signature_is_reported(monkeypatch):
    from cron import scheduler_delivery
    monkeypatch.setattr(scheduler_delivery, "_resolve_delivery_targets", lambda: None)
    assert any("_resolve_delivery_targets" in issue for issue in runtime.missing_contracts())


async def test_public_host_execution_api_precedes_legacy_queue(adapter, monkeypatch):
    adp, api, _ = adapter
    enable(adp)
    from hermes_cli.profiles import current_profile_name
    calls = []
    def current():
        calls.append(True)
        return SimpleNamespace(job_id="public-job", execution_id="public-run", profile=current_profile_name("default"))
    adp.host = SimpleNamespace(current_cron_execution=current)
    def forbidden(*args):raise AssertionError("Public context must not inspect private queue state")
    monkeypatch.setattr(hermes_compat, "queued_execution", forbidden)
    assert (await adp.send("dm", "public-context")).success
    assert calls and len(api.deliveries) == 1


async def test_foreign_profile_execution_cannot_send_even_via_public_api(adapter):
    adp, api, _ = adapter
    enable(adp)
    adp.host = SimpleNamespace(current_cron_execution=lambda: SimpleNamespace(job_id="job", execution_id="run", profile="foreign"))
    assert not (await adp.send("dm", "forbidden")).success
    assert not api.requests
