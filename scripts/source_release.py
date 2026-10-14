"""Store pristine, commit-locked repo checkouts in GitHub Release assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from source_snapshot import verify_checkout

PART_LIMIT = 1900 * 1024 * 1024  # Each asset stays below GitHub's 2 GiB limit.
SAFE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,230}$')


def safe_name(value):
    if not SAFE_NAME.fullmatch(value):
        raise ValueError(f'Invalid asset name: {value!r}')
    return value


def target_name(android, kernel, sublevel, patch_level, revision):
    for value, pattern in [(android, r'android\d+'), (kernel, r'\d+\.\d+'),
                           (sublevel, r'\d+'), (patch_level, r'lts|\d{4}-\d{2}'),
                           (revision, r'(?:r\d+)?')]:
        if not re.fullmatch(pattern, value):
            raise ValueError(f'Invalid source target: {value!r}')
    if patch_level == 'lts':
        return f'{android}-{kernel}-lts'
    return f'{android}-{kernel}.{sublevel}-{patch_level}' + (f'-{revision}' if revision else '')


def index_name(target, epoch, digest):
    safe_name(target)
    safe_name(epoch)
    if target.endswith('-lts'):
        if not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('LTS archives require the resolved manifest digest')
        return safe_name(f'{target}-{epoch}-{digest}.json')
    return safe_name(f'{target}-{epoch}.json')


def manifest_digest(locked, gcc_sha):
    return hashlib.sha256((locked + gcc_sha.lower()).encode()).hexdigest()


def validate_identity(metadata, expected_digest=''):
    if metadata.get('schema') != 1:
        raise ValueError('Unsupported source archive schema')
    gcc_sha = metadata.get('gcc_sha', '')
    if gcc_sha and not re.fullmatch(r'[a-f0-9]{40}', gcc_sha):
        raise ValueError('Invalid GCC revision')
    actual = manifest_digest(metadata['locked'], gcc_sha)
    if actual != metadata.get('digest') or (expected_digest and actual != expected_digest):
        raise ValueError('Source archive identity does not match its locked manifest')


class ReleaseStore:
    def __init__(self, repository, tag):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise ValueError('Invalid GitHub repository')
        self.repository = repository
        self.tag = safe_name(tag)

    def gh(self, *args, missing_ok=False):
        for attempt in range(3):
            result = subprocess.run(['gh', *args], text=True, capture_output=True)
            if result.returncode == 0:
                return result.stdout
            if missing_ok and 'HTTP 404' in result.stderr:
                return None
            if 'already_exists' in result.stderr or 'already exists' in result.stderr:
                break
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f'GitHub Release operation failed: {result.stderr.strip()}')

    def release(self):
        text = self.gh('api', f'repos/{self.repository}/releases/tags/{self.tag}', missing_ok=True)
        return json.loads(text) if text is not None else None

    def assets(self):
        release = self.release()
        if release is None:
            return []
        pages = json.loads(self.gh('api',
            f'repos/{self.repository}/releases/{release["id"]}/assets?per_page=100',
            '--paginate', '--slurp'))
        return [asset for page in pages for asset in page]

    def read_metadata(self, name):
        safe_name(name)
        if not any(asset['name'] == name for asset in self.assets()):
            return None
        with tempfile.TemporaryDirectory(prefix='source-index-') as directory:
            path = Path(directory) / name
            self.download(name, path)
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError('Source index is unexpectedly large')
            return json.loads(path.read_text(encoding='utf-8'))

    def download(self, name, destination):
        safe_name(name)
        self.gh('release', 'download', self.tag, '--repo', self.repository,
                '--pattern', name, '--dir', str(destination.parent), '--clobber')
        downloaded = destination.parent / name
        if downloaded != destination:
            downloaded.replace(destination)

    def ensure_release(self):
        if self.release() is not None:
            return
        try:
            self.gh('release', 'create', self.tag, '--repo', self.repository,
                    '--target', os.environ.get('GITHUB_SHA', 'main'),
                    '--title', f'Pristine sources: {self.tag}', '--prerelease', '--latest=false',
                    '--notes', 'Automatically maintained pristine sources. JSON indexes are uploaded '
                    'only after all archive parts are available. Keep referenced parts together.')
        except RuntimeError:
            # A concurrent build may have created the same release.
            if self.release() is None:
                raise

    def upload(self, path):
        self.ensure_release()
        # In particular, completion indexes must be create-only: a concurrent
        # publisher must not replace the winner's index after reading a miss.
        self.gh('release', 'upload', self.tag, str(path), '--repo', self.repository)

    def delete_asset(self, name):
        safe_name(name)
        for asset in self.assets():
            if asset['name'] == name:
                self.gh('api', '--method', 'DELETE',
                        f'repos/{self.repository}/releases/assets/{asset["id"]}', missing_ok=True)
                return


def split_stream(stream, work, prefix, limit=PART_LIMIT):
    parts = []
    while True:
        first = stream.read(min(8 * 1024 * 1024, limit))
        if not first:
            break
        name = safe_name(f'{prefix}.tar.zst.part{len(parts):04d}')
        checksum = hashlib.sha256()
        size = 0
        with (work / name).open('wb') as output:
            chunk = first
            while chunk:
                output.write(chunk)
                checksum.update(chunk)
                size += len(chunk)
                if size == limit:
                    break
                chunk = stream.read(min(8 * 1024 * 1024, limit - size))
        parts.append({'name': name, 'size': size, 'sha256': checksum.hexdigest()})
    if not parts:
        raise ValueError('Refusing to publish an empty archive')
    if len(parts) == 1:
        name = safe_name(f'{prefix}.tar.zst')
        (work / parts[0]['name']).replace(work / name)
        parts[0]['name'] = name
    return parts


def archive_tree(root, work, prefix, exclude_gcc=False, exclude_history=False):
    command = ['tar', '-C', str(root)]
    if exclude_gcc:
        command += ['--exclude=./gcc']
    if exclude_history:
        # Match metadata directories, .git files and .git symlinks at any depth.
        # Keep ordinary versioned files such as .gitignore and .gitmodules.
        command += ['--exclude=.repo', '--exclude=.git']
    command += ['-cf', '-', '.']
    # Never print binary archive data. Both subprocesses must finish successfully.
    with subprocess.Popen(command, stdout=subprocess.PIPE) as tar:
        with subprocess.Popen(['zstd', '-T0', '-6', '-c'], stdin=tar.stdout,
                              stdout=subprocess.PIPE) as zstd:
            tar.stdout.close()
            try:
                parts = split_stream(zstd.stdout, work, prefix)
            except BaseException:
                zstd.kill()
                tar.kill()
                raise
            finally:
                zstd.stdout.close()
            zstd_status = zstd.wait()
        tar_status = tar.wait()
    if tar_status or zstd_status:
        raise RuntimeError(f'Archive creation failed: tar={tar_status}, zstd={zstd_status}')
    return parts


def assemble_parts(store, parts, output):
    if not isinstance(parts, list) or not parts:
        raise ValueError('Archive has no parts')
    names = set()
    with output.open('wb') as combined:
        for part in parts:
            name = safe_name(part['name'])
            if name in names or not re.fullmatch(r'[a-f0-9]{64}', part['sha256']):
                raise ValueError('Invalid archive part descriptor')
            names.add(name)
            path = output.parent / name
            if path == output:
                raise ValueError('Archive part collides with output')
            try:
                store.download(name, path)
                checksum = hashlib.sha256()
                size = 0
                with path.open('rb') as source:
                    while chunk := source.read(8 * 1024 * 1024):
                        checksum.update(chunk)
                        size += len(chunk)
                        combined.write(chunk)
                if size != part['size'] or checksum.hexdigest() != part['sha256']:
                    raise ValueError(f'Archive checksum mismatch: {name}')
            finally:
                path.unlink(missing_ok=True)


def discard_unreferenced_uploads(store, parts, winner):
    """Delete only this attempt's unique payloads not referenced by the index."""
    referenced = {part['name'] for part in winner['parts']}
    for part in parts:
        if part['name'] not in referenced:
            try:
                store.delete_asset(part['name'])
                print(f'Removed redundant upload: {part["name"]}', flush=True)
            except (RuntimeError, OSError) as error:
                # The winning archive remains usable even if cleanup is denied.
                print(f'Could not remove redundant upload {part["name"]}: {error}', file=sys.stderr)


