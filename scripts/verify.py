"""Run native Hermes tests against this public plugin in an isolated tracked source copy."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes', type=Path, required=True)
    parser.add_argument('--venv', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.hermes.resolve()
    python = args.venv.resolve() / 'bin/python'
    plugin = Path(__file__).resolve().parents[1]
    args.output.mkdir(parents=True, exist_ok=True)
    sha = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    names = subprocess.check_output(['git', '-C', str(source), 'ls-files', '-z']).decode().split('\0')
    with tempfile.TemporaryDirectory(prefix='sowork-verify-') as temporary:
        root = Path(temporary)
        # Copy only hydrated, tracked code. No profile, environment or Git metadata.
        for name in names:
            relative = Path(name)
            if not name or relative.is_absolute() or '..' in relative.parts:
                continue
            original = source / relative
            if original.is_file() and not original.is_symlink():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        target = root / 'tests/sowork_publication'
        shutil.copytree(plugin, target, ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache', 'evidence', 'activation'))
        home = root / 'test-home'
        home.mkdir()
        environment = {'HOME': str(home), 'PATH': '/usr/local/bin:/usr/bin:/bin',
                       'HERMES_PYTHON': str(python), 'HERMES_TEST_FILE_RETRIES': '0'}
        command = ['bash', 'scripts/run_tests.sh', 'tests/sowork_publication/tests', '-j', '4', '--', '--tb=short']
        with (args.output / 'tests.log').open('w') as log:
            result = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT, env=environment)
        summary = next((line for line in (args.output / 'tests.log').read_text().splitlines()
                        if line.startswith('=== Summary:')), 'No test summary')
        report = {'hermes_sha': sha, 'exit_code': result.returncode, 'summary': summary,
                  'credentials_loaded': False, 'production_modified': False}
        (args.output / 'tests.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
        raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
