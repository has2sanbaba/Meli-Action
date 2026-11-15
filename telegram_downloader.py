#!/usr/bin/env python3
import requests
import hashlib
import json
import os
import re
import subprocess
import tempfile
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

def _metadata_path(temp_path):
    return Path(str(temp_path) + '.meta')


def _strong_etag(value):
    return bool(value and re.fullmatch(r'"[\x21\x23-\x7e]*"', value))


def _read_validator(temp_path, url):
    try:
        metadata = json.loads(_metadata_path(temp_path).read_text(encoding='utf-8'))
        validator = metadata['etag']
        if metadata['url_sha256'] == hashlib.sha256(url.encode()).hexdigest() and _strong_etag(validator):
            return validator
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return None


def _save_validator(temp_path, url, validator):
    path = _metadata_path(temp_path)
    if not _strong_etag(validator):
        path.unlink(missing_ok=True)
        return
    data = {'url_sha256': hashlib.sha256(url.encode()).hexdigest(), 'etag': validator}
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(data, f)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def download_with_resume(url, output_path, headers, max_retries=10, chunk_size=64*1024):
    """Download atomically; append only when a strong HTTP validator matches."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + '.partial')
    session = requests.Session()
    session.headers.update(headers)
    # Range offsets are for encoded bytes; requests otherwise decompresses them.
    session.headers['Accept-Encoding'] = 'identity'
    try:
        return _download_with_session(session, url, output_path, temp_path, max_retries, chunk_size)
    finally:
        session.close()


def _download_with_session(session, url, output_path, temp_path, max_retries, chunk_size):
    for attempt in range(max_retries):
        resp = None
        try:
            validator = _read_validator(temp_path, url)
            downloaded = temp_path.stat().st_size if validator and temp_path.exists() else 0
            range_header = {'Range': f'bytes={downloaded}-', 'If-Range': validator} if downloaded else {}
            resp = session.get(url, stream=True, timeout=30, headers=range_header)
            # A complete-looking partial cannot be trusted without a complete
            # successful response. Restart rather than promoting it on HTTP 416.
            if downloaded and resp.status_code == 416:
                _metadata_path(temp_path).unlink(missing_ok=True)
                raise requests.exceptions.ChunkedEncodingError('Range no longer satisfiable; restarting')
            resp.raise_for_status()
            encoding = resp.headers.get('Content-Encoding', 'identity').lower()
            if encoding not in ('', 'identity'):
                raise ValueError('Server returned an encoded response for a byte-range download')

            etag = resp.headers.get('ETag')
            if resp.status_code == 200:
                downloaded = 0  # Range ignored or entity changed; overwrite.
                total_size = int(resp.headers.get('content-length', 0))
                range_end = None
            elif resp.status_code == 206:
                if not downloaded or not validator or etag != validator:
                    _metadata_path(temp_path).unlink(missing_ok=True)
                    raise requests.exceptions.ChunkedEncodingError('Range validator missing or changed; restarting')
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', resp.headers.get('Content-Range', ''))
                if not match:
                    raise ValueError('Invalid or incomplete Content-Range')
                start, range_end, total_size = map(int, match.groups())
                if start != downloaded or range_end < start or range_end >= total_size:
                    raise ValueError('Invalid Content-Range in resumed download')
                length = resp.headers.get('content-length')
                if length is not None and int(length) != range_end - start + 1:
                    raise ValueError('Content-Length does not match Content-Range')
            else:
                raise ValueError(f'Unexpected HTTP status {resp.status_code}')

            mode = 'ab' if downloaded else 'wb'
            with open(temp_path, mode) as f:
                # Truncate old bytes before associating a new entity validator.
                if resp.status_code == 200:
                    _save_validator(temp_path, url, etag)
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            percent = downloaded / total_size * 100
                            filled = min(40, int(40 * downloaded // total_size))
                            print(f"\rProgress: |{'█' * filled}{'░' * (40 - filled)}| "
                                  f'{percent:.1f}% ({downloaded}/{total_size} bytes)', end='', flush=True)
            print()
            if range_end is not None and downloaded != range_end + 1:
                raise requests.exceptions.ChunkedEncodingError('Incomplete response body')
            if total_size and downloaded != total_size:
                raise requests.exceptions.ChunkedEncodingError('Incomplete download')
            temp_path.replace(output_path)
            _metadata_path(temp_path).unlink(missing_ok=True)
            return True
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            print(f'\n⚠️ Connection lost: {e}. Retry {attempt+1}/{max_retries}')
            if attempt + 1 < max_retries:
                time.sleep(min(2 ** attempt, 30))
        except Exception as e:
            print(f'\n❌ Fatal error: {e}')
            return False
        finally:
            if resp is not None:
                resp.close()
    print('\n❌ Max retries reached.')
    return False

def download_public_video(telegram_link, output_dir):
    """Fallback for single public videos exposed in Telegram's embed page."""
    try:
        parsed = urlsplit(telegram_link)
        match = re.fullmatch(r'/([A-Za-z0-9_]{1,32})/([1-9][0-9]{0,18})/?', parsed.path)
        if (parsed.scheme != 'https' or parsed.hostname != 't.me' or parsed.username
                or parsed.password or parsed.port is not None or not match):
            raise ValueError('Expected an HTTPS public Telegram post link')
        channel, message_id = match.groups()
        # Use a canonical link, avoiding unexpected query parameters.
        canonical_url = f'https://t.me/{channel}/{message_id}'
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.telegram-video-', dir=output_dir) as tmp:
            staging = Path(tmp).resolve()
            listing = staging / 'completed.txt'
            template = str(staging / f'telegram_{channel}_{message_id}.%(ext)s')
            cmd = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist',
                   '--restrict-filenames', '--no-progress', '--quiet', '--socket-timeout', '30',
                   '--retries', '3', '--output', template,
                   '--print-to-file', 'after_move:filepath', str(listing), '--', canonical_url]
            error_log = staging / 'ytdlp-errors.txt'
            with error_log.open('wb') as errors:
                result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=errors, timeout=3600)
            if result.returncode or not listing.is_file():
                # Report status only: yt-dlp diagnostics can contain signed media URLs.
                with error_log.open('rb') as errors:
                    errors.seek(max(0, error_log.stat().st_size - 65536))
                    error_tail = errors.read().decode('utf-8', errors='replace')
                status = re.search(r'HTTP Error ([45][0-9]{2})', error_tail)
                detail = f' (HTTP {status.group(1)})' if status else ''
                raise ValueError('The public Telegram embed did not provide a downloadable video' + detail)
            entries = listing.read_text(encoding='utf-8').splitlines()
            if len(entries) != 1:
                raise ValueError('Expected exactly one completed video')
            source = Path(entries[0])
            if (source.is_symlink() or not source.is_file() or source.resolve().parent != staging
                    or not source.name.startswith(f'telegram_{channel}_{message_id}.')
                    or source.stat().st_size == 0):
                raise ValueError('Invalid video output')
            destination = output_dir / source.name
            # Exclusive hard link prevents overwriting existing output.
            os.link(source, destination, follow_symlinks=False)
            size = source.stat().st_size
            print(f'✅ Downloaded public Telegram video ({size} bytes)')
            return True, destination.name, size
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f'❌ Public video fallback failed: {exc}')
        return False, '', 0


