import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import source_git_seed


@unittest.skipUnless(os.name == 'posix', 'Uses Linux Git and file permissions')
class GitSeedTests(unittest.TestCase):
    def test_seed_preserves_identity_and_version_queries_without_blobs_or_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            project = root / 'common'
            project.mkdir(parents=True)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(project), *args], text=True).strip()
            git('init', '-q')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.com')
            (project / 'nested').mkdir()
            (project / 'nested' / 'code.c').write_text('first\n')
            git('add', '.')
            git('commit', '-qm', 'first')
            parent = git('rev-parse', 'HEAD')
            (project / 'nested' / 'code.c').write_text('second\n')
            git('commit', '-qam', 'second')
            git('tag', '-a', 'release-r9', '-m', 'release')
            commit, timestamp = git('rev-parse', 'HEAD'), git('log', '-1', '--format=%ct')
            blob = git('rev-parse', 'HEAD:nested/code.c')
            bundle = root / '.source-vcs'
            source_git_seed.create_seed_bundle(root, bundle)
            shutil.rmtree(project / '.git')
            source_git_seed.restore_seed_bundle(root)
            self.assertEqual(git('rev-parse', 'HEAD'), commit)
            self.assertEqual(git('log', '-1', '--format=%ct'), timestamp)
            self.assertEqual(git('rev-list', '--count', 'HEAD'), '1')
            self.assertEqual(git('describe', '--exact-match'), 'release-r9')
            self.assertEqual(git('status', '--porcelain'), '')
            for missing in (parent, blob):
                result = subprocess.run(['git', '-C', str(project), 'cat-file', '-e', missing],
                                        capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            (project / 'nested' / 'code.c').write_text('modified\n')
            self.assertEqual(git('diff-index', '--name-only', 'HEAD'), 'nested/code.c')
            self.assertFalse(bundle.exists())

    def test_bundle_paths_cannot_escape_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / '.source-vcs'
            bundle.mkdir()
            (bundle / 'manifest.json').write_text(
                '{"schema":1,"projects":[{"path":"../escape","slot":"0000","commit":"' + 'a'*40 + '"}]}')
            with self.assertRaises(ValueError):
                source_git_seed.restore_seed_bundle(root)
