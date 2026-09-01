"""Unit tests for gpmc_gpsoauth_auth (offline; gpsoauth is stubbed).

Run directly:  python3 tests/test_gpsoauth_auth.py -v
The gpsoauth network calls (perform_oauth / exchange_token) are never made;
``_load_gpsoauth`` is monkeypatched to a fake so these tests need no network,
no real credentials, and no gpsoauth runtime deps.
"""
from __future__ import annotations

import os
import sys
import time
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gpmc_gpsoauth_auth as g


class _FakeGpsoauth:
    """Stand-in for the gpsoauth module: records calls, returns canned responses."""

    def __init__(self, auth_request_result=None, exchange_token_result=None):
        self._auth_request_result = auth_request_result or {}
        self._exchange_token_result = exchange_token_result or {}
        self.auth_request_calls = []
        self.exchange_token_calls = []

    def _perform_auth_request(self, data, proxies=None):
        self.auth_request_calls.append((data, proxies))
        return dict(self._auth_request_result)

    def exchange_token(self, *args, **kwargs):
        self.exchange_token_calls.append((args, kwargs))
        return dict(self._exchange_token_result)


class _PatchMixin:
    def use_fake(self, fake):
        original = g._load_gpsoauth
        g._load_gpsoauth = lambda: fake
        self.addCleanup(setattr, g, "_load_gpsoauth", original)


class _FakeApi:
    def __init__(self):
        self.auth_response_cache = {"Expiry": "0", "Auth": ""}
        self._get_auth_token = None


class _FakeClient:
    def __init__(self):
        self.api = _FakeApi()


class CredentialStoreTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "sub" / "gpsoauth.json"
            g.save_credentials(path, email="me@gmail.com", master_token="aas_et/x", android_id="0123456789abcdef")
            loaded = g.load_credentials(path)
            self.assertEqual(loaded["email"], "me@gmail.com")
            self.assertEqual(loaded["master_token"], "aas_et/x")
            self.assertEqual(loaded["android_id"], "0123456789abcdef")

    @unittest.skipIf(os.name == "nt", "POSIX mode bits do not exist on Windows (ACLs are set instead)")
    def test_saved_file_is_0600(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "gpsoauth.json"
            g.save_credentials(path, email="me@gmail.com", master_token="aas_et/x", android_id="0123456789abcdef")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_load_missing_raises_filenotfound(self):
        with TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                g.load_credentials(Path(d) / "nope.json")


class AndroidIdTests(unittest.TestCase):
    def test_new_android_id_is_16_hex(self):
        aid = g.new_android_id()
        self.assertEqual(len(aid), 16)
        int(aid, 16)  # raises ValueError if not hex
        self.assertEqual(aid, aid.lower())

    def test_new_android_id_is_random(self):
        self.assertNotEqual(g.new_android_id(), g.new_android_id())


class MintBearerTests(_PatchMixin, unittest.TestCase):
    def test_posts_gpmc_photos_field_set(self):
        fake = _FakeGpsoauth(auth_request_result={"Auth": "ya29.TESTTOKEN"})
        self.use_fake(fake)
        out = g.mint_bearer("me@gmail.com", "aas_et/x", "0123456789abcdef")
        self.assertEqual(len(fake.auth_request_calls), 1)
        data, _ = fake.auth_request_calls[0]
        # The privileged photos.native request must present Photos' OWN client
        # identity as both client and caller (the proven gpmc field set), with
        # the master token in Token= — not gpsoauth's stock perform_oauth shape.
        self.assertEqual(data["app"], g.PHOTOS_APP)
        self.assertEqual(data["callerPkg"], g.PHOTOS_APP)
        self.assertEqual(data["client_sig"], g.PHOTOS_CLIENT_SIG)
        self.assertEqual(data["callerSig"], g.PHOTOS_CLIENT_SIG)
        self.assertEqual(data["service"], g.PHOTOS_SERVICE)
        self.assertEqual(data["Email"], "me@gmail.com")
        self.assertEqual(data["androidId"], "0123456789abcdef")
        self.assertEqual(data["Token"], "aas_et/x")
        self.assertEqual(data.get("oauth2_foreground"), "1")
        self.assertEqual(out["Auth"], "ya29.TESTTOKEN")

    def test_uses_overridden_client_sig_for_client_and_caller(self):
        fake = _FakeGpsoauth(auth_request_result={"Auth": "ya29.X"})
        self.use_fake(fake)
        g.mint_bearer("me@gmail.com", "aas_et/x", "0123456789abcdef", client_sig="deadbeef")
        data, _ = fake.auth_request_calls[0]
        self.assertEqual(data["client_sig"], "deadbeef")
        self.assertEqual(data["callerSig"], "deadbeef")

    def test_synthesizes_expiry_when_absent(self):
        fake = _FakeGpsoauth(auth_request_result={"Auth": "ya29.X"})
        self.use_fake(fake)
        before = int(time.time())
        out = g.mint_bearer("me@gmail.com", "aas_et/x", "0123456789abcdef", ttl=1000)
        self.assertIn("Expiry", out)
        self.assertGreaterEqual(int(out["Expiry"]), before + 1000)

    def test_preserves_expiry_when_present(self):
        fake = _FakeGpsoauth(auth_request_result={"Auth": "ya29.X", "Expiry": "9999999999"})
        self.use_fake(fake)
        out = g.mint_bearer("me@gmail.com", "aas_et/x", "0123456789abcdef")
        self.assertEqual(out["Expiry"], "9999999999")

    def test_raises_on_error_response(self):
        fake = _FakeGpsoauth(auth_request_result={"Error": "UNREGISTERED_ON_API_CONSOLE"})
        self.use_fake(fake)
        with self.assertRaises(RuntimeError) as ctx:
            g.mint_bearer("me@gmail.com", "aas_et/x", "0123456789abcdef")
        self.assertIn("UNREGISTERED_ON_API_CONSOLE", str(ctx.exception))


class ExchangeOAuthTokenTests(_PatchMixin, unittest.TestCase):
    def test_returns_master_token(self):
        fake = _FakeGpsoauth(exchange_token_result={"Token": "aas_et/master"})
        self.use_fake(fake)
        mt = g.exchange_oauth_token("me@gmail.com", "oauth2_4/abc", "0123456789abcdef")
        self.assertEqual(mt, "aas_et/master")
        args, _ = fake.exchange_token_calls[0]
        self.assertEqual(args[0], "me@gmail.com")
        self.assertEqual(args[1], "oauth2_4/abc")
        self.assertEqual(args[2], "0123456789abcdef")

    def test_raises_on_error(self):
        fake = _FakeGpsoauth(exchange_token_result={"Error": "BadAuthentication"})
        self.use_fake(fake)
        with self.assertRaises(RuntimeError) as ctx:
            g.exchange_oauth_token("me@gmail.com", "oauth2_4/abc", "0123456789abcdef")
        self.assertIn("BadAuthentication", str(ctx.exception))


class AttachTests(_PatchMixin, unittest.TestCase):
    def test_installs_getter_and_primes_cache(self):
        fake = _FakeGpsoauth(auth_request_result={"Auth": "ya29.PRIMED"})
        self.use_fake(fake)
        client = _FakeClient()
        g.attach_gpsoauth_auth(client, "me@gmail.com", "aas_et/x", "0123456789abcdef")
        self.assertEqual(client.api.auth_response_cache["Auth"], "ya29.PRIMED")
        result = client.api._get_auth_token()
        self.assertEqual(result["Auth"], "ya29.PRIMED")
        self.assertIn("Expiry", result)


class ForceRefreshTests(unittest.TestCase):
    def test_reinstalls_fresh_bearer(self):
        client = _FakeClient()
        client.api._get_auth_token = lambda: {"Auth": "ya29.NEW", "Expiry": "9999999999"}
        token = g.force_refresh(client)
        self.assertEqual(token, "ya29.NEW")
        self.assertEqual(client.api.auth_response_cache["Auth"], "ya29.NEW")


class ResolveStorePathTests(unittest.TestCase):
    def test_explicit_store_wins(self):
        self.assertEqual(
            g.resolve_store_path(store="/tmp/x.json", email="a@b.com"), Path("/tmp/x.json")
        )

    def test_email_gives_default_path(self):
        self.assertEqual(g.resolve_store_path(email="a@b.com"), g.default_store_path("a@b.com"))

    def test_discovers_single_credentials_under_base(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            cred = base / "a@b.com" / "gpsoauth.json"
            g.save_credentials(cred, email="a@b.com", master_token="aas_et/x", android_id="0123456789abcdef")
            self.assertEqual(g.resolve_store_path(base=base), cred)

    def test_no_credentials_raises_filenotfound(self):
        with TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                g.resolve_store_path(base=Path(d))

    def test_multiple_credentials_raises(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            for email in ("a@b.com", "c@d.com"):
                g.save_credentials(
                    base / email / "gpsoauth.json", email=email, master_token="t", android_id="0123456789abcdef"
                )
            with self.assertRaises(RuntimeError):
                g.resolve_store_path(base=base)


class ClientSigTests(unittest.TestCase):
    def test_default_is_google_photos_cert_not_gmscore(self):
        # 24bb24c... is Google Photos' own signing cert (required for photos.native).
        # 38918a...5788 is the GmsCore/GoogleLoginService cert (only good for the
        # master-token exchange) — using it for photos.native yields
        # UNREGISTERED_ON_API_CONSOLE.
        self.assertEqual(g.PHOTOS_CLIENT_SIG, "24bb24c05e47e0aefa68a58a766179d9b613a600")
        self.assertNotEqual(g.PHOTOS_CLIENT_SIG, "38918a453d07199354f8b19af05ec6562ced5788")

    def test_client_sig_is_stored_and_loaded(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "c.json"
            g.save_credentials(
                path, email="me@gmail.com", master_token="aas_et/x",
                android_id="0123456789abcdef", client_sig="deadbeef",
            )
            self.assertEqual(g.load_credentials(path)["client_sig"], "deadbeef")


class LoadGpsoauthErrorTests(unittest.TestCase):
    def test_import_failure_with_submodule_present_is_clear(self):
        # When the vendored package exists but importing it fails (its deps are
        # missing), the error must NOT tell the user to initialize the submodule —
        # it must point at the missing runtime deps instead.
        if not (g._GPSOAUTH_SUBMODULE / "gpsoauth" / "__init__.py").is_file():
            self.skipTest("gpsoauth submodule not initialized")
        original = sys.modules.get("gpsoauth", "absent")
        sys.modules["gpsoauth"] = None  # forces ImportError on `import gpsoauth`
        try:
            with self.assertRaises(RuntimeError) as ctx:
                g._load_gpsoauth()
        finally:
            if original == "absent":
                sys.modules.pop("gpsoauth", None)
            else:
                sys.modules["gpsoauth"] = original
        msg = str(ctx.exception)
        self.assertIn("failed to import", msg)
        self.assertIn("pycryptodomex", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
