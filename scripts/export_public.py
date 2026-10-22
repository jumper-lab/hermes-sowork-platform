"""Export a fresh publication tree; never include private pilot Git history."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT_FILES = ('__init__.py', 'plugin.yaml', 'pyproject.toml', 'soworkctl.py',
              'README.md', 'LICENSE', 'CHANGELOG.md', 'SECURITY.md', '.gitignore', 'MANIFEST.in')
DOCS = ('COMPATIBILITY.md', 'MIGRATION.md', 'PUBLICATION.md', 'VALIDATION.json', 'HERMES-API-PROPOSAL.md')
SCRIPTS = ('verify.py', 'export_public.py')


def export(root, output):
    root, output = Path(root).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('Publication destination must be new')
    files = [root / name for name in ROOT_FILES]
    files += sorted((root / 'sowork').glob('*.py'))
    files += sorted((root / 'tests').glob('*.py'))
    files += [root / 'docs' / name for name in DOCS]
    files += [root / 'scripts' / name for name in SCRIPTS]
    files += [root / 'examples/config.yaml', root / 'examples/channels.yaml', root / '.github/workflows/ci.yml']
    if any(not p.is_file() or p.is_symlink() for p in files):
        raise ValueError('Publication source must contain regular files only')
    output.mkdir(parents=True)
    hashes = {}
    for source in files:
        relative = source.relative_to(root)
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[relative.as_posix()] = hashlib.sha256(target.read_bytes()).hexdigest()
    # A sibling report does not alter the exported source tree or install payload.
    manifest = output.with_name(output.name + '-manifest.json')
    with manifest.open('x') as file:
        json.dump({'schema': 'sowork-public-source/v1', 'files': hashes}, file, indent=2)
        file.write('\n')
    return {'source': str(output), 'manifest': str(manifest), 'files': len(hashes)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(Path(__file__).resolve().parents[1], args.output)))


if __name__ == '__main__':
    main()
