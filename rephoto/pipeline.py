from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import os
from pathlib import Path
import shutil
from typing import Any
import zipfile

from rephoto.adb_push import push_batch_files
from rephoto.browser import PhotosBrowserSession
from rephoto.config import RephotoConfig
from rephoto.manifest import (
    BATCH_DOWNLOADED,
    BATCH_FAILED,
    BATCH_PUSHED,
    BATCH_TRASHED,
    BATCH_VERIFIED,
    ManifestStore,
)


class RunMode(str, Enum):
    DRY_RUN = "dry-run"
    DOWNLOAD_ONLY = "download-only"
    DOWNLOAD_DELETE = "download-delete"
    DOWNLOAD_DELETE_PUSH = "download-delete-push"
    PUSH_ONLY = "push-only"


@dataclass
class CatalogEntry:
    relative_path: str
    sha256: str
    size_bytes: int


@dataclass
class RunSummary:
    mode: str
    processed_batches: int = 0
    dry_run_batches: int = 0
    downloaded_batches: int = 0
    downloaded_files: int = 0
    downloaded_bytes: int = 0
    trashed_batches: int = 0
    pushed_batches: int = 0
    pushed_files: int = 0
    failed_batches: int = 0
    failure_artifacts: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_target(base_dir: Path, member_name: str) -> Path:
    target = (base_dir / member_name).resolve()
    base = base_dir.resolve()
    if target == base:
        return target

    if not str(target).startswith(str(base) + os.sep):
        raise ValueError(f"Unsafe archive member path: {member_name}")
    return target


def extract_and_catalog(archive_path: Path, extract_dir: Path) -> tuple[list[CatalogEntry], int]:
    extract_dir.mkdir(parents=True, exist_ok=True)

    entries: list[CatalogEntry] = []
    total_bytes = 0

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue

                target = _safe_extract_target(extract_dir, member.filename)
                target.parent.mkdir(parents=True, exist_ok=True)

                with archive.open(member, "r") as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)

                relative_path = target.relative_to(extract_dir).as_posix()
                size_bytes = target.stat().st_size
                entries.append(
                    CatalogEntry(
                        relative_path=relative_path,
                        sha256=_sha256_file(target),
                        size_bytes=size_bytes,
                    )
                )
                total_bytes += size_bytes

        return entries, total_bytes

    target = extract_dir / archive_path.name
    shutil.copy2(archive_path, target)
    size_bytes = target.stat().st_size
    entries.append(
        CatalogEntry(
            relative_path=target.relative_to(extract_dir).as_posix(),
            sha256=_sha256_file(target),
            size_bytes=size_bytes,
        )
    )
    total_bytes += size_bytes
    return entries, total_bytes


