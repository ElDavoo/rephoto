# Google Photos Requota Migration

A workflow built on top of `google_photos_mobile_client` to:

1. Download all media items that currently consume storage quota.
2. Export a JSON sidecar for each item with metadata from cache.
3. During re-upload, originals are deleted from google photos before upload by default to avoid hash-dedupe behavior that can keep files storage-charged.
4. Re-upload using gpmc non-quota mode.
5. Restore caption, favorite, and archived flags.
6. Optionally delete original quota-charged items from workspace folder after successful re-upload.



## Prerequisites

- Python 3.11+
- `GP_AUTH_DATA` set in your shell (or pass `--auth-data`)

Install dependencies from the submodule:

```bash
cd google_photos_mobile_client
pip install -e .
cd ..
```

### Windows

Linux, macOS and Windows are all supported; on Windows use stock CPython (no WSL needed):

```powershell
git submodule update --init
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e google_photos_mobile_client
pip install pycryptodomex            # only for --gpsoauth (requests comes with gpmc)
py requota_migration.py --gpsoauth --download-only --limit 5
```

Windows specifics:

- **Keep the workspace path short.** Paths over 260 characters fail unless long paths are
  enabled (`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled=1`), so prefer
  something like `--work-dir C:\gp`. The script warns when the path is at risk.
- **`--adb-token`** needs `adb.exe` on `PATH` (Android SDK platform-tools); point `GPMC_ADB`
  at the executable if it lives elsewhere. The device still has to be rooted.
- **Credentials permissions:** Windows has no POSIX mode bits, so `~/.gpmc/<email>/gpsoauth.json`
  is locked down with an owner-only ACL (`icacls`) instead of `chmod 0600`.
- Downloaded file names are sanitized for Windows (reserved characters and device names), and
  output is forced to UTF-8 so the status lines survive a redirect to a file.

## Authentication

Pick one auth mode:

- `--auth-data` (or the `GP_AUTH_DATA` env var): a full gpmc `auth_data` string.
- `--adb-token`: pull the short-lived `photos.native` bearer off a rooted device via ADB
  (see `CLAUDE.md`); re-pulled automatically on expiry.
- `--gpsoauth`: **device-free** — no root, no phone. Hold the account's long-lived master
  token locally and mint bearers in-process via the vendored `gpsoauth` submodule. See
  **Device-free setup** below.

### Device-free setup (`--gpsoauth`)

A one-time browser login captures the account's master token; every run afterward is just
`--gpsoauth`, with no phone and no interaction (bearers re-mint silently on expiry).

1. **Log in once**, for the account you're migrating:

   ```bash
   git submodule update --init vendor/gpsoauth   # first clone only
   nix develop                                   # non-Nix: pip install pycryptodomex requests
   python gpmc_gpsoauth_auth.py login --email you@gmail.com
   ```

   It prints a Google `EmbeddedSetup` URL and waits at `Paste oauth_token:`.

2. **In a browser**, open that URL and sign in as that account (2FA/passkeys work). Then copy
   the cookie it sets and paste it back at the prompt:

   - DevTools (F12) → **Application** → **Cookies** → `https://accounts.google.com`
   - Copy the value of **`oauth_token`** (starts with `oauth2_4/`). It is single-use and
     short-lived, so grab it promptly.

   The token is exchanged for the durable master token, stored with owner-only permissions at
   `~/.gpmc/<email>/gpsoauth.json`, and a test bearer is minted to confirm it works
   (`✓ Stored and verified…`).

3. **Run** with `--gpsoauth` added to any command:

   ```bash
   python requota_migration.py --gpsoauth --download-only --limit 5   # dry run
   python requota_migration.py --gpsoauth --download-only             # full download
   python requota_migration.py --gpsoauth --reupload-only             # re-upload
   ```

Already hold the `aas_et/…` master token? Skip the browser with
`login --email you@gmail.com --master-token aas_et/…`.

Should a mint ever fail with `UNREGISTERED_ON_API_CONSOLE`, retry against the saved token with
the rotated Google Photos signing cert — no second browser login needed:

```bash
python gpmc_gpsoauth_auth.py login --email you@gmail.com --reuse-master \
  --client-sig f8456b1d9986acf9ce21fb450b0d32b895f36885
```

## Safety Notes

- During re-upload, originals are deleted before upload by default.
- Use `--keep-original-before-upload` to disable pre-upload deletion.
- `--delete-originals` is an additional cleanup pass after upload.
- Keep the generated manifest and sidecar metadata until you verify the migration.
- Without `--delete-originals`, storage usage will not drop.

## Network Resilience

Every API call (cache sync, download-URL fetch, upload, metadata restore, deletion)
is retried on transient failures — HTTP 5xx/429/408, connection resets, DNS blips
and read timeouts — with exponential backoff and jitter. Google's photos endpoints
return sporadic 500s on perfectly valid requests, so a run over a whole library
would otherwise die hours in.

```bash
python requota_migration.py --network-retries 8 --network-backoff-seconds 5
```

- `--network-retries N` (default 5, `0` disables): retries per API call.
- `--network-backoff-seconds S` (default 3): base delay; doubles per attempt with
  jitter, capped at 120s.
- `--download-retries N` (default 5): retries per media file download. A dropped
  connection discards the partial `.part` file, so no truncated original is kept.
- HTTP 401/403 is handled separately: `--adb-token` / `--gpsoauth` refresh the
  bearer and retry, without consuming a network retry.

Both phases flush the manifest as they go, and both are resumable: re-running
`--reupload-only` skips items already uploaded, restores only metadata not yet
restored, and (with `--delete-originals`) retries only originals still present.
Items whose original could not be deleted keep an `old_media_delete_error` in the
manifest, and a failed batch is retried key by key so one rejected item does not
strand the other 499.

## Typical Runs

Dry run on first 20 items, download only:

```bash
python requota_migration.py --download-only --limit 20
```

Full migration (download + upload, keep originals):

```bash
python requota_migration.py --keep-original-before-upload
```

Full migration including permanent deletion of originals:

```bash
python requota_migration.py --delete-originals
```

Resume from existing manifest and only perform re-upload:

```bash
python requota_migration.py --reupload-only --manifest migration_workspace/manifest.json
```

Re-upload only while keeping originals until upload completes:

```bash
python requota_migration.py --reupload-only --keep-original-before-upload --manifest migration_workspace/manifest.json
```

## Output Layout

Default output directory: `migration_workspace`

- `migration_workspace/files/`: downloaded media files
- `migration_workspace/metadata/`: sidecar JSON metadata per media key
- `migration_workspace/manifest.json`: operation state, upload results, restoration status, deletion status

## Metadata Preservation Scope

Preserved by file bytes and timestamp handling:

- EXIF and embedded media metadata from downloaded originals
- Original capture/upload timestamp (by setting file mtime before upload)

Restored through API calls:

- Caption
- Favorite flag
- Archived flag

Caption handling note:

- Placeholder values such as `{}` are treated as empty caption and are not re-applied.

Not currently restored by this script:

- Album membership
- Partner/shared-library relationships
- Other server-side-only attributes not exposed as settable operations in gpmc
