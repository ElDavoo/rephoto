from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from rephoto.config import RephotoConfig


class BrowserAutomationError(RuntimeError):
    pass


@dataclass
class SelectionResult:
    remote_batch_id: str
    selected_count: int


_BROWSER_PATH_CANDIDATES = (
    "chromium",
    "google-chrome-stable",
    "google-chrome",
    "chrome",
)

_PLAYWRIGHT_CHROME_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-web-security",
    "--disable-infobars",
    "--disable-extensions",
    "--start-maximized",
    "--window-size=1280,720",
)

_WAYLAND_GPU_SAFE_ARGS = (
    "--ozone-platform=wayland",
    "--enable-features=UseOzonePlatform",
    "--disable-gpu",
    "--disable-gpu-compositing",
    "--disable-accelerated-2d-canvas",
    "--disable-accelerated-video-decode",
    "--disable-features=VaapiVideoDecoder",
)

_PLAYWRIGHT_DESKTOP_CHROME_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


def _browser_process_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("NIXOS_OZONE_WL", "1")
    env.setdefault("OZONE_PLATFORM", "wayland")
    return env


def discover_browser_executable() -> str | None:
    for candidate in _BROWSER_PATH_CANDIDATES:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def resolve_browser_executable(config: RephotoConfig) -> str | None:
    if config.browser_executable_path:
        return config.browser_executable_path
    return discover_browser_executable()


def launch_manual_login_browser(
    config: RephotoConfig,
    *,
    target_url: str = "https://accounts.google.com/",
) -> subprocess.Popen[Any]:
    executable = resolve_browser_executable(config)
    if executable is None:
        raise BrowserAutomationError(
            "No Chrome/Chromium executable is available. "
            "Use 'nix-shell -p chromium --run ...' or set browser_executable_path in config."
        )

    config.ensure_directories()
    command = [
        executable,
        f"--user-data-dir={config.chrome_profile_dir.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        *_WAYLAND_GPU_SAFE_ARGS,
        target_url,
    ]
    try:
        return subprocess.Popen(command, env=_browser_process_env())
    except OSError as exc:
        raise BrowserAutomationError(
            f"Failed to launch browser executable '{executable}': {exc}"
        ) from exc


