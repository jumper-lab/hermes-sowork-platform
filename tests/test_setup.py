"""Real native secret hydration, REST pairing and config I/O in disposable profiles."""
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from hermes_yaml import safe_dump

from sowork import admin, client, cli, setup, setup_secrets
from sowork.config import DEFAULT_CREDENTIAL_ENV
from sowork.lock import SubscriptionLock
from e2e_support import SoWorkServer


@pytest.fixture
def rig(plugin_home, monkeypatch, tmp_path):
    for name in DEFAULT_CREDENTIAL_ENV.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "absent-managed"))
    home = plugin_home
    fake = tmp_path / "op"
    script = tmp_path / "fake_op.py"
    record = tmp_path / "native-secret-calls.jsonl"
    script.write_text("\n".join([
        "import os,sys,json",
        "assert os.environ.get('OP_SERVICE_ACCOUNT_TOKEN') == 'own-bootstrap'",
        "assert sys.argv[1] == 'read'",
        f"with open({str(record)!r},'a') as f: f.write(json.dumps({{'own_identity':True,'field':sys.argv[-1].split('/')[-1]}})+'\\n')",
        "print({'username':'test-client','credential':'test-secret','installation':'install'}[sys.argv[-1].split('/')[-1]])",
    ]))
    fake.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(script)) + ' "$@"\n')
    fake.chmod(0o700)
    cfg = {"plugins": {"enabled": ["sowork-platform"]},
        "model": {"default": "existing-model", "provider": "existing-provider"},
        "streaming": {"enabled": False, "transport": "auto"},
        "display": {"platforms": {"slack": {"streaming": True}, "buzz": {"show_reasoning": True}}},
        "platforms": {"slack": {"enabled": False}, "telegram": {"enabled": False}},
        "secrets": {"sources": ["onepassword"], "onepassword": {"enabled": True,
            "binary_path": str(fake), "service_account_token_env": "OP_SERVICE_ACCOUNT_TOKEN",
            "cache_ttl_seconds": 0, "env": {}}}}
    (home / "config.yaml").write_text("# preserve this comment\n" + safe_dump(cfg))
    (home / ".env").write_text("OP_SERVICE_ACCOUNT_TOKEN=own-bootstrap\n")
    peer = tmp_path / "peer";peer.mkdir()
    (peer / ".env").write_text("OP_SERVICE_ACCOUNT_TOKEN=foreign-bootstrap\n")
    (peer / "config.yaml").write_text("# other profile must remain byte-identical\n")
    api = SoWorkServer().start()
    monkeypatch.setattr(client, "BASE_URL", api.url)
    result = SimpleNamespace(home=home, before=copy.deepcopy(cfg), api=api, record=record, peer=peer)
    try:
        yield result
    finally:
        api.close()
        from hermes_cli.env_loader import reset_secret_source_cache
        reset_secret_source_cache(home)


class UI:
    def __init__(self, rig, *, deny=None, mutate=None):
        self.rig, self.deny, self.mutate = rig, deny, mutate
        self.output, self.confirmations = [], []

    def ask(self, prompt):
        field = {"SOWORK_USERNAME": "username", "SOWORK_CREDENTIAL": "credential", "SOWORK_INSTALLATION_ID": "installation"}
        return "op://" + "a"*26 + "/" + "b"*26 + "/" + field[prompt.split(" ")[0]]

    def reference(self, prompt):
        return self.ask(prompt)

    def write(self, text):
        self.output.append(text)
        if text.startswith("HERMES-PAIR-"):
            # A third party with any other code must never become the owner.
            self.rig.api.message("irrelevant", sender="another-human")
            self.rig.api.message(text)
            if self.mutate:
                self.mutate()

    def confirm(self, text, *, default=False):
        self.confirmations.append(text)
        return not (self.deny and self.deny in text)


async def run(rig, ui=None, **kwargs):
    return await setup.run_setup(rig.home, ui=ui or UI(rig), owner_id="human", pair_timeout=10, **kwargs)


