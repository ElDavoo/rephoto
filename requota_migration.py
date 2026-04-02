#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any


REPO_ROOT = Path(__file__).resolve().parent
GPMC_ROOT = REPO_ROOT / "google_photos_mobile_client"

if TYPE_CHECKING:
    from gpmc import Client


def load_client_class() -> type["Client"]:
    if (GPMC_ROOT / "gpmc").is_dir() and str(GPMC_ROOT) not in sys.path:
        sys.path.insert(0, str(GPMC_ROOT))

    try:
        from gpmc import Client as GpmcClient
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependencies. Install them with: cd google_photos_mobile_client && pip install -e ."
        ) from exc

    return GpmcClient


BOOL_COLUMNS = {
    "is_canonical",
    "is_archived",
    "is_favorite",
    "is_locked",
    "is_original_quality",
    "is_edited",
    "is_micro_video",
}


EMPTY_CAPTION_TOKENS = {
    "",
    "{}",
    "[]",
    "null",
    "none",
    '""',
    "''",
}


RETRYABLE_ERROR_MARKERS = {
    "timed out",
    "timeout",
    "connection",
    "temporarily",
    "temporary",
    "reset by peer",
    "broken pipe",
    "service unavailable",
    "too many requests",
    "429",
    "500",
    "502",
    "503",
    "504",
    "http error",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def sanitize_filename(name: str) -> str:
    clean = name.replace("/", "_").replace("\\", "_").strip()
    if not clean:
        clean = "unnamed"
    return clean


def normalize_caption(value: Any) -> str:
    """Convert cache/manifest caption representations into a clean caption string."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return ""

    caption = str(value).strip()
    if caption.lower() in EMPTY_CAPTION_TOKENS:
        return ""

    if caption.startswith("{") and caption.endswith("}"):
        try:
            parsed = json.loads(caption)
            if parsed == {}:
                return ""
        except Exception:
            pass

    if caption.startswith("[") and caption.endswith("]"):
        try:
            parsed = json.loads(caption)
            if parsed == []:
                return ""
        except Exception:
            pass

    return caption


def is_retryable_upload_error(error: str) -> bool:
    """Return True for upload errors that should be retried."""
    text = str(error or "").strip().lower()
    if not text:
        return True
    if "local file missing" in text:
        return False
    return any(marker in text for marker in RETRYABLE_ERROR_MARKERS)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def query_quota_items(db_path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    query = """
        SELECT *
        FROM remote_media
        WHERE COALESCE(quota_charged_bytes, 0) > 0
          AND COALESCE(trash_timestamp, 0) = 0
        ORDER BY utc_timestamp ASC, media_key ASC
    """
    params: list[Any] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for col in BOOL_COLUMNS:
            if col in item and item[col] is not None:
                item[col] = bool(item[col])
        out.append(item)
    return out


def query_dedup_keys(db_path: Path, media_keys: list[str]) -> dict[str, str]:
    if not media_keys:
        return {}

    result: dict[str, str] = {}
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for batch in chunked(media_keys, 400):
            placeholders = ",".join("?" for _ in batch)
            query = f"SELECT media_key, dedup_key FROM remote_media WHERE media_key IN ({placeholders})"
            rows = conn.execute(query, batch).fetchall()
            for row in rows:
                result[row["media_key"]] = row["dedup_key"]
    return result


def collect_urls(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            collect_urls(value, out)
        return

    if isinstance(node, list):
        for value in node:
            collect_urls(value, out)
        return

    if isinstance(node, str) and node.startswith("http"):
        out.append(node)


def get_download_urls(download_response: dict[str, Any]) -> tuple[str | None, str | None, list[str]]:
    payload = download_response.get("1", {}).get("5", {}).get("2", {})
    edited = payload.get("5")
    original = payload.get("6")

    discovered: list[str] = []
    collect_urls(download_response, discovered)
    discovered = list(dict.fromkeys(discovered))
    return original, edited, discovered


def download_file(url: str, destination: Path, timeout: int, retries: int) -> None:
    import requests

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(destination.suffix + ".part")

    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                with temp_path.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            fh.write(chunk)
            temp_path.replace(destination)
            return
        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            if attempt == retries:
                raise
            time.sleep(min(2 * attempt, 10))


def init_manifest(manifest_path: Path, work_dir: Path, db_path: Path) -> dict[str, Any]:
    manifest = {
        "version": 1,
        "generated_at": utc_now_iso(),
        "work_dir": str(work_dir.resolve()),
        "db_path": str(db_path.resolve()),
        "items": [],
    }
    write_json(manifest_path, manifest)
    return manifest


def download_phase(client: "Client", args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    print("[1/2] Updating local cache...")
    client.update_cache(show_progress=args.progress)

    print("[1/2] Loading quota-charged items from cache...")
    items = query_quota_items(client.db_path, limit=args.limit)
    print(f"[1/2] Found {len(items)} quota-charged items.")

    work_dir = args.work_dir.resolve()
    files_dir = work_dir / "files"
    metadata_dir = work_dir / "metadata"
    manifest_path = args.manifest.resolve()
    manifest = init_manifest(manifest_path, work_dir, client.db_path)

    failures = 0
    for index, item in enumerate(items, start=1):
        media_key = item["media_key"]
        file_name = sanitize_filename(item.get("file_name") or f"{media_key}.bin")
        local_name = f"{media_key}_{file_name}"
        local_path = files_dir / local_name
        metadata_path = metadata_dir / f"{media_key}.json"

        entry: dict[str, Any] = {
            "media_key": media_key,
            "dedup_key": item.get("dedup_key"),
            "file_name": item.get("file_name") or file_name,
            "local_path": str(local_path),
            "metadata_path": str(metadata_path),
            "utc_timestamp": item.get("utc_timestamp"),
            "quota_charged_bytes": item.get("quota_charged_bytes", 0),
            "caption": normalize_caption(item.get("caption")),
            "is_favorite": bool(item.get("is_favorite")),
            "is_archived": bool(item.get("is_archived")),
            "download_status": "pending",
        }

        try:
            print(f"[1/2] ({index}/{len(items)}) Preparing download for {media_key}...")
            urls_response = client.api.get_download_urls(media_key)
            original_url, edited_url, discovered_urls = get_download_urls(urls_response)
            selected_url = original_url or edited_url or (discovered_urls[0] if discovered_urls else None)
            if not selected_url:
                raise RuntimeError("No downloadable URL found in API response")

            entry["download_url_original"] = original_url
            entry["download_url_edited"] = edited_url
            entry["download_url_selected"] = selected_url

            if local_path.exists() and args.skip_existing:
                print(f"[1/2] ({index}/{len(items)}) Skipping existing file {local_path.name}")
            else:
                download_file(selected_url, local_path, timeout=args.download_timeout, retries=args.download_retries)

            timestamp = int(item.get("utc_timestamp") or item.get("server_creation_timestamp") or time.time())
            os.utime(local_path, (timestamp, timestamp), follow_symlinks=False)

            sidecar = {
                "downloaded_at": utc_now_iso(),
                "download_urls": {
                    "original": original_url,
                    "edited": edited_url,
                },
                "remote_item": item,
            }
            write_json(metadata_path, sidecar)

            entry["download_status"] = "ok"
            entry["downloaded_at"] = utc_now_iso()
        except Exception as exc:
            failures += 1
            entry["download_status"] = "failed"
            entry["download_error"] = str(exc)
            print(f"[1/2] ({index}/{len(items)}) Download failed for {media_key}: {exc}")

        manifest["items"].append(entry)

    write_json(manifest_path, manifest)
    print(f"[1/2] Download phase complete. Success: {len(items) - failures}, Failed: {failures}")
    print(f"[1/2] Manifest written to: {manifest_path}")
    return manifest, failures


def restore_metadata(client: "Client", dedup_key: str, entry: dict[str, Any]) -> None:
    caption = normalize_caption(entry.get("caption"))
    if caption:
        client.api.set_item_caption(dedup_key=dedup_key, caption=caption)

    if entry.get("is_favorite"):
        client.api.set_favorite(dedup_key=dedup_key, is_favorite=True)

    if entry.get("is_archived"):
        client.api.set_archived([dedup_key], is_archived=True)


def reupload_phase(client: "Client", args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    manifest_path = args.manifest.resolve()
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest does not exist: {manifest_path}")

    manifest = load_json(manifest_path)
    entries = [item for item in manifest.get("items", []) if item.get("download_status") == "ok"]
    if not entries:
        print("[2/2] No downloadable items in manifest. Nothing to upload.")
        return manifest, 0

    pending_entries = [item for item in entries if item.get("upload_status") != "ok"]
    if not pending_entries:
        print("[2/2] All downloadable items are already marked as uploaded.")
        manifest["failed_upload_queue"] = []
        manifest["failed_upload_count"] = 0
        manifest["last_reupload_run_at"] = utc_now_iso()
        write_json(manifest_path, manifest)
        return manifest, 0

    max_attempts = max(1, int(args.upload_max_attempts))
    retry_backoff_seconds = max(0, int(args.retry_backoff_seconds))

    upload_failures = 0
    delete_before_upload = not args.keep_original_before_upload
    queue = list(pending_entries)
    for attempt in range(1, max_attempts + 1):
        if not queue:
            break

        print(f"[2/2] Upload attempt round {attempt}/{max_attempts} for {len(queue)} item(s)...")
        next_queue: list[dict[str, Any]] = []

        for index, entry in enumerate(queue, start=1):
            media_path = Path(entry["local_path"])
            attempts_so_far = int(entry.get("upload_attempts") or 0) + 1
            entry["upload_attempts"] = attempts_so_far
            entry["last_upload_attempt_at"] = utc_now_iso()

            if not media_path.exists():
                entry["upload_status"] = "failed"
                entry["upload_error"] = f"Local file missing: {media_path}"
                entry["upload_error_type"] = "local_file_missing"
                print(f"[2/2] ({index}/{len(queue)}) Missing local file for {entry.get('media_key')}")
                continue

            try:
                if delete_before_upload and not entry.get("old_media_deleted_before_upload"):
                    old_dedup_key = entry.get("dedup_key")
                    if old_dedup_key:
                        try:
                            client.api.move_remote_media_to_trash([old_dedup_key])
                            client.api.delete_remote_media_permanently([old_dedup_key])
                            entry["old_media_deleted_before_upload"] = True
                            entry["old_media_deleted_before_upload_at"] = utc_now_iso()
                        except Exception as delete_exc:
                            entry["old_media_deleted_before_upload"] = False
                            entry["old_media_deleted_before_upload_error"] = str(delete_exc)
                    else:
                        entry["old_media_deleted_before_upload"] = False
                        entry["old_media_deleted_before_upload_error"] = "missing dedup_key"

                timestamp = int(entry.get("utc_timestamp") or time.time())
                os.utime(media_path, (timestamp, timestamp), follow_symlinks=False)

                target = {
                    media_path: {
                        "filename": entry.get("file_name") or media_path.name,
                    }
                }

                print(f"[2/2] ({index}/{len(queue)}) Uploading {media_path.name}...")
                upload_result = client.upload(
                    target=target,
                    use_quota=False,
                    saver=args.saver,
                    show_progress=args.progress,
                    threads=1,
                    force_upload=not args.no_force_upload,
                )
                new_media_key = next(iter(upload_result.values()))

                entry["upload_status"] = "ok"
                entry["upload_error"] = ""
                entry["upload_error_type"] = ""
                entry["uploaded_at"] = utc_now_iso()
                entry["new_media_key"] = new_media_key
                entry["upload_reused_existing"] = new_media_key == entry.get("media_key")
            except Exception as exc:
                entry["upload_status"] = "failed"
                entry["upload_error"] = str(exc)
                entry["upload_error_type"] = "retryable" if is_retryable_upload_error(str(exc)) else "non_retryable"
                print(f"[2/2] ({index}/{len(queue)}) Upload failed for {entry.get('media_key')}: {exc}")

                if attempt < max_attempts and is_retryable_upload_error(str(exc)):
                    next_queue.append(entry)

        queue = next_queue
        manifest["failed_upload_queue"] = [entry.get("media_key") for entry in queue if entry.get("media_key")]
        manifest["failed_upload_count"] = len([item for item in entries if item.get("upload_status") != "ok"])
        manifest["last_reupload_run_at"] = utc_now_iso()
        write_json(manifest_path, manifest)

        if queue and attempt < max_attempts:
            sleep_seconds = retry_backoff_seconds * attempt
            if sleep_seconds > 0:
                print(f"[2/2] Retrying {len(queue)} failed item(s) after {sleep_seconds}s backoff...")
                time.sleep(sleep_seconds)

    write_json(manifest_path, manifest)

    upload_failures = len([item for item in entries if item.get("upload_status") != "ok"])
    manifest["failed_upload_queue"] = [entry.get("media_key") for entry in entries if entry.get("upload_status") != "ok" and entry.get("media_key")]
    manifest["failed_upload_count"] = upload_failures
    manifest["last_reupload_run_at"] = utc_now_iso()
    write_json(manifest_path, manifest)

    if upload_failures:
        print(f"[2/2] Failed upload queue retained: {upload_failures} item(s). See manifest.failed_upload_queue.")
    else:
        print("[2/2] Failed upload queue retained: 0 item(s).")

    if not args.no_restore_metadata:
        uploaded_entries = [item for item in entries if item.get("upload_status") == "ok" and item.get("new_media_key")]
        if uploaded_entries:
            print("[2/2] Refreshing cache to resolve new dedup keys for metadata restoration...")
            client.update_cache(show_progress=args.progress)
            dedup_map = query_dedup_keys(client.db_path, [item["new_media_key"] for item in uploaded_entries])
            for entry in uploaded_entries:
                new_media_key = entry["new_media_key"]
                dedup_key = dedup_map.get(new_media_key)
                if not dedup_key:
                    entry["metadata_restore_status"] = "failed"
                    entry["metadata_restore_error"] = "dedup_key not found in cache"
                    continue

                try:
                    restore_metadata(client, dedup_key, entry)
                    entry["metadata_restore_status"] = "ok"
                    entry["metadata_restored_at"] = utc_now_iso()
                except Exception as exc:
                    entry["metadata_restore_status"] = "failed"
                    entry["metadata_restore_error"] = str(exc)

            write_json(manifest_path, manifest)

    if args.delete_originals:
        deletable_entries = [
            item
            for item in entries
            if item.get("upload_status") == "ok"
            and item.get("new_media_key")
            and item.get("new_media_key") != item.get("media_key")
            and item.get("dedup_key")
        ]

        dedup_to_entries: dict[str, list[dict[str, Any]]] = {}
        for entry in deletable_entries:
            dedup_key = str(entry["dedup_key"])
            dedup_to_entries.setdefault(dedup_key, []).append(entry)

        dedup_keys = list(dedup_to_entries.keys())
        if dedup_keys:
            print(f"[2/2] Deleting {len(dedup_keys)} original quota-charged items...")
            for batch in chunked(dedup_keys, 500):
                client.api.move_remote_media_to_trash(batch)
                client.api.delete_remote_media_permanently(batch)
                for dedup_key in batch:
                    for entry in dedup_to_entries[dedup_key]:
                        entry["old_media_deleted"] = True
                        entry["old_media_deleted_at"] = utc_now_iso()
            write_json(manifest_path, manifest)

    print(f"[2/2] Re-upload phase complete. Success: {len(entries) - upload_failures}, Failed: {upload_failures}")
    print(f"[2/2] Manifest updated at: {manifest_path}")
    return manifest, upload_failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download all quota-charged Google Photos media with metadata sidecars, then re-upload them via gpmc in non-quota mode. "
            "By default this runs both phases: download and re-upload."
        )
    )

    parser.add_argument("--auth-data", default="", help="Google auth_data string. If omitted, GP_AUTH_DATA environment variable is used.")
    parser.add_argument("--proxy", default="", help="Optional proxy URL in the form protocol://user:pass@host:port")
    parser.add_argument("--timeout", type=int, default=60, help="API timeout in seconds for gpmc requests.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="gpmc log level")
    parser.add_argument("--progress", action="store_true", help="Enable rich progress output from gpmc")

    parser.add_argument("--work-dir", type=Path, default=Path("migration_workspace"), help="Directory for downloaded files and metadata sidecars.")
    parser.add_argument("--manifest", type=Path, default=None, help="Path to manifest JSON. Defaults to <work-dir>/manifest.json")
    parser.add_argument("--limit", type=int, default=None, help="Optional cap on number of quota-charged items to process.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip downloading files that already exist in work-dir/files.")
    parser.add_argument("--download-timeout", type=int, default=120, help="Direct media download timeout in seconds.")
    parser.add_argument("--download-retries", type=int, default=3, help="Retries for media file downloads.")

    parser.add_argument("--download-only", action="store_true", help="Run only phase 1 (download + metadata export).")
    parser.add_argument("--reupload-only", action="store_true", help="Run only phase 2 (re-upload using existing manifest).")
    parser.add_argument("--upload-max-attempts", type=int, default=3, help="Max upload attempts per item during reupload phase.")
    parser.add_argument("--retry-backoff-seconds", type=int, default=5, help="Base backoff in seconds between retry rounds for failed uploads.")

    parser.add_argument("--no-force-upload", action="store_true", help="Do not force upload; this may reuse an existing remote item by hash.")
    parser.add_argument(
        "--keep-original-before-upload",
        action="store_true",
        help="Skip deleting original quota-charged item before upload. By default originals are deleted first to avoid storage-charged dedupe.",
    )
    parser.add_argument("--saver", action="store_true", help="Upload in Storage Saver quality instead of original quality.")
    parser.add_argument("--no-restore-metadata", action="store_true", help="Skip caption/favorite/archive restoration on uploaded items.")
    parser.add_argument(
        "--delete-originals",
        action="store_true",
        help="After successful re-upload, permanently delete original quota-charged items. This is destructive.",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.download_only and args.reupload_only:
        parser.error("--download-only and --reupload-only are mutually exclusive")

    args.work_dir = args.work_dir.resolve()
    args.manifest = args.manifest.resolve() if args.manifest else (args.work_dir / "manifest.json").resolve()

    try:
        client_class = load_client_class()
    except RuntimeError as exc:
        print(str(exc))
        return 1

    client = client_class(
        auth_data=args.auth_data,
        proxy=args.proxy,
        timeout=args.timeout,
        log_level=args.log_level,
    )

    run_download = not args.reupload_only
    run_reupload = not args.download_only

    total_failures = 0

    if run_download:
        _, download_failures = download_phase(client, args)
        total_failures += download_failures

    if run_reupload:
        _, upload_failures = reupload_phase(client, args)
        total_failures += upload_failures

    if total_failures:
        print(f"Finished with {total_failures} failed item operations. Check manifest for details.")
        return 2

    print("All requested operations completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
