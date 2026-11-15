import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import requests
import telegram_downloader as telegram


def prepare_partial(path, content, url='https://example.com'):
    partial = Path(str(path) + '.partial')
    partial.write_bytes(content)
    telegram._save_validator(partial, url, '"version-one"')
    return partial


def response(status, data, headers):
    result = Mock(status_code=status, headers=headers)
    result.iter_content.return_value = [data]
    return result


class TelegramTests(unittest.TestCase):
    def test_range_ignored_restarts_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'file'
            prepare_partial(path, b'partial')
            session = Mock(headers={})
            session.get.return_value = response(200, b'complete', {'content-length': '8'})
            with patch.object(requests, 'Session', return_value=session):
                self.assertTrue(telegram.download_with_resume('https://example.com', path, {}))
            self.assertEqual(path.read_bytes(), b'complete')

    def test_valid_range_is_appended(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'file'
            prepare_partial(path, b'abc')
            session = Mock(headers={})
            session.get.return_value = response(206, b'def', {'content-length': '3', 'Content-Range': 'bytes 3-5/6', 'ETag': '"version-one"'})
            with patch.object(requests, 'Session', return_value=session):
                self.assertTrue(telegram.download_with_resume('https://example.com', path, {}))
            self.assertEqual(path.read_bytes(), b'abcdef')

    def test_invalid_range_does_not_publish(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'file'
            prepare_partial(path, b'abc')
            session = Mock(headers={})
            session.get.return_value = response(206, b'def', {'content-length': '3', 'Content-Range': 'bytes 0-2/6', 'ETag': '"version-one"'})
            with patch.object(requests, 'Session', return_value=session):
                self.assertFalse(telegram.download_with_resume('https://example.com', path, {}))
            self.assertFalse(path.exists())

class TelegramHTTPIntegrationTests(unittest.TestCase):
    def test_resume_against_local_http_server(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(206)
                self.send_header('Content-Range', 'bytes 3-5/6')
                self.send_header('Content-Length', '3')
                self.send_header('ETag', '"version-one"')
                self.end_headers()
                self.wfile.write(b'def')
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / 'file'
                url = f'http://127.0.0.1:{server.server_port}/file'
                prepare_partial(path, b'abc', url)
                self.assertTrue(telegram.download_with_resume(url, path, {}))
                self.assertEqual(path.read_bytes(), b'abcdef')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

class TelegramRangeTests(unittest.TestCase):
    def test_range_shorter_than_resource_is_not_published(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'file'
            prepare_partial(path, b'abc')
            session = Mock(headers={})
            session.get.return_value = response(206, b'def', {'content-length': '3', 'Content-Range': 'bytes 3-5/9', 'ETag': '"version-one"'})
            with patch.object(requests, 'Session', return_value=session), patch.object(telegram.time, 'sleep'):
                self.assertFalse(telegram.download_with_resume('https://example.com', path, {}, max_retries=1))
            self.assertFalse(path.exists())
            self.assertEqual(Path(str(path) + '.partial').read_bytes(), b'abcdef')

    def test_malformed_range_does_not_append(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'file'
            partial = Path(str(path) + '.partial')
            prepare_partial(path, b'abc')
            session = Mock(headers={})
            session.get.return_value = response(206, b'def', {'content-length': '3', 'Content-Range': 'bytes 3-5/5', 'ETag': '"version-one"'})
            with patch.object(requests, 'Session', return_value=session):
                self.assertFalse(telegram.download_with_resume('https://example.com', path, {}))
            self.assertEqual(partial.read_bytes(), b'abc')

class TelegramValidatorTests(unittest.TestCase):
    def test_existing_partial_without_metadata_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'file'
            Path(str(path) + '.partial').write_bytes(b'old')
            session = Mock(headers={})
            session.get.return_value = response(200, b'new-data', {'content-length': '8'})
            with patch.object(requests, 'Session', return_value=session):
                self.assertTrue(telegram.download_with_resume('https://example.com', path, {}))
            self.assertEqual(path.read_bytes(), b'new-data')
            self.assertEqual(session.get.call_args.kwargs['headers'], {})

    def test_changed_validator_never_appends_old_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'file'
            prepare_partial(path, b'old')
            session = Mock(headers={})
            session.get.side_effect = [
                response(206, b'xyz', {'content-length': '3', 'Content-Range': 'bytes 3-5/6', 'ETag': '"different"'}),
                response(200, b'new-data', {'content-length': '8', 'ETag': '"different"'}),
            ]
            with patch.object(requests, 'Session', return_value=session), patch.object(telegram.time, 'sleep'):
                self.assertTrue(telegram.download_with_resume('https://example.com', path, {}, max_retries=2))
            self.assertEqual(path.read_bytes(), b'new-data')
            self.assertEqual(session.get.call_args_list[0].kwargs['headers']['If-Range'], '"version-one"')
            self.assertEqual(session.get.call_args_list[1].kwargs['headers'], {})

    def test_changed_url_does_not_reuse_partial(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'file'
            prepare_partial(path, b'old', 'https://example.com/one')
            session = Mock(headers={})
            session.get.return_value = response(200, b'other', {'content-length': '5'})
            with patch.object(requests, 'Session', return_value=session):
                self.assertTrue(telegram.download_with_resume('https://example.com/two', path, {}))
            self.assertEqual(path.read_bytes(), b'other')
            self.assertEqual(session.get.call_args.kwargs['headers'], {})

class TelegramServiceIntegrationTests(unittest.TestCase):
    def test_metadata_uses_service_origin_and_downloads_file(self):
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from urllib.parse import parse_qs

        seen = []
        download_headers = []
        content = b'downloaded media'
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                headers = dict(self.headers)
                length = int(self.headers['Content-Length'])
                body = parse_qs(self.rfile.read(length).decode())
                seen.append((headers, body))
                if headers.get('Origin') != 'https://telegramdownloader.net' or headers.get('Referer') != 'https://telegramdownloader.net/':
                    self.send_response(403); self.end_headers(); return
                result = json.dumps({'data': {'data': {'link': f'http://127.0.0.1:{self.server.server_port}/file',
                                                       'file_name': '../example.bin'}}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(result)))
                self.end_headers(); self.wfile.write(result)
            def do_GET(self):
                download_headers.append(dict(self.headers))
                self.send_response(200)
                self.send_header('Content-Length', str(len(content)))
                self.end_headers(); self.wfile.write(content)
            def log_message(self, *args): pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            real_post = requests.post
            def local_post(url, **kwargs):
                self.assertEqual(url, 'https://telegramdownloader.net/proxy.php')
                return real_post(f'http://127.0.0.1:{server.server_port}/proxy.php', **kwargs)
            with tempfile.TemporaryDirectory() as tmp, patch.object(requests, 'post', side_effect=local_post):
                ok, filename, size = telegram.download_telegram_file('https://t.me/example/1', tmp)
                self.assertTrue(ok)
                self.assertEqual(size, len(content))
                self.assertEqual((Path(tmp) / filename).read_bytes(), content)
                self.assertEqual(seen[0][1], {'telegram_link': ['https://t.me/example/1']})
                self.assertNotIn('Origin', download_headers[0])
                self.assertNotIn('Referer', download_headers[0])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_403_is_reported_without_parsing_body_or_starting_download(self):
        import io
        from contextlib import redirect_stdout
        response_403 = Mock(status_code=403)
        with tempfile.TemporaryDirectory() as tmp, patch.object(requests, 'post', return_value=response_403), \
             patch.object(telegram, 'download_with_resume') as download, \
             patch.object(telegram, 'download_public_video', return_value=(False, '', 0)) as fallback, \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(telegram.download_telegram_file('https://t.me/example/1', tmp), (False, '', 0))
            self.assertIn('HTTP 403', output.getvalue())
            response_403.json.assert_not_called()
            download.assert_not_called()
            fallback.assert_called_once_with('https://t.me/example/1', tmp)

class TelegramVideoFallbackTests(unittest.TestCase):
    def test_rejects_untrusted_urls_before_subprocess(self):
        invalid = ['https://t.me.evil.example/channel/1', 'https://t.me@evil.example/channel/1',
                   'https://t.me/c/123/1', 'https://t.me/channel/-1', 'http://t.me/channel/1',
                   'https://t.me/channel/1/extra', 'https://t.me:444/channel/1']
        with tempfile.TemporaryDirectory() as tmp, patch.object(telegram.subprocess, 'run') as run:
            for url in invalid:
                with self.subTest(url=url):
                    self.assertEqual(telegram.download_public_video(url, tmp), (False, '', 0))
            run.assert_not_called()

    def test_success_and_no_overwrite(self):
        def fake_run(args, **kwargs):
            self.assertEqual(args[-1], 'https://t.me/channel/123')
            self.assertIn('--ignore-config', args)
            self.assertIn('--no-playlist', args)
            template = Path(args[args.index('--output') + 1])
            video = Path(str(template).replace('%(ext)s', 'mp4'))
            video.write_bytes(b'video content')
            Path(args[args.index('--print-to-file') + 2]).write_text(str(video) + '\n')
            return Mock(returncode=0)
        with tempfile.TemporaryDirectory() as tmp, patch.object(telegram.subprocess, 'run', side_effect=fake_run):
            result = telegram.download_public_video('https://t.me/channel/123?tracking=yes', tmp)
            self.assertEqual(result, (True, 'telegram_channel_123.mp4', 13))
            self.assertEqual((Path(tmp) / result[1]).read_bytes(), b'video content')
            self.assertEqual(telegram.download_public_video('https://t.me/channel/123', tmp), (False, '', 0))
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), [result[1]])

    def test_bad_output_is_not_published(self):
        def fake_run(args, **kwargs):
            Path(args[args.index('--print-to-file') + 2]).write_text('/etc/passwd\n')
            return Mock(returncode=0)
        with tempfile.TemporaryDirectory() as tmp, patch.object(telegram.subprocess, 'run', side_effect=fake_run):
            self.assertEqual(telegram.download_public_video('https://t.me/channel/123', tmp), (False, '', 0))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_403_fallback_uses_real_ytdlp_telegram_extractor_with_local_media(self):
        import importlib.util
        import os
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        if not importlib.util.find_spec('yt_dlp'):
            self.skipTest('yt-dlp is not installed')
        media = b'example-video-bytes'
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(len(media)))
                self.end_headers(); self.wfile.write(media)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); output = root / 'out'; output.mkdir()
                # Replace only the external embed page; actual extractor,
                # format selection, HTTP download and CLI output are real.
                sitecustomize = root / 'sitecustomize.py'
                sitecustomize.write_text('''import os
from yt_dlp.extractor.telegram import TelegramEmbedIE
TelegramEmbedIE._download_webpage = lambda self, *args, **kwargs: (
    '<a class="tgme_widget_message_video_player" href="https://t.me/channel/123">'
    '<video src="' + os.environ['TEST_VIDEO_URL'] + '"></video>'
    '<time class="duration">0:01</time></a>'
)
''')
                env = {'PYTHONPATH': tmp + os.pathsep + os.environ.get('PYTHONPATH', ''),
                       'TEST_VIDEO_URL': f'http://127.0.0.1:{server.server_port}/video.mp4'}
                with patch.dict(os.environ, env), patch.object(requests, 'post', return_value=Mock(status_code=403)):
                    result = telegram.download_telegram_file('https://t.me/channel/123', str(output))
                self.assertEqual(result, (True, 'telegram_channel_123.mp4', len(media)))
                self.assertEqual((output / result[1]).read_bytes(), media)
        finally:
            server.shutdown(); server.server_close(); thread.join()
