#!/usr/bin/env python3
"""Authenticate gpmc with a Google Photos bearer minted in-process via gpsoauth.

A device-free alternative to ``gpmc_adb_auth.py``: instead of pulling the
short-lived ``photos.native`` bearer off a rooted phone, we hold the account's
long-lived AAS master token locally and mint fresh ``ya29.*`` bearers on demand
with gpsoauth's reverse-engineered ``/auth`` exchange — the same call gpmc's own
``_get_auth_token()`` makes, and exactly what microG/ReVanced would produce.

One-time setup (browser only; no root, no device)::

    python gpmc_gpsoauth_auth.py login --email you@gmail.com

signs you in at Google's EmbeddedSetup page; you paste back the resulting
``oauth_token`` cookie, which is exchanged for the durable master token and
stored with owner-only permissions under ``~/.gpmc/<email>/gpsoauth.json``. Thereafter
``--gpsoauth`` mints bearers from that token with no interaction; because the
master token is long-lived, expiry refreshes are silent (unlike the ADB
pause/re-pull).

Requires the vendored ``gpsoauth`` submodule (its pinned commit is the code that
runs) or a pip-installed ``gpsoauth``, plus its runtime deps (pycryptodomex,
requests).
"""
from __future__ import annotations

import getpass
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from gpmc import Client

# --- Google Photos app identity presented to /auth (mirrors gpmc/api.py) ---
PHOTOS_APP = "com.google.android.apps.photos"
PHOTOS_SERVICE = "oauth2:https://www.googleapis.com/auth/photos.native"
# SHA1 digest of Google Photos' OWN app signing certificate — the ``client_sig``
# the privileged ``photos.native`` scope is registered against. It is NOT
# gpsoauth's default ``38918a…5788``: that is the GmsCore / GoogleLoginService
# cert, which is correct for the master-token exchange (``exchange_token``) but
# yields ``UNREGISTERED_ON_API_CONSOLE`` for photos.native. Google Photos uses
# signing-key rotation (APK Signature Scheme v3); the value below is the ORIGINAL
# cert (Android 7–12 signer), which the legacy signature API — and hence this
# auth protocol — reports. Overridable via GP_PHOTOS_CLIENT_SIG / --client-sig.
PHOTOS_CLIENT_SIG = os.environ.get(
    "GP_PHOTOS_CLIENT_SIG", "24bb24c05e47e0aefa68a58a766179d9b613a600"
)
# The rotated Google Photos signing cert (Android 13+ signer) — the fallback to
# try if the original above is rejected for a given setup.
PHOTOS_CLIENT_SIG_ROTATED = "f8456b1d9986acf9ce21fb450b0d32b895f36885"

# EmbeddedSetup is the same "add account" web flow Android uses. After sign-in
# the page sets an ``oauth_token`` cookie (starts ``oauth2_4/``) — that is what
# exchange_token turns into the master token.
EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"

MASTER_TOKEN_ENV = "GP_MASTER_TOKEN"
ANDROID_ID_ENV = "GP_ANDROID_ID"

# Seconds to trust a freshly minted bearer when Google's response omits Expiry.
# Kept under the ~1h bearer lifetime so gpmc re-mints before Google rejects it.
DEFAULT_TTL = 3000

REPO_ROOT = Path(__file__).resolve().parent
_GPSOAUTH_SUBMODULE = REPO_ROOT / "vendor" / "gpsoauth"


def _import_gpsoauth_module(vendored: bool):
    """Import and return the ``gpsoauth`` module object.

    gpsoauth's ``__init__`` runs ``version(__package__)`` at import time, which
    needs installed distribution metadata that a bare submodule checkout does not
    have. When importing the vendored source we therefore temporarily shim
    ``importlib.metadata.version`` so that one lookup returns a placeholder
    instead of raising ``PackageNotFoundError`` — metadata for real deps such as
    urllib3 is left untouched, and the shim is removed as soon as the import
    finishes.
    """
    existing = sys.modules.get("gpsoauth")
    if existing is not None:
        return existing
    if not vendored:
        import gpsoauth
        return gpsoauth

    import importlib.metadata as _md

    original_version = _md.version

    def _version_with_vendored_fallback(name):
        try:
            return original_version(name)
        except _md.PackageNotFoundError:
            if name == "gpsoauth":
                return "0.0.0+vendored"
            raise

    _md.version = _version_with_vendored_fallback
    try:
        import gpsoauth
    finally:
        _md.version = original_version
    return gpsoauth


