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
- **Windows is supported too** (stock CPython, no WSL): same commands with `py`/`python`.
  The portability helpers live in the "portability" section of `requota_migration.py` — see
  Architecture below.

## Running

```bash
# download (dry-run a few first), then full
python requota_migration.py --adb-token --download-only --limit 5
python requota_migration.py --adb-token --download-only
# re-upload from the manifest
python requota_migration.py --adb-token --reupload-only
```

`--adb-token` is the auth path used here (see below). The original `--auth-data` /
`GP_AUTH_DATA` path still works, as does the device-free `--gpsoauth` path (mint bearers
in-process from a stored master token; one-time `python gpmc_gpsoauth_auth.py login --email
you@gmail.com`, see Auth below). Syntax-check edits with
`python -m py_compile requota_migration.py gpmc_adb_auth.py gpmc_gpsoauth_auth.py`.
Offline unit tests live at the repo root and need no network/creds:
`python tests/test_gpsoauth_auth.py`, `python tests/test_auth_retry.py`,
`python tests/test_delete_resilience.py` and `python tests/test_portability.py`
(stdlib unittest).

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
On an HTTP 401/403 mid-run, `call_with_retry(fn, *, on_auth_error=...)` (wrapping every
gpmc network call: `update_cache`, `get_download_urls`, `upload`, metadata restore, deletes)
invokes a mode-specific refresher and retries; `build_auth_refresher()` picks it. In ADB mode
that refresher pauses for the operator to refresh the phone's token, then re-pulls.

**Transient failures are retried in the same wrapper.** `call_with_retry` also retries
`is_transient_error()` failures — 5xx/429/408 by status, plus anything deriving from `OSError`
(connection reset, DNS, read timeout) — `--network-retries` times (default 5) with exponential
backoff + jitter (`retry_delay`, base `--network-backoff-seconds`, capped at 120s); auth
refreshes and transient retries have separate budgets. The photos API returns sporadic 500s,
and a library-sized run must survive them. Both phases flush the manifest every
`MANIFEST_FLUSH_INTERVAL` items so a killed run keeps its work, and `reupload_phase` is
resumable end to end: an all-uploaded manifest no longer short-circuits the phase, metadata
restore skips entries already `ok`, and `--delete-originals` skips originals already gone. A
failed delete batch falls back to key-by-key deletes, records `old_media_delete_error` per
entry and keeps going instead of aborting the run (deletion failures are added to the phase's
returned failure count).

**Device-free auth is minted in-process (`--gpsoauth`).** `gpmc_gpsoauth_auth.py` holds the
account's long-lived AAS master token locally and mints `photos.native` bearers with the
vendored `gpsoauth` submodule (`vendor/gpsoauth`, upstream `simon-weber/gpsoauth`) via its
`/auth` exchange (`perform_oauth`, app `com.google.android.apps.photos`, first-party
`client_sig` `38918a…5788`) — the same call gpmc's own `_get_auth_token()` makes.
`attach_gpsoauth_auth()` installs it exactly like the ADB path, but because the master token
is long-lived the refresher re-mints **silently** on 401 (no operator pause). The master token
comes from a one-time browser `EmbeddedSetup` login (`exchange_token`) or is supplied directly
(`--master-token`), and is stored mode-0600 at `~/.gpmc/<email>/gpsoauth.json`
(auto-discovered by `resolve_store_path`).

**Cross-platform bits are centralised in one section.** `requota_migration.py` has a
"portability" block holding everything that differs off Linux, all of it applied
unconditionally so a workspace is identical on every OS: `sanitize_filename` /
`workspace_name` (Windows-illegal characters, DOS device names, trailing dots, a 150-char cap
that keeps names clear of `MAX_PATH`), `to_epoch_seconds` + `set_file_mtime` (the cache stores
`utc_timestamp` in **milliseconds**; that value dates files to year 58000, which Linux accepts
and Windows rejects — and gpmc sends the mtime as the re-uploaded item's timestamp, so seconds
is also the correct value), `register_media_mimetypes` (on Windows `mimetypes` seeds from
HKEY_CLASSES_ROOT and those entries *override* the built-in table, which can make gpmc's
image/video filter reject a perfectly good `.jpg`), and `configure_stdio` (UTF-8 output on a
cp1252 console). ADB mode resolves `adb`/`adb.exe` through `PATH` (override: `GPMC_ADB`);
`restrict_to_owner` in `gpmc_gpsoauth_auth.py` is `chmod 0600` on POSIX and an `icacls`
owner-only ACL on Windows.

## Gotchas

- **Deletion defaults are destructive.** Re-upload **deletes each original before
  uploading** its replacement by default (`--keep-original-before-upload` disables it);
  `--delete-originals` is a further permanent cleanup pass. Never run those on
  unverified data.
- The gpmc submodule fork (`ElDavoo/google_photos_mobile_client`) tracks upstream
  `xob0t/gpmc`; its `main` is kept synced, but the **pinned commit is on the fork's
  `fixes` branch**. When changing gpmc behaviour, know whether you're editing the
  submodule or this repo.
- There are now **two submodules**: `google_photos_mobile_client` (gpmc) and
  `vendor/gpsoauth` (used only by `--gpsoauth`, pinned to upstream `simon-weber/gpsoauth`).
  `git submodule update --init` initializes both; the devshell adds `pycryptodomex` for it.
- **Album backup/restore is deferred (TODO).** gpmc discards album envelope items and
  doesn't store album titles; `collection_id` alone is ambiguous (mixes user albums with
  auto-groupings), so implementing it needs mobile-API reverse-engineering or a Takeout
  source — not a quick add.
