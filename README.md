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

### Open login session (recommended before first run)

```bash
nix-shell -p chromium --run 'nix run path:. -- login --config rephoto.config.json'
```

### Dry-run one batch

```bash
nix-shell -p chromium --run 'nix run path:. -- run --config rephoto.config.json --mode dry-run --max-batches 1'
```

If Chrome/Chromium is not installed system-wide, run through a temporary Nix shell that provides Chromium:

```bash
nix-shell -p chromium --run 'nix run path:. -- login --config rephoto.config.json'
nix-shell -p chromium --run 'nix run path:. -- run --config rephoto.config.json --mode dry-run --max-batches 1'
```

### Download + delete + push

```bash
nix-shell -p chromium --run 'nix run path:. -- run --config rephoto.config.json --mode download-delete-push'
```

### Push only previously downloaded batches

```bash
nix run . -- push-pending --config rephoto.config.json
```

## First login

Use the `login` command to open the persistent profile and authenticate once:

```bash
nix-shell -p chromium --run 'nix run path:. -- login --config rephoto.config.json'
```

Session state is persisted under `state/chrome-profile` and a snapshot file is written to `state/logs/storage-state.json` after successful verification.

If Google shows "This browser or app may not be secure", configure a normal local browser binary in `browser_executable_path` and rerun. Example values on Linux are typically:
- `/run/current-system/sw/bin/google-chrome-stable`
- `/run/current-system/sw/bin/chromium`

## Config keys

Main fields in `rephoto.config.json`:
- `categories`: list of category display names as they appear in your Google Photos language.
- `locale`: UI locale used by Playwright context (default `it-IT`).
- `login_url`: URL used for manual login bootstrap (default `https://accounts.google.com/`).
- `batch_size`: max selected items per batch.
- `browser_executable_path`: absolute path to a normal local Chrome/Chromium binary.
- `browser_channel`: optional Playwright channel (for example `chrome`) when available.
- `delete_without_prompt`: when true, run continues without per-batch confirmation.
- `media_checkbox_selector`, `download_button_names`, `delete_button_names`: UI selectors/labels to adapt if Google changes UI.
- `adb_destination`, `adb_serial`: Android destination path and optional target device serial.

## Troubleshooting login rejection

If Google sign-in is rejected as unsafe:
1. Find your browser path:

```bash
which google-chrome-stable || which chromium
```

2. If neither exists, run with temporary Chromium from Nix:

```bash
nix-shell -p chromium --run 'which chromium'
```

3. Set `browser_executable_path` in `rephoto.config.json` to that absolute path (optional when always using `nix-shell -p chromium --run ...`, because PATH discovery will auto-use it).
4. Re-run preflight:

```bash
nix run . -- doctor --config rephoto.config.json --mode dry-run
```

5. Re-run login then dry-run:

```bash
nix-shell -p chromium --run 'nix run path:. -- login --config rephoto.config.json'
nix-shell -p chromium --run 'nix run path:. -- run --config rephoto.config.json --mode dry-run --max-batches 1'
```

## Notes and caveats

- Remote deletion moves items to trash; this script does not empty trash permanently.
- UI labels differ by account language, so category names and button names may need edits.
- Browser automation can break when Google changes markup.
- Metadata preservation depends on Google download output; this script keeps downloaded bytes and does not rewrite EXIF.
