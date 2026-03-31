from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from rephoto.config import RephotoConfig


class BrowserAutomationError(RuntimeError):
    pass


@dataclass
class SelectionResult:
    remote_batch_id: str
    selected_count: int


class PhotosBrowserSession:
    def __init__(self, config: RephotoConfig) -> None:
        self.config = config
        self._playwright_context: Any = None
        self._context: Any = None
        self._page: Any = None

    def __enter__(self) -> "PhotosBrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserAutomationError(
                "Playwright is not available. Use Nix shell before running this command."
            ) from exc

        self.config.ensure_directories()
        self._playwright_context = sync_playwright().start()
        self._context = self._playwright_context.chromium.launch_persistent_context(
            user_data_dir=str(self.config.chrome_profile_dir),
            headless=self.config.headless,
            downloads_path=str(self.config.download_root),
            accept_downloads=True,
            args=["--disable-dev-shm-usage"],
        )

        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = self._context.new_page()

        self._page.set_default_timeout(self.config.browser_timeout_ms)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._playwright_context is not None:
                self._playwright_context.stop()

    @property
    def page(self) -> Any:
        if self._page is None:
            raise BrowserAutomationError("Browser session is not active")
        return self._page

    def open_manage_storage(self) -> None:
        self.page.goto(self.config.manage_storage_url)
        self.page.wait_for_load_state("networkidle")

    def open_category(self, category_name: str) -> None:
        self.open_manage_storage()
        target = self.page.get_by_text(category_name, exact=False).first
        try:
            target.click()
        except Exception as exc:
            raise BrowserAutomationError(
                f"Unable to open category '{category_name}'. Update category text in config."
            ) from exc
        self.page.wait_for_timeout(800)

    def select_next_batch(self, batch_size: int) -> SelectionResult | None:
        if batch_size <= 0:
            raise BrowserAutomationError("batch_size must be greater than zero")

        checkboxes = self.page.locator(self.config.media_checkbox_selector)
        available_count = checkboxes.count()
        if available_count == 0:
            return None

        selected_count = min(batch_size, available_count)
        for _ in range(selected_count):
            checkboxes.first.click()

        self.page.wait_for_timeout(250)
        return SelectionResult(remote_batch_id=str(uuid4()), selected_count=selected_count)

    def clear_selection(self) -> None:
        selected = self.page.locator(self.config.selected_checkbox_selector)
        selected_count = selected.count()
        for _ in range(selected_count):
            selected.first.click()
        self.page.wait_for_timeout(200)

    def _click_button_by_name(self, names: list[str], *, timeout_ms: int = 1500) -> bool:
        for name in names:
            locator = self.page.get_by_role(
                "button",
                name=re.compile(re.escape(name), re.IGNORECASE),
            ).first
            try:
                if locator.is_visible(timeout=timeout_ms):
                    locator.click()
                    return True
            except Exception:
                continue
        return False

    def download_selected(self, remote_batch_id: str) -> Path:
        with self.page.expect_download(timeout=self.config.download_wait_seconds * 1000) as download_info:
            if not self._click_button_by_name(self.config.download_button_names):
                raise BrowserAutomationError(
                    "Download button not found for selected items. "
                    "Adjust download_button_names in configuration."
                )
        download = download_info.value

        suffix = Path(download.suggested_filename).suffix or ".zip"
        archive_path = self.config.download_root / f"{remote_batch_id}{suffix}"
        download.save_as(str(archive_path))
        return archive_path

    def trash_selected(self) -> None:
        if not self._click_button_by_name(self.config.delete_button_names):
            raise BrowserAutomationError(
                "Delete/trash button not found. Adjust delete_button_names in configuration."
            )

        self.page.wait_for_timeout(400)
        self._click_button_by_name(self.config.confirm_delete_button_names, timeout_ms=3000)
        self.page.wait_for_timeout(350)

    def capture_failure_artifact(self, label: str) -> Path:
        safe_label = re.sub(r"[^a-zA-Z0-9._-]", "-", label)
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        screenshot_path = self.config.logs_root / f"{stamp}-{safe_label}.png"
        self.config.logs_root.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(screenshot_path), full_page=True)
        return screenshot_path
