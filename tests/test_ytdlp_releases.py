"""Release-mode workflow and flat multi-file asset tests."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

from scripts.publish_ytdlp_collection import publish_collection, release_asset_name


WORKFLOW = Path('.github/workflows/pornhub_downloader.yml')


def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_is_separate_from_repository_push_and_published_only_after_upload(self):
        data = workflow()
        # PyYAML's YAML 1.1 parser treats the unquoted 'on' key as a boolean.
        inputs = data.get('on', data.get(True))['workflow_dispatch']['inputs']
        self.assertEqual(inputs['upload_method']['default'], 'repository')
        self.assertEqual(inputs['upload_method']['options'], ['repository', 'release'])
        steps = data['jobs']['download-and-maybe-split']['steps']
        push = next(s for s in steps if s.get('name') == 'Commit and push to repository')
        stage = next(s for s in steps if s.get('name') == 'Prepare flat Release assets')
        upload = next(s for s in steps if s.get('name') == 'Upload to GitHub Releases')
        publish = next(s for s in steps if s.get('name') == 'Publish completed GitHub Release')
        self.assertEqual(push['if'], "inputs.upload_method != 'release'")
        for step in (stage, upload, publish):
            self.assertEqual(step['if'], "inputs.upload_method == 'release'")
        self.assertTrue(upload['with']['draft'])
        self.assertFalse(upload['with']['overwrite_files'])
        self.assertTrue(upload['with']['fail_on_unmatched_files'])
        self.assertEqual(upload['with']['files'], '${{ runner.temp }}/meli-release/*')
        self.assertIn('github.run_id', upload['with']['tag_name'])
        self.assertIn('github.run_attempt', upload['with']['tag_name'])
        self.assertIn('gh release edit', publish['run'])
        self.assertLess(steps.index(upload), steps.index(publish))
        self.assertNotIn('git push', stage['run'])
        self.assertNotIn('push_current_branch', stage['run'])

    def stage(self, root, collection):
        step = next(s['run'] for s in workflow()['jobs']['download-and-maybe-split']['steps']
                    if s.get('name') == 'Prepare flat Release assets')
        env = os.environ.copy()
        env.update(RUNNER_TEMP=str(root), WF_IS_COLLECTION=str(collection).lower(), WF_ARCHIVE_NAME='video')
        return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', step], env=env, capture_output=True, text=True)

    def test_single_archive_volumes_stage_flat_without_git_and_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'meli-video'; source.mkdir()
            (source / 'video.7z.001').write_bytes(b'first')
            (source / 'video.7z.002').write_bytes(b'second')
            result = self.stage(root, False)
            self.assertEqual(result.returncode, 0, result.stderr)
            target = root / 'meli-release'
            self.assertEqual({p.name for p in target.iterdir()}, {'video.7z.001', 'video.7z.002'})
            self.assertEqual((target / 'video.7z.001').read_bytes(), b'first')
            self.assertEqual((source / 'video.7z.002').read_bytes(), b'second')

    def test_collection_staging_produces_flat_assets_and_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'meli-video'; source.mkdir()
            (source / 'one.mp4').write_bytes(b'one'); (source / 'two.webm').write_bytes(b'two')
            (root / 'meli-video-manifest.json').write_text('["one.mp4", "two.webm"]')
            result = self.stage(root, True)
            self.assertEqual(result.returncode, 0, result.stderr)
            release = root / 'meli-release'
            mapping = json.loads((release / 'yt-dlp-collection-manifest.json').read_text())
            self.assertEqual([item['filename'] for item in mapping], ['one.mp4', 'two.webm'])
            self.assertEqual((release / mapping[0]['assets'][0]).read_bytes(), b'one')
            self.assertEqual((release / mapping[1]['assets'][0]).read_bytes(), b'two')
            self.assertEqual(len(list(release.iterdir())), 3)
            self.assertTrue(all(p.is_file() for p in release.iterdir()))

    def test_empty_or_oversize_assets_fail_before_creating_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'meli-video').mkdir()
            result = self.stage(root, False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('No completed assets', result.stdout)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'meli-video'; source.mkdir()
            with open(source / 'too-large.mp4', 'wb') as f:
                f.truncate(2 * 1024 * 1024 * 1024)
            result = self.stage(root, False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('smaller than 2 GiB', result.stdout)

    def test_nonregular_asset_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'meli-video'; source.mkdir()
            (source / 'fake').symlink_to(root / 'outside')
            result = self.stage(root, False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Invalid Release asset', result.stdout)


class FlatPublicationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('7z'), '7-Zip is required')
    def test_mixed_flat_collection_has_bounded_extractable_volumes_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; source.mkdir()
            content = os.urandom(1024 * 1024 + 4096)
            (source / 'large.webm').write_bytes(content)
            (source / 'small.mp3').write_bytes(b'audio')
            manifest = root / 'files.json'; manifest.write_text('["large.webm", "small.mp3"]')
            release = root / 'release'
            publish_collection(source, manifest, release, 'video', threshold=1024 * 1024, volume_mb=1, flat=True)
            mapping = json.loads((release / 'yt-dlp-collection-manifest.json').read_text())
            parts = [release / name for name in mapping[0]['assets']]
            self.assertGreaterEqual(len(parts), 2)
            self.assertTrue(all(p.stat().st_size <= 1024 * 1024 for p in parts))
            out = root / 'out'
            subprocess.run(['7z', 'x', '-y', f'-o{out}', str(parts[0])], check=True, capture_output=True)
            self.assertEqual((out / 'large.webm').read_bytes(), content)
            self.assertEqual((release / mapping[1]['assets'][0]).read_bytes(), b'audio')
            self.assertTrue((source / 'large.webm').exists())

    def test_existing_asset_is_not_overwritten_and_partial_assets_rolled_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; source.mkdir()
            (source / 'a').write_bytes(b'a'); (source / 'b').write_bytes(b'b')
            manifest = root / 'files.json'; manifest.write_text('["a", "b"]')
            release = root / 'release'; release.mkdir()
            collision = release / release_asset_name(1, 'b'); collision.write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                publish_collection(source, manifest, release, 'video', flat=True)
            self.assertEqual(collision.read_bytes(), b'keep')
            self.assertEqual(list(release.iterdir()), [collision])
            self.assertTrue((source / 'a').exists())

    def test_release_names_are_unique_bounded_and_preserve_7z_volume_suffix(self):
        name = 'x' * 230 + '.7z.001'
        a = release_asset_name(0, name); b = release_asset_name(1, name)
        self.assertNotEqual(a, b)
        self.assertLessEqual(len(a.encode()), 240)
        self.assertTrue(a.endswith('.7z.001'))
