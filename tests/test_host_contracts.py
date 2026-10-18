"""Host API drift must stop startup before any secret lookup or network."""
from types import SimpleNamespace

import pytest

from sowork import hermes_compat, plugin, runtime
from test_contracts import adapter  # noqa: F401
from test_proactive import enable


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
