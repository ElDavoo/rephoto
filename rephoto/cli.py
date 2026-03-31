from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from rephoto.browser import (
    BrowserAutomationError,
    PhotosBrowserSession,
)
from rephoto.config import DEFAULT_CONFIG_PATH, load_config, write_default_config
from rephoto.pipeline import RephotoPipeline, RunMode
from rephoto.preflight import run_preflight


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rephoto",
        description="Download selected Google Photos media, remove remote copies, then push to phone via ADB.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_config = subparsers.add_parser("init-config", help="Write a starter JSON config")
    init_config.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Config file to create",
    )

    doctor = subparsers.add_parser("doctor", help="Run environment and dependency checks")
    doctor.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    doctor.add_argument(
        "--mode",
        choices=[mode.value for mode in RunMode],
        default=RunMode.DRY_RUN.value,
        help="Mode used to decide which checks are mandatory",
    )

    login = subparsers.add_parser(
        "login",
        help="Open persistent browser profile and verify Google Photos authentication",
    )
    login.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))

    run = subparsers.add_parser("run", help="Execute one pipeline run")
    run.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    run.add_argument(
        "--mode",
        choices=[
            RunMode.DRY_RUN.value,
            RunMode.DOWNLOAD_ONLY.value,
            RunMode.DOWNLOAD_DELETE.value,
            RunMode.DOWNLOAD_DELETE_PUSH.value,
        ],
        default=RunMode.DRY_RUN.value,
    )
    run.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="Stop after this many batches (0 means unlimited)",
    )

    push_pending = subparsers.add_parser(
        "push-pending",
        help="Push previously downloaded manifest batches that are not fully transferred",
    )
    push_pending.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    push_pending.add_argument("--max-batches", type=int, default=0)

    return parser


def _mode_requires_adb(command: str, mode: RunMode) -> bool:
    if command == "push-pending":
        return True
    return mode in (RunMode.DOWNLOAD_DELETE_PUSH, RunMode.PUSH_ONLY)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init-config":
        config_path = write_default_config(Path(args.config))
        print(f"Wrote config template: {config_path}")
        return 0

    config_path = Path(args.config)
    try:
        config = load_config(config_path)
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    mode_name = getattr(
        args,
        "mode",
        RunMode.PUSH_ONLY.value
        if args.command == "push-pending"
        else RunMode.DRY_RUN.value,
    )
    mode = RunMode(mode_name)

    preflight = run_preflight(config, require_adb=_mode_requires_adb(args.command, mode))
    print(preflight.render())
    if not preflight.ok:
        return 2

    if args.command == "doctor":
        return 0

    if args.command == "login":
        try:
            with PhotosBrowserSession(config) as browser:
                try:
                    browser.open_manage_storage()
                    if browser.is_authenticated():
                        print("[OK] Google Photos is already authenticated in this profile.")
                        storage_state_path = browser.save_storage_state(
                            config.logs_root / "storage-state.json"
                        )
                        print(f"[OK] Session state saved: {storage_state_path}")
                        return 0
                except BrowserAutomationError as exc:
                    print(f"[WARN] Initial auth check failed: {exc}")

                browser.open_account_login_page()
                print("[INFO] Playwright browser session started with persistent profile.")
                print("[INFO] Complete Google login in the opened browser window.")

                input(
                    "After login completes, press Enter to verify session..."
                )

                browser.open_manage_storage()
                auth_error = browser.authentication_error()
                if auth_error:
                    raise BrowserAutomationError(auth_error)
                storage_state_path = browser.save_storage_state(
                    config.logs_root / "storage-state.json"
                )

            print(f"[OK] Google Photos authentication verified.")
            print(f"[OK] Session state saved: {storage_state_path}")
            return 0
        except BrowserAutomationError as exc:
            print(f"[ERROR] Login flow failed: {exc}", file=sys.stderr)
            return 2
        except EOFError:
            print(
                "[ERROR] Login flow requires interactive stdin. "
                "Run it in an interactive terminal.",
                file=sys.stderr,
            )
            return 2
        except KeyboardInterrupt:
            print("\n[WARN] Login flow interrupted by user.", file=sys.stderr)
            return 130

    pipeline = RephotoPipeline(config)
    try:
        if args.command == "push-pending":
            summary = pipeline.run(RunMode.PUSH_ONLY, max_batches=args.max_batches)
        else:
            summary = pipeline.run(mode, max_batches=args.max_batches)
    finally:
        pipeline.close()

    print(json.dumps(summary.to_json(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
