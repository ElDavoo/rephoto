# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-purpose workflow (`requota_migration.py`) that migrates a Google Photos
account off storage quota: download every quota-charged item with metadata, then
re-upload it in gpmc's non-quota mode and restore its flags. All the Google Photos
protocol work lives in the **`google_photos_mobile_client`** git submodule (gpmc) —
a reverse-engineered mobile-API client. This repo is only the orchestration on top.

## Environment (NixOS-first)

The repo ships a `flake.nix` devshell — use it; do **not** `pip install` on NixOS.

```bash
git submodule update --init            # gpmc must be present
nix develop                            # python + gpmc deps (bbpb, rich, requests) + adb
```

- `bbpb` imports as `blackboxprotobuf` (gpmc uses that name).
- The submodule is pure-Python and is put on `sys.path` at runtime by `load_client_class()`, so the **pinned submodule commit is the gpmc code that runs** — not any pip-installed copy.
- No `sqlite3` CLI on the host; inspect the cache DB with Python's `sqlite3` module.
- Non-Nix fallback only: `cd google_photos_mobile_client && pip install -e .` (see README).

## Running

```bash
# download (dry-run a few first), then full
python requota_migration.py --adb-token --download-only --limit 5
python requota_migration.py --adb-token --download-only
# re-upload from the manifest
python requota_migration.py --adb-token --reupload-only
```

`--adb-token` is the auth path used here (see below). The original `--auth-data` /
`GP_AUTH_DATA` path still works. Syntax-check edits with
`python -m py_compile requota_migration.py gpmc_adb_auth.py`.

Tests live only in the submodule: `cd google_photos_mobile_client && python -m pytest`.
Most tests need a live `GP_AUTH_DATA` and fail without it; the offline ones are
`tests/media_key_decode_test.py`, `tests/album_test.py`, `tests/upload_features_test.py`.

## Architecture

**Two phases, coordinated by a manifest.** `download_phase` and `reupload_phase`
(in `requota_migration.py`) are independent and re-runnable, linked through
`migration_workspace/manifest.json` (per-item status) plus `files/` (original bytes)
and `metadata/<media_key>.json` (sidecars). `--download-only` / `--reupload-only`
select a phase; re-upload reads the manifest, so download must run first.

**The cache DB is the source of truth for what to migrate.** `client.update_cache()`
(gpmc) syncs the whole library into `~/.gpmc/<email>/storage.db` (`remote_media`
table). `query_quota_items()` reads it — `quota_charged_bytes > 0 AND not trashed`.

**Album duplication is a load-bearing detail.** A photo in N albums appears as N
`remote_media` rows: same `dedup_key`, different `media_key`, one per `collection_id`.
`query_quota_items()` therefore **groups by `dedup_key`** (keeping the canonical row via
the `MAX(is_canonical)` bare-column trick) so each photo is migrated once. Metadata
restore also keys on `dedup_key`, not `media_key`.

**Auth is injected, not exchanged, in ADB mode.** `gpmc_adb_auth.py` pulls the cached
`photos.native` OAuth bearer from a rooted device (`adb` + `su` + `sqlite3` reading
`/data/system_ce/0/accounts_ce.db`) and writes it into `client.api.auth_response_cache`,
overriding gpmc's `_get_auth_token()` so the normal `/auth` master-token exchange is
bypassed. The bearer is short-lived (~1h); it auto-refetches from the device on expiry.
On an HTTP 401/403 mid-run, `call_with_auth_retry()` (wrapping every gpmc network call:
`update_cache`, `get_download_urls`, `upload`, metadata restore) pauses for the operator
to refresh the phone's token, re-pulls, and retries.

## Gotchas

- **Deletion defaults are destructive.** Re-upload **deletes each original before
  uploading** its replacement by default (`--keep-original-before-upload` disables it);
  `--delete-originals` is a further permanent cleanup pass. Never run those on
  unverified data.
- The gpmc submodule fork (`ElDavoo/google_photos_mobile_client`) tracks upstream
  `xob0t/gpmc`; its `main` is kept synced, but the **pinned commit is on the fork's
  `fixes` branch**. When changing gpmc behaviour, know whether you're editing the
  submodule or this repo.
- **Album backup/restore is deferred (TODO).** gpmc discards album envelope items and
  doesn't store album titles; `collection_id` alone is ambiguous (mixes user albums with
  auto-groupings), so implementing it needs mobile-API reverse-engineering or a Takeout
  source — not a quick add.
