"""Prepare the shared GCC Release once before the source archive matrix starts."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from source_release import ReleaseStore, publish_archive, variant_index


GCC_URL = 'https://github.com/LineageOS/android_prebuilts_gcc_linux-x86_aarch64_aarch64-linux-gnu-6.4.1.git'


def git(*args):
    for attempt in range(3):
        try:
            return subprocess.check_output(['git', *args], text=True).strip()
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(3 * (attempt + 1))


def main():
    include_history = os.environ.get('SOURCE_INCLUDE_HISTORY', 'false') == 'true'
    remote = git('ls-remote', GCC_URL, 'HEAD').split()
    if len(remote) != 2 or not re.fullmatch(r'[a-f0-9]{40}', remote[0]):
        raise ValueError('Cannot resolve the shared GCC HEAD commit')
    sha = remote[0]
    store = ReleaseStore(os.environ['GITHUB_REPOSITORY'], 'source-toolchains')
    marker = variant_index(f'gcc-{sha}-v1.json', include_history)
    existing = store.read_metadata(marker)
    if existing is not None:
        if existing.get('gcc_sha') != sha or existing.get('git_metadata') is not include_history \
                or (not include_history and existing.get('git_seed_version') != 1):
            raise ValueError(f'Shared GCC index has an unexpected format: {marker}')
        if not store.has_parts(existing['parts']):
            raise ValueError(f'Shared GCC archive is incomplete; remove its stale index: {marker}')
        print(f'Shared GCC is already archived: {sha}; skipping source download and packaging', flush=True)
    else:
        print(f'Preparing shared GCC archive: {sha}', flush=True)
        with tempfile.TemporaryDirectory(prefix='shared-gcc-') as directory:
            root = Path(directory) / 'gcc'
            git('init', str(root))
            git('-C', str(root), 'remote', 'add', 'origin', GCC_URL)
            git('-C', str(root), 'fetch', '--depth', '1', 'origin', sha)
            git('-C', str(root), 'checkout', '--detach', 'FETCH_HEAD')
            if git('-C', str(root), 'rev-parse', 'HEAD') != sha:
                raise ValueError('Shared GCC checkout does not match the resolved commit')
            metadata = {'schema': 1, 'gcc_sha': sha, 'git_metadata': include_history}
            if not include_history:
                metadata['git_seed_version'] = 1
            publish_archive(store, root, marker, metadata,
                            exclude_history=not include_history, seed_git=not include_history)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        output.write(f'gcc_sha={sha}\n')


if __name__ == '__main__':
    main()
