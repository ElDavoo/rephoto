#!/usr/bin/env python3
"""Authenticate gpmc with an OAuth bearer token pulled from a rooted device via ADB.

Instead of a full ``auth_data`` string (which embeds the account's long-lived AAS
master token), this reads the short-lived ``photos.native`` OAuth token that GMS
caches on the device in ``accounts_ce.db`` and injects it into a gpmc ``Client``.

The cached token lives ~1 hour. ``attach_adb_auth`` re-pulls it from the device
automatically when gpmc considers the current one expired. If the *device's* cached
token has itself gone stale, open the Google Photos app on the phone (or let it sync)
so GMS mints a fresh one, then the next pull picks it up.

Requires: ``adb`` on PATH (``adb.exe`` on Windows; override with ``GPMC_ADB``), the
device authorized for adb, and ``su`` (root) on device.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from urllib.parse import quote
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gpmc import Client

# Resolved through PATH, so plain "adb" also finds adb.exe on Windows.
ADB_BINARY = os.environ.get("GPMC_ADB", "adb")

ACCOUNTS_DB = "/data/system_ce/0/accounts_ce.db"
SCOPE_MATCH = "photos.native"
# Seconds to trust a freshly pulled token before re-pulling. Kept under the ~1h
# GMS token lifetime so we re-pull before Google would reject it.
DEFAULT_TTL = 3000


def _adb_base(serial: str | None) -> list[str]:
    if shutil.which(ADB_BINARY) is None:
        raise RuntimeError(
            f"'{ADB_BINARY}' not found on PATH. Install platform-tools (Windows: the "
            "Android SDK platform-tools zip, then add it to PATH) or set GPMC_ADB to "
            "the full path of the adb executable."
        )
    return [ADB_BINARY] + (["-s", serial] if serial else [])


def _run_sql(sql: str, serial: str | None = None, timeout: int = 30) -> str:
    """Run a SQL statement against accounts_ce.db as root, SQL passed over stdin.

    Passing SQL on stdin avoids quoting it through both adb's transport and the
    device shell, so LIKE patterns with quotes work reliably.
    """
    proc = subprocess.run(
        _adb_base(serial) + ["shell", f"su -c 'sqlite3 {ACCOUNTS_DB}'"],
        input=sql,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(f"adb/sqlite query failed: {detail}")
    return proc.stdout.replace("\r", "").strip()


def detect_google_account(serial: str | None = None) -> tuple[int, str]:
    """Return (accounts_id, email) for the single com.google account on the device."""
    out = _run_sql("SELECT _id, name FROM accounts WHERE type='com.google';", serial)
    rows = [line for line in out.splitlines() if line.strip()]
    if not rows:
        raise RuntimeError("No com.google account found on the device.")
    if len(rows) > 1:
        listing = "\n".join(f"  {r}" for r in rows)
        raise RuntimeError(
            "Multiple Google accounts on the device; pass account_id explicitly.\n" + listing
        )
    _id, name = rows[0].split("|", 1)
    return int(_id), name


def pull_bearer_token(
    serial: str | None = None,
    account_id: int = 1,
    scope_match: str = SCOPE_MATCH,
) -> str:
    """Read the cached photos.native OAuth bearer token for the given account."""
    sql = (
        f"SELECT authtoken FROM authtokens "
        f"WHERE accounts_id={int(account_id)} AND type LIKE '%{scope_match}%';"
    )
    token = _run_sql(sql, serial).strip()
    if not token:
        raise RuntimeError(
            f"No cached '{scope_match}' token for account_id={account_id}. "
            "Open Google Photos on the device so GMS mints one, then retry."
        )
    if not token.startswith("ya29."):
        raise RuntimeError(f"Unexpected token format (got {token[:16]!r}); expected 'ya29.*'.")
    return token


def minimal_auth_data(email: str, lang: str = "en") -> str:
    """Build the smallest auth_data string that satisfies gpmc's Client constructor.

    gpmc only parses ``Email`` (and optionally ``lang``) at construction time; the
    real token exchange is bypassed by ``attach_adb_auth``.
    """
    return f"Email={quote(email)}&lang={lang}"


def attach_adb_auth(
    client: "Client",
    serial: str | None = None,
    account_id: int = 1,
    ttl: int = DEFAULT_TTL,
    scope_match: str = SCOPE_MATCH,
) -> "Client":
    """Make ``client`` authenticate using device-pulled bearer tokens.

    Overrides gpmc's ``/auth`` exchange so that whenever a fresh bearer is needed
    (at startup and on expiry) it is pulled from the device instead.
    """

    def _pull_as_auth_response() -> dict[str, str]:
        token = pull_bearer_token(serial, account_id, scope_match)
        return {"Auth": token, "Expiry": str(int(time.time()) + ttl)}

    # gpmc calls self._get_auth_token() from its bearer_token property when the
    # cached token is expired; point that at the device pull instead of /auth.
    client.api._get_auth_token = _pull_as_auth_response  # type: ignore[method-assign]
    # Prime immediately so any adb/root problem surfaces at startup, not mid-run.
    client.api.auth_response_cache = _pull_as_auth_response()
    return client


def is_auth_error(exc: BaseException) -> bool:
    """True if ``exc`` is an HTTP 401/403 (expired/invalid bearer)."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status in (401, 403)


def force_refresh(client: "Client") -> str:
    """Re-pull a fresh bearer from the device right now and install it.

    Only valid after :func:`attach_adb_auth`, whose device-puller is reused here.
    """
    client.api.auth_response_cache = client.api._get_auth_token()
    return client.api.auth_response_cache.get("Auth", "")


def interactive_refresh(client: "Client", reason: str = "Auth token expired (401).") -> str:
    """Pause for the operator, then re-pull the device token and resume.

    Prints why it paused, waits for Enter (so the user can open Google Photos on
    the phone to make GMS mint a fresh token), then re-pulls and returns the new
    bearer. A non-interactive stdin (EOF) falls through to an immediate re-pull.
    """
    print(f"\n⏸  {reason}")
    print("   Open Google Photos on the phone (or wait a moment) so GMS mints a fresh token.")
    try:
        input("   Press Enter to re-pull the token from the device and continue... ")
    except EOFError:
        print("   (no interactive stdin; re-pulling immediately)")
    return force_refresh(client)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Pull the cached Google Photos OAuth token from a rooted device via ADB.")
    ap.add_argument("--serial", default=None, help="adb device serial (optional)")
    ap.add_argument("--account-id", type=int, default=None, help="accounts_ce.db _id (default: auto-detect)")
    args = ap.parse_args()

    acct_id = args.account_id
    if acct_id is None:
        acct_id, email = detect_google_account(args.serial)
        print(f"# account_id={acct_id} email={email}")
    print(pull_bearer_token(args.serial, acct_id))
