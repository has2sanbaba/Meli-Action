"""Split and publish each completed yt-dlp collection item in its own folder."""
import argparse
import errno
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

try:
    from .publish_grouped import publish_grouped
except ImportError:  # Executed as a script from the workflow.
    from publish_grouped import publish_grouped


def release_asset_name(index: int, name: str) -> str:
    """Give each collection entry a unique prefix, keeping volume suffixes intact."""
    prefix = f'item{index + 1:06}_'
    match = re.search(r'(\.7z\.[0-9]{3,})$', name)
    suffix = match.group(1) if match else Path(name).suffix
    if len(suffix.encode('utf-8')) > 40:
        suffix = ''
    stem = name[:-len(suffix)] if suffix else name
    while len((prefix + stem + suffix).encode('utf-8')) > 240:
        stem = stem[:-1]
    return prefix + stem + suffix


def publish_collection(source: Path, manifest: Path, destination: Path, archive_name: str,
                       *, threshold: int = 99 * 1024 * 1024, volume_mb: int = 95,
                       flat: bool = False) -> list[Path]:
    if source.is_symlink() or not source.is_dir():
        raise ValueError('Source must be a real directory')
    names = json.loads(manifest.read_text(encoding='utf-8'))
    if (not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names)
            or len(names) != len(set(names))):
        raise ValueError('Invalid collection manifest')
    for name in names:
        if (not isinstance(name, str) or not name or name in ('.', '..') or Path(name).name != name
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise ValueError('Invalid filename in collection manifest')
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Collection file is missing or is not a regular file')
    if any((source / name).stat().st_size > threshold for name in names):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', archive_name):
            raise ValueError('Invalid archive name')

    published = []
    # Stage all items before publishing any: failure preserves the original
    # downloads and cannot commit or push partial results.
    with tempfile.TemporaryDirectory(prefix='meli-ytdlp-collection-', dir=source.parent) as tmp:
        staged = []
        for index, name in enumerate(names):
            src = source / name
            item = Path(tmp) / str(index)
            item.mkdir()
            if src.stat().st_size > threshold:
                archive = item / f'{archive_name}_{secrets.token_hex(3)}.7z'
                subprocess.run(['7z', 'a', f'-v{volume_mb}m', '-mx=9', '-mmt=2', '-md=64m',
                                str(archive), '--', name], cwd=source, check=True)
                if not list(item.glob('*.7z.001')):
                    raise ValueError('7-Zip returned no archive volumes')
            else:
                try:
                    os.link(src, item / name, follow_symlinks=False)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    shutil.copyfile(src, item / name)
            staged.append((name, item))
        if flat:
            if destination.is_symlink():
                raise ValueError('Destination must not be a symlink')
            destination.mkdir(parents=True, exist_ok=True)
        asset_map = []
        try:
            for index, (name, item) in enumerate(staged):
                if not flat:
                    published.extend(publish_grouped(item, destination, name=name))
                    continue
                assets = []
                for entry in sorted(item.iterdir()):
                    target = destination / release_asset_name(index, entry.name)
                    try:
                        os.link(entry, target, follow_symlinks=False)
                    except OSError as exc:
                        if exc.errno != errno.EXDEV:
                            raise
                        with open(entry, 'rb') as src, open(target, 'xb') as dst:
                            published.append(target)
                            shutil.copyfileobj(src, dst, length=1024 * 1024)
                    else:
                        published.append(target)
                    assets.append(target.name)
                asset_map.append({'filename': name, 'assets': assets})
            if flat:
                manifest_path = destination / 'yt-dlp-collection-manifest.json'
                with open(manifest_path, 'x', encoding='utf-8') as out:
                    published.append(manifest_path)
                    json.dump(asset_map, out, ensure_ascii=False, indent=2)
                    out.write('\n')
        except Exception:
            folders = {path.parent for path in published} if not flat else set()
            for path in reversed(published):
                path.unlink(missing_ok=True)
            for folder in folders:
                folder.rmdir()
            raise
    return published


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('archive_name')
    parser.add_argument('--flat', action='store_true', help='Stage uniquely named flat Release assets')
    args = parser.parse_args()
    try:
        files = publish_collection(args.source, args.manifest, args.destination, args.archive_name, flat=args.flat)
    except (OSError, ValueError, TypeError, subprocess.CalledProcessError) as exc:
        print(f'::error::Could not publish collection: {exc}', file=sys.stderr)
        return 1
    print(f'Published {len(files)} file(s) in {args.destination}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
