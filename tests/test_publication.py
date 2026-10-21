"""Publication contracts: compatibility before secrets and no private updater."""
import argparse
import importlib.util
from pathlib import Path
import shutil
import tomllib

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


def test_native_dependency_workspace_keeps_profile_versions_distinct(tmp_path):
    """Two profile copies must coexist, including an older buildable release."""
    import tomli_w
    from pm.workspace import _generate_pyproject

    source = Path(__file__).resolve().parents[1]
    core = tmp_path / "core"
    core.mkdir()
    (core / "pyproject.toml").write_text('[project]\nname="test-core"\nversion="0.0.0"\n')
    copies = []
    original = (source / "pyproject.toml").read_bytes()
    for profile in ("first", "second", "legacy"):
        target = tmp_path / profile / "plugins/sowork-platform"
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        copies.append(target)
    legacy = tomllib.loads(original.decode())
    legacy["build-system"] = {"requires": ["setuptools>=77,<81"], "build-backend": "setuptools.build_meta"}
    legacy.pop("tool", None)
    (copies[-1] / "pyproject.toml").write_text(tomli_w.dumps(legacy))
    before = {path: (path / "pyproject.toml").read_bytes() for path in copies}

    workspace = tmp_path / "workspace"
    _generate_pyproject(copies, workspace, source=core)
    root = tomllib.loads((workspace / "pyproject.toml").read_text())
    members = [tomllib.loads((workspace / member / "pyproject.toml").read_text())
               for member in root["tool"]["uv"]["workspace"]["members"]]
    assert len({member["project"]["name"] for member in members}) == len(copies)
    assert all(member["project"]["dependencies"] == legacy["project"]["dependencies"] for member in members)
    assert sum("build-system" in member for member in members) == 1
    assert all((path / "pyproject.toml").read_bytes() == contents for path, contents in before.items())
    assert (source / "pyproject.toml").read_bytes() == original
