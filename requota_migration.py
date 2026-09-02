#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import mimetypes
import os
import random
import re
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


# HTTP statuses that mean "the server hiccuped, ask again" rather than "no".
TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504, 509}

NETWORK_RETRIES = 5
NETWORK_BACKOFF_SECONDS = 3.0
NETWORK_BACKOFF_CAP_SECONDS = 120.0

# How often the download phase flushes the manifest, so an aborted run keeps its work.
MANIFEST_FLUSH_INTERVAL = 50


def is_transient_error(exc: BaseException) -> bool:
    """True for failures worth retrying unchanged: 5xx/429/408 and connectivity errors.

    HTTP errors are classified by status: a 4xx other than 408/429 is a real
    rejection, not a blip. Errors without a response are checked against
    ``OSError`` -- every ``requests`` and ``socket`` failure (connection reset,
    DNS, read timeout, chunked-encoding truncation) derives from it -- and then
    against the same message markers the upload queue uses.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        try:
            return int(status) in TRANSIENT_STATUS_CODES
        except (TypeError, ValueError):
            return False
    if isinstance(exc, OSError):
        return True
    return is_retryable_upload_error(str(exc))


def retry_delay(attempt: int, backoff: float) -> float:
    """Backoff for retry ``attempt`` (1-based): exponential, capped, with equal jitter.

    Jitter matters here because a library-wide run hammers one endpoint: without it
    every retry after a server-side wobble lands at the same instant.
    """
    ceiling = min(backoff * (2 ** (attempt - 1)), NETWORK_BACKOFF_CAP_SECONDS)
    return ceiling / 2 + random.uniform(0, ceiling / 2)


def call_with_retry(
    fn,
    *,
    on_auth_error=None,
    label: str,
    max_pauses: int = 20,
    retries: int = NETWORK_RETRIES,
    backoff: float = NETWORK_BACKOFF_SECONDS,
):
    """Call ``fn``, recovering from expired bearers *and* transient network failures.

    Two independent recoveries share one loop:

    * **HTTP 401/403** -- the bearer used by ``--adb-token`` / ``--gpsoauth`` is
      short-lived. ``on_auth_error(reason)`` refreshes it (ADB: pause + re-pull the
      device token; gpsoauth: silently re-mint from the master token) and the call
      is retried. With no refresher (static ``--auth-data``) auth errors propagate.
    * **Transient failures** -- 5xx/429/408 plus connection and timeout errors are
      retried up to ``retries`` times with exponential backoff and jitter. The
      photos endpoints hand out sporadic 500s on perfectly valid requests, and a
      run over a whole library must not die on one.

    Auth refreshes do not consume transient attempts (and vice versa); any other
    error propagates unchanged.
    """
    from gpmc_adb_auth import is_auth_error

    pauses = 0
    attempts = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below unless it's handled here
            if on_auth_error is not None and is_auth_error(exc) and pauses < max_pauses:
                pauses += 1
                on_auth_error(f"Auth token expired during {label} (HTTP 401/403).")
                continue
            if is_transient_error(exc) and attempts < retries:
                attempts += 1
                delay = retry_delay(attempts, backoff)
                print(f"↻ Transient failure during {label} ({attempts}/{retries}): {exc}")
                print(f"   Retrying in {delay:.1f}s...")
                time.sleep(delay)
                continue
            raise


def retry_settings(args: argparse.Namespace) -> dict[str, Any]:
    """Per-run transient-retry knobs, as kwargs for :func:`call_with_retry`."""
    return {
        "retries": max(0, int(getattr(args, "network_retries", NETWORK_RETRIES))),
        "backoff": max(0.0, float(getattr(args, "network_backoff_seconds", NETWORK_BACKOFF_SECONDS))),
    }


def delete_dedup_keys(client: "Client", dedup_keys: list[str], *, refresher, label: str, retry: dict[str, Any]) -> None:
    """Trash then permanently delete ``dedup_keys``, retrying transient failures.

    Both calls are idempotent for our purposes (re-trashing an already-trashed item
    is a no-op), so retrying them is safe.
    """
    call_with_retry(
        lambda: client.api.move_remote_media_to_trash(dedup_keys),
        on_auth_error=refresher, label=f"trash ({label})", **retry,
    )
    call_with_retry(
        lambda: client.api.delete_remote_media_permanently(dedup_keys),
        on_auth_error=refresher, label=f"permanent delete ({label})", **retry,
    )


def build_auth_refresher(client, args):
    """Return an ``on_auth_error(reason)`` callable for the active auth mode.

    ``--adb-token`` pauses for the operator and re-pulls the device token;
    ``--gpsoauth`` silently re-mints a bearer from the stored master token;
    static ``--auth-data`` has no refresher (auth errors propagate).
    """
    if getattr(args, "adb_token", False):
        from gpmc_adb_auth import interactive_refresh

        return lambda reason: interactive_refresh(client, reason)
    if getattr(args, "gpsoauth", False):
        from gpmc_gpsoauth_auth import force_refresh

        def _refresh(reason: str) -> None:
            print(f"↻ {reason} Re-minting bearer via gpsoauth...")
            force_refresh(client)

        return _refresh
    return None


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


# ------------------------------------------------------------------ portability

IS_WINDOWS = os.name == "nt"

# Windows forbids <>:"/\|?* and the control range in a path component, treats the
# DOS device names as reserved (even with an extension), and silently strips
# trailing dots/spaces. Applying all of that everywhere keeps a workspace copied
# between machines consistent, so we never branch on the platform here.
_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_KEY_LENGTH = 64
MAX_NAME_LENGTH = 150

# 1980-01-01: the oldest timestamp Windows will store on a file.
MIN_FILE_TIMESTAMP = 315_532_800
# Above this, a value cannot be seconds (it would be past year 5138), so it is
# the millisecond form the cache actually stores.
MILLISECOND_THRESHOLD = 100_000_000_000

# Extensions gpmc must recognise as image/video, or it refuses to upload the file
# ("File's mime type does not match image or video mime type"). On Windows the
# mimetypes module seeds itself from HKEY_CLASSES_ROOT and those registry entries
# override the built-in table, so whichever app last claimed .jpg/.mp4/.heic
# decides what gpmc sees. Registering them explicitly wins over the registry, and
# also fills in types (HEIC/HEIF/AVIF) Python does not ship on any platform.
MEDIA_MIMETYPES = {
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".jfif": "image/jpeg",
    ".jpe": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".3gp": "video/3gpp",
    ".avi": "video/x-msvideo",
    ".m2ts": "video/mp2t",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".mts": "video/mp2t",
    ".webm": "video/webm",
    ".wmv": "video/x-ms-wmv",
}


def register_media_mimetypes() -> None:
    """Pin the media types gpmc filters on, whatever the OS registry says."""
    for extension, mime_type in MEDIA_MIMETYPES.items():
        mimetypes.add_type(mime_type, extension)


def configure_stdio() -> None:
    """Force UTF-8 on stdout/stderr so status lines survive a legacy code page.

    A Windows console (and any redirect to a file) defaults to cp1252, which
    raises UnicodeEncodeError on the symbols we print and on non-ASCII filenames.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def warn_on_long_paths(work_dir: Path) -> None:
    """Warn when the workspace sits so deep that Windows' MAX_PATH could bite."""
    if not IS_WINDOWS:
        return
    longest = len(str(work_dir)) + len("\\metadata\\") + MAX_NAME_LENGTH
    if longest > 259:
        print(
            f"! Workspace path is long ({work_dir}); generated file paths may exceed the\n"
            "  260-character Windows limit. Use a shorter --work-dir (e.g. C:\\gp) or enable\n"
            "  long paths (HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem\\LongPathsEnabled=1)."
        )


