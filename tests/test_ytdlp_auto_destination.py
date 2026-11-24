"""Quality selection and automatic repository/Release routing."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW = Path('.github/workflows/pornhub_downloader.yml')


def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding='utf-8'))


def steps():
    return workflow()['jobs']['download-and-maybe-split']['steps']


class AutoDestinationTests(unittest.TestCase):
    def decide(self, method, size):
        script = next(step['run'] for step in steps() if step.get('id') == 'destination')
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'output'
            env = os.environ.copy()
            env.update(REQUESTED_METHOD=method, TOTAL_BYTES=size, GITHUB_OUTPUT=str(output))
            result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], env=env,
                                    capture_output=True, text=True)
            return result, output.read_text() if output.exists() else ''

    def test_threshold_and_explicit_choice(self):
        gib = 1024 * 1024 * 1024
        cases = [
            ('repository', 0, 'repository'),
            ('repository', gib - 1, 'repository'),
            ('repository', gib, 'release'),
            ('repository', gib + 1, 'release'),
            ('release', 1, 'release'),
            ('release', gib + 1, 'release'),
        ]
        for requested, size, expected in cases:
            with self.subTest(requested=requested, size=size):
                result, output = self.decide(requested, str(size))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.strip(), f'method={expected}')
                if requested == 'repository' and expected == 'release':
                    self.assertIn('using Release instead of repository', result.stdout)

    def test_multiple_download_sizes_are_summed_before_routing(self):
        download = next(s['run'] for s in steps() if s.get('id') == 'download')
        decision = next(s['run'] for s in steps() if s.get('id') == 'destination')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); fake = root / 'yt-dlp'
            fake.write_text("""#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
folder = Path(os.environ['RUNNER_TEMP']) / 'meli-video'
files = [folder / 'one.mp4', folder / 'two.mp4']
for path in files:
    with path.open('wb') as f: f.truncate(600 * 1024 * 1024)
Path(args[args.index('--print-to-file') + 2]).write_text('\\n'.join(map(str, files)) + '\\n')
""")
            fake.chmod(0o755)
            env = os.environ.copy()
            env.update(PATH=tmp + os.pathsep + env['PATH'], RUNNER_TEMP=tmp,
                       GITHUB_OUTPUT=str(root / 'download-output'), WF_GITHUB_EVENT_INPUTS_QUALITY='best',
                       WF_GITHUB_EVENT_INPUTS_URL='https://example.com/video', WF_DOWNLOAD_PLAYLIST='true')
            subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', download], env=env, check=True, capture_output=True)
            outputs = dict(line.split('=', 1) for line in (root / 'download-output').read_text().splitlines())
            self.assertEqual(int(outputs['total_bytes']), 1200 * 1024 * 1024)
            self.assertEqual(outputs['is_collection'], 'true')
            env.update(REQUESTED_METHOD='repository', TOTAL_BYTES=outputs['total_bytes'],
                       GITHUB_OUTPUT=str(root / 'destination-output'))
            subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', decision], env=env, check=True, capture_output=True)
            self.assertEqual((root / 'destination-output').read_text().strip(), 'method=release')

    def test_invalid_input_does_not_route_or_publish(self):
        for method, size in [('invalid', '1'), ('repository', ''), ('repository', '-1'), ('release', '1.5')]:
            with self.subTest(method=method, size=size):
                result, output = self.decide(method, size)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output)

    def test_resolved_method_controls_every_publication_step(self):
        all_steps = steps()
        download = next(s for s in all_steps if s.get('id') == 'download')
        decision = next(s for s in all_steps if s.get('id') == 'destination')
        split = next(s for s in all_steps if s.get('id') == 'split')
        self.assertLess(all_steps.index(download), all_steps.index(decision))
        self.assertLess(all_steps.index(decision), all_steps.index(split))
        self.assertEqual(decision['env']['TOTAL_BYTES'], '${{ steps.download.outputs.total_bytes }}')
        for name in ('Commit and push to repository', 'Validate inputs'):
            self.assertEqual(next(s for s in all_steps if s.get('name') == name)['if'],
                             "steps.destination.outputs.method == 'repository'")
        for name in ('Prepare flat Release assets', 'Upload to GitHub Releases', 'Publish completed GitHub Release'):
            self.assertEqual(next(s for s in all_steps if s.get('name') == name)['if'],
                             "steps.destination.outputs.method == 'release'")


class QualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data = workflow()
        inputs = data.get('on', data.get(True))['workflow_dispatch']['inputs']
        cls.selector = inputs['quality']['default']

    def test_default_caps_both_merge_and_combined_video_fallbacks(self):
        self.assertEqual(self.selector, 'bv*[height<=720]+ba/b[height<=720]/bv*[height<=720]/ba')

    @unittest.skipUnless(importlib.util.find_spec('yt_dlp'), 'yt-dlp is required')
    def test_actual_yt_dlp_selector_picks_720_then_480_and_supports_audio(self):
        import yt_dlp
        cases = [
            ([('1080', 1080, False), ('720', 720, False), ('480', 480, False), ('audio', None, True)], '720', 720),
            ([('1080', 1080, False), ('480', 480, False), ('audio', None, True)], '480', 480),
            ([('1080', 1080, False), ('360', 360, False), ('audio', None, True)], '360', 360),
            ([('audio', None, True)], 'audio', None),
        ]
        for formats, expected, height in cases:
            info = {'id': 'test', 'title': 'test', 'extractor': 'generic', 'extractor_key': 'Generic',
                    'formats': [dict(format_id=name, height=h, vcodec='none' if audio else 'avc1',
                                     acodec='mp4a' if audio else 'none', ext='mp4',
                                     url='http://localhost/file') for name, h, audio in formats]}
            with self.subTest(formats=formats), yt_dlp.YoutubeDL({'format': self.selector, 'quiet': True,
                                                                 'simulate': True, 'no_warnings': True}) as downloader:
                selected = downloader.process_ie_result(info, download=False)
                self.assertEqual(selected.get('height'), height)
                self.assertEqual(selected['format_id'].split('+')[0], expected)

    @unittest.skipUnless(importlib.util.find_spec('yt_dlp'), 'yt-dlp is required')
    def test_combined_only_fallback_does_not_choose_1080p(self):
        import yt_dlp
        formats = [dict(format_id=str(h), height=h, vcodec='avc1', acodec='mp4a', ext='mp4',
                        url='http://localhost/file') for h in (1080, 480)]
        with yt_dlp.YoutubeDL({'format': self.selector, 'quiet': True, 'simulate': True, 'no_warnings': True}) as ydl:
            selected = ydl.process_ie_result({'id': 'test', 'title': 'test', 'extractor': 'generic',
                                              'extractor_key': 'Generic', 'formats': formats}, download=False)
        self.assertEqual(selected['format_id'], '480')
        self.assertEqual(selected['height'], 480)
