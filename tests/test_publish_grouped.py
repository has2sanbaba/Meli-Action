import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.publish_grouped import folder_name, publish_grouped


class PublishGroupedTests(unittest.TestCase):
    def test_explicit_download_groups_all_parts_and_preserves_previous_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; source.mkdir(); dest = root / 'downloads'
            (source / 'video.7z.001').write_bytes(b'first')
            (source / 'video.7z.002').write_bytes(b'second')
            files = publish_grouped(source, dest, name='original video.mp4')
            self.assertEqual({p.name for p in files}, {'video.7z.001', 'video.7z.002'})
            self.assertEqual((dest / 'original video.mp4' / 'video.7z.002').read_bytes(), b'second')
            (source / 'video.7z.001').write_bytes(b'new')
            publish_grouped(source, dest, name='original video.mp4')
            self.assertEqual((dest / 'original video.mp4' / 'video.7z.001').read_bytes(), b'first')
            self.assertEqual((dest / 'original video.mp4 (2)' / 'video.7z.001').read_bytes(), b'new')

    def test_mixed_playlist_and_split_zip_and_7z_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; source.mkdir(); dest = root / 'out'
            names = ('song one.mp3.z01', 'song one.mp3.z02', 'song one.mp3.zip',
                     'song two.mp3', 'video.7z.001', 'video.7z.002', 'standalone.zip')
            for name in names: (source / name).write_bytes(name.encode())
            publish_grouped(source, dest)
            self.assertEqual({p.name for p in (dest / 'song one.mp3').iterdir()}, set(names[:3]))
            self.assertEqual((dest / 'song two.mp3' / 'song two.mp3').read_bytes(), b'song two.mp3')
            self.assertEqual({p.name for p in (dest / 'video.7z').iterdir()}, set(names[4:6]))
            self.assertTrue((dest / 'standalone.zip' / 'standalone.zip').exists())
            self.assertEqual(list(source.iterdir()), [])

    def test_youtube_playlist_sidecars_grouped_by_title_and_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, dest = Path(tmp) / 'src', Path(tmp) / 'out'; source.mkdir()
            names = ['Title [id1].mp4.z01', 'Title [id1].mp4.zip', 'Title [id1].en.vtt',
                     'Title [id1].description', 'Another [id2].mp4']
            for name in names: (source / name).write_bytes(b'data')
            publish_grouped(source, dest, youtube_sidecars=True)
            self.assertEqual({p.name for p in (dest / 'Title [id1]').iterdir()}, set(names[:4]))
            self.assertEqual({p.name for p in (dest / 'Another [id2]').iterdir()}, {names[4]})

    def test_folder_names_are_bounded_and_cannot_escape(self):
        for name in ('../../etc/passwd', '..', '\n', '/', '\\', 'ا' * 300):
            with self.subTest(name=name):
                result = folder_name(name)
                self.assertNotIn('/', result); self.assertNotIn('\\', result)
                self.assertNotIn(result, ('', '.', '..'))
                self.assertLessEqual(len(result.encode('utf-8')), 180)

    def test_symlinks_and_empty_source_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, dest = Path(tmp) / 'source', Path(tmp) / 'out'; source.mkdir()
            with self.assertRaises(ValueError): publish_grouped(source, dest)
            (source / 'bad').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError): publish_grouped(source, dest)
            self.assertFalse(dest.exists())

    def test_failure_rolls_back_all_new_folders_and_keeps_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, dest = Path(tmp) / 'source', Path(tmp) / 'out'; source.mkdir(); dest.mkdir()
            (dest / 'existing').mkdir(); (dest / 'existing' / 'keep').write_bytes(b'keep')
            (source / 'one').write_bytes(b'one'); (source / 'two').write_bytes(b'two')
            with patch('scripts.publish_grouped.os.link', side_effect=OSError(errno.EXDEV, 'cross device')):
                with patch('scripts.publish_grouped.shutil.copyfileobj', side_effect=OSError('full disk')):
                    with self.assertRaises(OSError): publish_grouped(source, dest)
            self.assertEqual(sorted(p.name for p in source.iterdir()), ['one', 'two'])
            self.assertEqual(sorted(p.name for p in dest.iterdir()), ['existing'])
            self.assertEqual((dest / 'existing' / 'keep').read_bytes(), b'keep')

    def test_cross_filesystem_fallback_preserves_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, dest = Path(tmp) / 'source', Path(tmp) / 'out'; source.mkdir()
            (source / 'one').write_bytes(b'abc')
            with patch('scripts.publish_grouped.os.link', side_effect=OSError(errno.EXDEV, 'cross device')):
                publish_grouped(source, dest)
            self.assertEqual((dest / 'one' / 'one').read_bytes(), b'abc')

    def test_existing_folder_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'src'; source.mkdir()
            dest = root / 'out'; dest.mkdir(); outside = root / 'outside'; outside.mkdir()
            (dest / 'sample').symlink_to(outside, target_is_directory=True)
            (source / 'sample').write_bytes(b'new')
            publish_grouped(source, dest)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual((dest / 'sample (2)' / 'sample').read_bytes(), b'new')

    def test_orphan_part_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'src'; source.mkdir()
            (source / 'orphan.z01').write_bytes(b'part')
            publish_grouped(source, root / 'out')
            self.assertEqual((root / 'out' / 'orphan.z01' / 'orphan.z01').read_bytes(), b'part')