async def test_setup_native_onepassword_real_http_pairing_and_preservation(rig):
    before_env = (rig.home / ".env").read_bytes()
    before_peer = {p.name:p.read_bytes() for p in rig.peer.iterdir()}
    ui = UI(rig)
    result = await run(rig, ui)
    after, _ = admin.read_config(rig.home)
    expected = copy.deepcopy(rig.before)
    expected["secrets"] = after["secrets"]
    expected["platforms"]["sowork"] = after["platforms"]["sowork"]
    expected["display"]["platforms"]["sowork"] = after["display"]["platforms"]["sowork"]
    assert after == expected
    assert after["platforms"]["sowork"]["extra"]["allow_from"] == ["human"]
    assert after["platforms"]["sowork"]["extra"]["threads_enabled"]
    assert not after["platforms"]["sowork"]["extra"]["preview_enabled"]
    assert not after["streaming"]["enabled"]
    assert (rig.home / ".env").read_bytes() == before_env
    assert {p.name:p.read_bytes() for p in rig.peer.iterdir()} == before_peer
    assert all(json.loads(l)["own_identity"] for l in rig.record.read_text().splitlines())
    assert "test-secret" not in json.dumps(result) + "".join(ui.output) + (rig.home / "config.yaml").read_text()
    assert not os.environ.get("SOWORK_CREDENTIAL")
    assert "# preserve this comment" in (rig.home / "config.yaml").read_text()
    assert result["restart_required"] and not result["receiver_started"] and result["messages_sent"] == 0
    assert len([r for r in rig.api.requests if r[0] == "POST"]) == 2  # OAuth + the exclusive subscription.
    assert len(rig.api.subscriptions) == 2 and "sub" in rig.api.subscriptions
    journal = rig.home / "sowork/setup.json"
    assert journal.stat().st_mode & 0o077 == 0
    assert json.loads(journal.read_text())["phase"] == "configured"
    before = (rig.home / "config.yaml").read_bytes()
    # Same public credential path on rerun; no second subscription or identity change.
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    scope = set_secret_scope({"SOWORK_USERNAME":"test-client", "SOWORK_CREDENTIAL":"test-secret", "SOWORK_INSTALLATION_ID":"install"}, profile_home=str(rig.home))
    try:
        second = await run(rig)
    finally:
        reset_secret_scope(scope)
    assert second["already_configured"] and not second["restart_required"]
    assert (rig.home / "config.yaml").read_bytes() == before
    assert len(rig.api.subscriptions) == 2


async def test_cancelled_pairing_resumes_owned_subscription_without_duplicate(rig):
    with pytest.raises(admin.AdminError, match="setup_cancelled_subscription_retained"):
        await run(rig, UI(rig, deny="Save this SoWork"))
    assert "sowork" not in admin.read_config(rig.home)[0]["platforms"]
    result = await run(rig)
    assert result["configured"] and len(rig.api.subscriptions) == 2


async def test_lost_create_receipt_never_retries_and_requires_explicit_recovery(rig):
    rig.api.fail_subscription_after_accept = True
    with pytest.raises(admin.AdminError, match="setup_api_failed_http_0"):
        await run(rig)
    assert len(rig.api.subscriptions) == 2
    with pytest.raises(admin.AdminError, match="creation_uncertain"):
        await run(rig)
    assert len(rig.api.subscriptions) == 2
    result = await run(rig, resume_subscription="setup-1")
    assert result["configured"] and len(rig.api.subscriptions) == 2


async def test_concurrent_config_write_during_pairing_is_not_overwritten(rig):
    def edit():
        c, _ = admin.read_config(rig.home);c["model"]["default"]="concurrent-change"
        (rig.home / "config.yaml").write_text(safe_dump(c))
    with pytest.raises(admin.AdminError, match="config_changed_review_again"):
        await run(rig, UI(rig, mutate=edit))
    after, _ = admin.read_config(rig.home)
    assert after["model"]["default"] == "concurrent-change"
    assert "sowork" not in after["platforms"]


