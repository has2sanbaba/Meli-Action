"""Exercise the actual workflow publication scripts without pushing to GitHub."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


class WorkflowGroupedPublicationTests(unittest.TestCase):
    def test_repository_publish_steps_group_every_download(self):
        cases = [
            ('url_downloader.yml', 'direct-download', 'Push to Repository', 'temp_dl/archive',
             {'WF_STEPS_DOWNLOAD_OUTPUTS_FILENAME': 'direct.bin'}, ['direct.7z.001', 'direct.7z.002'], 'direct.bin'),
            ('youtube_downloader.yml', 'youtube-download', '📤 Push to Repository', 'release_files',
             {'WF_STEPS_RETRIEVE_OUTPUTS_FILENAME': 'video.mp4'}, ['video.z01', 'video.zip'], 'video.mp4'),
            ('telegram_downloader.yml', 'download', '📤 Commit and push files', 'meli-telegram',
             {'WF_STEPS_DOWNLOAD_OUTPUTS_DOWNLOADED_NAME': 'tg.mp4'}, ['tg.mp4.z01', 'tg.mp4.zip'], 'tg.mp4'),
            ('pornhub_downloader.yml', 'download-and-maybe-split', 'Commit and push to repository', 'meli-video',
             {'WF_GITHUB_EVENT_INPUTS_OUTPUT_FOLDER': 'downloads', 'WF_STEPS_DOWNLOAD_OUTPUTS_FILENAME': 'movie.mp4'},
             ['archive_abc.7z.001', 'archive_abc.7z.002'], 'movie.mp4'),
            ('spotfyandsoundcloud.yml', 'media-download', 'Push to Repository', 'release_files', {},
             ['song.mp3.z01', 'song.mp3.zip'], 'song.mp3'),
            ('youtube_adv_download.yml', 'fetch-and-store', 'Push changes to repository', 'meli-media',
             {'WF_GITHUB_EVENT_INPUTS_OUTPUT_FOLDER': 'downloads'},
             ['Film [abcd].mp4.z01', 'Film [abcd].mp4.zip', 'Film [abcd].en.vtt'], 'Film [abcd]'),
        ]
        for filename, job, step_name, source, values, parts, folder in cases:
            with self.subTest(workflow=filename), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                work = root / 'work'; work.mkdir()
                runner = root / 'runner'; runner.mkdir()
                scripts = work / 'scripts'; scripts.mkdir()
                shutil.copyfile('scripts/publish_grouped.py', scripts / 'publish_grouped.py')
                (scripts / 'check_staged_sizes.py').write_text('pass\n')
                (scripts / 'push_current_branch.sh').write_text('#!/bin/bash\nexit 0\n')
                bindir = root / 'bin'; bindir.mkdir()
                fakegit = bindir / 'git'; fakegit.write_text('#!/bin/bash\nexit 0\n'); fakegit.chmod(0o755)
                source_root = runner if source.startswith('meli-') else work
                source_dir = source_root / source; source_dir.mkdir(parents=True)
                for i, part in enumerate(parts): (source_dir / part).write_bytes(f'part-{i}'.encode())
                workflow = yaml.safe_load((Path('.github/workflows') / filename).read_text())
                script = next(s['run'] for s in workflow['jobs'][job]['steps'] if s.get('name') == step_name)
                env = os.environ.copy(); env.update(values)
                env.update(PATH=str(bindir) + os.pathsep + env['PATH'], RUNNER_TEMP=str(runner), GITHUB_TOKEN='test')
                subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=work, env=env, check=True, capture_output=True)
                output = work / 'downloads' / folder
                self.assertEqual({p.name for p in output.iterdir()}, set(parts))
                for i, part in enumerate(parts):
                    self.assertEqual((output / part).read_bytes(), f'part-{i}'.encode())

    def test_google_play_archive_step_groups_all_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); scripts = root / 'scripts'; scripts.mkdir()
            shutil.copyfile('scripts/publish_grouped.py', scripts / 'publish_grouped.py')
            source = root / 'gplay-output'; source.mkdir(); (source / 'base.apk').write_bytes(b'apk')
            bindir = root / 'bin'; bindir.mkdir()
            fake7z = bindir / '7z'
            fake7z.write_text('#!/bin/bash\nfor arg in "$@"; do case "$arg" in *.7z) printf a > "$arg.001"; printf b > "$arg.002";; esac; done\n')
            fake7z.chmod(0o755)
            workflow = yaml.safe_load(Path('.github/workflows/google_play_downloader.yml').read_text())
            script = next(s['run'] for s in workflow['jobs']['download-apk']['steps'] if s.get('name') == 'Create split archive (85MB chunks)')
            env = os.environ.copy()
            env.update(PATH=str(bindir) + os.pathsep + env['PATH'], RUNNER_TEMP=tmp, GITHUB_WORKSPACE=tmp,
                       WF_INPUTS_PACKAGE_NAME='com.example.app', WF_INPUTS_ARCHITECTURE='arm64',
                       WF_INPUTS_MERGE_SPLITS='true', WF_INPUTS_OUTPUT_FOLDER='downloads',
                       WF_STEPS_TIMESTAMP_OUTPUTS_VALUE='123')
            subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=root, env=env, check=True, capture_output=True)
            name = 'com.example.app_arm64_merged_123'
            target = root / 'downloads' / name
            self.assertEqual({p.name for p in target.iterdir()}, {name + '.7z.001', name + '.7z.002'})

    def test_adult_downloader_real_7z_split_is_extractable_after_publication(self):
        from scripts.publish_grouped import publish_grouped
        tool = shutil.which('7z')
        if not tool:
            self.skipTest('7-Zip is not available')
        workflow = yaml.safe_load(Path('.github/workflows/pornhub_downloader.yml').read_text())
        script = next(s['run'] for s in workflow['jobs']['download-and-maybe-split']['steps']
                      if s.get('name') == 'Conditional split (if file > 99 MB)')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'meli-video'; source.mkdir()
            payload = os.urandom(1024 * 1024 + 128)
            (source / 'video.mp4').write_bytes(payload)
            env = os.environ.copy()
            env.update(RUNNER_TEMP=tmp, GITHUB_OUTPUT=str(root / 'output'),
                       WF_STEPS_DOWNLOAD_OUTPUTS_FILENAME='video.mp4',
                       WF_STEPS_DOWNLOAD_OUTPUTS_SIZE_BYTES=str(100 * 1024 * 1024),
                       WF_GITHUB_EVENT_INPUTS_ARCHIVE_NAME='video')
            subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], env=env, cwd=root, check=True, capture_output=True)
            publish_grouped(source, root / 'downloads', name='video.mp4')
            files = sorted((root / 'downloads' / 'video.mp4').glob('*.7z.001'))
            self.assertEqual(len(files), 1)
            extract = root / 'extract'
            subprocess.run([tool, 'x', '-y', f'-o{extract}', str(files[0])], check=True, capture_output=True)
            self.assertEqual((extract / 'video.mp4').read_bytes(), payload)

    def test_release_uploads_still_use_flat_assets(self):
        cases = [
            ('url_downloader.yml', 'direct-download', 'temp_dl/archive/*'),
            ('youtube_downloader.yml', 'youtube-download', 'release_files/*'),
            ('spotfyandsoundcloud.yml', 'media-download', 'release_files/*'),
        ]
        for filename, job, assets in cases:
            with self.subTest(workflow=filename):
                workflow = yaml.safe_load((Path('.github/workflows') / filename).read_text())
                steps = workflow['jobs'][job]['steps']
                release = next(s for s in steps if str(s.get('uses', '')).startswith('softprops/action-gh-release@'))
                self.assertEqual(release['with']['files'], assets)
                publication = next(s for s in steps if 'scripts/publish_grouped.py' in s.get('run', ''))
                self.assertIn('repository', publication.get('if', ''))
