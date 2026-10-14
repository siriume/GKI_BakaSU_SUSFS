"""Pin every project before caching a pristine repo source/toolchain archive."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

# Repo resolves relative fetch paths against the manifest repository URL as a file.
MANIFEST_URL = 'https://android.googlesource.com/kernel/manifest'
SHA = re.compile(r'^[0-9a-fA-F]{40}$')

def resolve_revision(url, revision):
    refs = [revision] if revision.startswith('refs/') else ['refs/heads/' + revision, 'refs/tags/' + revision]
    patterns = refs + [ref + '^{}' for ref in refs if ref.startswith('refs/tags/')]
    for attempt in range(3):
        result = subprocess.run(['git', 'ls-remote', url, *patterns], text=True, capture_output=True, timeout=90)
        if result.returncode == 0:
            values = dict((ref, commit) for commit, ref in (line.split() for line in result.stdout.splitlines()))
            for ref in refs:
                commit = values.get(ref + '^{}') or values.get(ref)
                if commit and SHA.fullmatch(commit):
                    return commit.lower()
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    raise ValueError(f'Cannot resolve {url} {revision}')

def lock_manifest(text, resolve=resolve_revision, extra_revision=""):
    root = ET.fromstring(text)
    if root.find('include') is not None:
        raise ValueError('Use the expanded output of repo manifest, not an include manifest')
    default = root.find('default')
    defaults = default.attrib if default is not None else {}
    remotes = {r.get('name'): r for r in root.findall('remote')}
    projects = root.findall('project')
    if not projects:
        raise ValueError('Manifest has no projects')
    def pin(project):
        remote = remotes.get(project.get('remote', defaults.get('remote')))
        if remote is None or not remote.get('fetch'):
            raise ValueError('Project has no fetch remote')
        revision = project.get('revision') or remote.get('revision') or defaults.get('revision')
        if not revision:
            raise ValueError('Project has no revision')
        if SHA.fullmatch(revision):
            return revision.lower()
        base = urljoin(MANIFEST_URL, remote.get('fetch').rstrip('/') + '/')
        url = urljoin(base, project.get('name'))
        commit = resolve(url, revision)
        if not SHA.fullmatch(commit):
            raise ValueError('Resolver did not return a commit SHA')
        return commit.lower()
    with ThreadPoolExecutor(max_workers=8) as pool:
        commits = list(pool.map(pin, projects))
    for project, commit in zip(projects, commits):
        project.set('revision', commit)
    locked = ET.tostring(root, encoding='unicode')
    if extra_revision and not SHA.fullmatch(extra_revision):
        raise ValueError('Extra toolchain revision must be a commit SHA')
    return locked, hashlib.sha256((locked + extra_revision.lower()).encode()).hexdigest()

def sync_manifest(locked, original):
    """Keep named release tags for fetching; snapshot identity stays SHA based."""
    root = ET.fromstring(locked)
    source = ET.fromstring(original)
    default = source.find('default')
    defaults = default.attrib if default is not None else {}
    remotes = {r.get('name'): r for r in source.findall('remote')}
    originals = {(p.get('name'), p.get('path')): p for p in source.findall('project')}
    for project in root.findall('project'):
        previous = originals[(project.get('name'), project.get('path'))]
        remote = remotes.get(previous.get('remote', defaults.get('remote')))
        revision = previous.get('revision') or (remote.get('revision') if remote is not None else None) or defaults.get('revision', '')
        if revision.startswith('refs/tags/'):
            project.set('revision', revision)
    return ET.tostring(root, encoding='unicode')


def verify_checkout(locked, checkout_root, extra_revision=""):
    """Reject missing or mismatched source/toolchain commits before cache save."""
    projects = ET.fromstring(locked).findall('project')
    if not projects:
        raise ValueError('Manifest has no projects')
    expected = [(p.get('name'), p.get('path') or p.get('name'), p.get('revision', '')) for p in projects]
    if extra_revision:
        expected.append(('gcc', 'gcc', extra_revision.lower()))
    for name, path, commit in expected:
        if not SHA.fullmatch(commit):
            raise ValueError(f'{name}: verification requires a locked commit SHA')
        result = subprocess.run(['git', '-C', str(Path(checkout_root) / path), 'rev-parse', 'HEAD'], text=True, capture_output=True, timeout=30)
        actual = result.stdout.strip().lower()
        if result.returncode or actual != commit.lower():
            raise ValueError(f'{name}: expected {commit}, got {actual or result.stderr.strip()}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('locked', type=Path, nargs='?')
    parser.add_argument('--sync-manifest', type=Path, help='Fetch manifest preserving named release tags')
    parser.add_argument('--verify-root', type=Path, help='Verify checkouts against an existing locked manifest')
    parser.add_argument('--extra-revision', default='', help='Additional GCC toolchain commit')
    args = parser.parse_args()
    if args.verify_root is not None:
        verify_checkout(args.manifest.read_text(), args.verify_root, args.extra_revision)
        print('All source/toolchain commits match the locked manifest')
        return
    if args.locked is None:
        parser.error('locked output is required unless --verify-root is used')
    original = args.manifest.read_text()
    locked, digest = lock_manifest(original, extra_revision=args.extra_revision)
    args.locked.write_text(locked)
    if args.sync_manifest is not None:
        args.sync_manifest.write_text(sync_manifest(locked, original))
    print(digest)

if __name__ == '__main__':
    main()
