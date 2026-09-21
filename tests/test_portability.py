"""Unit tests for the cross-platform helpers in requota_migration (offline).

Run directly:  python3 tests/test_portability.py -v
Nothing here touches the network, the cache DB, or gpmc; the Windows-specific
branches are exercised by patching ``requota_migration.IS_WINDOWS``.
"""
from __future__ import annotations

import mimetypes
import os
import sys
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requota_migration as r


class SanitizeFilenameTests(unittest.TestCase):
    def test_strips_windows_illegal_characters(self):
        self.assertEqual(r.sanitize_filename('a<b>c:d"e|f?g*h'), "a_b_c_d_e_f_g_h")

    def test_strips_both_separators(self):
        self.assertEqual(r.sanitize_filename("a/b\\c.jpg"), "a_b_c.jpg")

    def test_drops_trailing_dots_and_spaces(self):
        # Windows silently discards them, which would desync the manifest.
        self.assertEqual(r.sanitize_filename("photo.jpg. "), "photo.jpg")

    def test_escapes_reserved_device_names(self):
        self.assertEqual(r.sanitize_filename("CON.jpg"), "_CON.jpg")
        self.assertEqual(r.sanitize_filename("com1"), "_com1")

    def test_ordinary_name_is_unchanged(self):
        self.assertEqual(r.sanitize_filename("PXL_20260613_130530226.jpg"), "PXL_20260613_130530226.jpg")

    def test_empty_name_falls_back(self):
        self.assertEqual(r.sanitize_filename("   "), "unnamed")

    def test_long_name_is_capped_but_keeps_extension(self):
        clean = r.sanitize_filename("x" * 400 + ".jpg")
        self.assertEqual(len(clean), r.MAX_NAME_LENGTH)
        self.assertTrue(clean.endswith(".jpg"))


class WorkspaceNameTests(unittest.TestCase):
    def test_real_media_key_and_name_survive_intact(self):
        key = "AF1QipPDtLuw0o02HHprJ31iOD7041P6VVdIEy4rE8B7"
        name = "PXL_20260613_130530226.jpg"
        self.assertEqual(r.workspace_name(key, name), f"{key}_{name}")

    def test_result_is_a_single_path_component(self):
        name = r.workspace_name("AF1/Qip+key", "sub\\dir\\photo.jpg")
        self.assertEqual(Path(name).name, name)

    def test_stays_within_the_cap(self):
        self.assertLessEqual(len(r.workspace_name("k" * 200, "n" * 200 + ".mp4")), r.MAX_NAME_LENGTH)


class EpochSecondsTests(unittest.TestCase):
    def test_milliseconds_are_converted(self):
        # remote_media.utc_timestamp is in ms: 1781348730226 -> 2026-06-13.
        self.assertEqual(r.to_epoch_seconds(1781348730226), 1781348730)

    def test_seconds_pass_through(self):
        self.assertEqual(r.to_epoch_seconds(1781348730), 1781348730)

    def test_string_input_is_accepted(self):
        self.assertEqual(r.to_epoch_seconds("1781348730226"), 1781348730)

    def test_unusable_values_return_none(self):
        for value in (None, "", "abc", 0, -5, {}):
            self.assertIsNone(r.to_epoch_seconds(value), value)


class SetFileMtimeTests(unittest.TestCase):
    def _file(self, directory: str) -> Path:
        path = Path(directory) / "photo.jpg"
        path.write_bytes(b"x")
        return path

    def test_sets_the_mtime(self):
        with TemporaryDirectory() as d:
            path = self._file(d)
            r.set_file_mtime(path, 1781348730)
            self.assertEqual(int(path.stat().st_mtime), 1781348730)

    def test_none_is_a_no_op(self):
        with TemporaryDirectory() as d:
            path = self._file(d)
            before = path.stat().st_mtime
            r.set_file_mtime(path, None)
            self.assertEqual(path.stat().st_mtime, before)

    def test_pre_1980_dates_are_kept(self):
        # NTFS stores these fine; clamping would redate old photos on upload.
        with TemporaryDirectory() as d:
            path = self._file(d)
            for ts in (1000, -631152000):
                r.set_file_mtime(path, ts)
                self.assertEqual(int(path.stat().st_mtime), ts)

    def test_pre_1980_is_clamped_only_when_the_volume_refuses_it(self):
        real_utime = os.utime

        def fat_utime(path, times, **kwargs):
            if times[0] < r.MIN_FILE_TIMESTAMP:
                raise OSError(22, "Invalid argument")
            real_utime(path, times, **kwargs)

        with TemporaryDirectory() as d:
            path = self._file(d)
            with mock.patch.object(r.os, "utime", fat_utime):
                r.set_file_mtime(path, 1000)
            self.assertEqual(int(path.stat().st_mtime), r.MIN_FILE_TIMESTAMP)

    def test_failure_is_reported_not_raised(self):
        with TemporaryDirectory() as d:
            r.set_file_mtime(Path(d) / "missing.jpg", 1781348730)


class MimetypeTests(unittest.TestCase):
    def test_media_types_are_registered_as_image_or_video(self):
        r.register_media_mimetypes()
        for extension in r.MEDIA_MIMETYPES:
            guessed = mimetypes.guess_type(f"photo{extension}")[0]
            self.assertTrue(
                guessed and (guessed.startswith("image/") or guessed.startswith("video/")),
                f"{extension} -> {guessed}",
            )

    def test_registration_overrides_a_bogus_platform_mapping(self):
        # This is what a stray Windows registry Content Type does to gpmc's filter.
        mimetypes.add_type("application/octet-stream", ".jpg")
        r.register_media_mimetypes()
        self.assertEqual(mimetypes.guess_type("photo.jpg")[0], "image/jpeg")


if __name__ == "__main__":
    unittest.main()
