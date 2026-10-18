"""Publication contracts: compatibility before secrets and no private updater."""
import argparse
import importlib.util
from pathlib import Path

import pytest

from sowork import cli, plugin, runtime


def test_unsupported_runtime_rejects_before_adapter_or_credential_lookup(monkeypatch):
    monkeypatch.setattr(runtime, "missing_contracts", lambda: ["missing-gateway-contract"])
    with pytest.raises(RuntimeError, match="missing-gateway-contract"):
        plugin.build_adapter(None)


def test_installed_runtime_has_required_native_contracts():
    runtime.require_supported_runtime()


@pytest.mark.parametrize("name", ["maintenance", "release"])
def test_catalog_distribution_has_no_self_update_command(name):
    parser=argparse.ArgumentParser()
    cli.setup_parser(parser)
    with pytest.raises(SystemExit):parser.parse_args([name])


def test_runtime_check_needs_no_profile_or_credentials(tmp_path, monkeypatch, capsys):
    from sowork import admin
    def forbidden(*args, **kwargs):raise AssertionError("No credential/config lookup")
    monkeypatch.setattr(admin, "fetch_credentials", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    assert cli.main(["--home", str(tmp_path/"absent"), "--json", "check-runtime"])==0
    assert '"supported": true' in capsys.readouterr().out


def test_public_export_keeps_only_reviewable_distribution_and_refuses_overwrite(tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("public_export", root / "scripts/export_public.py")
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    destination = tmp_path / "public"
    result = exporter.export(root, destination)
    assert (destination / "sowork/plugin.py").is_file()
    assert (destination / "tests/test_contracts.py").is_file()
    assert (destination / "LICENSE").is_file()
    assert result["files"] > 0
    for name in (".git", "activation", "evidence", ".env", "sowork/maintenance.py", "sowork/pilot.py"):
        assert not (destination / name).exists()
    with pytest.raises(ValueError, match="must be new"):
        exporter.export(root, destination)
