from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("rephoto.config.json")


@dataclass
class RephotoConfig:
    manage_storage_url: str = "https://photos.google.com/quotamanagement"
    login_url: str = "https://accounts.google.com/"
    categories: list[str] = field(
        default_factory=lambda: [
            "Large photos and videos",
            "Blurry photos",
            "Screenshots",
        ]
    )
    batch_size: int = 50
    headless: bool = False
    locale: str = "it-IT"
    browser_timeout_ms: int = 30_000
    download_wait_seconds: int = 600
    browser_channel: str | None = None
    browser_executable_path: str | None = None

    chrome_profile_dir: Path = Path("state/chrome-profile")
    download_root: Path = Path("data/downloads")
    extract_root: Path = Path("data/extracted")
    logs_root: Path = Path("state/logs")
    manifest_db: Path = Path("state/manifest.sqlite3")

    delete_without_prompt: bool = True

    media_checkbox_selector: str = "main div[role='checkbox'][aria-checked='false']"
    selected_checkbox_selector: str = "main div[role='checkbox'][aria-checked='true']"
    download_button_names: list[str] = field(default_factory=lambda: ["Download", "Scarica"])
    delete_button_names: list[str] = field(
        default_factory=lambda: ["Move to trash", "Delete", "Trash", "Sposta nel cestino", "Elimina"]
    )
    confirm_delete_button_names: list[str] = field(
        default_factory=lambda: ["Move to trash", "Delete", "Sposta nel cestino", "Elimina"]
    )

    adb_destination: str = "/sdcard/DCIM/Rephoto"
    adb_serial: str | None = None

    def ensure_directories(self) -> None:
        for path in (
            self.chrome_profile_dir,
            self.download_root,
            self.extract_root,
            self.logs_root,
            self.manifest_db.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def to_json(self) -> dict[str, Any]:
        return {
            "manage_storage_url": self.manage_storage_url,
            "login_url": self.login_url,
            "categories": self.categories,
            "batch_size": self.batch_size,
            "headless": self.headless,
            "locale": self.locale,
            "browser_timeout_ms": self.browser_timeout_ms,
            "download_wait_seconds": self.download_wait_seconds,
            "browser_channel": self.browser_channel,
            "browser_executable_path": self.browser_executable_path,
            "chrome_profile_dir": str(self.chrome_profile_dir),
            "download_root": str(self.download_root),
            "extract_root": str(self.extract_root),
            "logs_root": str(self.logs_root),
            "manifest_db": str(self.manifest_db),
            "delete_without_prompt": self.delete_without_prompt,
            "media_checkbox_selector": self.media_checkbox_selector,
            "selected_checkbox_selector": self.selected_checkbox_selector,
            "download_button_names": self.download_button_names,
            "delete_button_names": self.delete_button_names,
            "confirm_delete_button_names": self.confirm_delete_button_names,
            "adb_destination": self.adb_destination,
            "adb_serial": self.adb_serial,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "RephotoConfig":
        cfg = cls()
        path_keys = {
            "chrome_profile_dir",
            "download_root",
            "extract_root",
            "logs_root",
            "manifest_db",
        }

        for key, value in raw.items():
            if not hasattr(cfg, key):
                continue
            if key in path_keys and value is not None:
                setattr(cfg, key, Path(value))
            else:
                setattr(cfg, key, value)

        cfg.categories = [str(category) for category in cfg.categories]
        cfg.locale = str(cfg.locale)
        cfg.login_url = str(cfg.login_url)
        cfg.download_button_names = [str(name) for name in cfg.download_button_names]
        cfg.delete_button_names = [str(name) for name in cfg.delete_button_names]
        cfg.confirm_delete_button_names = [str(name) for name in cfg.confirm_delete_button_names]
        if cfg.browser_channel is not None:
            cfg.browser_channel = str(cfg.browser_channel)
        if cfg.browser_executable_path is not None:
            cfg.browser_executable_path = str(cfg.browser_executable_path)
        return cfg


def load_config(config_path: Path | None = None) -> RephotoConfig:
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Run 'rephoto init-config --config {path}' first."
        )

    raw = json.loads(path.read_text(encoding="utf-8"))
    config = RephotoConfig.from_json(raw)
    config.ensure_directories()
    return config


def write_default_config(config_path: Path | None = None) -> Path:
    path = config_path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    config = RephotoConfig()
    path.write_text(json.dumps(config.to_json(), indent=2) + "\n", encoding="utf-8")
    return path
