"""Regression tests for the optional impersonation transport used by yt-dlp."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

import yaml


class ImpersonationTests(unittest.TestCase):
    def test_workflow_installs_supported_python_and_transport(self):
        data = yaml.safe_load(Path('.github/workflows/pornhub_downloader.yml').read_text())
        steps = data['jobs']['download-and-maybe-split']['steps']
        python = next(step for step in steps if step.get('name') == 'Set up Python')
        install = next(step['run'] for step in steps if step.get('name') == 'Install yt-dlp and 7-Zip')
        self.assertEqual(python['with']['python-version'], '3.12')
        self.assertIn('yt-dlp[default,curl-cffi]', install)
        self.assertIn('--list-impersonate-targets', install)
        self.assertIn('grep -q', install)

    @unittest.skipUnless(importlib.util.find_spec('yt_dlp') and importlib.util.find_spec('curl_cffi'),
                         'yt-dlp and curl_cffi are required')
    def test_impersonation_target_can_make_local_request(self):
        # A real HTTP request exercises the transport without contacting the
        # external site or exposing user URLs and account data.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'video.mp4').write_bytes(b'test media')
            agents = []
            class Handler(SimpleHTTPRequestHandler):
                def do_HEAD(self):
                    agents.append(self.headers.get('User-Agent', ''))
                    super().do_HEAD()
                def do_GET(self):
                    agents.append(self.headers.get('User-Agent', ''))
                    super().do_GET()
                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=tmp))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run([sys.executable, '-m', 'yt_dlp', '--ignore-config', '--impersonate', 'chrome',
                                         '--skip-download', '--print', 'title',
                                         f'http://127.0.0.1:{server.server_port}/video.mp4'],
                                        text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(agents)
                self.assertTrue(any('Chrome/' in agent for agent in agents), agents)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