def _load_gpsoauth():
    """Import and return the ``gpsoauth`` module.

    Prefers the vendored submodule (its pinned commit is the code that runs),
    falling back to any installed ``gpsoauth``. This is the single injection
    point the tests stub, so no network is touched under test.
    """
    pkg_init = _GPSOAUTH_SUBMODULE / "gpsoauth" / "__init__.py"
    vendored = pkg_init.is_file()
    if vendored and str(_GPSOAUTH_SUBMODULE) not in sys.path:
        sys.path.insert(0, str(_GPSOAUTH_SUBMODULE))
    try:
        gpsoauth = _import_gpsoauth_module(vendored)
    except ImportError as exc:
        if vendored:
            raise RuntimeError(
                f"gpsoauth is vendored at {_GPSOAUTH_SUBMODULE} but failed to import "
                f"({exc}); its runtime deps are missing. The Nix devshell provides them; "
                "non-Nix: `pip install pycryptodomex requests`."
            ) from exc
        raise RuntimeError(
            "gpsoauth is not available. Initialize the submodule with "
            "`git submodule update --init vendor/gpsoauth`, or `pip install gpsoauth` "
            "(runtime deps: pycryptodomex, requests)."
        ) from exc
    if not hasattr(gpsoauth, "perform_oauth"):
        raise RuntimeError(
            "Imported 'gpsoauth' has no perform_oauth; the submodule is likely "
            "uninitialized (empty vendor/gpsoauth). Run "
            "`git submodule update --init vendor/gpsoauth`."
        )
    return gpsoauth


def _proxy_dict(proxy: str | None) -> dict[str, str] | None:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def new_android_id() -> str:
    """Return a random 64-bit device id as 16 lowercase hex chars."""
    return secrets.token_hex(8)


def default_store_path(email: str) -> Path:
    """Default location for the stored master-token credentials for ``email``."""
    return Path.home() / ".gpmc" / email / "gpsoauth.json"


def minimal_auth_data(email: str, lang: str = "en") -> str:
    """Smallest auth_data string that satisfies gpmc's Client constructor.

    gpmc parses only ``Email`` (and ``lang``) at construction; the real token
    exchange is replaced by :func:`attach_gpsoauth_auth`.
    """
    return f"Email={quote(email)}&lang={lang}"


def restrict_to_owner(path: Path) -> None:
    """Make ``path`` readable by its owner only.

    POSIX gets mode 0600. Windows has no mode bits — ``os.chmod`` there only
    toggles the read-only attribute — so the ACL is rewritten instead: drop
    inherited entries and grant the current account alone full access. If that
    fails we say so rather than leaving a master token silently world-readable.
    """
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    try:
        user = os.environ.get("USERNAME") or getpass.getuser()
        proc = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "icacls failed")
    except Exception as exc:  # noqa: BLE001 - never lose the saved token over this
        print(
            f"⚠ Could not restrict permissions on {path} ({exc}). It holds the account's "
            "master token — tighten its ACL manually."
        )


def save_credentials(
    path: "Path | str",
    *,
    email: str,
    master_token: str,
    android_id: str,
    client_sig: str | None = None,
) -> Path:
    """Write the master-token credentials to ``path`` as owner-only JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "email": email,
        "master_token": master_token,
        "android_id": android_id,
        "client_sig": client_sig or PHOTOS_CLIENT_SIG,
    }
    # Create the file private from the start, then restrict it explicitly (a
    # pre-existing file opened with O_TRUNC keeps its old permissions).
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    restrict_to_owner(path)
    return path


def load_credentials(path: "Path | str") -> dict[str, str]:
    """Load master-token credentials from ``path``.

    Raises ``FileNotFoundError`` (with a hint to run ``login``) when absent.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"No gpsoauth credentials at {path}. Run: "
            "python gpmc_gpsoauth_auth.py login --email you@gmail.com"
        )
    with path.open() as fh:
        return json.load(fh)


def exchange_oauth_token(email: str, oauth_token: str, android_id: str, *, proxy: str | None = None) -> str:
    """Exchange a web ``oauth_token`` (oauth2_4/...) for a durable master token."""
    gpsoauth = _load_gpsoauth()
    result = gpsoauth.exchange_token(email, oauth_token, android_id, proxy=_proxy_dict(proxy))
    token = result.get("Token")
    if not token:
        detail = result.get("Error") or result
        raise RuntimeError(
            f"Master-token exchange failed: {detail}. Re-copy a fresh oauth_token "
            "from the EmbeddedSetup page (it is single-use) and retry."
        )
    return token


