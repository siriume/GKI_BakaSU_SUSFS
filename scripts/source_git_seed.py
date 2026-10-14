"""Retain current commit/tree information without source blobs or old commits."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zlib

BUNDLE = '.source-vcs'


def git(project, *args):
    return subprocess.check_output(['git', '-C', str(project), *args])


def create_seed_bundle(root, destination, exclude_gcc=False):
    root, destination = Path(root), Path(destination)
    destination.mkdir(parents=True)
    projects = []
    for folder, directories, files in os.walk(root):
        project = Path(folder)
        relative = project.relative_to(root)
        has_git = '.git' in directories or '.git' in files
        directories[:] = [name for name in directories if name not in {'.git', '.repo', BUNDLE}]
        if exclude_gcc and project == root:
            directories[:] = [name for name in directories if name != 'gcc']
        if not has_git:
            continue
        commit = git(project, 'rev-parse', 'HEAD').decode().strip()
        trees = {git(project, 'rev-parse', 'HEAD^{tree}').decode().strip()}
        for line in git(project, 'ls-tree', '-r', '-t', 'HEAD').splitlines():
            header = line.split(b'\t', 1)[0].split()
            if header[1] == b'tree':
                trees.add(header[2].decode())
        objects = {commit, *trees}
        tags = {}
        for name in git(project, 'tag', '--points-at', 'HEAD').decode().splitlines():
            oid = git(project, 'rev-parse', f'refs/tags/{name}').decode().strip()
            tags[name] = oid
            objects.add(oid)
        ids = sorted(objects)
        batch = subprocess.run(['git', '-C', str(project), 'cat-file', '--batch'],
            input=('\n'.join(ids) + '\n').encode(), stdout=subprocess.PIPE, check=True).stdout
        slot = f'{len(projects):04d}'
        seed = destination / slot
        (seed / 'objects').mkdir(parents=True)
        (seed / 'refs' / 'tags').mkdir(parents=True)
        offset = 0
        for expected in ids:
            end = batch.index(b'\n', offset)
            oid, kind, size = batch[offset:end].split()
            size = int(size)
            body = batch[end + 1:end + 1 + size]
            offset = end + 2 + size
            raw = kind + b' ' + str(size).encode() + b'\0' + body
            if oid.decode() != expected or kind not in {b'commit', b'tree', b'tag'} \
                    or hashlib.sha1(raw).hexdigest() != expected:
                raise ValueError('Invalid current-commit seed object')
            path = seed / 'objects' / expected[:2] / expected[2:]
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(zlib.compress(raw))
        for name, oid in tags.items():
            path = seed / 'refs' / 'tags' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(oid + '\n')
        (seed / 'HEAD').write_text(commit + '\n')
        (seed / 'shallow').write_text(commit + '\n')
        (seed / 'config').write_text('[core]\nrepositoryformatversion = 0\nbare = false\n'
                                   'logallrefupdates = false\n[diff]\nrenames = false\n'
                                   '[status]\nrenames = false\n')
        index = Path(git(project, 'rev-parse', '--git-path', 'index').decode().strip())
        if not index.is_absolute():
            index = project / index
        if index.exists():
            shutil.copyfile(index, seed / 'index')
            for shared in index.parent.glob('sharedindex.*'):
                shutil.copyfile(shared, seed / shared.name)
        projects.append({'path': relative.as_posix(), 'slot': slot, 'commit': commit})
    (destination / 'manifest.json').write_text(json.dumps({'schema': 1, 'projects': projects}))


def restore_seed_bundle(root):
    root = Path(root).resolve()
    bundle = root / BUNDLE
    data = json.loads((bundle / 'manifest.json').read_text())
    if data.get('schema') != 1:
        raise ValueError('Unsupported current-commit seed schema')
    for entry in data['projects']:
        relative = Path(entry['path'])
        project = (root / relative).resolve()
        if relative.is_absolute() or not project.is_relative_to(root) \
                or not re.fullmatch(r'\d{4,}', entry['slot']) \
                or not re.fullmatch(r'[a-f0-9]{40}', entry['commit']):
            raise ValueError('Invalid current-commit seed path or revision')
        if not project.is_dir() or (project / '.git').exists() or (project / '.git').is_symlink():
            raise ValueError('Current-commit seed requires a history-free working directory')
        shutil.move(str(bundle / entry['slot']), str(project / '.git'))
        if not (project / '.git' / 'index').exists():
            listing = git(project, 'ls-tree', '-r', '-z', 'HEAD')
            subprocess.run(['git', '-C', str(project), 'update-index', '--info-only', '-z',
                            '--index-info'], input=listing, check=True)
        if git(project, 'rev-parse', 'HEAD').decode().strip() != entry['commit']:
            raise ValueError('Restored current-commit seed does not match its revision')
    shutil.rmtree(bundle)
