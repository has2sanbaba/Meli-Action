"""Compression policy and actual split-archive regression tests."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


class CompressionWorkflowTests(unittest.TestCase):
    def test_every_split_workflow_uses_highest_format_level(self):
        zip_cases = [
            ('telegram_downloader.yml', 'download', '✂️ Split large file (if needed)'),
            ('youtube_adv_download.yml', 'fetch-and-store', 'Split oversized files (optional)'),
            ('youtube_downloader.yml', 'youtube-download', '✂️ Split Large File (if needed)'),
            ('spotfyandsoundcloud.yml', 'media-download', 'Split Large Files'),
        ]
        seven_cases = [
            ('url_downloader.yml', 'direct-download', 'Create Split Archive'),
            ('pornhub_downloader.yml', 'download-and-maybe-split', 'Conditional split (if file > 99 MB)'),
            ('google_play_downloader.yml', 'download-apk', 'Create split archive (85MB chunks)'),
        ]
        for filename, job, name in zip_cases + seven_cases:
            with self.subTest(workflow=filename):
                workflow = yaml.safe_load((Path('.github/workflows') / filename).read_text())
                script = next(step['run'] for step in workflow['jobs'][job]['steps'] if step.get('name') == name)
                if (filename, job, name) in zip_cases:
                    lines = [line.strip() for line in script.splitlines() if line.strip().startswith('zip ')]
                    self.assertTrue(lines)
                    self.assertTrue(all('-9' in line.split() for line in lines))
                else:
                    lines = [line.strip() for line in script.splitlines() if line.strip().startswith('7z a ')]
                    self.assertTrue(lines)
                    for line in lines:
                        self.assertIn('-mx=9', line.split())
                        self.assertIn('-mmt=2', line.split())
                        self.assertIn('-md=64m', line.split())

    @unittest.skipUnless(shutil.which('zip') and shutil.which('unzip'), 'zip and unzip are required')
    def test_compressible_inputs_remain_extractable_when_split_not_needed_after_compression(self):
        cases = [
            ('telegram_downloader.yml', 'download', '✂️ Split large file (if needed)', 'meli-telegram'),
            ('youtube_adv_download.yml', 'fetch-and-store', 'Split oversized files (optional)', 'meli-media'),
            ('youtube_downloader.yml', 'youtube-download', '✂️ Split Large File (if needed)', 'release_files'),
            ('spotfyandsoundcloud.yml', 'media-download', 'Split Large Files', 'release_files'),
        ]
        payload = b'A compressible line of repeated text\n' * 65000
        for filename, job, name, source in cases:
            with self.subTest(workflow=filename), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); target = root / source; target.mkdir()
                (target / 'media.bin').write_bytes(payload)
                workflow = yaml.safe_load((Path('.github/workflows') / filename).read_text())
                script = next(step['run'] for step in workflow['jobs'][job]['steps'] if step.get('name') == name)
                env = os.environ.copy()
                env.update(RUNNER_TEMP=tmp, WF_GITHUB_EVENT_INPUTS_SPLIT_THRESHOLD_MB='1',
                           WF_STEPS_DOWNLOAD_OUTPUTS_DOWNLOADED_NAME='media.bin',
                           WF_STEPS_DOWNLOAD_OUTPUTS_FILE_SIZE=str(len(payload)),
                           WF_GITHUB_EVENT_INPUTS_OUTPUT_FOLDER='downloads', WF_GITHUB_EVENT_INPUTS_MAX_PART_MB='1',
                           WF_STEPS_RETRIEVE_OUTPUTS_FILENAME='media.bin')
                subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], env=env, cwd=root,
                               check=True, capture_output=True)
                archive, = target.glob('*.zip')
                self.assertLess(archive.stat().st_size, 1024 * 1024)
                self.assertFalse(list(target.glob('*.z01')))
                self.assertFalse((target / 'media.bin').exists())
                from scripts.publish_grouped import publish_grouped
                archive_name = archive.name
                publish_grouped(target, root / 'downloads', name='media.bin')
                published = root / 'downloads' / 'media.bin' / archive_name
                self.assertEqual(subprocess.check_output(['unzip', '-p', str(published)]), payload)

    @unittest.skipUnless(shutil.which('7z'), '7-Zip is required')
    def test_direct_7z_strong_compression_remains_lossless(self):
        workflow = yaml.safe_load(Path('.github/workflows/url_downloader.yml').read_text())
        script = next(step['run'] for step in workflow['jobs']['direct-download']['steps']
                      if step.get('name') == 'Create Split Archive')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'temp_dl' / 'source'; source.mkdir(parents=True)
            archive_dir = root / 'temp_dl' / 'archive'; archive_dir.mkdir()
            payload = b'compressible sample text 123456789\n' * 50000
            (source / 'data.bin').write_bytes(payload)
            env = os.environ.copy()
            env.update(WF_STEPS_DOWNLOAD_OUTPUTS_FILEPATH='temp_dl/source/data.bin',
                       WF_GITHUB_EVENT_INPUTS_ZIP_PASSWORD='', WF_GITHUB_EVENT_INPUTS_MAX_PART_MB='1',
                       GITHUB_OUTPUT=str(root / 'output'))
            subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], env=env, cwd=root,
                           check=True, capture_output=True)
            archive, = archive_dir.glob('*.7z')
            self.assertLess(archive.stat().st_size, len(payload) // 10)
            extraction = root / 'extracted'
            subprocess.run(['7z', 'x', '-y', f'-o{extraction}', str(archive)], check=True, capture_output=True)
            self.assertEqual((extraction / 'data.bin').read_bytes(), payload)

    def test_split_failure_does_not_delete_input(self):
        cases = [
            ('telegram_downloader.yml', 'download', '✂️ Split large file (if needed)', 'meli-telegram', 'zip'),
            ('youtube_adv_download.yml', 'fetch-and-store', 'Split oversized files (optional)', 'meli-media', 'zip'),
            ('youtube_downloader.yml', 'youtube-download', '✂️ Split Large File (if needed)', 'release_files', 'zip'),
            ('spotfyandsoundcloud.yml', 'media-download', 'Split Large Files', 'release_files', 'zip'),
            ('pornhub_downloader.yml', 'download-and-maybe-split', 'Conditional split (if file > 99 MB)', 'meli-video', '7z'),
        ]
        for filename, job, name, source, program in cases:
            with self.subTest(workflow=filename), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); target = root / source; target.mkdir()
                data = b'X' * (1024 * 1024 + 1)
                (target / 'media.bin').write_bytes(data)
                bindir = root / 'bin'; bindir.mkdir()
                fake = bindir / program
                fake.write_text('#!/bin/sh\nexit 1\n'); fake.chmod(0o755)
                workflow = yaml.safe_load((Path('.github/workflows') / filename).read_text())
                script = next(step['run'] for step in workflow['jobs'][job]['steps'] if step.get('name') == name)
                env = os.environ.copy()
                env.update(PATH=str(bindir) + os.pathsep + env['PATH'], RUNNER_TEMP=tmp,
                           GITHUB_OUTPUT=str(root / 'output'), WF_GITHUB_EVENT_INPUTS_ARCHIVE_NAME='archive',
                           WF_GITHUB_EVENT_INPUTS_SPLIT_THRESHOLD_MB='1',
                           WF_STEPS_DOWNLOAD_OUTPUTS_DOWNLOADED_NAME='media.bin',
                           WF_STEPS_DOWNLOAD_OUTPUTS_FILE_SIZE=str(len(data)),
                           WF_STEPS_DOWNLOAD_OUTPUTS_FILENAME='media.bin',
                           WF_STEPS_DOWNLOAD_OUTPUTS_SIZE_BYTES=str(100 * 1024 * 1024),
                           WF_GITHUB_EVENT_INPUTS_OUTPUT_FOLDER='downloads', WF_GITHUB_EVENT_INPUTS_MAX_PART_MB='1',
                           WF_STEPS_RETRIEVE_OUTPUTS_FILENAME='media.bin')
                result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], env=env, cwd=root,
                                        capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((target / 'media.bin').read_bytes(), data)