@pytest.mark.parametrize("phase", ["references", "binding"])
async def test_concurrent_other_channel_write_during_native_serialization_survives(rig, monkeypatch, phase):
    import hermes_cli.config as native
    write = native.atomic_config_write
    changes = []
    live = rig.home / "config.yaml"
    def interleave(path, candidate, **kwargs):
        binding_write = "sowork" in candidate.get("platforms", {})
        if binding_write == (phase == "binding") and not changes:
            latest, _ = admin.read_config(rig.home)
            latest["platforms"]["slack"]["enabled"] = True
            # Use the actual live path even though preparation targets a private file.
            write(live, latest)
            changes.append(True)
        return write(path, candidate, **kwargs)
    monkeypatch.setattr(native, "atomic_config_write", interleave)
    with pytest.raises(admin.AdminError, match="config_changed_review_again"):
        await run(rig, UI(rig))
    assert changes
    after, _ = admin.read_config(rig.home)
    assert after["platforms"]["slack"]["enabled"] is True
    assert "sowork" not in after["platforms"]
    assert not list(rig.home.glob(".sowork-config-*"))


async def test_native_plugin_writer_in_another_process_preserves_setup(rig, monkeypatch, tmp_path):
    import hermes_cli.config as native
    write = native.atomic_config_write
    ready = tmp_path / "writer-ready"
    worker = tmp_path / "config_writer.py"
    worker.write_text("\n".join([
        "import fcntl,json,os",
        "from pathlib import Path",
        "from hermes_cli.plugins_state import save_plugin_setting",
        f"lock=Path({str(rig.home / '.config.yaml.lock')!r})",
        "fd=os.open(lock,os.O_RDWR)",
        "blocked=False",
        "try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)",
        "except BlockingIOError: blocked=True",
        "os.close(fd)",
        f"Path({str(ready)!r}).write_text(json.dumps({{'blocked':blocked}}))",
        "save_plugin_setting('other-plugin',('marker',),'after-setup')",
    ]))
    processes = []
    def prepare(path, candidate, **kwargs):
        if "sowork" in candidate.get("platforms", {}) and not processes:
            child = subprocess.Popen([sys.executable, str(worker)], cwd=Path.cwd(),
                env={**os.environ, "PYTHONPATH":str(Path.cwd())}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(child)
            deadline = time.monotonic()+10
            while not ready.exists() and time.monotonic()<deadline:
                time.sleep(.01)
            assert ready.exists(), "Native config writer did not reach the shared lock"
            assert json.loads(ready.read_text())["blocked"]
        return write(path, candidate, **kwargs)
    monkeypatch.setattr(native, "atomic_config_write", prepare)
    try:
        assert (await run(rig))["configured"]
        stdout, stderr = processes[0].communicate(timeout=15)
        assert processes[0].returncode == 0, stdout+stderr
        after, _ = admin.read_config(rig.home)
        assert admin.binding(after).allowed_user_id == "human"
        assert after["plugins"]["entries"]["other-plugin"]["settings"]["marker"] == "after-setup"
        assert after["model"] == rig.before["model"]
    finally:
        for child in processes:
            if child.poll() is None:
                child.kill();child.communicate()


@pytest.mark.parametrize("location", ["default", "named", "custom"])
async def test_activation_command_resolves_exact_configured_home(rig, monkeypatch, tmp_path, location):
    import shutil
    from hermes_cli.profiles import get_active_profile_name, resolve_profile_env
    operator = tmp_path / "operator home"
    root = operator / ".hermes"
    home = {"default":root, "named":root/"profiles/test-agent", "custom":root/"experiments/pilot"}[location]
    home.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(rig.home, home, symlinks=True)
    rig.home = home
    monkeypatch.setenv("HOME", str(operator))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE_NAME", "unrelated-inherited-name")
    assert get_active_profile_name() == {"default":"default", "named":"test-agent", "custom":"custom"}[location]
    result = await run(rig)
    command = result["restart_command"]
    assert command[:2] == ["env", "HERMES_HOME="+str(home)]
    # The instruction is safe even when pasted in a shell that selected another home.
    monkeypatch.setenv("HERMES_HOME", command[1].split("=",1)[1])
    selected = Path(resolve_profile_env(command[command.index("--profile")+1])).resolve()
    assert selected == home.resolve()
    assert "custom" not in command


async def test_setup_guard_before_foreign_profile_or_second_operator(rig):
    before = (rig.home / "config.yaml").read_bytes()
    with pytest.raises(admin.AdminError, match="active_profile"):
        await setup.run_setup(rig.peer, ui=UI(rig))
    lease = SubscriptionLock("setup:" + str(rig.home));assert lease.acquire()
    try:
        with pytest.raises(admin.AdminError, match="already_running"):
            await run(rig)
    finally:
        lease.release()
    assert (rig.home / "config.yaml").read_bytes() == before
    assert not rig.api.requests


async def test_setup_refuses_active_consumer_and_never_deletes_other_subscription(rig):
    lease = SubscriptionLock("install:sub");assert lease.acquire()
    try:
        with pytest.raises(admin.AdminError, match="active_consumer"):
            await run(rig, resume_subscription="sub")
    finally:
        lease.release()
    assert len(rig.api.subscriptions) == 1
    assert not any(r[0] == "DELETE" for r in rig.api.requests)
    assert "sowork" not in admin.read_config(rig.home)[0]["platforms"]


async def test_setup_cross_checks_membership_and_installation_owner(rig):
    rig.api.owner_user_id = "anotherhuman"
    with pytest.raises(admin.AdminError, match="owner_mismatch"):
        await run(rig)
    assert len(rig.api.subscriptions) == 1
    rig.api.owner_user_id = "human"
    rig.api.dm_participants.append({"id":"anotherhuman"})
    with pytest.raises(admin.AdminError, match="setup_api_failed_http_403"):
        await run(rig)
    assert "sowork" not in admin.read_config(rig.home)[0]["platforms"]


async def test_managed_policy_appearing_during_pairing_blocks_commit(rig, monkeypatch):
    managed = rig.home.parent/"managed-later"
    def edit():
        managed.mkdir();(managed/"config.yaml").write_text("{}")
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    with pytest.raises(admin.AdminError, match="managed"):
        await run(rig, UI(rig, mutate=edit))
    assert "sowork" not in admin.read_config(rig.home)[0]["platforms"]


async def test_missing_native_contract_rejects_before_credentials(rig, monkeypatch):
    from sowork import runtime
    def forbidden(*args, **kwargs):raise AssertionError("No credentials")
    monkeypatch.setattr(setup,"credentials",forbidden)
    monkeypatch.setattr(runtime,"missing_contracts",lambda:["missing-native-contract"])
    with pytest.raises(RuntimeError, match="missing-native-contract"):
        await run(rig)
    assert not rig.api.requests


@pytest.mark.parametrize("kind", ["symlink", "managed", "wildcard", "noninteractive"])
async def test_setup_fail_closed_before_credentials_or_network(rig, monkeypatch, kind):
    def forbidden(*args, **kwargs):raise AssertionError("No credentials")
    monkeypatch.setattr(setup, "credentials", forbidden)
    if kind == "symlink":
        (rig.home / "sowork").symlink_to(rig.peer, target_is_directory=True)
        with pytest.raises(admin.AdminError, match="symlink"):await run(rig)
    elif kind == "managed":
        managed=rig.home.parent/"managed";managed.mkdir();(managed/"config.yaml").write_text("{}")
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
        with pytest.raises(admin.AdminError, match="managed"):await run(rig)
    elif kind == "wildcard":
        with pytest.raises(admin.AdminError, match="one_explicit_human"):
            await setup.run_setup(rig.home, ui=UI(rig), owner_id="*")
    else:
        assert cli.main(["--json", "setup"]) == 2
    assert not rig.api.requests


@pytest.mark.parametrize("change", ["sender", "group", "workspace", "installation", "thread", "stale", "self", "nonce", "future"])
def test_pairing_nonce_never_approves_wrong_origin(change):
    api = SoWorkServer()
    e = api.message("CODE")
    identity = {"installation":{"id":"install"}, "group":{"id":"workspace"}, "appUser":{"id":"app"}}
    started = time.time()
    assert setup.paired_candidate(e, identity, "CODE", started, "human")
    if change == "sender":e["data"]["message"]["sender"]["id"]="someone-else"
    elif change == "group":e["data"]["memberCount"]=3
    elif change == "workspace":e["officeId"]="other-workspace"
    elif change == "installation":e["installationId"]="other-installation"
    elif change == "thread":e["data"]["message"]["parentId"]="some-thread"
    elif change == "stale":e["createdAt"]-=3600
    elif change == "self":e["data"]["message"]["sender"]["id"]="app"
    elif change == "nonce":e["data"]["message"]["text"]="wrong-code"
    else:e["createdAt"]+=3600
    assert setup.paired_candidate(e, identity, "CODE", started, "human") is None


@pytest.mark.parametrize("value", ["plaintext-secret", "https://start.1password.com/open/item", "op://vault/item", "op://v/i/field\nvalue"])
def test_reference_prompt_refuses_values_and_non_field_links(value):
    with pytest.raises(admin.AdminError):setup_secrets.validate_reference(value)


def test_console_labels_remove_terminal_controls():
    assert "\x1b" not in setup.label("Newton\x1b[31m\n")


@pytest.mark.spawns_gateway_lookalike
def test_native_cli_setup_from_empty_profile_over_a_real_terminal(rig, tmp_path):
    import pty
    import re
    import select
    import shutil
    shim=tmp_path/"shim";shim.mkdir()
    shutil.copyfile(Path(__file__).with_name("e2e_sitecustomize.py"),shim/"sitecustomize.py")
    environment={**os.environ, "HOME":str(tmp_path), "HERMES_HOME":str(rig.home),
                 "PYTHONPATH":str(shim)+os.pathsep+str(Path.cwd()), "SOWORK_TEST_API":rig.api.url}
    master,slave=pty.openpty()
    process=subprocess.Popen([sys.executable,"-m","hermes_cli.main","sowork","setup","--owner-id","human"],
        stdin=slave,stdout=slave,stderr=slave,env=environment,cwd=Path.cwd())
    os.close(slave)
    output="";sent=set();paired=False;deadline=time.monotonic()+90
    responses={"SOWORK_USERNAME — paste":"username", "SOWORK_CREDENTIAL — paste":"credential",
               "SOWORK_INSTALLATION_ID — paste":"installation"}
    try:
        while time.monotonic()<deadline:
            if select.select([master],[],[],.2)[0]:
                try:chunk=os.read(master,65536)
                except OSError:break
                if not chunk:break
                output+=chunk.decode(errors="replace")
            for prompt,field in responses.items():
                if prompt in output and prompt not in sent:
                    os.write(master,("op://"+"a"*26+"/"+"b"*26+"/"+field+"\n").encode());sent.add(prompt)
            for prompt in ("Save these three references", "Enable isolated threads", "Create ONE exclusive", "Save this SoWork connection"):
                if prompt in output and prompt not in sent:
                    os.write(master,b"yes\n");sent.add(prompt)
            match=re.search(r"HERMES-PAIR-[a-f0-9]{16}",output)
            if match and not paired:
                rig.api.message(match.group());paired=True
            if process.poll() is not None:break
        assert process.wait(timeout=5)==0,output[-7000:]
        assert paired
        assert admin.binding(admin.read_config(rig.home)[0]).allowed_user_id=="human"
        assert len(rig.api.subscriptions)==2
        assert not rig.api.visible() and not rig.api.stream_cursors
        assert "Configured. Activate using the native gateway lifecycle" in output
        assert "test-secret" not in output and "own-bootstrap" not in output
    finally:
        if process.poll() is None:process.kill();process.wait()
        os.close(master)