def download_telegram_file(telegram_link, output_dir="."):
    api_url = "https://telegramdownloader.net/proxy.php"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    data = {"telegram_link": telegram_link}
    # The service rejects requests without its own origin. These headers are
    # for the metadata endpoint only, not arbitrary returned download hosts.
    api_headers = {**headers, "Origin": "https://telegramdownloader.net",
                   "Referer": "https://telegramdownloader.net/"}

    try:
        print("🔗 Fetching download info...")
        resp = requests.post(api_url, headers=api_headers, data=data, timeout=15)
        if resp.status_code == 403:
            print("⚠️ Telegram downloader service returned HTTP 403; trying the public video embed.")
            return download_public_video(telegram_link, output_dir)
        resp.raise_for_status()
        result = resp.json()
        raw_url = result.get('data', {}).get('data', {}).get('link')
        file_name = result.get('data', {}).get('data', {}).get('file_name')
        if not raw_url or not file_name:
            print("❌ Could not extract download link or filename")
            return False, "", 0

        # The service response is untrusted; never allow paths or output-file injection.
        file_name = re.sub(r'[^a-zA-Z0-9._-]', '_', str(file_name))[:180].strip('.') or 'telegram_download'
        if file_name.startswith('-'):
            file_name = '_' + file_name
        out_path = Path(output_dir) / file_name
        print(f"📁 File: {file_name}")
        print(f"🚀 Starting download with resume support...")

        success = download_with_resume(raw_url, out_path, headers)
        if success:
            size = out_path.stat().st_size
            print(f"✅ Download complete: {file_name} ({size/1e6:.2f} MB)")
            return True, file_name, size
        return False, "", 0
    except Exception as e:
        print(f"❌ Error: {e}")
        return False, "", 0

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python telegram_downloader.py <telegram_link> [output_dir]")
        sys.exit(1)
    link = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "."
    success, fname, fsize = download_telegram_file(link, out_dir)
    if success:
        with open(Path(out_dir) / ".download_info", "w") as info:
            info.write(f"{fname}\n{fsize}")
    sys.exit(0 if success else 1)
