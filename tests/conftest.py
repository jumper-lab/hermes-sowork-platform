import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def plugin_home(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    for key, value in {"SOWORK_USERNAME": "test-client", "SOWORK_CREDENTIAL": "test-secret", "SOWORK_INSTALLATION_ID": "install"}.items():
        monkeypatch.setenv(key, value)
    (home / "plugins").mkdir()
    (home / "plugins" / "sowork-platform").symlink_to(Path(__file__).resolve().parents[1], target_is_directory=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [sowork-platform]\n")
    from hermes_cli.plugins import PluginManager
    manager = PluginManager()
    manager.discover_and_load()
    yield home
    manager.unload()


def pytest_configure(config):
    config.addinivalue_line("markers", "spawns_gateway_lookalike: starts only a disposable test gateway")