def mint_bearer(
    email: str,
    master_token: str,
    android_id: str,
    *,
    client_sig: str | None = None,
    ttl: int = DEFAULT_TTL,
    proxy: str | None = None,
) -> dict[str, str]:
    """Mint a fresh photos.native bearer from the master token.

    Sends the exact ``/auth`` field set gpmc's own ``_get_auth_token()`` uses —
    the proven first-party request: Google Photos as both ``app`` and
    ``callerPkg``, ``client_sig``/``callerSig`` = Photos' signing cert,
    ``oauth2_foreground``, and the master token in ``Token=`` — through gpsoauth's
    TLS-tuned ``_perform_auth_request`` (Google rejects the wrong TLS ciphers with
    403). gpsoauth's stock ``perform_oauth`` omits the caller fields and puts the
    master token in ``EncryptedPasswd``, which Google refuses for this privileged
    scope (``UNREGISTERED_ON_API_CONSOLE``).

    Returns the ``{"Auth": ..., "Expiry": ...}`` shape gpmc's ``_get_auth_token``
    contract expects; ``Expiry`` is synthesized (now + ttl) when Google omits it.
    """
    gpsoauth = _load_gpsoauth()
    sig = client_sig or PHOTOS_CLIENT_SIG
    data = {
        "androidId": android_id,
        "app": PHOTOS_APP,
        "client_sig": sig,
        "callerPkg": PHOTOS_APP,
        "callerSig": sig,
        "device_country": "us",
        "Email": email,
        "google_play_services_version": 240913000,
        "lang": "en",
        "oauth2_foreground": "1",
        "sdk_version": 17,
        "service": PHOTOS_SERVICE,
        "Token": master_token,
    }
    result = gpsoauth._perform_auth_request(data, _proxy_dict(proxy))
    auth = result.get("Auth")
    if not auth:
        detail = result.get("Error") or result
        raise RuntimeError(
            f"Bearer mint failed: {detail}. If this is UNREGISTERED_ON_API_CONSOLE, the "
            f"Google Photos client_sig ({sig}) is wrong for your setup — retry with the "
            f"rotated cert: --client-sig {PHOTOS_CLIENT_SIG_ROTATED}. Otherwise the master "
            "token may be revoked/expired — re-run `python gpmc_gpsoauth_auth.py login`."
        )
    expiry = result.get("Expiry") or str(int(time.time()) + ttl)
    return {"Auth": auth, "Expiry": expiry}


def attach_gpsoauth_auth(
    client: "Client",
    email: str,
    master_token: str,
    android_id: str,
    *,
    client_sig: str | None = None,
    ttl: int = DEFAULT_TTL,
    proxy: str | None = None,
) -> "Client":
    """Make ``client`` mint bearers via gpsoauth instead of gpmc's ``/auth`` call.

    Mirrors :func:`gpmc_adb_auth.attach_adb_auth`: overrides ``_get_auth_token``
    and primes the cache immediately so any credential problem surfaces at
    startup, not mid-run.
    """

    def _mint() -> dict[str, str]:
        return mint_bearer(email, master_token, android_id, client_sig=client_sig, ttl=ttl, proxy=proxy)

    client.api._get_auth_token = _mint  # type: ignore[method-assign]
    client.api.auth_response_cache = _mint()
    return client


def force_refresh(client: "Client") -> str:
    """Silently re-mint a bearer right now and install it; return the new bearer.

    Valid after :func:`attach_gpsoauth_auth`; reuses its installed minter. Unlike
    the ADB path this needs no operator interaction — the master token mints a
    fresh bearer on its own.
    """
    client.api.auth_response_cache = client.api._get_auth_token()
    return client.api.auth_response_cache.get("Auth", "")


# --------------------------------------------------------------------------- CLI


def resolve_store_path(store: "Path | str | None" = None, email: str | None = None, base: "Path | str | None" = None) -> Path:
    """Locate the credentials file.

    Precedence: explicit ``store`` > ``email`` (its default per-account path) >
    auto-discovery of a single ``<base>/*/gpsoauth.json`` (``base`` defaults to
    ``~/.gpmc``). Raises when discovery finds nothing or is ambiguous.
    """
    if store:
        return Path(store)
    if email:
        return default_store_path(email)
    base = Path(base) if base else (Path.home() / ".gpmc")
    matches = sorted(base.glob("*/gpsoauth.json")) if base.is_dir() else []
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(
            f"No gpsoauth credentials found under {base}. Run "
            "`python gpmc_gpsoauth_auth.py login --email you@gmail.com` first, "
            "or pass --gpsoauth-store / --gpsoauth-email."
        )
    raise RuntimeError(
        f"Multiple gpsoauth credentials under {base}: {[str(m) for m in matches]}. "
        "Disambiguate with --gpsoauth-email or --gpsoauth-store."
    )


