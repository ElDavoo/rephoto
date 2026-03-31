from __future__ import annotations

import posixpath
from pathlib import Path
import subprocess
from typing import Any

from rephoto.manifest import ManifestStore


class AdbError(RuntimeError):
    pass


def run_adb(args: list[str], *, serial: str | None, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["adb"]
    if serial:
        command.extend(["-s", serial])
    command.extend(args)

    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        raise AdbError(
            "adb command failed: "
            + " ".join(command)
            + f"\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def resolve_device(serial: str | None) -> str:
    completed = run_adb(["devices"], serial=None, check=True)
    devices: list[str] = []

    for line in completed.stdout.splitlines()[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])

    if serial:
        if serial not in devices:
            raise AdbError(f"Configured adb_serial not connected: {serial}")
        return serial

    if not devices:
        raise AdbError("No connected adb devices found")
    if len(devices) > 1:
        raise AdbError(
            "Multiple adb devices connected. Set adb_serial in config to choose one. "
            f"Detected: {', '.join(devices)}"
        )
    return devices[0]


def push_batch_files(
    store: ManifestStore,
    *,
    batch_id: int,
    extract_dir: Path,
    destination_dir: str,
    serial: str | None,
) -> dict[str, Any]:
    active_serial = resolve_device(serial)

    pushed_files = 0
    failed_files = 0

    files = store.list_files(batch_id, only_unpushed=True)
    for file_row in files:
        relative_path = str(file_row["relative_path"])
        local_path = extract_dir / relative_path
        if not local_path.exists():
            failed_files += 1
            continue

        remote_path = posixpath.join(destination_dir.rstrip("/"), relative_path.replace("\\", "/"))
        remote_dir = posixpath.dirname(remote_path)

        run_adb(["shell", "mkdir", "-p", remote_dir], serial=active_serial, check=True)
        completed = run_adb(
            ["push", str(local_path), remote_path],
            serial=active_serial,
            check=False,
        )
        if completed.returncode != 0:
            failed_files += 1
            continue

        store.mark_file_pushed(batch_id, relative_path)
        pushed_files += 1

    # This is best-effort only; different Android versions expose different scan hooks.
    run_adb(
        [
            "shell",
            "am",
            "broadcast",
            "-a",
            "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
            "-d",
            f"file://{destination_dir.rstrip('/')}"
        ],
        serial=active_serial,
        check=False,
    )
    run_adb(["shell", "cmd", "media", "rescan", destination_dir], serial=active_serial, check=False)

    return {
        "serial": active_serial,
        "pushed_files": pushed_files,
        "failed_files": failed_files,
    }
