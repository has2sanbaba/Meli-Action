"""Validate HTTP(S) URLs or registered yt-dlp extractor pseudo-URLs.

Reject file paths and arbitrary protocol URLs: workflow inputs are remote media,
not a way to publish files from the runner. The installed yt-dlp determines the
accepted pseudo-URL extractors, rather than a static list of supported sites.
"""
import re
import sys

try:
    from .validate_workflow import validate_url
except ImportError:  # Executed as a script from the workflow.
    from validate_workflow import validate_url


def validate_input(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError('Input must be an HTTP(S) URL or supported yt-dlp pseudo-URL')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Control characters are not allowed')
    match = re.match(r'^([A-Za-z][A-Za-z0-9+._-]*):(.*)$', value)
    if not match:
        raise ValueError('Input must be an HTTP(S) URL or supported yt-dlp pseudo-URL')
    scheme, rest = match.groups()
    if scheme.lower() in ('http', 'https'):
        validate_url(value)
        return
    if scheme.lower() in ('file', 'data', 'javascript') or rest.startswith('//'):
        raise ValueError('Local files and non-HTTP protocol URLs are not allowed')
    from yt_dlp.extractor import gen_extractor_classes
    if not any(ie.ie_key() != 'Generic' and ie.suitable(value) for ie in gen_extractor_classes()):
        raise ValueError('No installed yt-dlp extractor recognises this pseudo-URL')


def main() -> int:
    if len(sys.argv) != 2:
        print('::error::Expected one media URL', file=sys.stderr)
        return 1
    try:
        validate_input(sys.argv[1])
    except ValueError as exc:
        print(f'::error::{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