class PhotosBrowserSession:
    def __init__(self, config: RephotoConfig) -> None:
        self.config = config
        self._playwright_context: Any = None
        self._context: Any = None
        self._page: Any = None
        self._resolved_browser_executable: str | None = None

    def __enter__(self) -> "PhotosBrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserAutomationError(
                "Playwright is not available. Use Nix shell before running this command."
            ) from exc

        self.config.ensure_directories()
        self._playwright_context = sync_playwright().start()
        desktop_chrome = self._playwright_context.devices.get("Desktop Chrome", {})
        desktop_chrome_user_agent = desktop_chrome.get(
            "user_agent",
            _PLAYWRIGHT_DESKTOP_CHROME_USER_AGENT,
        )
        launch_options: dict[str, Any] = {
            "user_data_dir": str(self.config.chrome_profile_dir),
            "headless": self.config.headless,
            "args": [*_PLAYWRIGHT_CHROME_ARGS, *_WAYLAND_GPU_SAFE_ARGS],
            "downloads_path": str(self.config.download_root),
            "accept_downloads": True,
            "env": _browser_process_env(),
            "user_agent": desktop_chrome_user_agent,
            "locale": self.config.locale,
            "viewport": {"width": 1280, "height": 720},
            "device_scale_factor": 1,
        }
        used_auto_discovered_executable = False
        self._resolved_browser_executable = None
        resolved_browser = resolve_browser_executable(self.config)
        if self.config.browser_executable_path:
            launch_options["executable_path"] = self.config.browser_executable_path
            self._resolved_browser_executable = self.config.browser_executable_path
        elif self.config.browser_channel:
            launch_options["channel"] = self.config.browser_channel
        elif resolved_browser:
            launch_options["executable_path"] = resolved_browser
            used_auto_discovered_executable = True
            self._resolved_browser_executable = resolved_browser

        launch_errors: list[str] = []
        try:
            self._context = self._playwright_context.chromium.launch_persistent_context(
                **launch_options,
            )
        except Exception as exc:
            launch_errors.append(str(exc))
            if used_auto_discovered_executable and launch_options.get("executable_path"):
                launch_options.pop("executable_path", None)
                self._resolved_browser_executable = None
                try:
                    self._context = self._playwright_context.chromium.launch_persistent_context(
                        **launch_options,
                    )
                except Exception as retry_exc:
                    launch_errors.append(str(retry_exc))

        if self._context is None:
            if self._playwright_context is not None:
                self._playwright_context.stop()
                self._playwright_context = None

            launch_hint = ""
            if self.config.browser_executable_path:
                launch_hint = (
                    f"browser_executable_path='{self.config.browser_executable_path}' could not be launched. "
                )
            elif self.config.browser_channel:
                launch_hint = (
                    f"browser_channel='{self.config.browser_channel}' is unavailable. "
                )
            elif self._resolved_browser_executable:
                launch_hint = (
                    f"Auto-discovered browser executable '{self._resolved_browser_executable}' could not be launched. "
                )

            details = ""
            if launch_errors:
                unique_errors = [entry for entry in dict.fromkeys(launch_errors) if entry]
                details = " Playwright error: " + " | ".join(unique_errors)

            raise BrowserAutomationError(
                "Unable to launch browser context. "
                + launch_hint
                + "Set browser_executable_path to a locally installed Chrome/Chromium binary."
                + details
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
        if self._context is None:
            raise BrowserAutomationError("Browser context is not active")

        if self._page is None or self._page.is_closed():
            self._page = self._context.new_page()
            self._page.set_default_timeout(self.config.browser_timeout_ms)

        if self._page is None:
            raise BrowserAutomationError("Browser session is not active")
        return self._page

    def _language_hint(self) -> str:
        normalized_locale = str(self.config.locale).strip().replace("_", "-")
        if not normalized_locale:
            return "en"

        primary_language = normalized_locale.split("-", maxsplit=1)[0].lower()
        return primary_language or "en"

    def _url_with_language_hint(self, target_url: str) -> str:
        parsed = urlsplit(str(target_url))
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["hl"] = self._language_hint()
        encoded_query = urlencode(query)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, encoded_query, parsed.fragment))

    def open_account_login_page(self) -> None:
        localized_login_url = self._url_with_language_hint(self.config.login_url)
        self.page.goto(localized_login_url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(400)

    def _is_manage_storage_destination(self, url: str) -> bool:
        expected_url = self._url_with_language_hint(self.config.manage_storage_url)
        expected = urlsplit(expected_url)
        current = urlsplit(url)

        if expected.netloc.lower() != current.netloc.lower():
            return False

        expected_path = expected.path.rstrip("/") or "/"
        current_path = current.path.rstrip("/") or "/"
        return current_path == expected_path or current_path.startswith(expected_path + "/")

    def _category_name_variants(self, category_name: str) -> list[str]:
        normalized = " ".join(category_name.split())
        variants = [
            normalized,
            normalized.replace(" and ", " & "),
            normalized.replace(" & ", " and "),
            normalized.replace("&", "and"),
        ]
        unique_variants: list[str] = []
        for variant in variants:
            cleaned = " ".join(variant.split())
            if cleaned and cleaned not in unique_variants:
                unique_variants.append(cleaned)
        return unique_variants

    def _resolve_checkbox_locator(self, configured_selector: str, fallback_selector: str) -> Any:
        locator = self.page.locator(configured_selector)
        if locator.count() > 0:
            return locator

        fallback_selectors: list[str] = []
        if configured_selector.startswith("main "):
            fallback_selectors.append(configured_selector[len("main ") :])
        fallback_selectors.append(fallback_selector)

        seen: set[str] = set()
        for selector in fallback_selectors:
            if selector in seen:
                continue
            seen.add(selector)
            candidate = self.page.locator(selector)
            if candidate.count() > 0:
                return candidate

        return locator

    def open_manage_storage(self) -> None:
        self.open_login_page()

        if self._is_manage_storage_destination(self.page.url):
            return

        # Google can briefly redirect through accounts.google.com even for
        # already authenticated profiles; wait for the final destination.
        for _ in range(12):
            current_url = self.page.url
            if self._is_manage_storage_destination(current_url):
                return

            auth_error = self._authentication_error()
            if auth_error:
                raise BrowserAutomationError(auth_error)

            self.page.wait_for_timeout(500)

        current_url = self.page.url
        raise BrowserAutomationError(
            "Unable to reach Google Photos manage storage page. "
            f"Current URL: {current_url}"
        )

    def open_login_page(self) -> None:
        localized_manage_storage_url = self._url_with_language_hint(self.config.manage_storage_url)
        self.page.goto(localized_manage_storage_url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(400)

    def authentication_error(self) -> str | None:
        return self._authentication_error()

    def is_authenticated(self) -> bool:
        return self._authentication_error() is None

    def _authentication_error(self) -> str | None:
        current_url = self.page.url.lower()
        if "accounts.google.com" not in current_url:
            return None

        body_text = ""
        try:
            body_text = self.page.inner_text("body").lower()
        except Exception:
            body_text = ""

        if "may not be secure" in body_text or "not safe" in body_text:
            return (
                "Google rejected sign-in for this browser context ('not safe'). "
                "Use a normal Chrome/Chromium binary by setting browser_executable_path in config, "
                "or run through 'nix-shell -p chromium' so Chromium is available in PATH, "
                "then log in once in that profile and rerun."
            )

        if (
            "non puoi accedere da questo dispositivo" in body_text
            or "browser o app potrebbe non essere sicura" in body_text
            or "non è possibile accedere da questo dispositivo" in body_text
        ):
            return (
                "Google ha bloccato l'accesso per questo contesto browser. "
                "Apri una sessione di login manuale con un Chromium reale (per esempio via "
                "'nix-shell -p chromium --run ...'), completa il login e poi riesegui il comando."
            )

        sign_in_markers = (
            "sign in",
            "accedi",
            "scegli un account",
            "choose an account",
            "use your google account",
            "utilizza il tuo account google",
        )
        if any(marker in body_text for marker in sign_in_markers):
            return (
                "Google Photos is not authenticated in the configured browser profile. "
                "Log in manually in the opened browser and rerun dry-run."
            )

        if "servicelogin" in current_url or "identifier" in current_url:
            return (
                "Google Photos is not authenticated in the configured browser profile. "
                "Log in manually in the opened browser and rerun dry-run."
            )

        # Unknown accounts.google.com intermediate page, likely part of redirect chain.
        return None

    def save_storage_state(self, output_path: Path) -> Path:
        if self._context is None:
            raise BrowserAutomationError("Browser context is not active")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(output_path))
        return output_path

    def open_category(self, category_name: str) -> None:
        self.open_manage_storage()
        timeout_ms = min(self.config.browser_timeout_ms, 8_000)
        category_variants = self._category_name_variants(category_name)

        for candidate_name in category_variants:
            target = self.page.get_by_role("option", name=re.compile(re.escape(candidate_name), re.IGNORECASE)).first
            try:
                target.click(timeout=timeout_ms)
                self.page.wait_for_timeout(800)
                return
            except Exception:
                continue

        for candidate_name in category_variants:
            target = self.page.get_by_text(candidate_name, exact=False).first
            try:
                target.click(timeout=timeout_ms)
                self.page.wait_for_timeout(800)
                return
            except Exception:
                continue

        current_url = self.page.url
        raise BrowserAutomationError(
            f"Unable to open category '{category_name}' from {current_url}. "
            "Update category text for your language and confirm Google Photos is logged in."
        )

    def select_next_batch(self, batch_size: int) -> SelectionResult | None:
        if batch_size <= 0:
            raise BrowserAutomationError("batch_size must be greater than zero")

        checkboxes = self._resolve_checkbox_locator(
            self.config.media_checkbox_selector,
            "div[role='checkbox'][aria-checked='false']",
        )
        available_count = checkboxes.count()
        if available_count == 0:
            return None

        selected_count = min(batch_size, available_count)
        for _ in range(selected_count):
            checkboxes.first.click(force=True)

        self.page.wait_for_timeout(250)
        return SelectionResult(remote_batch_id=str(uuid4()), selected_count=selected_count)

    def clear_selection(self) -> None:
        selected = self._resolve_checkbox_locator(
            self.config.selected_checkbox_selector,
            "div[role='checkbox'][aria-checked='true']",
        )
        selected_count = selected.count()
        for _ in range(selected_count):
            selected.first.click(force=True)
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
