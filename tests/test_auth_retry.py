"""Unit tests for requota_migration's generalized auth-retry wiring (offline).

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


class CallWithAuthRetryTests(unittest.TestCase):
    def test_refreshes_then_succeeds_on_auth_error(self):
        state = {"fn": 0, "refresh": 0}

        def fn():
            state["fn"] += 1
            if state["fn"] == 1:
                raise _AuthError(401)
            return "ok"

        def on_auth_error(reason):
            state["refresh"] += 1

        out = rm.call_with_auth_retry(fn, on_auth_error=on_auth_error, label="test")
        self.assertEqual(out, "ok")
        self.assertEqual(state["fn"], 2)
        self.assertEqual(state["refresh"], 1)

    def test_no_refresher_propagates_auth_error(self):
        def fn():
            raise _AuthError(403)

        with self.assertRaises(_AuthError):
            rm.call_with_auth_retry(fn, on_auth_error=None, label="test")

    def test_non_auth_error_propagates_even_with_refresher(self):
        def fn():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            rm.call_with_auth_retry(fn, on_auth_error=lambda reason: None, label="test")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
