"""Appearance preparation must preserve color choices and never need app credentials."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from sowork import appearance, cli, plugin


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
ROOT = Path(__file__).resolve().parents[1]


def documents():
    custom, assets = {}, {}
    # Both presets share slots but use different tones/hair, so collect all selections.
    for path in (ROOT / 'skills/appearance/assets').glob('*.json'):
        for slot, asset in json.loads(path.read_text())['slots'].items():
            name, variant = asset[len('characterA_' + slot + '_'):].rsplit('+', 1)
            item = custom.setdefault(slot, {'items': {}})['items'].setdefault(name, {'variations': {}})
            transformed = slot in {'hair', 'shirt', 'pants', 'shoes', 'headAccessory'}
            item['variations'][variant] = {'swatch': {'type': 'transform' if transformed else 'single'}}
            base = 'basecolor' if transformed else variant
            if transformed:item['baseVariation'] = base
            assets['characterA_' + slot + '_' + name + '+' + base] = {'preview': {'type': 'atlas', 'ver': 1}}
    required = {'body', 'eyes', 'eyebrows', 'mouth', 'shirt', 'pants'}
    body_slots = {slot: {} if slot in required else {'optional': True} for slot in custom}
    customizer = {'bodyTypes': {'characterA': {'slots': custom}}}
    art = {'bodyTypes': {'characterA': {'slots': body_slots}}, 'assets': assets}
    return customizer, art


def preset(name='sabrina'):
    return json.loads((ROOT / 'skills/appearance/assets' / (name + '.json')).read_text())


def write_catalog(directory):
    customizer, art = documents()
    by_name = dict(zip(appearance.CATALOG_FILES, (customizer, art)))
    def api(request):
        assert request.method == 'GET'
        assert request.url.host == 'app.sowork.com'
        assert 'authorization' not in request.headers
        return httpx.Response(200, json=by_name[request.url.path.rsplit('/', 1)[-1]])
    appearance.download_catalog(directory, transport=httpx.MockTransport(api))
    return appearance.Catalog.load(directory)


@pytest.mark.parametrize('name', ['sabrina', 'newton'])
def test_presets_keep_selected_colors_when_base_art_is_resolved(name):
    customizer, art = documents()
    config = preset(name)
    before = copy.deepcopy(config)
    report = appearance.Catalog(customizer, art).validate(config)
    assert config == before
    hair = next(row for row in report['slots'] if row['slot'] == 'hair')
    assert hair['selected'] == config['slots']['hair']
    assert hair['resolved_art'].endswith('+basecolor')
    assert report['catalog_valid'] and not report['server_validated']
    assert report['visual_review_required']


@pytest.mark.parametrize('mutation,error', [
    (lambda c: c['slots'].pop('body'), 'missing_required_slots'),
    (lambda c: c['slots'].__setitem__('eyes', 'characterA_eyes_eyes1+invented'), 'unknown_variant'),
    (lambda c: c['slots'].__setitem__('eyes', 'other_eyes_eyes1+green'), 'asset_does_not_match_slot'),
    (lambda c: c['slots'].__setitem__('invented', 'characterA_invented_x+black'), 'unknown_character_slot'),
    (lambda c: c.__setitem__('version', True), 'expected_complete_v5'),
    (lambda c: c.__setitem__('version', 4), 'expected_complete_v5'),
])
def test_invalid_configs_cannot_pass_by_falling_back_to_any_base_art(mutation, error):
    catalog = appearance.Catalog(*documents())
    config = preset()
    mutation(config)
    with pytest.raises(appearance.AppearanceError, match=error):catalog.validate(config)


def test_hidden_required_defaults_allowed_but_disabled_choices_refused():
    customizer, art = documents()
    customizer['bodyTypes']['characterA']['slots']['eyebrows']['disabled'] = True
    catalog = appearance.Catalog(customizer, art)
    assert catalog.validate(preset())['catalog_valid']
    customizer['bodyTypes']['characterA']['slots']['eyes']['items']['eyes1']['variations']['green']['disabled'] = True
    with pytest.raises(appearance.AppearanceError, match='disabled_selection'):catalog.validate(preset())


def test_entitlements_are_reported_without_claiming_account_availability():
    customizer, art = documents()
    customizer['bodyTypes']['characterA']['slots']['hair']['items']['ophelia2']['accessKey'] = 'PREMIUM'
    report = appearance.Catalog(customizer, art).validate(preset())
    hair = next(row for row in report['slots'] if row['slot'] == 'hair')
    assert 'check_account_entitlement:accessKey' in hair['warnings']


def test_unsafe_base_variation_cannot_become_a_download_path():
    customizer, art = documents()
    customizer['bodyTypes']['characterA']['slots']['hair']['items']['ophelia2']['baseVariation'] = '../../secret'
    with pytest.raises(appearance.AppearanceError, match='invalid_character_identifier'):
        appearance.Catalog(customizer, art).validate(preset())


def test_snapshot_hashes_detect_changes_and_existing_output_is_preserved(tmp_path):
    directory = tmp_path / 'catalog'
    write_catalog(directory)
    with pytest.raises(appearance.AppearanceError, match='output_must_be_new'):
        appearance.download_catalog(directory)
    path = directory / 'customizer_config.json'
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(appearance.AppearanceError, match='hash_or_source_mismatch'):
        appearance.Catalog.load(directory)


@pytest.mark.parametrize('status', [302, 401, 503])
def test_failed_public_download_publishes_no_snapshot_or_redirect(tmp_path, status):
    requests = []
    def api(request):
        requests.append(request)
        return httpx.Response(status, headers={'Location': 'http://127.0.0.1/private'})
    output = tmp_path / 'catalog'
    with pytest.raises(appearance.AppearanceError, match='public_download_http_'):
        appearance.download_catalog(output, transport=httpx.MockTransport(api))
    assert len(requests) == 1 and not output.exists()


def test_download_is_bounded_before_json_parse(tmp_path, monkeypatch):
    monkeypatch.setattr(appearance, 'MAX_JSON_BYTES', 16)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b'x' * 17))
    with pytest.raises(appearance.AppearanceError, match='public_download_too_large'):
        appearance.download_catalog(tmp_path / 'catalog', transport=transport)


def test_preview_download_is_not_mislabeled_as_visual_review(tmp_path):
    catalog = appearance.Catalog(*documents())
    calls = []
    def api(request):
        calls.append(str(request.url))
        assert request.url.scheme == 'https' and request.url.host == 'app.sowork.com'
        assert 'authorization' not in request.headers
        return httpx.Response(200, content=PNG)
    output = tmp_path / 'previews'
    result = appearance.download_previews(catalog, preset(), output, transport=httpx.MockTransport(api))
    assert not result['visual_review_completed']
    manifest = json.loads((output / 'manifest.json').read_text())
    assert not manifest['visual_review_completed']
    assert set(row['slot'] for row in manifest['previews']) == set(preset()['slots'])
    for row in manifest['previews']:
        assert row['preview_url'] in calls
        assert hashlib.sha256((output / row['file']).read_bytes()).hexdigest() == row['sha256']


def test_bad_preview_never_publishes_a_successful_review(tmp_path):
    with pytest.raises(appearance.AppearanceError, match='invalid_png_preview'):
        appearance.download_previews(appearance.Catalog(*documents()), preset(), tmp_path/'previews',
            transport=httpx.MockTransport(lambda request:httpx.Response(200, content=b'<html>no</html>')))
    assert not (tmp_path/'previews').exists()


def test_offline_cli_does_not_resolve_a_profile_or_secret(tmp_path, monkeypatch, capsys):
    import hermes_constants
    from sowork import admin
    directory, config = tmp_path/'catalog', tmp_path/'character.json'
    write_catalog(directory)
    config.write_text(json.dumps(preset()))
    def forbidden(*args, **kwargs):raise AssertionError('No profile or credentials needed')
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', forbidden)
    monkeypatch.setattr(admin, 'fetch_credentials', forbidden)
    assert cli.main(['--json','appearance','validate','--catalog',str(directory),'--config',str(config)]) == 0
    assert json.loads(capsys.readouterr().out)['catalog_valid']


def test_direct_entry_operates_in_an_unconfigured_home(tmp_path):
    directory, config, home = tmp_path/'catalog', tmp_path/'character.json', tmp_path/'absent-home'
    write_catalog(directory)
    config.write_text(json.dumps(preset()))
    env = {**os.environ, 'HERMES_HOME':str(home)}
    result = subprocess.run([sys.executable,str(ROOT/'soworkctl.py'),'--json','appearance','validate',
                            '--catalog',str(directory),'--config',str(config)],env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)['catalog_valid']
    assert not home.exists()


def test_native_plugin_discovery_resolves_the_bundled_skill(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager, PluginContext
    if not callable(getattr(PluginContext,'register_skill',None)):
        pytest.skip('Host predates optional native plugin skill registration')
    home = tmp_path/'profile'; (home/'plugins').mkdir(parents=True)
    (home/'plugins/sowork-platform').symlink_to(ROOT,target_is_directory=True)
    (home/'config.yaml').write_text('plugins:\n  enabled: [sowork-platform]\n')
    monkeypatch.setenv('HERMES_HOME',str(home))
    manager = PluginManager()
    try:
        manager.discover_and_load()
        skill = manager.find_plugin_skill('sowork-platform:appearance')
        assert skill is not None and skill.resolve() == (ROOT/'skills/appearance/SKILL.md').resolve()
        assert 'appearance' in manager.list_plugin_skills('sowork-platform')
        assert (skill.parent/'assets/sabrina.json').is_file()
    finally:manager.unload()


def test_older_plugin_context_keeps_platform_and_cli_available():
    class OlderContext:
        def __init__(self):self.platforms=[];self.commands=[]
        def register_platform(self, **kwargs):self.platforms.append(kwargs['name'])
        def register_cli_command(self, **kwargs):self.commands.append(kwargs['name'])
    context = OlderContext()
    plugin.register(context)
    assert 'sowork' in context.platforms and 'sowork' in context.commands


def test_clean_export_keeps_workflow_presets_and_working_offline_cli(tmp_path):
    spec=importlib.util.spec_from_file_location('appearance_export',ROOT/'scripts/export_public.py')
    exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)
    target=tmp_path/'public';exporter.export(ROOT,target)
    for name in ['SKILL.md','references/app-profile.md','assets/sabrina.json','assets/newton.json','evals/evals.json']:
        assert (target/'skills/appearance'/name).is_file()
    directory,config=tmp_path/'catalog',tmp_path/'character.json'
    write_catalog(directory);config.write_text(json.dumps(preset('newton')))
    result=subprocess.run([sys.executable,str(target/'soworkctl.py'),'--json','appearance','validate',
                           '--catalog',str(directory),'--config',str(config)],capture_output=True,text=True,timeout=30)
    assert result.returncode == 0,result.stderr+result.stdout
    assert json.loads(result.stdout)['catalog_valid']
