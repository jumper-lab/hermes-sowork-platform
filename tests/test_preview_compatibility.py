"""Use real core selectors and installed provider discovery, without credentials or clients."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_diagnostics import profile, write_config  # noqa: F401
from sowork import compatibility


def install_provider(home, *, name="preview-process", declaration="True", extra="", auth="api_key", base="https://example.invalid"):
    directory = home / "plugins" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.yaml").write_text(f"name: {name}\nkind: model-provider\n")
    (directory / "__init__.py").write_text(f'''
from providers import register_provider
from providers.base import ProviderProfile
class Profile(ProviderProfile):
    def create_client(self, **kwargs):
        raise AssertionError('diagnostics must never create a client')
    def fetch_models(self, **kwargs):
        raise AssertionError('diagnostics must never fetch a catalog')
p = Profile(name={name!r}, auth_type={auth!r}, base_url={base!r})
p.supports_streaming = {declaration}
{extra}
register_provider(p)
''')
    return directory


def routes_db(home, entries=()):
    from hermes_state import SessionDB
    db = SessionDB(home / "state.db")
    for suffix, override, changes in entries:
        key = "agent:main:sowork:dm:dm" + (":" + suffix if suffix else "")
        entry = {"origin": {"platform": "sowork", "chat_id": "dm", "user_id": "human",
                            "thread_id": suffix or None}, "model_override": override, **changes}
        db.save_gateway_routing_entry(key, json.dumps(entry), scope=str(home / "sessions"))
    db.close()


@pytest.fixture
def configured(profile):  # noqa: F811 - imported pytest fixture
    home, config = profile
    config["model"] = {"provider": "preview-process", "default": "fixture-model"}
    write_config(home, config)
    return home, config


def fingerprint(home):
    return {str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in home.rglob("*") if p.is_file()}


@pytest.mark.parametrize("declaration,status,reason", [
    ("True", "compatible", "core_allows_streaming"),
    ("False", "blocked", "provider_streaming_not_declared"),
    ("None", "blocked", "provider_streaming_not_declared")])
def test_real_external_process_selector_respects_declaration_without_writes(configured, declaration, status, reason):
    home, _ = configured
    directory = install_provider(home, declaration=declaration, auth="external_process", base="process://fixture")
    # Compare the disposable diagnostic with the real native selector. A provider
    # declaration cannot override a core that does not support this transport.
    import runpy
    runpy.run_path(str(directory / "__init__.py"))
    from agent.turn_api_call import _should_stream
    from types import SimpleNamespace
    allowed = _should_stream(SimpleNamespace(provider="preview-process", model="fixture-model",
        base_url="process://fixture", _disable_streaming=False, _has_stream_consumers=lambda: True, client=None))
    status = "compatible" if allowed else "blocked"
    reason = "core_allows_streaming" if allowed else ("core_rejects_provider_streaming" if declaration == "True" else "provider_streaming_not_declared")
    routes_db(home)
    before = fingerprint(home)
    result = compatibility.check_preview(home)
    assert result["status"] == status, result
    assert result["routes"][0]["reason"] == reason
    assert result["credentials_resolved"] is False and result["generation_tested"] is False
    assert result["live_agent_state_checked"] is False
    assert fingerprint(home) == before


def test_persisted_routes_are_scoped_to_this_human_dm_and_home(configured):
    home, config = configured
    install_provider(home)
    install_provider(home, name="http-fixture", declaration="None", auth="api_key", base="https://example.invalid")
    routes_db(home, [("A", None, {}), ("B", {"provider": "http-fixture", "model": "other-model"}, {}),
        ("outsider", {"provider": "PRIVATE-SENTINEL"},
         {"origin": {"platform": "sowork", "chat_id": "different", "user_id": "other"}})])
    result = compatibility.check_preview(home)
    assert result["status"] == "compatible", result
    assert [(r["kind"], r["thread_id"], r["provider"]) for r in result["routes"]] == [
        ("profile_default", None, "preview-process"), ("thread", "A", "preview-process"), ("thread", "B", "http-fixture")]
    assert "PRIVATE-SENTINEL" not in json.dumps(result)


@pytest.mark.parametrize("section,value,reason", [
    ("global", False, "preview_streaming_configuration_blocked"),
    ("transport", "edit", "preview_streaming_configuration_blocked"),
    ("platform", False, "preview_streaming_configuration_blocked"),
    ("model", False, "model_streaming_disabled")])
def test_configuration_gates_match_real_native_selector(configured, section, value, reason):
    home, config = configured
    install_provider(home);routes_db(home)
    if section == "global":config["streaming"]["enabled"] = value
    elif section == "transport":config["streaming"]["transport"] = value
    elif section == "platform":config["display"]["platforms"]["sowork"]["streaming"] = value
    else:config["model"]["streaming"] = value
    write_config(home, config)
    result = compatibility.check_preview(home)
    assert result["status"] == "blocked", result
    assert reason in result["issues"]


def test_acp_remains_blocked_even_when_provider_declares_streaming(configured):
    home, config = configured
    install_provider(home);routes_db(home)
    config["model"]["base_url"] = "acp+tcp://user:PRIVATE-SENTINEL@localhost:9999?secret=PRIVATE-SENTINEL"
    write_config(home, config)
    result = compatibility.check_preview(home)
    assert result["status"] == "blocked"
    assert result["routes"][0]["reason"] == "acp_streaming_blocked"
    assert "PRIVATE-SENTINEL" not in json.dumps(result)


@pytest.mark.parametrize("provider", ["zai", "openai-codex", "xai-oauth"])
def test_native_http_and_oauth_routes_need_no_credentials(configured, provider):
    home, config = configured
    config["model"]["provider"] = provider
    write_config(home, config);routes_db(home)
    result = compatibility.check_preview(home)
    assert result["status"] == "compatible", result
    assert result["credentials_resolved"] is False and result["generation_tested"] is False


def test_missing_provider_or_session_state_reports_unknown(configured):
    home, _ = configured
    result = compatibility.check_preview(home)
    assert result["status"] == "unknown"
    assert "preview_session_routes_unavailable" in result["issues"]
    assert result["routes"][0]["reason"] == "preview_provider_check_unavailable"
    assert not (home / "state.db").exists()


def test_symlink_state_and_unconfirmed_sender_do_not_expand_binding(configured, tmp_path):
    home, config = configured
    install_provider(home)
    routes_db(home, [("B", {"model":"PRIVATE-SENTINEL"},
        {"origin":{"platform":"sowork","chat_id":"dm","user_id":"other","thread_id":"B"}})])
    result = compatibility.check_preview(home)
    assert result["status"] == "unknown" and "preview_session_binding_unconfirmed" in result["issues"]
    assert "PRIVATE-SENTINEL" not in json.dumps(result)
    target=tmp_path / "elsewhere.db"
    (home / "state.db").rename(target);(home / "state.db").symlink_to(target)
    result = compatibility.check_preview(home)
    assert result["status"] == "unknown" and "preview_session_routes_unavailable" in result["issues"]


def test_concurrent_config_change_requires_review(configured, monkeypatch):
    home, config = configured
    install_provider(home);routes_db(home)
    original = compatibility.probe
    def changed(*args):
        result = original(*args)
        config["model"]["streaming"] = False;write_config(home, config)
        return result
    monkeypatch.setattr(compatibility, "probe", changed)
    result = compatibility.check_preview(home)
    assert result["status"] == "unknown" and "config_changed_review_again" in result["issues"]


def test_managed_overlay_and_preview_off_do_not_load_providers(configured, monkeypatch):
    home, config = configured
    monkeypatch.setattr(compatibility, "probe", lambda *args: pytest.fail("unexpected provider import"))
    config["platforms"]["sowork"]["extra"]["preview_enabled"] = False
    write_config(home, config)
    assert compatibility.check_preview(home)["status"] == "disabled"
    monkeypatch.setattr(compatibility, "managed_config_present", lambda: True)
    assert compatibility.check_preview(home)["issues"] == ["managed_config_requires_native_review"]


@pytest.mark.parametrize("operation", ["network", "process", "secrets", "write"])
def test_import_side_effects_are_blocked_without_leaking_secrets(configured, monkeypatch, operation):
    home, _ = configured
    (home / ".env").write_text("OP_SERVICE_ACCOUNT_TOKEN=PRIVATE-SENTINEL\n")
    marker = home / "must-not-write"
    code = {
        "network": "import socket; socket.create_connection(('127.0.0.1', 9))",
        "process": "import subprocess; subprocess.run(['op', '--version'])",
        "secrets": f"open({str(home / '.env')!r}).read()",
        "write": f"open({str(marker)!r}, 'w').write('oops')"}[operation]
    install_provider(home, extra=code)
    routes_db(home)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "PRIVATE-SENTINEL")
    before = fingerprint(home)
    result = compatibility.check_preview(home)
    assert result["status"] == "unknown", result
    assert result["import_side_effects_blocked"]
    assert "PRIVATE-SENTINEL" not in json.dumps(result)
    assert fingerprint(home) == before and not marker.exists()


def test_timeout_is_bounded_and_sanitized(configured, monkeypatch):
    home, _ = configured
    install_provider(home, extra="import time;time.sleep(60)");routes_db(home)
    monkeypatch.setattr(compatibility, "PROBE_TIMEOUT", 2)
    result = compatibility.check_preview(home)
    assert result["status"] == "unknown" and "preview_probe_timeout" in result["issues"]


def test_probe_never_inherits_bootstrap_or_transport_credentials(configured, monkeypatch):
    home, _ = configured
    for key in ("OP_SERVICE_ACCOUNT_TOKEN", "SOWORK_CREDENTIAL", "ANTHROPIC_API_KEY", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(key, "PRIVATE-SENTINEL")
    install_provider(home, extra="import os\nassert not any(k in os.environ for k in ('OP_SERVICE_ACCOUNT_TOKEN','SOWORK_CREDENTIAL','ANTHROPIC_API_KEY','SSH_AUTH_SOCK'))")
    routes_db(home)
    result = compatibility.check_preview(home)
    assert result["status"] == "compatible", result
    assert "PRIVATE-SENTINEL" not in json.dumps(result)


def test_disabled_thread_routes_do_not_override_dm_compatibility(configured):
    home, config = configured
    install_provider(home);routes_db(home, [("B", {"provider":"unknown-thread-provider"}, {})])
    config["platforms"]["sowork"]["extra"]["threads_enabled"] = False;write_config(home, config)
    result = compatibility.check_preview(home)
    assert result["status"] == "compatible" and len(result["routes"]) == 1, result


@pytest.mark.parametrize("sidecar", ["-wal", "-shm"])
def test_native_sidecar_symlinks_are_not_read(configured, tmp_path, sidecar):
    home, config = configured
    install_provider(home);routes_db(home)
    target=tmp_path/'sensitive';target.write_text('PRIVATE-SENTINEL')
    (home/('state.db'+sidecar)).symlink_to(target)
    result=compatibility.check_preview(home)
    assert result["status"] == "unknown" and "preview_session_routes_unavailable" in result["issues"]
    assert target.read_text() == 'PRIVATE-SENTINEL' and "PRIVATE-SENTINEL" not in json.dumps(result)


def test_cli_doctor_and_check_preview_return_nonzero_for_incompatibility(configured):
    home, _ = configured
    install_provider(home, declaration="False", auth="external_process");routes_db(home)
    root = Path(__file__).parents[1]
    source = Path(__file__).resolve().parents[3]
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONPATH=str(source), OP_SERVICE_ACCOUNT_TOKEN="PRIVATE-SENTINEL")
    for command in ("check-preview", "doctor"):
        result = subprocess.run([sys.executable, str(root / "soworkctl.py"), "--json", command],
                                env=env, text=True, capture_output=True, timeout=25)
        assert result.returncode == 1, result.stderr + result.stdout
        report = json.loads(result.stdout)
        if command == "doctor":report = report["preview_compatibility"]
        assert report["status"] == "blocked"
        assert "PRIVATE-SENTINEL" not in result.stdout + result.stderr
