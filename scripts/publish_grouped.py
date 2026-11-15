"""Publish download outputs in separate, non-overwriting folders."""
import argparse
from collections import defaultdict
import errno
import os
from pathlib import Path
import re
import shutil
import sys


def folder_name(name: str) -> str:
    """Convert an untrusted filename to one portable, bounded path component."""
    name = re.sub(r'[\\/*?:"<>|\x00-\x1f\x7f]', '_', name).strip(' .')
    while len(name.encode('utf-8')) > 180:
        name = name[:-1]
    return name or 'download'


def group_files(files: list[Path], *, youtube_sidecars: bool = False) -> dict[str, list[Path]]:
    """Group ZIP volumes with their matching ZIP; group 7z volumes together."""
    names = {p.name for p in files}
    split_zip_bases = set()
    for name in names:
        match = re.fullmatch(r'(.+)\.z[0-9]{2,}', name, re.IGNORECASE)
        if match and match.group(1) + '.zip' in names:
            split_zip_bases.add(match.group(1))
    groups = defaultdict(list)
    for path in files:
        name = path.name
        zip_part = re.fullmatch(r'(.+)\.z[0-9]{2,}', name, re.IGNORECASE)
        seven_part = re.fullmatch(r'(.+\.7z)\.[0-9]{3,}', name, re.IGNORECASE)
        if zip_part and zip_part.group(1) in split_zip_bases:
            key = zip_part.group(1)
        elif name.endswith('.zip') and name[:-4] in split_zip_bases:
            key = name[:-4]
        elif seven_part:
            key = seven_part.group(1)
        else:
            key = name
        if youtube_sidecars:
            match = re.match(r'^(.+ \[[A-Za-z0-9_-]+\])\.', key)
            if match:
                key = match.group(1)
        groups[key].append(path)
    return dict(groups)


def _new_folder(destination: Path, name: str) -> Path:
    base = folder_name(name)
    for number in range(1, 1000000):
        suffix = '' if number == 1 else f' ({number})'
        path = destination / (base + suffix)
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise FileExistsError('Too many folders with the same name')


def publish_grouped(source: Path, destination: Path, *, name: str | None = None,
                    youtube_sidecars: bool = False) -> list[Path]:
    """Publish every regular file, preserving sources if any copy fails.

    When name is given, all files are parts of one download. Otherwise each
    file gets a folder, with recognised split volumes kept together.
    """
    if not source.is_dir() or source.is_symlink():
        raise ValueError('Source must be a real directory')
    files = sorted(source.iterdir())
    if not files:
        raise ValueError('No files to publish')
    if any(p.is_symlink() or not p.is_file() for p in files):
        raise ValueError('Only regular files can be published')
    if destination.is_symlink():
        raise ValueError('Destination must not be a symlink')
    destination.mkdir(parents=True, exist_ok=True)
    groups = {name: files} if name is not None else group_files(files, youtube_sidecars=youtube_sidecars)
    created_files, created_dirs = [], []
    try:
        for group, entries in groups.items():
            folder = _new_folder(destination, group)
            created_dirs.append(folder)
            for source_file in entries:
                target = folder / source_file.name
                try:
                    os.link(source_file, target, follow_symlinks=False)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    with open(source_file, 'rb') as src, open(target, 'xb') as dst:
                        created_files.append(target)
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                else:
                    created_files.append(target)
    except Exception:
        for target in reversed(created_files):
            target.unlink(missing_ok=True)
        for folder in reversed(created_dirs):
            folder.rmdir()
        raise
    for source_file in files:
        source_file.unlink()
    return created_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--name', help='Group all files under this original download name')
    parser.add_argument('--youtube-sidecars', action='store_true', help='Group yt-dlp sidecars by title and ID')
    args = parser.parse_args()
    try:
        files = publish_grouped(args.source, args.destination, name=args.name, youtube_sidecars=args.youtube_sidecars)
    except (OSError, ValueError) as exc:
        print(f'::error::{exc}', file=sys.stderr)
        return 1
    print(f'Published {len(files)} file(s) in {args.destination}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
