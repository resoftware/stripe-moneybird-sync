from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path

from .core import Http, Ledger, Moneybird, Stripe, SyncError, load_config, log, require, state_lock
from .worker import Worker


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Weekly Stripe to Moneybird invoice sync")
    parser.add_argument("command", choices=["sync", "backfill", "status", "backup", "reconcile", "resolve"])
    parser.add_argument("--config", default=os.getenv("SYNC_CONFIG", "/config/config.json"))
    parser.add_argument("--state", default=os.getenv("SYNC_STATE", "/state/ledger.sqlite3"))
    parser.add_argument("--mode", choices=["dry-run", "apply"], default=os.getenv("SYNC_MODE", "dry-run"))
    parser.add_argument("--from-date", help="Optional narrower backfill boundary; never before start_date")
    parser.add_argument("--output", help="SQLite backup destination (backup command)")
    parser.add_argument("--reference", help="Operation reference shown by status (resolve command)")
    parser.add_argument("--reason", help="Operator audit reason (resolve command)")
    parser.add_argument(
        "--confirm-request-finished-without-effect",
        action="store_true",
        help="Attest that Moneybird request completion and absence of accounting effects were verified",
    )
    args = parser.parse_args(argv)
    try:
        with state_lock(args.state), contextlib.ExitStack() as stack:
            if args.command == "backup":
                require(args.output is not None, "backup_output_required")
                require(Path(args.state).is_file(), "no_ledger_to_back_up")
                require(
                    Path(args.output).resolve() != Path(args.state).resolve(),
                    "backup_must_use_different_path",
                )
                source = stack.enter_context(
                    contextlib.closing(
                        sqlite3.connect(f"file:{Path(args.state).resolve()}?mode=ro", uri=True)
                    )
                )
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                target = stack.enter_context(contextlib.closing(sqlite3.connect(args.output)))
                source.backup(target)
                require(
                    target.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "backup_integrity_failed"
                )
                log("backup_complete")
                return 0
            if args.command in ("status", "resolve"):
                require(Path(args.state).is_file(), "ledger_does_not_exist")
            ledger = Ledger(args.state)
            stack.callback(ledger.close)
            if args.command == "resolve":
                require(
                    args.confirm_request_finished_without_effect and args.reference and args.reason,
                    "explicit_no_effect_confirmation_reference_and_reason_required",
                )
                ledger.permit_retry(args.reference, args.reason)
                log("operator_permitted_retry", reference=args.reference)
                return 0
            if args.command == "status":
                rows = ledger.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 10").fetchall()
                log("run_history", runs=[dict(row) for row in rows])
                rows = ledger.db.execute(
                    "SELECT reference, status, target_id FROM operations WHERE status != 'succeeded'"
                ).fetchall()
                log("pending_operations", operations=[dict(row) for row in rows])
                for kind in ("document", "contact"):
                    rows = ledger.db.execute(
                        f"SELECT reference, target_id, linked_at FROM {kind}_links ORDER BY reference"
                    ).fetchall()
                    log(kind + "_links", links=[dict(row) for row in rows])
                return 0
            config = load_config(args.config)
            key, token = os.getenv("STRIPE_API_KEY"), os.getenv("MONEYBIRD_API_TOKEN")
            require(bool(key) and bool(token), "api_credentials_required")
            http = Http()
            moneybird = Moneybird(config["administration_id"], token, http)
            stripe = Stripe(key, config["stripe_account_id"], config["livemode"], http)
            scope = {
                "stripe_account_id": config["stripe_account_id"],
                "livemode": config["livemode"],
                "administration_id": config["administration_id"],
            }
            ledger.bind(scope)
            worker = Worker(config, stripe, moneybird, ledger, apply=args.mode == "apply")
            with ledger.db:
                run_id = ledger.db.execute(
                    "INSERT INTO runs (started_at, mode, status) VALUES (?, ?, 'running')",
                    (int(time.time()), args.mode),
                ).lastrowid
            summary, status = {}, "failed"
            try:
                if args.command == "reconcile":
                    worker.reconcile()
                else:
                    summary = worker.run(args.from_date)
                status = "failed" if summary.get("failed") or summary.get("review") else "succeeded"
            finally:
                with ledger.db:
                    ledger.db.execute(
                        "UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?",
                        (int(time.time()), status, json.dumps(summary), run_id),
                    )
            log("run_complete", mode=args.mode, status=status, **summary)
            return 0 if status == "succeeded" else 1
    except SyncError as error:
        log("run_failed", code=str(error))
        return 1
    except (KeyError, TypeError, ValueError, ArithmeticError, sqlite3.Error, OSError):
        # Malformed payloads and configuration must not leak PII or credentials through tracebacks.
        log("run_failed", code="invalid_configuration_payload_or_state")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
