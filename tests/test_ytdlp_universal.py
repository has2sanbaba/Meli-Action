"""Universal yt-dlp workflow and collection publication regression tests."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts.publish_ytdlp_collection import publish_collection
from scripts.validate_ytdlp_input import validate_input


WORKFLOW = Path('.github/workflows/pornhub_downloader.yml')


def workflow_steps():
    return yaml.safe_load(WORKFLOW.read_text())['jobs']['download-and-maybe-split']['steps']


class UniversalWorkflowTests(unittest.TestCase):
    def test_install_uses_latest_stable_and_enables_required_dependencies(self):
        steps = workflow_steps()
        install = next(step['run'] for step in steps if step.get('name') == 'Install yt-dlp and 7-Zip')
        self.assertIn('pip install --upgrade "yt-dlp[default,curl-cffi]"', install)
        self.assertIn('python -m yt_dlp --version', install)
        self.assertEqual(next(s for s in steps if s.get('name') == 'Set up Node.js')['with']['node-version'], '22')
        download = next(s['run'] for s in steps if s.get('id') == 'download')
        self.assertIn('--js-runtimes node', download)
        self.assertNotIn('--merge-output-format mp4', download)
        self.assertNotIn('--impersonate', download)  # Leave target selection to each extractor.

    def run_fake_download(self, tmp, *, playlist='true', cookies='', outside=False, fail=False):
        root = Path(tmp)
        fake = root / 'yt-dlp'
        fake.write_text('''#!/usr/bin/env python3
import json, os, stat, sys
from pathlib import Path
a = sys.argv[1:]
info = {'args': a}
if '--cookies' in a:
    p = Path(a[a.index('--cookies') + 1])
    info.update(cookie_mode=stat.S_IMODE(p.stat().st_mode), cookie_path=str(p), cookie=p.read_text())
Path(os.environ['ARG_LOG']).write_text(json.dumps(info))
if os.environ.get('FAKE_FAIL') == 'true':
    sys.exit(1)
folder = Path(os.environ['RUNNER_TEMP']) / 'meli-video'
paths = [folder / 'one.mp4', folder / 'two.webm']
for i, path in enumerate(paths): path.write_bytes(bytes([i]) * 10)
if os.environ.get('FAKE_OUTSIDE') == 'true':
    paths.append(Path(os.environ['RUNNER_TEMP']) / 'outside.mp4')
    paths[-1].write_bytes(b'outside')
Path(a[a.index('--print-to-file') + 2]).write_text('\\n'.join(map(str, paths)) + '\\n')
''')
        fake.chmod(0o755)
        if fail:
            sleep = root / 'sleep'; sleep.write_text('#!/bin/sh\nexit 0\n'); sleep.chmod(0o755)
        env = os.environ.copy()
        env.update(PATH=tmp + os.pathsep + env['PATH'], RUNNER_TEMP=tmp, GITHUB_OUTPUT=str(root / 'output'),
                   ARG_LOG=str(root / 'args.json'), WF_GITHUB_EVENT_INPUTS_QUALITY='bv*+ba/b',
                   WF_GITHUB_EVENT_INPUTS_URL='https://example.com/video', WF_DOWNLOAD_PLAYLIST=playlist,
                   SECRET_YTDLP_COOKIES=cookies, FAKE_OUTSIDE=str(outside).lower(), FAKE_FAIL=str(fail).lower())
        source = root / 'meli-video'; source.mkdir(); (source / 'stale.part').write_text('partial')
        script = next(s['run'] for s in workflow_steps() if s.get('id') == 'download')
        result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=root, env=env,
                                capture_output=True, text=True)
        return result, json.loads((root / 'args.json').read_text())

    def test_collection_tracks_all_files_and_cookies_are_private_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, data = self.run_fake_download(tmp, cookies='private-session-cookie')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--yes-playlist', data['args'])
            self.assertEqual(data['cookie_mode'], 0o600)
            self.assertEqual(data['cookie'], 'private-session-cookie')
            self.assertFalse(Path(data['cookie_path']).exists())
            self.assertNotIn(data['cookie'], result.stdout + result.stderr)
            self.assertEqual(json.loads((Path(tmp) / 'meli-video-manifest.json').read_text()), ['one.mp4', 'two.webm'])
            self.assertEqual({p.name for p in (Path(tmp) / 'meli-video').iterdir()}, {'one.mp4', 'two.webm'})
            self.assertIn('is_collection=true', (Path(tmp) / 'output').read_text())
            self.assertIn('total_bytes=20', (Path(tmp) / 'output').read_text())

    @unittest.skipUnless(shutil.which('yt-dlp'), 'yt-dlp is required')
    def test_real_ytdlp_downloads_generic_audio_collection_and_workflow_publishes_it(self):
        import functools
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        import threading
        import wave

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); web = root / 'web'; web.mkdir()
            for name in ('one', 'two'):
                with wave.open(str(web / f'{name}.wav'), 'wb') as audio:
                    audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000)
                    audio.writeframes(b'\0' * 16000)
            (web / 'index.html').write_text('<title>Collection</title><audio src="one.wav"></audio><audio src="two.wav"></audio>')
            class QuietHandler(SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(web)))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                work = root / 'work'; work.mkdir()
                scripts = work / 'scripts'; scripts.mkdir()
                for name in ('publish_grouped.py', 'publish_ytdlp_collection.py'):
                    shutil.copyfile(Path('scripts') / name, scripts / name)
                (scripts / 'check_staged_sizes.py').write_text('pass\n')
                (scripts / 'push_current_branch.sh').write_text('#!/bin/sh\nexit 0\n')
                bindir = root / 'bin'; bindir.mkdir()
                git = bindir / 'git'; git.write_text('#!/bin/sh\nexit 0\n'); git.chmod(0o755)
                env = os.environ.copy()
                env.update(PATH=str(bindir) + os.pathsep + env['PATH'], RUNNER_TEMP=tmp, GITHUB_OUTPUT=str(root / 'output'),
                           WF_GITHUB_EVENT_INPUTS_QUALITY='bv*+ba/b',
                           WF_GITHUB_EVENT_INPUTS_URL=f'http://127.0.0.1:{server.server_port}/index.html',
                           WF_DOWNLOAD_PLAYLIST='true', WF_GITHUB_EVENT_INPUTS_OUTPUT_FOLDER='downloads',
                           WF_STEPS_DOWNLOAD_OUTPUTS_FILENAME='unused', WF_IS_COLLECTION='true', WF_ARCHIVE_NAME='video')
                download = next(s['run'] for s in workflow_steps() if s.get('id') == 'download')
                subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', download], cwd=work, env=env, check=True, capture_output=True)
                names = json.loads((root / 'meli-video-manifest.json').read_text())
                self.assertEqual(len(names), 2)
                self.assertIn('is_collection=true', (root / 'output').read_text())
                publish = next(s['run'] for s in workflow_steps() if s.get('name') == 'Commit and push to repository')
                subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', publish], cwd=work, env=env, check=True, capture_output=True)
                for name in names:
                    self.assertEqual((work / 'downloads' / name / name).read_bytes(), (web / 'one.wav').read_bytes())
            finally:
                server.shutdown(); server.server_close(); thread.join()

    def test_default_is_no_playlist_and_no_cookie_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, data = self.run_fake_download(tmp, playlist='false')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--no-playlist', data['args'])
            self.assertNotIn('--cookies', data['args'])

    def test_invalid_output_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self.run_fake_download(tmp, outside=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(tmp) / 'meli-video-manifest.json').exists())
            self.assertEqual((Path(tmp) / 'outside.mp4').read_bytes(), b'outside')

    def test_cookie_removed_on_download_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, data = self.run_fake_download(tmp, cookies='private-session-cookie', fail=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(Path(data['cookie_path']).exists())
            self.assertNotIn(data['cookie'], result.stdout + result.stderr)


class InputValidationTests(unittest.TestCase):
    def test_http_and_rejected_local_or_arbitrary_protocol_inputs(self):
        validate_input('https://example.com/video?id=1')
        for value in ('/etc/passwd', 'file:///etc/passwd', 'file:foo', 'ftp://example.com/file',
                      'https://example.com/\nmalicious', 'https://', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_input(value)

    @unittest.skipUnless(importlib.util.find_spec('yt_dlp'), 'yt-dlp is required')
    def test_pseudo_urls_are_recognised_dynamically_from_installed_extractors(self):
        validate_input('ytsearch5:example search')
        validate_input('audius:123456789')
        with self.assertRaises(ValueError):
            validate_input('not-an-extractor:whatever')


class CollectionPublicationTests(unittest.TestCase):
    def test_multiple_small_items_get_separate_folders_and_originals_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; source.mkdir()
            (source / 'one.mp3').write_bytes(b'one')
            (source / 'two.webm').write_bytes(b'two')
            manifest = root / 'manifest.json'; manifest.write_text('["one.mp3", "two.webm"]')
            published = publish_collection(source, manifest, root / 'downloads', 'video')
            self.assertEqual(len(published), 2)
            self.assertEqual((root / 'downloads/one.mp3/one.mp3').read_bytes(), b'one')
            self.assertEqual((root / 'downloads/two.webm/two.webm').read_bytes(), b'two')
            self.assertTrue((source / 'one.mp3').exists())

    @unittest.skipUnless(shutil.which('7z'), '7-Zip is required')
    def test_mixed_collection_splits_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; source.mkdir()
            data = os.urandom(1024 * 1024 + 4096)
            (source / 'large.webm').write_bytes(data)
            (source / 'small.mp3').write_bytes(b'audio')
            manifest = root / 'manifest.json'; manifest.write_text('["large.webm", "small.mp3"]')
            publish_collection(source, manifest, root / 'downloads', 'video', threshold=1024 * 1024, volume_mb=1)
            parts = sorted((root / 'downloads/large.webm').glob('*.7z.0*'))
            self.assertGreaterEqual(len(parts), 2)
            self.assertTrue(all(part.stat().st_size <= 1024 * 1024 for part in parts))
            out = root / 'out'
            subprocess.run(['7z', 'x', '-y', f'-o{out}', str(parts[0])], check=True, capture_output=True)
            self.assertEqual((out / 'large.webm').read_bytes(), data)
            self.assertEqual((root / 'downloads/small.mp3/small.mp3').read_bytes(), b'audio')
            self.assertEqual((source / 'large.webm').read_bytes(), data)

    def test_archive_failure_leaves_originals_unpublished(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; source.mkdir()
            (source / 'a.mp3').write_bytes(b'a'); (source / 'b.mp4').write_bytes(b'large')
            manifest = root / 'manifest.json'; manifest.write_text('["a.mp3", "b.mp4"]')
            with patch('scripts.publish_ytdlp_collection.subprocess.run', side_effect=subprocess.CalledProcessError(1, '7z')):
                with self.assertRaises(subprocess.CalledProcessError):
                    publish_collection(source, manifest, root / 'downloads', 'video', threshold=1)
            self.assertEqual((source / 'b.mp4').read_bytes(), b'large')
            self.assertFalse((root / 'downloads').exists())

    def test_later_publication_failure_rolls_back_earlier_item(self):
        from scripts.publish_grouped import publish_grouped
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; source.mkdir()
            (source / 'a').write_bytes(b'a'); (source / 'b').write_bytes(b'b')
            manifest = root / 'manifest.json'; manifest.write_text('["a", "b"]')
            def publish_or_fail(item, dest, *, name):
                if name == 'b':
                    raise OSError('simulated failure')
                return publish_grouped(item, dest, name=name)
            with patch('scripts.publish_ytdlp_collection.publish_grouped', side_effect=publish_or_fail):
                with self.assertRaises(OSError):
                    publish_collection(source, manifest, root / 'downloads', 'video')
            self.assertFalse((root / 'downloads' / 'a').exists())
            self.assertTrue((source / 'a').exists())
            self.assertTrue((source / 'b').exists())

    def test_manifest_rejects_traversal_duplicates_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; source.mkdir()
            (source / 'one').write_text('one'); (source / 'link').symlink_to(source / 'one')
            manifest = root / 'manifest.json'
            for names in [['../outside'], ['one', 'one'], ['link'], [None], []]:
                with self.subTest(names=names):
                    manifest.write_text(json.dumps(names))
                    with self.assertRaises(ValueError):
                        publish_collection(source, manifest, root / 'downloads', 'video')
            self.assertFalse((root / 'downloads').exists())
