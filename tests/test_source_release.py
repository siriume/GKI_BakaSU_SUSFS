import hashlib
import io
import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import source_release


class MemoryStore:
    def __init__(self):
        self.assets = {}
        self.uploads = []

    def read_metadata(self, name):
        value = self.assets.get(name)
        return json.loads(value) if value else None

    def download(self, name, destination):
        destination.write_bytes(self.assets[name])

    def upload(self, path):
        self.uploads.append(path.name)
        self.assets[path.name] = path.read_bytes()


class SourceReleaseTests(unittest.TestCase):
    def test_fixed_and_lts_names_do_not_collide(self):
        self.assertEqual(source_release.target_name('android13', '5.15', '123', '2023-11', 'r9'),
                         'android13-5.15.123-2023-11-r9')
        # LTS lookup must not depend on a potentially stale JSON sublevel.
        self.assertEqual(source_release.target_name('android13', '5.15', '220', 'lts', ''),
                         'android13-5.15-lts')
        with self.assertRaises(ValueError):
            source_release.target_name('../oops', '5.15', '123', '2023-11', 'r9')

    def test_fixed_reuses_index_but_lts_is_keyed_by_all_commits(self):
        fixed = source_release.index_name('android13-5.15.123-2023-11-r9', 'v1', '')
        first = source_release.index_name('android13-5.15-lts', 'v1', 'a' * 64)
        second = source_release.index_name('android13-5.15-lts', 'v1', 'b' * 64)
        self.assertNotEqual(first, second)
        self.assertNotEqual(fixed, source_release.index_name('android13-5.15.123-2023-11-r9', 'v2', ''))
        with self.assertRaises(ValueError):
            source_release.index_name('android13-5.15-lts', 'v1', '')

    def test_split_reassemble_and_detect_corruption_before_extract(self):
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parts = source_release.split_stream(io.BytesIO(b'1234567890'), root, 'source', limit=4)
            self.assertEqual([p['size'] for p in parts], [4, 4, 2])
            for part in parts:
                store.upload(root / part['name'])
            output = root / 'joined'
            source_release.assemble_parts(store, parts, output)
            self.assertEqual(output.read_bytes(), b'1234567890')
            store.assets[parts[0]['name']] = b'xxxx'
            with self.assertRaisesRegex(ValueError, 'checksum'):
                source_release.assemble_parts(store, parts, output)

    def test_reject_download_paths_outside_archive_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                source_release.assemble_parts(MemoryStore(), [
                    {'name': '../escape', 'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()}
                ], Path(temp) / 'joined')

    def test_missing_release_is_a_miss_without_touching_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'checkout'
            root.mkdir()
            marker = root / 'keep'
            marker.write_text('untouched')
            result = source_release.restore_snapshot(MemoryStore(), MemoryStore(), 'missing.json',
                                                     root, Path(temp) / 'locked.xml')
            self.assertIsNone(result)
            self.assertEqual(marker.read_text(), 'untouched')

    def test_metadata_identity_rejects_changed_manifest(self):
        metadata = {'schema': 1, 'locked': '<manifest/>', 'gcc_sha': '',
                    'digest': source_release.manifest_digest('<manifest/>', '')}
        source_release.validate_identity(metadata)
        metadata['locked'] = '<manifest><project/></manifest>'
        with self.assertRaisesRegex(ValueError, 'identity'):
            source_release.validate_identity(metadata)

    def test_updater_skips_download_for_existing_complete_snapshot(self):
        store = MemoryStore()
        metadata = {'schema': 1, 'locked': '<manifest/>', 'gcc_sha': '',
                    'digest': source_release.manifest_digest('<manifest/>', ''),
                    'target': 'fixed', 'parts': []}
        store.assets['fixed.json'] = json.dumps(metadata).encode()
        with patch.object(store, 'download', side_effect=AssertionError('Must not download')):
            restored = source_release.restore_snapshot(store, MemoryStore(), 'fixed.json',
                Path('unused'), Path('unused.xml'), metadata_only=True, expected_target='fixed')
        self.assertEqual(restored['digest'], metadata['digest'])
        with self.assertRaisesRegex(ValueError, 'target'):
            source_release.restore_snapshot(store, MemoryStore(), 'fixed.json',
                Path('unused'), Path('unused.xml'), metadata_only=True, expected_target='other')

    def test_publish_uploads_completion_marker_last_and_reuses_existing(self):
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def pack(tree, work, prefix, exclude_gcc=False):
                return source_release.split_stream(io.BytesIO(b'pristine'), work, prefix, limit=4)
            with patch.object(source_release, 'archive_tree', side_effect=pack):
                source_release.publish_archive(store, root, 'fixed-v1.json', {'schema': 1})
                self.assertEqual(store.uploads[-1], 'fixed-v1.json')
                count = len(store.uploads)
                source_release.publish_archive(store, root, 'fixed-v1.json', {'schema': 1})
                self.assertEqual(len(store.uploads), count)

    def test_failed_part_upload_never_publishes_completion_marker(self):
        store = MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def pack(tree, work, prefix, exclude_gcc=False):
                return source_release.split_stream(io.BytesIO(b'pristine'), work, prefix, limit=4)
            with patch.object(source_release, 'archive_tree', side_effect=pack), \
                 patch.object(store, 'upload', side_effect=RuntimeError('network failed')):
                with self.assertRaises(RuntimeError):
                    source_release.publish_archive(store, root, 'fixed-v1.json', {'schema': 1})
            self.assertNotIn('fixed-v1.json', store.assets)

    @unittest.skipUnless(os.name == 'posix' and shutil.which('zstd'), 'Requires Linux tar and zstd')
    def test_real_archive_roundtrip_preserves_git_symlinks_and_executable_mode(self):
        store, gcc_store = MemoryStore(), MemoryStore()
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            root = work / 'original'
            for name in ('common', 'gcc'):
                directory = root / name
                directory.mkdir(parents=True)
                (directory / 'payload').write_text(name)
                if name == 'common':
                    (directory / 'Makefile').write_text('SUBLEVEL = 123\n')
                subprocess.run(['git', 'init', '-q', str(directory)], check=True)
                subprocess.run(['git', '-C', str(directory), 'add', '.'], check=True)
                subprocess.run(['git', '-C', str(directory), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.com', 'commit', '-qm', 'fixture'], check=True)
            script = root / 'common' / 'run'
            script.write_text('#!/bin/sh\nexit 0\n')
            script.chmod(0o755)
            (root / 'common' / 'link').symlink_to('payload')
            # Model repo's relative .git/object links, not only standalone clones.
            git_dir = root / '.repo' / 'projects' / 'common.git'
            git_dir.parent.mkdir(parents=True)
            (root / 'common' / '.git').rename(git_dir)
            (root / 'common' / '.git').symlink_to('../.repo/projects/common.git', target_is_directory=True)
            object_dir = root / '.repo' / 'project-objects' / 'kernel' / 'common.git' / 'objects'
            object_dir.parent.mkdir(parents=True)
            (git_dir / 'objects').rename(object_dir)
            (git_dir / 'objects').symlink_to('../../project-objects/kernel/common.git/objects', target_is_directory=True)
            subprocess.run(['git', '--git-dir', str(git_dir), 'config', 'core.worktree',
                            '../../../common'], check=True)
            def head(path):
                return subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
            gcc_sha = head(root / 'gcc')
            locked = '<manifest><project name="kernel/common" path="common" revision="' + head(root / 'common') + '"/></manifest>'
            metadata = {'schema': 1, 'target': 'fixed', 'locked': locked, 'gcc_sha': gcc_sha,
                        'digest': source_release.manifest_digest(locked, gcc_sha)}
            source_release.publish_archive(gcc_store, root / 'gcc', f'gcc-{gcc_sha}-v1.json',
                                           {'schema': 1, 'gcc_sha': gcc_sha})
            source_release.publish_archive(store, root, 'fixed.json', metadata, exclude_gcc=True)
            restored = work / 'restored'
            source_release.restore_snapshot(store, gcc_store, 'fixed.json', restored,
                                           work / 'locked.xml', metadata['digest'])
            self.assertTrue((restored / 'common' / 'link').is_symlink())
            self.assertTrue((restored / 'common' / '.git').is_symlink())
            self.assertTrue((restored / 'common' / 'run').stat().st_mode & 0o111)
            self.assertEqual((restored / 'gcc' / 'payload').read_text(), 'gcc')
            self.assertEqual(head(restored / 'common'), head(root / 'common'))
            self.assertEqual((work / 'locked.xml').read_text(), locked)

            # Exercise the actual CLI and Bash fixed-version fast path with a
            # filesystem-backed gh fixture. No Google/repo request is needed.
            target = 'android13-5.15.123-2023-11-r9'
            index = json.loads(store.assets['fixed.json'])
            index['target'] = target
            store.assets[target + '-v1.json'] = json.dumps(index).encode()
            releases = work / 'releases'
            for tag, memory in [('source-android13-5.15', store), ('source-toolchains', gcc_store)]:
                folder = releases / tag
                folder.mkdir(parents=True)
                for name, data in memory.assets.items():
                    (folder / name).write_bytes(data)
            bin_dir = work / 'bin'
            bin_dir.mkdir()
            gh = bin_dir / 'gh'
            gh.write_text('#!' + sys.executable + '\n' + '''
import json, os, shutil, sys
from pathlib import Path
a = sys.argv[1:]
releases = Path(os.environ['FAKE_RELEASES'])
if a[0] == 'api':
    if '/releases/tags/' in a[1]:
        print(json.dumps({'id': a[1].rsplit('/', 1)[1]}))
    else:
        tag = a[1].split('/releases/')[1].split('/')[0]
        print(json.dumps([[{'name': p.name} for p in (releases / tag).iterdir()]]))
elif a[:2] == ['release', 'download']:
    name = a[a.index('--pattern') + 1]
    if os.environ.get('NO_PAYLOAD_DOWNLOAD') and '.part' in name:
        raise SystemExit('Updater downloaded an existing payload')
    destination = Path(a[a.index('--dir') + 1]) / name
    shutil.copyfile(releases / a[2] / name, destination)
else:
    raise SystemExit('Unexpected GitHub operation: ' + repr(a))
''')
            gh.chmod(0o755)
            workspace = work / 'workspace'
            workspace.mkdir()
            scripts = Path(__file__).resolve().parents[1] / 'scripts'
            (workspace / 'scripts').symlink_to(scripts, target_is_directory=True)
            runner_temp = work / 'runner'
            runner_temp.mkdir()
            environment = {**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
                'FAKE_RELEASES': str(releases), 'GITHUB_WORKSPACE': str(workspace),
                'GITHUB_REPOSITORY': 'owner/repo', 'RUNNER_TEMP': str(runner_temp),
                'GITHUB_OUTPUT': str(work / 'outputs'), 'GITHUB_STEP_SUMMARY': str(work / 'summary'),
                'SOURCE_ANDROID': 'android13', 'SOURCE_KERNEL': '5.15', 'SOURCE_SUBLEVEL': '123',
                'SOURCE_PATCH': '2023-11', 'SOURCE_REVISION': 'r9', 'SOURCE_EPOCH': 'v1',
                'SOURCE_UPDATE_ONLY': 'false'}
            result = subprocess.run(['bash', str(scripts / 'source_release.sh')],
                env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('hit=true', (work / 'outputs').read_text())
            self.assertIn('digest=' + metadata['digest'], (work / 'outputs').read_text())
            self.assertTrue((workspace / 'android13-5.15-123' / 'common' / 'link').is_symlink())
            environment.update(SOURCE_UPDATE_ONLY='true', NO_PAYLOAD_DOWNLOAD='true')
            result = subprocess.run(['bash', str(scripts / 'source_release.sh')],
                env=environment, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('skipping download', result.stdout)


if __name__ == '__main__':
    unittest.main()
