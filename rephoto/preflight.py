from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import subprocess

from rephoto.config import RephotoConfig


@dataclass
class PreflightReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines: list[str] = []
        if self.ok:
            lines.append("[OK] Preflight checks passed")
        else:
            lines.append("[ERROR] Preflight checks failed")

        for warning in self.warnings:
            lines.append(f"[WARN] {warning}")
        for error in self.errors:
            lines.append(f"[FAIL] {error}")
        return "\n".join(lines)


def _has_nix_environment() -> bool:
    return bool(os.environ.get("IN_NIX_SHELL") or os.environ.get("NIX_PROFILES"))


def _tool_exists(tool_name: str) -> bool:
    return shutil.which(tool_name) is not None


def _list_adb_devices(serial: str | None = None) -> list[str]:
    command = ["adb", "devices"]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return []

    device_lines = completed.stdout.splitlines()[1:]
    devices: list[str] = []
    for line in device_lines:
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        if parts[1] != "device":
            continue
        devices.append(parts[0])

    if serial is None:
        return devices
    return [device for device in devices if device == serial]


def run_preflight(config: RephotoConfig, *, require_adb: bool) -> PreflightReport:
    report = PreflightReport()

    if not _has_nix_environment():
        report.warnings.append(
            "Nix shell markers are missing. Prefer running through 'nix develop' or 'nix run'."
        )

    browser_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not browser_path:
        report.warnings.append(
            "PLAYWRIGHT_BROWSERS_PATH is unset. Nix shell should export it automatically."
        )
    elif not Path(browser_path).exists():
        report.errors.append(f"PLAYWRIGHT_BROWSERS_PATH does not exist: {browser_path}")

    if config.batch_size <= 0:
        report.errors.append("batch_size must be greater than zero")

    if not config.manage_storage_url.startswith("https://"):
        report.errors.append("manage_storage_url must be https")

    try:
        config.ensure_directories()
    except OSError as exc:
        report.errors.append(f"Unable to create state directories: {exc}")

    if not _tool_exists("adb"):
        if require_adb:
            report.errors.append("adb is required for push mode but was not found")
        else:
            report.warnings.append("adb is not in PATH, push mode will fail")

    if require_adb and _tool_exists("adb"):
        devices = _list_adb_devices(config.adb_serial)
        if not devices:
            if config.adb_serial:
                report.errors.append(
                    f"No connected adb device matches adb_serial={config.adb_serial}"
                )
            else:
                report.errors.append("No connected adb devices found")

    return report
