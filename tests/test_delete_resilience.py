"""Offline tests for re-upload phase resilience to transient API failures.

Reproduces the crash shape seen in a real run: a 500 from
``move_remote_media_to_trash`` during the ``--delete-originals`` pass aborting the
whole migration after every upload had already succeeded.

Run directly:  python3 tests/test_delete_resilience.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requota_migration as rm


class _Resp:
    def __init__(self, status):
        self.status_code = status


class _HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"{status} Server Error")
        self.response = _Resp(status)


class _Api:
    """Minimal stand-in for gpmc's ``client.api`` recording delete traffic."""

    def __init__(self, fail_keys=(), fail_times=None):
        """``fail_times=None`` fails forever; an int fails that many calls, then works."""
        self.fail_keys = set(fail_keys)
        self.fail_times = fail_times
        self.trashed: list[list[str]] = []
        self.deleted: list[list[str]] = []

    def move_remote_media_to_trash(self, dedup_keys):
        keys = list(dedup_keys)
        self.trashed.append(keys)
        if self.fail_keys & set(keys):
            if self.fail_times is None:
                raise _HttpError(500)
            if self.fail_times > 0:
                self.fail_times -= 1
                raise _HttpError(500)
        return {}

    def delete_remote_media_permanently(self, dedup_keys):
        self.deleted.append(list(dedup_keys))
        return {}


class _Client:
    def __init__(self, api):
        self.api = api
        self.db_path = Path("unused.db")

    def update_cache(self, show_progress=False):  # pragma: no cover - never reached here
        raise AssertionError("cache refresh not expected")


def _args(work_dir: Path, manifest: Path, **overrides):
    ns = types.SimpleNamespace(
        manifest=manifest,
        work_dir=work_dir,
        progress=False,
        saver=False,
        no_force_upload=False,
        keep_original_before_upload=True,
        no_restore_metadata=True,
        delete_originals=True,
        upload_max_attempts=1,
        retry_backoff_seconds=0,
        network_retries=1,
        network_backoff_seconds=0,
        adb_token=False,
        gpsoauth=False,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def _manifest(path: Path, count: int) -> None:
    items = [
        {
            "media_key": f"old{i}",
            "dedup_key": f"dedup{i}",
            "local_path": str(path.parent / f"{i}.jpg"),
            "download_status": "ok",
            "upload_status": "ok",
            "new_media_key": f"new{i}",
        }
        for i in range(count)
    ]
    path.write_text(json.dumps({"version": 1, "items": items}), encoding="utf-8")


class DeleteOriginalsResilienceTests(unittest.TestCase):
    def setUp(self):
        self._sleep = rm.time.sleep
        rm.time.sleep = lambda seconds: None
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        self.manifest_path = self.work_dir / "manifest.json"

    def tearDown(self):
        rm.time.sleep = self._sleep
        self._tmp.cleanup()

    def _run(self, api, count=3):
        _manifest(self.manifest_path, count)
        args = _args(self.work_dir, self.manifest_path)
        manifest, failures = rm.reupload_phase(_Client(api), args)
        saved = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return manifest, failures, saved

    def test_transient_500_is_retried_not_fatal(self):
        api = _Api(fail_keys={"dedup1"}, fail_times=1)
        _, failures, saved = self._run(api)
        self.assertEqual(failures, 0)
        self.assertTrue(all(item["old_media_deleted"] for item in saved["items"]))
        # The batch failed once, then the retry of the same batch succeeded.
        self.assertEqual(api.trashed, [["dedup0", "dedup1", "dedup2"]] * 2)

    def test_permanently_failing_key_does_not_strand_the_batch(self):
        api = _Api(fail_keys={"dedup1"})
        _, failures, saved = self._run(api)
        by_key = {item["dedup_key"]: item for item in saved["items"]}
        self.assertEqual(failures, 1)
        self.assertTrue(by_key["dedup0"]["old_media_deleted"])
        self.assertTrue(by_key["dedup2"]["old_media_deleted"])
        self.assertFalse(by_key["dedup1"]["old_media_deleted"])
        self.assertIn("500", by_key["dedup1"]["old_media_delete_error"])
        self.assertEqual(saved["failed_original_delete_count"], 1)

    def test_rerun_only_retries_what_is_left(self):
        api = _Api(fail_keys={"dedup1"})
        self._run(api)

        # Resume: everything is already uploaded, so this must not exit early --
        # it must retry just the one original that could not be deleted.
        healthy = _Api()
        args = _args(self.work_dir, self.manifest_path)
        _, failures = rm.reupload_phase(_Client(healthy), args)
        self.assertEqual(failures, 0)
        self.assertEqual(healthy.trashed, [["dedup1"]])
        self.assertEqual(healthy.deleted, [["dedup1"]])
        saved = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(all(item["old_media_deleted"] for item in saved["items"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
