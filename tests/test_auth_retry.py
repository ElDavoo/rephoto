"""Unit tests for requota_migration's retry wiring: auth refresh + transient failures (offline).

Run directly:  python3 tests/test_auth_retry.py -v
"""
from __future__ import annotations

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requota_migration as rm


class _Resp:
    def __init__(self, status):
        self.status_code = status


class _AuthError(Exception):
    """Looks like a gpmc/requests HTTP error carrying a 401/403 response."""

    def __init__(self, status=401):
        super().__init__(f"HTTP {status}")
        self.response = _Resp(status)


class _HttpError(Exception):
    """Looks like a requests HTTPError carrying an arbitrary status."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = _Resp(status)


class CallWithRetryTests(unittest.TestCase):
    def test_refreshes_then_succeeds_on_auth_error(self):
        state = {"fn": 0, "refresh": 0}

        def fn():
            state["fn"] += 1
            if state["fn"] == 1:
                raise _AuthError(401)
            return "ok"

        def on_auth_error(reason):
            state["refresh"] += 1

        out = rm.call_with_retry(fn, on_auth_error=on_auth_error, label="test")
        self.assertEqual(out, "ok")
        self.assertEqual(state["fn"], 2)
        self.assertEqual(state["refresh"], 1)

    def test_no_refresher_propagates_auth_error(self):
        def fn():
            raise _AuthError(403)

        with self.assertRaises(_AuthError):
            rm.call_with_retry(fn, on_auth_error=None, label="test")

    def test_non_auth_error_propagates_even_with_refresher(self):
        def fn():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            rm.call_with_retry(fn, on_auth_error=lambda reason: None, label="test")


class BuildAuthRefresherTests(unittest.TestCase):
    def test_static_mode_returns_none(self):
        ns = types.SimpleNamespace(adb_token=False, gpsoauth=False)
        self.assertIsNone(rm.build_auth_refresher(client=None, args=ns))

    def test_gpsoauth_mode_returns_callable(self):
        ns = types.SimpleNamespace(adb_token=False, gpsoauth=True)
        refresher = rm.build_auth_refresher(client=None, args=ns)
        self.assertTrue(callable(refresher))

    def test_adb_mode_returns_callable(self):
        ns = types.SimpleNamespace(adb_token=True, gpsoauth=False)
        refresher = rm.build_auth_refresher(client=None, args=ns)
        self.assertTrue(callable(refresher))


class TransientRetryTests(unittest.TestCase):
    def setUp(self):
        # Keep the suite instant: no real backoff sleeps.
        self._sleep = rm.time.sleep
        rm.time.sleep = lambda seconds: None

    def tearDown(self):
        rm.time.sleep = self._sleep

    def test_retries_server_error_then_succeeds(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise _HttpError(500)
            return "ok"

        self.assertEqual(rm.call_with_retry(fn, label="test", retries=5, backoff=0), "ok")
        self.assertEqual(len(calls), 3)

    def test_gives_up_after_retries(self):
        calls = []

        def fn():
            calls.append(1)
            raise _HttpError(503)

        with self.assertRaises(_HttpError):
            rm.call_with_retry(fn, label="test", retries=2, backoff=0)
        self.assertEqual(len(calls), 3)

    def test_client_error_is_not_retried(self):
        calls = []

        def fn():
            calls.append(1)
            raise _HttpError(404)

        with self.assertRaises(_HttpError):
            rm.call_with_retry(fn, label="test", retries=3, backoff=0)
        self.assertEqual(len(calls), 1)

    def test_connection_error_is_retried(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionResetError("Connection reset by peer")
            return "ok"

        self.assertEqual(rm.call_with_retry(fn, label="test", retries=3, backoff=0), "ok")
        self.assertEqual(len(calls), 2)

    def test_auth_refresh_and_transient_retry_share_one_call(self):
        calls = []
        refreshes = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise _AuthError(401)
            if len(calls) == 2:
                raise _HttpError(500)
            return "ok"

        out = rm.call_with_retry(
            fn, on_auth_error=refreshes.append, label="test", retries=1, backoff=0
        )
        self.assertEqual(out, "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(refreshes), 1)


class IsTransientErrorTests(unittest.TestCase):
    def test_statuses(self):
        for status in (408, 429, 500, 502, 503, 504):
            self.assertTrue(rm.is_transient_error(_HttpError(status)), status)
        for status in (400, 401, 403, 404, 410):
            self.assertFalse(rm.is_transient_error(_HttpError(status)), status)

    def test_os_errors_are_transient(self):
        self.assertTrue(rm.is_transient_error(TimeoutError("read timed out")))
        self.assertTrue(rm.is_transient_error(OSError("connection aborted")))

    def test_plain_errors_are_not_transient(self):
        self.assertFalse(rm.is_transient_error(ValueError("boom")))
        self.assertFalse(rm.is_transient_error(RuntimeError("No downloadable URL found in API response")))


class RetryDelayTests(unittest.TestCase):
    def test_grows_and_stays_capped(self):
        first = rm.retry_delay(1, 3.0)
        self.assertGreaterEqual(first, 1.5)
        self.assertLessEqual(first, 3.0)
        self.assertLessEqual(rm.retry_delay(20, 3.0), rm.NETWORK_BACKOFF_CAP_SECONDS)

    def test_zero_backoff_never_sleeps(self):
        self.assertEqual(rm.retry_delay(4, 0), 0)


class RetrySettingsTests(unittest.TestCase):
    def test_reads_cli_values(self):
        ns = types.SimpleNamespace(network_retries=9, network_backoff_seconds=1.5)
        self.assertEqual(rm.retry_settings(ns), {"retries": 9, "backoff": 1.5})

    def test_defaults_when_absent(self):
        settings = rm.retry_settings(types.SimpleNamespace())
        self.assertEqual(settings["retries"], rm.NETWORK_RETRIES)
        self.assertEqual(settings["backoff"], rm.NETWORK_BACKOFF_SECONDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