def publish_archive(store, root, marker, metadata, exclude_gcc=False, exclude_history=False):
    existing = store.read_metadata(marker)
    if existing is not None:
        return existing
    with tempfile.TemporaryDirectory(prefix='source-pack-') as directory:
        work = Path(directory)
        # Distinct payload names prevent concurrent builders from replacing parts
        # referenced by another builder's completion marker.
        prefix = marker.removesuffix('.json')
        if metadata.get('target', '').endswith('-lts') and metadata.get('kernel'):
            sublevel = metadata['kernel'].rsplit('.', 1)[1]
            prefix = prefix.replace('-lts-', f'.{sublevel}-lts-', 1)
        if metadata.get('digest') and metadata['digest'] not in prefix:
            prefix += '-' + metadata['digest'][:16]
        prefix += '-' + uuid.uuid4().hex[:12]
        if exclude_history:
            parts = archive_tree(root, work, prefix, exclude_gcc, exclude_history=True)
        else:
            parts = archive_tree(root, work, prefix, exclude_gcc)
        # Compression can take minutes; avoid uploading if another job finished
        # publishing the same target while we were packing it.
        existing = store.read_metadata(marker)
        if existing is not None:
            return existing
        for part in parts:
            print(f'Uploading {part["name"]} ({part["size"] / 1024**2:.1f} MiB)', flush=True)
            store.upload(work / part['name'])
            (work / part['name']).unlink()
        existing = store.read_metadata(marker)
        if existing is not None:
            discard_unreferenced_uploads(store, parts, existing)
            return existing
        metadata = {**metadata, 'parts': parts}
        path = work / marker
        path.write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
        try:
            store.upload(path)
        except RuntimeError:
            # The API atomically rejects a duplicate index name. Also handle a
            # lost success response: never delete payloads the index references.
            winner = store.read_metadata(marker)
            if winner is None:
                raise
            discard_unreferenced_uploads(store, parts, winner)
            return winner
        return metadata


