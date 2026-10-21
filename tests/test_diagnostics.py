"""Exercise native config I/O, offline entry and the explicit read-only API path."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
import pytest

from sowork import admin, diagnostics
from sowork.client import Client, REQUIRED_SCOPES, THREAD_EVENT, DM_EVENT
from sowork.config import Credentials
from sowork.state import Store


@pytest.fixture
def profile(tmp_path, monkeypatch):
    from hermes_yaml import safe_dump
    home = tmp_path / "own-profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "absent-managed"))
    cfg = {"model": {"provider": "test", "default": "test"},
        "streaming": {"enabled": True, "transport": "auto"},
        "display": {"platforms": {"sowork": {"streaming": True}, "slack": {"streaming": False}}},
        "secrets": {"onepassword": {"binary_path": "/usr/local/bin/op", "env": {
            "SOWORK_USERNAME": "op://vault/item/user", "SOWORK_CREDENTIAL": "op://vault/item/secret",
            "SOWORK_INSTALLATION_ID": "op://vault/item/installation"}}},
        "platforms": {"slack": {"enabled": True, "extra": {"allow_from": ["slack-owner"]}},
            "sowork": {"enabled": True, "extra": {"subscription_id": "sub", "workspace_id": "workspace",
                "app_user_id": "app", "allowed_user_id": "human", "channel_id": "dm", "allow_from": ["human"],
                **{x + "_enabled": True for x in admin.FEATURES}}}}}
    (home / "config.yaml").write_text("# keep this comment\n" + safe_dump(cfg))
    return home, cfg


def write_config(home, cfg):
    from hermes_yaml import safe_dump
    (home / "config.yaml").write_text(safe_dump(cfg))


def test_offline_inspection_never_resolves_secrets_or_creates_state(profile, monkeypatch):
    home, cfg = profile
    def forbidden(*a, **k):
        raise AssertionError("External side effect")
    monkeypatch.setattr(admin, "fetch_credentials", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    (home / ".env").write_text("SOWORK_CREDENTIAL=PRIVATE-SENTINEL\n")
    before = {str(p): p.read_bytes() for p in home.iterdir()}
    report = diagnostics.inspect(home)
    assert report["credentials_resolved"] is False and report["local_only"]
    assert report["installed_plugin_version"] != "unknown"
    assert report["binding"]["allowed_user_ids"] == ["human"]
    assert report["state"] is None
    assert "PRIVATE-SENTINEL" not in json.dumps(report)
    assert {str(p): p.read_bytes() for p in home.iterdir()} == before


def test_fatal_receiver_is_reported_as_fatal_in_fresh_native_snapshot(profile, monkeypatch):
    home, _ = profile
    monkeypatch.setattr("gateway.status.get_runtime_status_running_pid", lambda data, *, expected_home: os.getpid())
    (home / "gateway_state.json").write_text(json.dumps({
        "pid": os.getpid(), "hermes_home": str(home),
        "platforms": {"sowork": {"state": "fatal"}},
    }))
    report = diagnostics.inspect(home)
    assert report["gateway"]["state"] == "fatal"
    assert report["gateway"]["fresh"]
    assert "gateway_status_unconfirmed" in report["issues"]


def test_live_and_checkpointed_state_read_only_with_pending_review(profile):
    home, cfg = profile
    path = home / "sowork/state.sqlite3"
    store = Store(path, "install:sub")
    now = time.time()
    store.observed(now=now-72*3600)
    store.prepare_response("turn", 0, "dm", ["SECRET-BODY"])
    with store.db:
        store.db.execute("UPDATE outbox SET created=?", (now-3601,))
        store.db.execute("INSERT INTO inbox VALUES ('mid','SECRET-PAYLOAD','uncertain',?,NULL)", (now,))
    before = list(store.db.iterdump())
    report = diagnostics.inspect(home, now=now)
    assert report["state"]["pending_deliveries"] == 1
    assert {"retention_reconciliation_required", "delivery_reconciliation_required", "inbox_failure_review"} <= set(report["issues"])
    assert "SECRET-" not in json.dumps(report)
    assert list(store.db.iterdump()) == before
    store.close()
    files = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    assert diagnostics.inspect(home, now=now)["state"]["pending_deliveries"] == 1
    assert {p.name: p.read_bytes() for p in path.parent.iterdir()} == files


def test_multiple_state_scopes_and_corruption_require_review_without_repair(profile):
    home, _ = profile
    directory = home / "sowork"
    for name in ("a", "b"):
        store = Store(directory / (name + ".sqlite3"), name + ":sub")
        store.close()
    files = {p.name: p.read_bytes() for p in directory.iterdir()}
    assert "state_unreadable_or_ambiguous" in diagnostics.inspect(home)["issues"]
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == files
    (directory / "a.sqlite3").write_text("broken state")
    assert "state_unreadable_or_ambiguous" in diagnostics.inspect(home)["issues"]
    assert (directory / "a.sqlite3").read_text() == "broken state"


def test_gateway_snapshot_needs_live_pid_own_home_and_freshness(profile, monkeypatch):
    home, _ = profile
    def native_identity(data, *, expected_home):
        assert expected_home == home
        return os.getpid() if data.get("hermes_home") == str(home) else None
    monkeypatch.setattr("gateway.status.get_runtime_status_running_pid", native_identity)
    path = home / "gateway_state.json"
    state = {"pid": os.getpid(), "hermes_home": str(home), "platforms": {"sowork": {"state": "connected"}}}
    path.write_text(json.dumps(state))
    assert diagnostics.gateway_summary(home, time.time())["state"] == "connected"
    assert diagnostics.gateway_summary(home, time.time()+121)["state"] == "unknown"
    state["hermes_home"] = "another-profile";path.write_text(json.dumps(state))
    assert diagnostics.gateway_summary(home, time.time())["state"] == "unknown"


def test_gateway_snapshot_requires_native_process_identity_even_when_pid_exists(profile, monkeypatch):
    home, _ = profile
    (home / "gateway_state.json").write_text(json.dumps({
        "pid": os.getpid(), "hermes_home": str(home), "platforms": {"sowork": {"state": "connected"}},
    }))
    monkeypatch.setattr("gateway.status.get_runtime_status_running_pid", lambda data, *, expected_home: None)
    assert diagnostics.gateway_summary(home, time.time())["state"] == "unknown"


@pytest.mark.parametrize("feature", admin.EDITABLE)
def test_config_native_roundtrip_changes_only_selected_sowork_values(profile, feature):
    home, before = profile
    config_bytes = (home / "config.yaml").read_bytes()
    plan = admin.configure(home, feature, False)
    assert not plan["applied"] and plan["changed"]
    assert (home / "config.yaml").read_bytes() == config_bytes
    result = admin.configure(home, feature, False, apply=True, expected_sha=plan["config_sha256"])
    expected = copy.deepcopy(before)
    expected["platforms"]["sowork"]["extra"][feature + "_enabled"] = False
    if feature == "preview":
        expected["display"]["platforms"]["sowork"]["streaming"] = False
    assert admin.read_config(home)[0] == expected
    assert "# keep this comment" in (home / "config.yaml").read_text()
    assert Path(result["backup"]).read_bytes() == config_bytes
    assert Path(result["backup"]).stat().st_mode & 0o077 == 0
    assert result["restart_required"]  # CLI does not restart a service.
    bytes_after = (home / "config.yaml").read_bytes()
    assert not admin.configure(home, feature, False, apply=True, expected_sha=result["config_sha256"])["applied"]
    assert (home / "config.yaml").read_bytes() == bytes_after


def test_config_requires_review_checksum_and_refuses_global_streaming_change(profile):
    home, cfg = profile
    before = (home / "config.yaml").read_bytes()
    with pytest.raises(admin.AdminError, match="config_changed_review_again"):
        admin.configure(home, "media", False, apply=True, expected_sha="wrong")
    assert (home / "config.yaml").read_bytes() == before
    cfg["streaming"]["enabled"] = False;write_config(home, cfg)
    with pytest.raises(admin.AdminError, match="preview_requires_existing_safe_global_streaming"):
        admin.configure(home, "preview", True)


def test_feature_write_does_not_overwrite_concurrent_channel_edit(profile, monkeypatch):
    import hermes_cli.config as native
    home, _ = profile
    write = native.atomic_config_write
    changed = []
    def interleave(path, candidate, **kwargs):
        if not changed:
            latest, _ = admin.read_config(home)
            latest["platforms"]["slack"]["enabled"] = False
            write(home/"config.yaml", latest)
            changed.append(True)
        return write(path, candidate, **kwargs)
    monkeypatch.setattr(native, "atomic_config_write", interleave)
    plan = admin.configure(home, "media", False)
    with pytest.raises(admin.AdminError, match="config_changed_review_again"):
        admin.configure(home, "media", False, apply=True, expected_sha=plan["config_sha256"])
    after, _ = admin.read_config(home)
    assert after["platforms"]["slack"]["enabled"] is False
    assert after["platforms"]["sowork"]["extra"]["media_enabled"] is True


def test_staging_failure_keeps_live_config_and_removes_temporary_files(profile, monkeypatch):
    home, _ = profile
    before = (home/"config.yaml").read_bytes()
    def fail(*args, **kwargs):raise OSError("synthetic staging failure")
    monkeypatch.setattr("hermes_cli.config.atomic_config_write", fail)
    plan = admin.configure(home, "media", False)
    with pytest.raises(OSError):
        admin.configure(home, "media", False, apply=True, expected_sha=plan["config_sha256"])
    assert (home/"config.yaml").read_bytes() == before
    assert not list(home.glob(".sowork-config-*"))


@pytest.mark.parametrize("change", ["allowlist", "app", "home"])
def test_admin_refuses_ambiguous_access_binding(profile, change):
    home, cfg = profile
    platform = cfg["platforms"]["sowork"]
    if change == "allowlist":platform["extra"]["allow_from"].append("other")
    elif change == "app":platform["extra"]["app_user_id"] = "human"
    else:platform["home_channel"] = {"chat_id": "other"}
    write_config(home, cfg)
    with pytest.raises(admin.AdminError):admin.configure(home, "media", False)
    with pytest.raises(admin.AdminError):diagnostics.inspect(home)


def test_managed_overlay_never_silently_bypassed(profile, tmp_path, monkeypatch):
    home, _ = profile
    managed = tmp_path / "managed";managed.mkdir()
    (managed / "config.yaml").write_text("platforms: {}\n")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    assert "managed_config_requires_native_review" in diagnostics.inspect(home)["issues"]
    with pytest.raises(admin.AdminError, match="managed_config"):
        admin.configure(home, "media", False)


def test_credentials_use_public_route_and_never_open_credential_files(profile, monkeypatch):
    home, cfg = profile
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    from sowork.config import Credentials
    values = {"SOWORK_USERNAME": "client", "SOWORK_CREDENTIAL": "secret", "SOWORK_INSTALLATION_ID": "install"}
    token = set_secret_scope(values, profile_home=str(home))
    def forbidden(*args, **kwargs):raise AssertionError("Plugin must not read credential files")
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    try:
        assert admin.fetch_credentials(home, cfg, admin.binding(cfg)) == Credentials("client", "secret", "install")
    finally:reset_secret_scope(token)


def test_foreign_home_or_bound_miss_cannot_borrow_process_credentials(profile, monkeypatch):
    home, cfg = profile
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    monkeypatch.setenv("SOWORK_CREDENTIAL", "foreign-secret")
    for values, scoped_home, requested in [
        ({"SOWORK_USERNAME": "client", "SOWORK_INSTALLATION_ID": "install"}, home, home),
        ({"SOWORK_USERNAME": "client", "SOWORK_CREDENTIAL": "secret", "SOWORK_INSTALLATION_ID": "install"}, home.parent/"foreign", home),
        ({"SOWORK_USERNAME": "client", "SOWORK_CREDENTIAL": "secret", "SOWORK_INSTALLATION_ID": "install"}, home, home.parent/"foreign")]:
        token=set_secret_scope(values, profile_home=str(scoped_home))
        try:
            with pytest.raises(admin.AdminError, match="active_profile_credentials_required"):
                admin.fetch_credentials(requested, cfg, admin.binding(cfg))
        finally:reset_secret_scope(token)


@pytest.mark.parametrize("failure", [None, "identity", "subscription", "members", "scopes"])
async def test_explicit_connection_checks_real_client_path_and_only_gets(profile, monkeypatch, failure):
    home, cfg = profile
    from sowork import client as client_module
    requests = []
    def api(request):
        requests.append((request.method, request.url.path))
        path = request.url.path.removeprefix("/public")
        if path == "/oauth/token":return httpx.Response(200, json={"access_token": "TEST-PRIVATE", "expires_in": 3600})
        assert request.method == "GET"
        if path == "/v1/app":return httpx.Response(200, json={
            "installation": {"id": "install", "state": "active", "consentedScopes": list(REQUIRED_SCOPES) + ([] if failure == "scopes" else ["chat.history", "events.subscribe:" + THREAD_EVENT])},
            "group": {"id": "wrong" if failure == "identity" else "workspace"}, "appUser": {"id": "app"}})
        if path == "/v1/subscriptions":return httpx.Response(200, json={"webhooks": [{"id": "sub", "officeId": "workspace", "creator": {"id": "app"}, "events": [DM_EVENT, THREAD_EVENT], "url": "https://invalid" if failure == "subscription" else None}]})
        if path == "/v1/chat/dms":return httpx.Response(200, json={"conversations": [{"id": "dm", "type": "direct", "participants": [{"id": "other" if failure == "members" else "human"}]}]})
        raise AssertionError(path)
    monkeypatch.setattr(admin, "fetch_credentials", lambda *a: Credentials("client", "secret", "install"))
    monkeypatch.setattr(client_module, "Client", lambda creds: Client(creds, transport=httpx.MockTransport(api)))
    if failure:
        with pytest.raises(admin.AdminError):await admin.check_connection(home)
    else:
        result = await admin.check_connection(home)
        assert result["dm_membership_verified"] and result["allowed_user_ids"] == ["human"]
        assert not result["receiver_started"] and result["messages_sent"] == 0
    assert all(method == "GET" or path.endswith("/oauth/token") for method, path in requests)
    assert not (home / "sowork").exists()


def test_direct_cli_offline_entry_and_sanitized_errors(profile):
    home, _ = profile
    root = Path(__file__).parents[1]
    # Real subprocess/imports. A bogus 1Password executable proves no bootstrap.
    env = dict(os.environ, HERMES_HOME=str(home), OP_SERVICE_ACCOUNT_TOKEN="PRIVATE-SENTINEL")
    source = Path(__file__).resolve().parents[3]
    env["PYTHONPATH"] = str(source)
    result = subprocess.run([sys.executable, str(root / "soworkctl.py"), "--json", "status"], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["credentials_resolved"] is False
    assert "PRIVATE-SENTINEL" not in result.stdout + result.stderr
    (home / "config.yaml").write_text('secret: "PRIVATE-SENTINEL\n')
    result = subprocess.run([sys.executable, str(root / "soworkctl.py"), "--json", "status"], env=env, text=True, capture_output=True)
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"] == "config_unreadable_or_invalid"
    assert "PRIVATE-SENTINEL" not in result.stdout + result.stderr


def test_native_plugin_registers_cli_without_changing_platform_contract(plugin_home):
    from hermes_cli.plugins import PluginManager
    manager = PluginManager()
    try:
        manager.discover_and_load()
        commands = manager._cli_commands
        assert "sowork" in commands
        import argparse
        parser = argparse.ArgumentParser()
        commands["sowork"]["setup_fn"](parser)
        assert parser.parse_args(["doctor"]).sowork_command == "doctor"
    finally:
        manager.unload()