class RephotoPipeline:
    def __init__(self, config: RephotoConfig) -> None:
        self.config = config
        self.config.ensure_directories()
        self.store = ManifestStore(config.manifest_db)

    def close(self) -> None:
        self.store.close()

    def run(self, mode: RunMode, *, max_batches: int = 0) -> RunSummary:
        summary = RunSummary(mode=mode.value)

        if mode == RunMode.PUSH_ONLY:
            self._push_pending(summary, max_batches=max_batches)
            return summary

        deletion_approved = self.config.delete_without_prompt

        with PhotosBrowserSession(self.config) as browser:
            for category in self.config.categories:
                browser.open_category(category)

                while True:
                    if max_batches > 0 and summary.processed_batches >= max_batches:
                        return summary

                    selection = browser.select_next_batch(self.config.batch_size)
                    if selection is None:
                        break

                    summary.processed_batches += 1
                    if mode == RunMode.DRY_RUN:
                        summary.dry_run_batches += 1
                        browser.clear_selection()
                        continue

                    batch_id = self.store.ensure_batch(
                        selection.remote_batch_id,
                        category,
                        selection.selected_count,
                    )

                    try:
                        archive_path = browser.download_selected(selection.remote_batch_id)
                        extract_dir = self.config.extract_root / selection.remote_batch_id

                        files, total_bytes = extract_and_catalog(archive_path, extract_dir)
                        self.store.update_status(
                            batch_id,
                            BATCH_DOWNLOADED,
                            archive_path=archive_path,
                            extract_dir=extract_dir,
                            bytes_downloaded=total_bytes,
                        )

                        for entry in files:
                            self.store.add_file(
                                batch_id,
                                entry.relative_path,
                                entry.sha256,
                                entry.size_bytes,
                            )

                        self.store.update_status(batch_id, BATCH_VERIFIED)
                        summary.downloaded_batches += 1
                        summary.downloaded_files += len(files)
                        summary.downloaded_bytes += total_bytes

                        if mode in (RunMode.DOWNLOAD_DELETE, RunMode.DOWNLOAD_DELETE_PUSH):
                            if not deletion_approved:
                                answer = input(
                                    "Delete selected media from Google Photos trash now? [y/N]: "
                                )
                                if answer.strip().lower() not in {"y", "yes"}:
                                    raise RuntimeError("Deletion was cancelled by operator")
                                deletion_approved = True

                            browser.trash_selected()
                            self.store.update_status(batch_id, BATCH_TRASHED)
                            summary.trashed_batches += 1

                        browser.clear_selection()

                        if mode == RunMode.DOWNLOAD_DELETE_PUSH:
                            push_result = self._push_batch(batch_id)
                            summary.pushed_batches += 1
                            summary.pushed_files += int(push_result["pushed_files"])
                            if int(push_result["failed_files"]) > 0:
                                summary.failed_batches += 1

                    except Exception as exc:
                        self.store.update_status(batch_id, BATCH_FAILED, error=str(exc))
                        summary.failed_batches += 1
                        try:
                            artifact_path = browser.capture_failure_artifact(
                                f"batch-{selection.remote_batch_id}"
                            )
                            summary.failure_artifacts.append(str(artifact_path))
                        except Exception:
                            pass
                        try:
                            browser.clear_selection()
                        except Exception:
                            pass

        return summary

    def _push_pending(self, summary: RunSummary, *, max_batches: int = 0) -> None:
        for batch in self.store.list_batches([BATCH_VERIFIED, BATCH_TRASHED, BATCH_PUSHED]):
            if max_batches > 0 and summary.processed_batches >= max_batches:
                return

            batch_id = int(batch["id"])
            unpushed_files = self.store.count_unpushed_files(batch_id)
            if unpushed_files == 0:
                if str(batch["status"]) != BATCH_PUSHED:
                    self.store.update_status(batch_id, BATCH_PUSHED)
                continue

            summary.processed_batches += 1
            try:
                push_result = self._push_batch(batch_id)
                summary.pushed_batches += 1
                summary.pushed_files += int(push_result["pushed_files"])
                if int(push_result["failed_files"]) > 0:
                    summary.failed_batches += 1
            except Exception:
                self.store.update_status(batch_id, BATCH_FAILED, error="ADB push failed")
                summary.failed_batches += 1

    def _push_batch(self, batch_id: int) -> dict[str, Any]:
        batch = self.store.get_batch(batch_id)
        if batch is None:
            raise ValueError(f"Unknown batch id: {batch_id}")

        extract_dir_value = batch["extract_dir"]
        if extract_dir_value is None:
            raise RuntimeError(f"Batch {batch_id} has no extracted directory")

        extract_dir = Path(str(extract_dir_value))
        if not extract_dir.exists():
            raise RuntimeError(f"Extract directory not found: {extract_dir}")

        push_result = push_batch_files(
            self.store,
            batch_id=batch_id,
            extract_dir=extract_dir,
            destination_dir=self.config.adb_destination,
            serial=self.config.adb_serial,
        )

        if int(push_result["failed_files"]) == 0 and self.store.count_unpushed_files(batch_id) == 0:
            self.store.update_status(batch_id, BATCH_PUSHED)

        return push_result
