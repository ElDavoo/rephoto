---
name: google-photos-requota
description: >-
  Operate this repo's Google Photos requota migration tool (requota_migration.py):
  move a Google account's photos and videos off storage quota by downloading the
  originals with metadata, then re-uploading them in gpmc's non-quota mode and
  restoring their flags. Use this whenever the user wants to free up / reclaim
  Google Photos storage, migrate or re-upload a Google Photos library, run
  requota_migration.py, or pull a Google Photos OAuth token off a rooted Android
  device via ADB — even when phrased loosely ("get my photos off quota", "my
  Google storage is full", "back up and re-upload the library"). Covers the NixOS
  devshell, ADB-token auth, the download → verify → re-upload sequence, and the
  destructive deletion flags that need care.
---

# Google Photos requota migration

## What this tool does and why

Google Photos charges storage quota for media uploaded at "original" quality. This
tool reclaims that quota by round-tripping each item: **download** the original
bytes (which don't lose EXIF/GPS), then **re-upload** the same file through gpmc's
non-quota path so the copy that ends up in the account no longer counts against
storage. Caption / favorite / archived flags are re-applied afterward.

It runs in **two independent, re-runnable phases** linked by a manifest:

- **Download** (`--download-only`) → writes `migration_workspace/files/` (originals),
  `migration_workspace/metadata/*.json` (sidecars), and `manifest.json` (the index).
- **Re-upload** (`--reupload-only`) → reads the manifest, uploads each file in
  non-quota mode, restores flags.

The heavy lifting (the reverse-engineered mobile API) lives in the
`google_photos_mobile_client` submodule (gpmc); `requota_migration.py` is the
orchestration on top. Read `CLAUDE.md` for the architecture if you need it.

## 1. Environment (NixOS)

This repo is NixOS-first — a `flake.nix` provides everything. Don't `pip install`.

```bash
git submodule update --init          # gpmc must be present
nix develop                          # python + gpmc deps (bbpb/rich/requests) + adb
```

Run every command below from inside `nix develop`. On another distro, the fallback
is `cd google_photos_mobile_client && pip install -e .`.

## 2. Authentication

Two ways to authenticate, both selected on the command line:

- `--auth-data "<string>"` or the `GP_AUTH_DATA` env var — the classic gpmc path,
  embedding the account's long-lived master token.
- `--adb-token` — pull the short-lived `photos.native` OAuth bearer off a **rooted**
  Android device signed into the target account. This is the path this repo is built
  around; `gpmc_adb_auth.py` handles it, auto-refetches on expiry, and pauses on 401.

**If using `--adb-token` and it isn't working yet** (no token, wrong account, 401s on
the very first call), follow `references/adb-auth.md` — it has the full pull procedure,
how to confirm the token, and the device dead-ends that are NOT worth your time.

Because the ADB bearer only grants one account's access, treat it as a live
credential: it belongs to whoever owns that device/account. If that isn't the user's
own account, confirm they're authorized (e.g. migrating a family member's library
with consent) before extracting it.

## 3. Run the migration: download → verify → re-upload

**Always dry-run a handful first** so the user sees real output before committing:

```bash
python requota_migration.py --adb-token --download-only --limit 5
```

Then the full download:

```bash
python requota_migration.py --adb-token --download-only
```

**Verify before re-uploading — this is the user's only copy:**

```bash
ls migration_workspace/files | wc -l
python -c "import json;m=json.load(open('migration_workspace/manifest.json'));print(sum(i['download_status']=='ok' for i in m['items']),'ok /',len(m['items']))"
```

Only once downloads look complete, re-upload:

```bash
python requota_migration.py --adb-token --reupload-only
```

Useful flags: `--limit N`, `--skip-existing`, `--saver` (Storage-Saver quality),
`--work-dir DIR` (default `migration_workspace`), `--progress`, `--no-restore-metadata`,
`--adb-serial` / `--adb-account-id` (when multiple devices/accounts exist).

## 4. Deletion flags — the dangerous part

Re-upload is destructive by default, so surface this before running phase 2:

- **By default, re-upload deletes each original before uploading its replacement**
  (to dodge hash-dedupe that would keep the item quota-charged). Pass
  `--keep-original-before-upload` to disable that and keep originals until the upload
  succeeds — safer for a first real run.
- `--delete-originals` is a **further permanent cleanup pass** after upload. Only
  suggest it once the user has verified the migration worked. Without it, storage
  usage won't actually drop.

Never run the deleting variants on data you haven't verified. Recommend the sequence:
**download → verify files exist → re-upload (keeping originals) → confirm in the
Photos app → only then consider `--delete-originals`.**

## 5. Things that will otherwise trip you up

- **Metadata scope.** EXIF/GPS/capture-time ride along in the original bytes; caption,
  favorite, and archived are stored in sidecars and restored via API. Album membership
  and shared-library relationships are **not** handled (see below). If the user expects
  Google's full Takeout-style JSON, set that expectation.
- **Only quota-charged items** are migrated (`quota_charged_bytes > 0`, not trashed) —
  that's the point; items already free don't need it.
- **Token freshness (ADB mode).** The bearer lives ~1h and only refreshes when the
  phone's GMS re-mints it. `gpmc_adb_auth.py` re-pulls automatically, and on a 401 the
  run **pauses and waits for Enter** so the operator can open Google Photos on the phone
  to force a fresh token, then continues. Keep the phone plugged in and reachable during
  long runs.
- **Albums are deferred.** Backup/restore of albums is a known TODO — gpmc discards album
  data and `collection_id` is ambiguous. Don't promise albums without the reverse-
  engineering work noted in `CLAUDE.md`.
- **The cache** lives at `~/.gpmc/<email>/storage.db` (a SQLite DB gpmc builds). There's
  no `sqlite3` CLI on NixOS here; inspect it with Python's `sqlite3` module.