def extract_archive(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run(['tar', '--zstd', '-xf', str(archive), '-C', str(destination)], check=True)


def compare_without_history(store, gcc_store, marker, root):
    original = store.read_metadata(marker)
    if original is None:
        raise ValueError('Create the original pristine archive before comparing its size')
    validate_identity(original)
    verify_checkout(original['locked'], root, original['gcc_sha'])
    # Separate indexes keep the experiment independent of verified Git restores.
    light_marker = marker.removesuffix('.json') + '-no-history.json'
    light_metadata = {key: value for key, value in original.items() if key != 'parts'}
    light_metadata.update(git_metadata=False, comparison_of=marker)
    rows = []
    gcc_sha = original['gcc_sha']
    if gcc_sha:
        gcc_marker = f'gcc-{gcc_sha}-v1.json'
        gcc_original = gcc_store.read_metadata(gcc_marker)
        if gcc_original is None or gcc_original.get('gcc_sha') != gcc_sha:
            raise ValueError('Original GCC archive is missing or mismatched')
        gcc_light_marker = f'gcc-{gcc_sha}-v1-no-history.json'
        gcc_light = publish_archive(gcc_store, root / 'gcc', gcc_light_marker,
            {'schema': 1, 'gcc_sha': gcc_sha, 'git_metadata': False, 'comparison_of': gcc_marker},
            exclude_history=True)
        light_metadata['gcc_index'] = gcc_light_marker
        rows.append(('额外 GCC', gcc_original, gcc_light))
    light = publish_archive(store, root, light_marker, light_metadata,
                            exclude_gcc=True, exclude_history=True)
    rows.insert(0, ('源码及 manifest 工具链', original, light))
    totals = {'original_bytes': 0, 'without_history_bytes': 0}
    lines = ['### 无 Git 历史归档大小对比', '',
             '格式均为 tar.zst，压缩级别均为 zstd -6。锁定 manifest 保存在 JSON 索引中。', '']
    for label, full, stripped in rows:
        full_size = sum(part['size'] for part in full['parts'])
        light_size = sum(part['size'] for part in stripped['parts'])
        totals['original_bytes'] += full_size
        totals['without_history_bytes'] += light_size
        lines.append(f'- {label}：含 Git 元数据 {full_size / 1024**2:.2f} MiB → '
                     f'无 Git 元数据 {light_size / 1024**2:.2f} MiB')
    full_size, light_size = totals['original_bytes'], totals['without_history_bytes']
    saving = (1 - light_size / full_size) * 100 if full_size else 0
    lines += [f'- 合计：{full_size / 1024**2:.2f} MiB → {light_size / 1024**2:.2f} MiB，'
              f'减少 {saving:.2f}%', '', f'无历史源码索引：`{light_marker}`', '']
    report = '\n'.join(lines)
    print(report, flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as output:
            output.write(report + '\n')
    return totals


def restore_snapshot(store, gcc_store, marker, root, locked_path, expected_digest='',
                     metadata_only=False, expected_target=''):
    metadata = store.read_metadata(marker)
    if metadata is None:
        print(f'Release archive not found: {store.tag if isinstance(store, ReleaseStore) else "test"}/{marker}')
        return None
    validate_identity(metadata, expected_digest)
    if metadata.get('git_metadata') is False:
        raise ValueError('History-free comparison archives cannot be restored as verified Git checkouts')
    if expected_target and metadata.get('target') != expected_target:
        raise ValueError('Source archive target does not match the requested version')
    gcc_sha = metadata['gcc_sha']
    gcc = gcc_store.read_metadata(f'gcc-{gcc_sha}-v1.json') if gcc_sha else None
    if gcc_sha and (gcc is None or gcc.get('gcc_sha') != gcc_sha):
        print('GCC archive missing; source synchronization is required')
        return None
    if metadata_only:
        print(f'Complete Release archive already exists: {marker}; skipping download')
        return metadata
    # Extract and verify in isolation before replacing the initialized checkout.
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.source-restore-', dir=root.parent) as directory:
        work = Path(directory)
        staging = work / 'checkout'
        archive = work / 'source.tar.zst'
        assemble_parts(store, metadata['parts'], archive)
        extract_archive(archive, staging)
        archive.unlink()
        if gcc_sha:
            assemble_parts(gcc_store, gcc['parts'], archive)
            extract_archive(archive, staging / 'gcc')
            archive.unlink()
        verify_checkout(metadata['locked'], staging, gcc_sha)
        if root.is_symlink():
            raise ValueError('Refusing to replace a symlink checkout')
        if root.exists():
            # Only repo init and optional GCC restoration may precede this step.
            if any(p.name not in {'.repo', 'gcc'} for p in root.iterdir()):
                raise ValueError('Refusing to replace a populated source checkout')
            shutil.rmtree(root)
        staging.replace(root)
    locked_path.write_text(metadata['locked'], encoding='utf-8')
    print(f'Restored pristine sources from Release: {marker}')
    return metadata


def write_outputs(**values):
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf-8') as output:
            for name, value in values.items():
                output.write(f'{name}={value}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['restore', 'save', 'names', 'restore-gcc', 'compare'])
    parser.add_argument('--android', required=True)
    parser.add_argument('--kernel', required=True)
    parser.add_argument('--sublevel', required=True)
    parser.add_argument('--patch', required=True)
    parser.add_argument('--revision', default='')
    parser.add_argument('--epoch', default='v1')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--locked', type=Path)
    parser.add_argument('--digest', default='')
    parser.add_argument('--gcc-sha', default='')
    parser.add_argument('--release-tag', default='')
    parser.add_argument('--metadata-only', action='store_true',
                        help='Updater: skip downloads when the complete archive already exists')
    args = parser.parse_args()
    target = target_name(args.android, args.kernel, args.sublevel, args.patch, args.revision)
    tag = f'source-{args.android}-{args.kernel}'
    if args.command == 'names':
        write_outputs(target=target, tag=tag)
        return
    if args.root is None or args.locked is None:
        parser.error('--root and --locked are required')
    # All callers use a generated, dedicated checkout directly inside the workspace.
    workspace = Path(os.environ['GITHUB_WORKSPACE']).resolve()
    expected_root = workspace / f'{args.android}-{args.kernel}-{args.sublevel}'
    if args.root.resolve() != expected_root or args.root.is_symlink():
        raise ValueError('Source checkout must be the generated workspace directory')
    repository = os.environ['GITHUB_REPOSITORY']
    store = ReleaseStore(repository, tag)
    gcc_store = ReleaseStore(repository, 'source-toolchains')
    if args.command == 'restore-gcc':
        if not args.gcc_sha:
            return
        if not re.fullmatch(r'[a-f0-9]{40}', args.gcc_sha):
            raise ValueError('Invalid GCC commit')
        metadata = gcc_store.read_metadata(f'gcc-{args.gcc_sha}-v1.json')
        if metadata is not None and metadata.get('gcc_sha') == args.gcc_sha:
            with tempfile.TemporaryDirectory(prefix='gcc-download-') as directory:
                archive = Path(directory) / 'gcc.tar.zst'
                assemble_parts(gcc_store, metadata['parts'], archive)
                extract_archive(archive, args.root / 'gcc')
            print('Restored shared GCC toolchain from Release')
        return
    marker = index_name(target, args.epoch, args.digest)
    if args.command == 'compare':
        compare_without_history(store, gcc_store, marker, args.root)
        return
    if args.command == 'restore':
        metadata = restore_snapshot(store, gcc_store, marker, args.root, args.locked,
                                    args.digest if args.patch == 'lts' else '',
                                    args.metadata_only, target)
        write_outputs(hit='true' if metadata else 'false',
                      digest=metadata['digest'] if metadata else '',
                      gcc_sha=metadata['gcc_sha'] if metadata else '')
        return 0 if metadata else 3
    locked = args.locked.read_text(encoding='utf-8')
    metadata = {'schema': 1, 'target': target, 'locked': locked,
                'digest': args.digest, 'gcc_sha': args.gcc_sha,
                'release_tag': args.release_tag}
    validate_identity(metadata)
    verify_checkout(locked, args.root, args.gcc_sha)
    makefile = (args.root / 'common' / 'Makefile').read_text(encoding='utf-8')
    actual_sublevel = re.search(r'^SUBLEVEL\s*=\s*(\d+)', makefile, re.M).group(1)
    metadata['kernel'] = f'{args.kernel}.{actual_sublevel}'
    if args.gcc_sha:
        publish_archive(gcc_store, args.root / 'gcc', f'gcc-{args.gcc_sha}-v1.json',
                        {'schema': 1, 'gcc_sha': args.gcc_sha})
    publish_archive(store, args.root, marker, metadata, exclude_gcc=True)
    print(f'Pristine source archive is available in Release {tag}: {marker}')


if __name__ == '__main__':
    try:
        sys.exit(main() or 0)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Source Release error: {error}', file=sys.stderr)
        sys.exit(1)