def _login_cli(args) -> int:
    email = args.email
    store = resolve_store_path(store=args.store, email=email)
    client_sig = args.client_sig or PHOTOS_CLIENT_SIG

    existing = None
    try:
        existing = load_credentials(store)
    except FileNotFoundError:
        pass

    # Keep the same device identity across re-logins (e.g. to change --client-sig).
    android_id = (
        args.android_id
        or os.environ.get(ANDROID_ID_ENV)
        or (existing or {}).get("android_id")
        or new_android_id()
    )

    master_token = args.master_token or os.environ.get(MASTER_TOKEN_ENV)
    if not master_token and args.reuse_master:
        if not existing:
            print(f"--reuse-master given but no stored credentials at {store}.")
            return 1
        master_token = existing["master_token"]
        print("Reusing the stored master token (no browser login needed).")
    if not master_token:
        oauth_token = args.oauth_token
        if not oauth_token:
            print("Sign in at the URL below in any browser (2FA/passkeys work), then read the")
            print("'oauth_token' cookie it sets (DevTools -> Application -> Cookies; starts 'oauth2_4/'):")
            print(f"\n  {EMBEDDED_SETUP_URL}\n")
            try:
                oauth_token = input("Paste oauth_token: ").strip()
            except EOFError:
                oauth_token = ""
        if not oauth_token:
            print("No oauth_token provided; aborting.")
            return 1
        master_token = exchange_oauth_token(email, oauth_token, android_id, proxy=args.proxy)

    # Persist the (long-lived, hard-won) master token BEFORE validating, so a
    # transient mint failure never forces another browser login — you can retry
    # minting with a different --client-sig against the saved token (--reuse-master).
    path = save_credentials(
        store, email=email, master_token=master_token, android_id=android_id, client_sig=client_sig
    )
    try:
        mint_bearer(email, master_token, android_id, client_sig=client_sig, proxy=args.proxy)
    except Exception as exc:  # noqa: BLE001 - report but keep the saved master token
        print(f"⚠ Stored the master token at {path} (owner-only), but a test bearer could not be minted:")
        print(f"    {exc}")
        print("  The master token is saved; retry the mint without another browser login:")
        print(
            f"    python gpmc_gpsoauth_auth.py login --email {email} --reuse-master "
            f"--client-sig {PHOTOS_CLIENT_SIG_ROTATED}"
        )
        return 1
    print(f"✓ Stored and verified gpsoauth credentials for {email} at {path} (owner-only).")
    print(f"  android_id={android_id}  client_sig={client_sig}")
    print("Next:  python requota_migration.py --gpsoauth --download-only --limit 5")
    return 0


def _bearer_cli(args) -> int:
    creds = load_credentials(resolve_store_path(store=args.store, email=args.email))
    sig = args.client_sig or creds.get("client_sig")
    out = mint_bearer(
        creds["email"], creds["master_token"], creds["android_id"], client_sig=sig, proxy=args.proxy
    )
    print(out["Auth"])
    return 0


def main() -> int:
    import argparse

    # A Windows console (and any redirected output) defaults to a legacy code page
    # that cannot encode the status symbols below.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    ap = argparse.ArgumentParser(description="Manage the gpsoauth master-token auth source for gpmc.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("login", help="Obtain (browser or direct) and store the account master token.")
    lp.add_argument("--email", required=True, help="Google account email.")
    lp.add_argument("--oauth-token", default=None, help="Web oauth_token (oauth2_4/...). If omitted, prompts.")
    lp.add_argument("--master-token", default=None, help="Existing aas_et/... master token; skips the browser.")
    lp.add_argument("--android-id", default=None, help="16-hex device id (default: random, persisted).")
    lp.add_argument("--store", default=None, help="Credentials path (default: ~/.gpmc/<email>/gpsoauth.json).")
    lp.add_argument(
        "--client-sig",
        default=None,
        help=f"Google Photos signing-cert SHA1 (default {PHOTOS_CLIENT_SIG}; rotated cert: {PHOTOS_CLIENT_SIG_ROTATED}).",
    )
    lp.add_argument(
        "--reuse-master",
        action="store_true",
        help="Reuse the stored master token instead of a new browser login (e.g. to retry with a different --client-sig).",
    )
    lp.add_argument("--proxy", default=None, help="Optional proxy URL for the /auth calls.")
    lp.set_defaults(func=_login_cli)

    bp = sub.add_parser("bearer", help="Mint and print a fresh bearer from stored credentials.")
    bp.add_argument("--email", required=True, help="Google account email.")
    bp.add_argument("--store", default=None, help="Credentials path (default: ~/.gpmc/<email>/gpsoauth.json).")
    bp.add_argument("--client-sig", default=None, help="Override the stored Google Photos client_sig for this mint.")
    bp.add_argument("--proxy", default=None, help="Optional proxy URL for the /auth call.")
    bp.set_defaults(func=_bearer_cli)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
