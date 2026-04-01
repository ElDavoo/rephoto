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

## Safety Notes

- During re-upload, originals are deleted before upload by default.
- Use `--keep-original-before-upload` to disable pre-upload deletion.
- `--delete-originals` is an additional cleanup pass after upload.
- Keep the generated manifest and sidecar metadata until you verify the migration.
- Without `--delete-originals`, storage usage will not drop.

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