def sanitize_filename(name: str, max_length: int = MAX_NAME_LENGTH) -> str:
    """Return ``name`` as a single path component that is legal on any platform."""
    clean = _ILLEGAL_NAME_CHARS.sub("_", name).strip().rstrip(". ")
    if not clean:
        return "unnamed"
    if clean.partition(".")[0].upper() in _RESERVED_NAMES:
        clean = f"_{clean}"
    if len(clean) > max_length:
        suffix = Path(clean).suffix[:16]
        clean = clean[: max_length - len(suffix)].rstrip(". ") + suffix
    return clean


def workspace_name(media_key: str, file_name: str) -> str:
    """Build the ``<media_key>_<file_name>`` workspace name, portable and capped."""
    return sanitize_filename(f"{sanitize_filename(media_key, MAX_KEY_LENGTH)}_{file_name}")


def to_epoch_seconds(value: Any) -> int | None:
    """Normalise a cache timestamp to whole seconds, or None if unusable.

    ``remote_media.utc_timestamp`` is in **milliseconds**. Passed straight to
    ``os.utime`` it dates files to the year 58000 — which Linux accepts and
    Windows rejects outright (its file times stop at year 30828) — and gpmc sends
    the mtime as the re-uploaded item's timestamp, so the seconds form is also
    the correct one.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > MILLISECOND_THRESHOLD:
        number //= 1000
    return number


def set_file_mtime(path: Path, timestamp: int | None) -> None:
    """Stamp ``path`` with ``timestamp``; warn rather than fail the item."""
    if timestamp is None:
        return
    if IS_WINDOWS and timestamp < MIN_FILE_TIMESTAMP:
        timestamp = MIN_FILE_TIMESTAMP
    try:
        if os.utime in os.supports_follow_symlinks:
            os.utime(path, (timestamp, timestamp), follow_symlinks=False)
        else:  # Windows only exposes the symlink-following form
            os.utime(path, (timestamp, timestamp))
    except (OSError, ValueError, NotImplementedError) as exc:
        print(f"    ! Could not set mtime on {path.name}: {exc}")


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
    # A photo that belongs to N albums appears as N rows in remote_media (one per
    # collection_id), each with its own media_key but the same dedup_key. Without
    # deduplication we would download and re-upload the same photo once per album.
    # GROUP BY dedup_key collapses those; the single MAX(is_canonical) aggregate makes
    # SQLite take every bare column from the canonical row of each group (documented
    # SQLite bare-column behaviour), so we keep the authoritative copy.
    query = """
        SELECT *, MAX(is_canonical) AS _max_canonical
        FROM remote_media
        WHERE COALESCE(quota_charged_bytes, 0) > 0
          AND COALESCE(trash_timestamp, 0) = 0
        GROUP BY dedup_key
        ORDER BY utc_timestamp ASC, media_key ASC
    """
    params: list[Any] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    # closing(): sqlite3's own context manager commits but never closes, and a
    # handle left open on the cache DB blocks gpmc's later writes on Windows.
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.pop("_max_canonical", None)
        for col in BOOL_COLUMNS:
            if col in item and item[col] is not None:
                item[col] = bool(item[col])
        out.append(item)
    return out


def query_dedup_keys(db_path: Path, media_keys: list[str]) -> dict[str, str]:
    if not media_keys:
        return {}

    result: dict[str, str] = {}
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
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


def download_file(
    url: str,
    destination: Path,
    timeout: int,
    retries: int,
    backoff: float = NETWORK_BACKOFF_SECONDS,
) -> None:
    """Stream ``url`` to ``destination``, retrying transient failures.

    Each attempt writes to a ``.part`` file that is discarded on failure, so a
    connection dropped mid-body never leaves a truncated original behind. A hard
    rejection (an expired or malformed URL: 4xx other than 408/429) fails fast --
    only blips and connectivity errors are worth waiting on.
    """
    import requests

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(destination.suffix + ".part")

    for attempt in range(1, max(1, retries) + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                with temp_path.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            fh.write(chunk)
            temp_path.replace(destination)
            return
        except Exception as exc:
            with contextlib.suppress(OSError):
                temp_path.unlink(missing_ok=True)
            if attempt == retries or not is_transient_error(exc):
                raise
            delay = retry_delay(attempt, backoff)
            print(f"   ↻ Download failed ({attempt}/{retries}): {exc}. Retrying in {delay:.1f}s...")
            time.sleep(delay)


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
    refresher = build_auth_refresher(client, args)
    retry = retry_settings(args)
    print("[1/2] Updating local cache...")
    call_with_retry(
        lambda: client.update_cache(show_progress=args.progress),
        on_auth_error=refresher, label="cache update", **retry,
    )

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
        local_name = workspace_name(media_key, file_name)
        local_path = files_dir / local_name
        metadata_path = metadata_dir / f"{sanitize_filename(media_key, MAX_KEY_LENGTH)}.json"

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
            urls_response = call_with_retry(
                lambda: client.api.get_download_urls(media_key),
                on_auth_error=refresher, label=f"download-URL fetch ({media_key})", **retry,
            )
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
                download_file(
                    selected_url,
                    local_path,
                    timeout=args.download_timeout,
                    retries=args.download_retries,
                    backoff=retry["backoff"],
                )

            timestamp = (
                to_epoch_seconds(item.get("utc_timestamp"))
                or to_epoch_seconds(item.get("server_creation_timestamp"))
                or int(time.time())
            )
            set_file_mtime(local_path, timestamp)

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
        # Flush periodically: a run that dies at item 9000 must not lose 8999 downloads.
        if index % MANIFEST_FLUSH_INTERVAL == 0:
            write_json(manifest_path, manifest)

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
    refresher = build_auth_refresher(client, args)
    retry = retry_settings(args)
    manifest_path = args.manifest.resolve()
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest does not exist: {manifest_path}")

    manifest = load_json(manifest_path)
    entries = [item for item in manifest.get("items", []) if item.get("download_status") == "ok"]
    if not entries:
        print("[2/2] No downloadable items in manifest. Nothing to upload.")
        return manifest, 0

    # An empty queue is not an early exit: metadata restore and --delete-originals may
    # still have work left from an interrupted run, and both are resumable.
    pending_entries = [item for item in entries if item.get("upload_status") != "ok"]
    if not pending_entries:
        print("[2/2] All downloadable items are already marked as uploaded; skipping upload rounds.")

    max_attempts = max(1, int(args.upload_max_attempts))
    retry_backoff_seconds = max(0, int(args.retry_backoff_seconds))

    upload_failures = 0
    delete_failures = 0
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
                            delete_dedup_keys(
                                client,
                                [old_dedup_key],
                                refresher=refresher,
                                label=f"pre-upload {entry.get('media_key')}",
                                retry=retry,
                            )
                            entry["old_media_deleted_before_upload"] = True
                            entry["old_media_deleted_before_upload_at"] = utc_now_iso()
                        except Exception as delete_exc:
                            entry["old_media_deleted_before_upload"] = False
                            entry["old_media_deleted_before_upload_error"] = str(delete_exc)
                    else:
                        entry["old_media_deleted_before_upload"] = False
                        entry["old_media_deleted_before_upload_error"] = "missing dedup_key"

                timestamp = to_epoch_seconds(entry.get("utc_timestamp")) or int(time.time())
                set_file_mtime(media_path, timestamp)

                target = {
                    media_path: {
                        "filename": entry.get("file_name") or media_path.name,
                    }
                }

                print(f"[2/2] ({index}/{len(queue)}) Uploading {media_path.name}...")
                upload_result = call_with_retry(
                    lambda: client.upload(
                        target=target,
                        use_quota=False,
                        saver=args.saver,
                        show_progress=args.progress,
                        threads=1,
                        force_upload=not args.no_force_upload,
                    ),
                    on_auth_error=refresher, label=f"upload ({media_path.name})", **retry,
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

            # Flush mid-round: a crash must not lose the record of what was already
            # uploaded (and whose original was already deleted).
            if index % MANIFEST_FLUSH_INTERVAL == 0:
                write_json(manifest_path, manifest)

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
        uploaded_entries = [
            item
            for item in entries
            if item.get("upload_status") == "ok"
            and item.get("new_media_key")
            and item.get("metadata_restore_status") != "ok"
        ]
        if uploaded_entries:
            print("[2/2] Refreshing cache to resolve new dedup keys for metadata restoration...")
            call_with_retry(
                lambda: client.update_cache(show_progress=args.progress),
                on_auth_error=refresher, label="cache refresh", **retry,
            )
            dedup_map = query_dedup_keys(client.db_path, [item["new_media_key"] for item in uploaded_entries])
            for restored, entry in enumerate(uploaded_entries, start=1):
                if restored % MANIFEST_FLUSH_INTERVAL == 0:
                    write_json(manifest_path, manifest)
                new_media_key = entry["new_media_key"]
                dedup_key = dedup_map.get(new_media_key)
                if not dedup_key:
                    entry["metadata_restore_status"] = "failed"
                    entry["metadata_restore_error"] = "dedup_key not found in cache"
                    continue

                try:
                    call_with_retry(
                        lambda: restore_metadata(client, dedup_key, entry),
                        on_auth_error=refresher, label=f"metadata restore ({new_media_key})", **retry,
                    )
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
            and not item.get("old_media_deleted")
            and not item.get("old_media_deleted_before_upload")
        ]

        dedup_to_entries: dict[str, list[dict[str, Any]]] = {}
        for entry in deletable_entries:
            dedup_key = str(entry["dedup_key"])
            dedup_to_entries.setdefault(dedup_key, []).append(entry)

        def mark_deleted(keys: list[str]) -> None:
            stamp = utc_now_iso()
            for dedup_key in keys:
                for entry in dedup_to_entries[dedup_key]:
                    entry["old_media_deleted"] = True
                    entry["old_media_deleted_at"] = stamp
                    entry.pop("old_media_delete_error", None)

        def mark_delete_failed(keys: list[str], error: BaseException) -> None:
            for dedup_key in keys:
                for entry in dedup_to_entries[dedup_key]:
                    entry["old_media_deleted"] = False
                    entry["old_media_delete_error"] = str(error)

        dedup_keys = list(dedup_to_entries.keys())
        if dedup_keys:
            print(f"[2/2] Deleting {len(dedup_keys)} original quota-charged items...")
            for batch_index, batch in enumerate(chunked(dedup_keys, 500), start=1):
                try:
                    delete_dedup_keys(
                        client, batch, refresher=refresher, label=f"originals batch {batch_index}", retry=retry
                    )
                    mark_deleted(batch)
                    write_json(manifest_path, manifest)
                    continue
                except Exception as exc:  # noqa: BLE001 - a stuck batch must not abort the run
                    print(f"[2/2] Batch delete failed for {len(batch)} item(s): {exc}")
                    if len(batch) == 1:
                        mark_delete_failed(batch, exc)
                        delete_failures += 1
                        write_json(manifest_path, manifest)
                        continue
                    # One rejected key would strand the whole batch, so retry key by key.
                    print("[2/2] Falling back to one-by-one deletion for this batch...")

                for dedup_key in batch:
                    try:
                        delete_dedup_keys(
                            client, [dedup_key], refresher=refresher, label=f"original {dedup_key}", retry=retry
                        )
                        mark_deleted([dedup_key])
                    except Exception as exc:  # noqa: BLE001 - recorded in the manifest, run continues
                        delete_failures += 1
                        mark_delete_failed([dedup_key], exc)
                        print(f"[2/2] Delete failed for {dedup_key}: {exc}")
                write_json(manifest_path, manifest)

            manifest["failed_original_delete_count"] = delete_failures
            write_json(manifest_path, manifest)
            if delete_failures:
                print(
                    f"[2/2] {delete_failures} original item(s) could not be deleted; "
                    "see manifest.items[].old_media_delete_error. Re-run with --delete-originals to retry them."
                )

    print(f"[2/2] Re-upload phase complete. Success: {len(entries) - upload_failures}, Failed: {upload_failures}")
    print(f"[2/2] Manifest updated at: {manifest_path}")
    return manifest, upload_failures + delete_failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download all quota-charged Google Photos media with metadata sidecars, then re-upload them via gpmc in non-quota mode. "
            "By default this runs both phases: download and re-upload."
        )
    )

    parser.add_argument("--auth-data", default="", help="Google auth_data string. If omitted, GP_AUTH_DATA environment variable is used.")
    parser.add_argument(
        "--adb-token",
        action="store_true",
        help="Authenticate with the photos.native OAuth bearer token pulled from a rooted device via ADB, "
        "instead of --auth-data. The token is re-pulled automatically when it expires.",
    )
    parser.add_argument("--adb-serial", default=None, help="adb device serial to pull the token from (optional; used with --adb-token).")
    parser.add_argument(
        "--adb-account-id",
        type=int,
        default=None,
        help="accounts_ce.db account _id to read the token for (default: auto-detect the single Google account).",
    )
    parser.add_argument(
        "--adb-token-ttl",
        type=int,
        default=3000,
        help="Seconds to trust a pulled token before re-pulling from the device (default 3000, under GMS's ~1h lifetime).",
    )
    parser.add_argument(
        "--gpsoauth",
        action="store_true",
        help="Authenticate device-free by minting bearers in-process from a stored gpsoauth master token, "
        "instead of --auth-data/--adb-token. Run `python gpmc_gpsoauth_auth.py login` once first. "
        "Bearers re-mint silently on expiry.",
    )
    parser.add_argument(
        "--gpsoauth-store",
        default=None,
        help="Path to the gpsoauth credentials file (default: auto-detect ~/.gpmc/<email>/gpsoauth.json).",
    )
    parser.add_argument(
        "--gpsoauth-email",
        default=None,
        help="Account email for --gpsoauth, to select ~/.gpmc/<email>/gpsoauth.json when several exist.",
    )
    parser.add_argument("--proxy", default="", help="Optional proxy URL in the form protocol://user:pass@host:port")
    parser.add_argument("--timeout", type=int, default=60, help="API timeout in seconds for gpmc requests.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="gpmc log level")
    parser.add_argument("--progress", action="store_true", help="Enable rich progress output from gpmc")

    parser.add_argument("--work-dir", type=Path, default=Path("migration_workspace"), help="Directory for downloaded files and metadata sidecars.")
    parser.add_argument("--manifest", type=Path, default=None, help="Path to manifest JSON. Defaults to <work-dir>/manifest.json")
    parser.add_argument("--limit", type=int, default=None, help="Optional cap on number of quota-charged items to process.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip downloading files that already exist in work-dir/files.")
    parser.add_argument("--download-timeout", type=int, default=120, help="Direct media download timeout in seconds.")
    parser.add_argument("--download-retries", type=int, default=5, help="Retries for media file downloads.")
    parser.add_argument(
        "--network-retries",
        type=int,
        default=NETWORK_RETRIES,
        help=f"Retries per API call for transient failures (5xx/429/timeouts/connection drops). Default {NETWORK_RETRIES}; 0 disables.",
    )
    parser.add_argument(
        "--network-backoff-seconds",
        type=float,
        default=NETWORK_BACKOFF_SECONDS,
        help=f"Base backoff between transient retries; doubles per attempt with jitter, capped at "
        f"{NETWORK_BACKOFF_CAP_SECONDS:.0f}s (default {NETWORK_BACKOFF_SECONDS:g}).",
    )

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
    configure_stdio()
    register_media_mimetypes()

    parser = build_parser()
    args = parser.parse_args()

    if args.download_only and args.reupload_only:
        parser.error("--download-only and --reupload-only are mutually exclusive")

    args.work_dir = args.work_dir.resolve()
    args.manifest = args.manifest.resolve() if args.manifest else (args.work_dir / "manifest.json").resolve()
    warn_on_long_paths(args.work_dir)

    try:
        client_class = load_client_class()
    except RuntimeError as exc:
        print(str(exc))
        return 1

    if args.adb_token and args.gpsoauth:
        print("Choose a single auth mode: --adb-token or --gpsoauth, not both.")
        return 1

    if args.adb_token:
        try:
            from gpmc_adb_auth import attach_adb_auth, detect_google_account, minimal_auth_data

            account_id = args.adb_account_id
            if account_id is None:
                account_id, email = detect_google_account(args.adb_serial)
            else:
                _, email = detect_google_account(args.adb_serial)
            print(f"Using ADB-pulled OAuth token for {email} (account_id={account_id}).")
            client = client_class(
                auth_data=minimal_auth_data(email),
                proxy=args.proxy,
                timeout=args.timeout,
                log_level=args.log_level,
            )
            attach_adb_auth(client, serial=args.adb_serial, account_id=account_id, ttl=args.adb_token_ttl)
        except Exception as exc:  # noqa: BLE001 - surface any adb/root failure clearly
            print(f"Failed to obtain OAuth token via ADB: {exc}")
            return 1
    elif args.gpsoauth:
        try:
            from gpmc_gpsoauth_auth import (
                attach_gpsoauth_auth,
                load_credentials,
                minimal_auth_data,
                resolve_store_path,
            )

            store_path = resolve_store_path(store=args.gpsoauth_store, email=args.gpsoauth_email)
            creds = load_credentials(store_path)
            email = creds["email"]
            print(f"Using in-process gpsoauth bearer for {email} (creds: {store_path}).")
            client = client_class(
                auth_data=minimal_auth_data(email),
                proxy=args.proxy,
                timeout=args.timeout,
                log_level=args.log_level,
            )
            attach_gpsoauth_auth(
                client,
                email,
                creds["master_token"],
                creds["android_id"],
                client_sig=creds.get("client_sig"),
                proxy=args.proxy or None,
            )
        except Exception as exc:  # noqa: BLE001 - surface any credential/mint failure clearly
            print(f"Failed to authenticate via gpsoauth: {exc}")
            return 1
    else:
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
