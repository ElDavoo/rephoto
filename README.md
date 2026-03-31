# Rephoto

Rephoto automates this workflow:
1. Select a category inside Google Photos storage management.
2. Download a batch locally and catalog file hashes.
3. Trash the same remote batch in Google Photos.
4. Push files to Android with ADB so phone backup can re-upload.

## Why browser automation

Google Photos Library API is not suitable for full-library download/delete in this scenario. This project uses browser automation against Google Photos web UI categories to process quota-consuming media.

## NixOS-first usage

All dependencies are provided through Nix. Do not install Python packages globally.

### Enter development shell

```bash
nix develop
```

### Write config template

```bash
nix run . -- init-config --config rephoto.config.json
```

### Run environment checks

```bash
nix run . -- doctor --config rephoto.config.json --mode dry-run
```

### Dry-run one batch

```bash
nix run . -- run --config rephoto.config.json --mode dry-run --max-batches 1
```

### Download + delete + push

```bash
nix run . -- run --config rephoto.config.json --mode download-delete-push
```

### Push only previously downloaded batches

```bash
nix run . -- push-pending --config rephoto.config.json
```

## First login

On first browser run, Chromium opens with the profile directory configured in `chrome_profile_dir`. Log into Google Photos in that browser context. Session state is persisted under `state/chrome-profile`.

## Config keys

Main fields in `rephoto.config.json`:
- `categories`: list of category display names as they appear in your Google Photos language.
- `batch_size`: max selected items per batch.
- `delete_without_prompt`: when true, run continues without per-batch confirmation.
- `media_checkbox_selector`, `download_button_names`, `delete_button_names`: UI selectors/labels to adapt if Google changes UI.
- `adb_destination`, `adb_serial`: Android destination path and optional target device serial.

## Notes and caveats

- Remote deletion moves items to trash; this script does not empty trash permanently.
- UI labels differ by account language, so category names and button names may need edits.
- Browser automation can break when Google changes markup.
- Metadata preservation depends on Google download output; this script keeps downloaded bytes and does not rewrite EXIF.
