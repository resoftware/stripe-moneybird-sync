import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_sync as fixtures

from stripe_moneybird.core import Ledger, Rejected, SyncError
from stripe_moneybird.worker import Worker

ARRIVAL = 1767654000  # 2026-01-06 in Europe/Amsterdam


class PayoutStripe(fixtures.FakeStripe):
    """One €121 invoice paid by card, paid out net of a €2.07 card fee and a €0.10 billing fee."""

    def __init__(self):
        super().__init__()
        self.allocations = [
            {
                "id": "inpay_test",
                "invoice": "in_test",
                "amount_paid": 12100,
                "payment": {"type": "payment_intent", "payment_intent": "pi_test"},
                "status_transitions": {"paid_at": 1767222000},
            }
        ]
        self.payouts = [
            {"id": "po_test", "currency": "eur", "amount": 11883, "arrival_date": ARRIVAL, "status": "paid"}
        ]
        self.transactions = [
            {"type": "payout", "currency": "eur", "amount": -11883, "fee": 0, "net": -11883},
            {
                "type": "charge",
                "reporting_category": "charge",
                "currency": "eur",
                "amount": 12100,
                "fee": 207,
                "net": 11893,
                "source": "ch_test",
            },
            {
                "type": "stripe_fee",
                "reporting_category": "fee",
                "currency": "eur",
                "amount": -10,
                "fee": 0,
                "net": -10,
            },
        ]

    def get(self, path, params=None):
        if path.startswith("/v1/payment_intents/"):
            return {"status": "succeeded", "currency": "eur", "amount_received": 12100}
        if path.startswith("/v1/charges/"):
            return {"id": "ch_test", "payment_intent": "pi_test"}
        return super().get(path, params)

    def list(self, path, params=None):
        if path == "/v1/payouts":
            self.params.append((path, params))
            return iter(copy.deepcopy(self.payouts))
        if path == "/v1/balance_transactions":
            return iter(copy.deepcopy(self.transactions))
        return super().list(path, params)


class PayoutMoneybird(fixtures.FakeMoneybird):
    def __init__(self):
        super().__init__()
        self.financial_accounts.append({"id": "20", "currency": "EUR", "active": True, "type": "BankAccount"})
        self.ledger_accounts += [
            {"id": "21", "account_type": "current_assets"},
            {"id": "22", "account_type": "expenses"},
        ]
        self.mutations = {"30": self.mutation("30", "118.83")}
        self.journals = {}
        self.scopes = True
        self.contacts["6"] = {"id": "6", "company_name": "Example", "customer_id": "K1", "archived": False}

    @staticmethod
    def mutation(identifier, amount, name="STRIPE", date="2026-01-06"):
        return {
            "id": identifier,
            "amount": amount,
            "date": date,
            "contra_account_name": name,
            "state": "unprocessed",
            "ledger_account_bookings": [],
        }

    def get(self, path, params=None):
        parts = path.strip("/").split("/")
        if parts[0] in ("financial_mutations", "documents") and not self.scopes:
            raise Rejected("remote_http_403")
        if path == "/financial_mutations":
            terms = dict(term.split(":", 1) for term in params["filter"].split(","))
            start, end = terms["period"].split("..")
            return copy.deepcopy(
                [item for item in self.mutations.values() if start <= item["date"].replace("-", "") <= end]
            )
        if parts[0] == "financial_mutations" and len(parts) == 2:
            if parts[1] in self.mutations:
                return copy.deepcopy(self.mutations[parts[1]])
            raise Rejected("remote_http_404")
        if path == "/documents/general_journal_documents":
            terms = dict(term.split(":", 1) for term in params["filter"].split(","))
            return copy.deepcopy(
                [
                    item
                    for item in self.journals.values()
                    if "reference" not in terms or terms["reference"] in item["reference"]
                ]
            )
        if parts[:2] == ["documents", "general_journal_documents"] and len(parts) == 3:
            if parts[2] in self.journals:
                return copy.deepcopy(self.journals[parts[2]])
            raise Rejected("remote_http_404")
        return super().get(path, params)

    def post(self, path, payload):
        if path == "/documents/general_journal_documents":
            self.actions.append(("POST", path))
            identifier = self.next_id()
            body = payload["general_journal_document"]
            self.journals[identifier] = {
                "id": identifier,
                "reference": body["reference"],
                "date": body["date"],
                "general_journal_document_entries": [
                    dict(entry)
                    for _, entry in sorted(body["general_journal_document_entries_attributes"].items())
                ],
            }
            return copy.deepcopy(self.journals[identifier])
        return super().post(path, payload)

    def patch(self, path, payload=None):
        parts = path.strip("/").split("/")
        if parts[0] == "financial_mutations" and parts[-1] == "link_booking":
            self.actions.append(("PATCH", path))
            record = self.mutations[parts[1]]
            record["state"] = "processed"
            record["ledger_account_bookings"].append(
                {
                    "ledger_account_id": payload["booking_id"],
                    "price": payload["price_base"],
                    "description": payload["description"],
                }
            )
            return copy.deepcopy(record)
        return super().patch(path, payload)


class PayoutTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("sys.stdout", new=io.StringIO()))
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.ledger.close)
        self.settings = fixtures.config()
        self.settings.update(
            sync_payments=True,
            receipt_ownership_confirmed=True,
            payment_financial_account_id="7",
            sync_payouts=True,
            payout_start_date="2026-01-05",
            payout_bank_account_id="20",
            payout_clearing_ledger_account_id="21",
            payout_fee_ledger_account_id="22",
        )
        self.stripe, self.moneybird = PayoutStripe(), PayoutMoneybird()

    def run_worker(self, apply=True):
        return Worker(self.settings, self.stripe, self.moneybird, self.ledger, apply).run()

    def test_payout_books_the_bank_line_to_clearing_and_moves_fees_out(self):
        summary = self.run_worker()
        self.assertEqual((summary["payouts_settled"], summary.get("failed", 0)), (1, 0))
        line = self.moneybird.mutations["30"]
        self.assertEqual(line["state"], "processed")
        self.assertEqual(
            line["ledger_account_bookings"],
            [{"ledger_account_id": "21", "price": "118.83", "description": "Stripe payout po_test (EX-1)"}],
        )
        (journal,) = self.moneybird.journals.values()
        self.assertEqual(
            (journal["reference"], journal["date"]), ("Stripe fees payout po_test", "2026-01-06")
        )
        self.assertEqual(
            [
                (e["ledger_account_id"], e["debit"], e["credit"])
                for e in journal["general_journal_document_entries"]
            ],
            [("22", "2.17", "0.00"), ("21", "0.00", "2.17")],
        )
        self.assertIn("other Stripe fees 0.10", journal["general_journal_document_entries"][0]["description"])
        _, params = next(item for item in self.stripe.params if item[0] == "/v1/payouts")
        self.assertEqual(params["arrival_date[gte]"], 1767567600)

    def test_replay_does_not_book_twice(self):
        self.run_worker()
        actions = len(self.moneybird.actions)
        self.assertEqual(self.run_worker()["payouts_settled"], 1)
        self.assertEqual(len(self.moneybird.actions), actions)
        self.assertEqual(len(self.moneybird.journals), 1)

    def test_missing_bank_line_waits_without_failing_or_writing(self):
        self.moneybird.mutations = {}
        summary = self.run_worker()
        self.assertEqual((summary["payouts_pending"], summary.get("failed", 0)), (1, 0))
        self.assertEqual(self.moneybird.journals, {})

    def test_bank_line_must_be_unique_and_from_stripe(self):
        self.moneybird.mutations["31"] = self.moneybird.mutation("31", "118.83", date="2026-01-07")
        self.assertEqual(self.run_worker()["failed"], 1)
        self.assertEqual(self.moneybird.journals, {})
        self.moneybird.mutations = {"31": self.moneybird.mutation("31", "118.83", name="Someone else")}
        self.assertEqual(self.run_worker()["payouts_pending"], 1)

    def test_unbooked_receipt_blocks_the_payout(self):
        self.settings.update(sync_payments=False, sync_payouts=False)
        self.run_worker()  # Imports the invoice without its payment.
        self.settings.update(sync_payments=True, sync_payouts=True)
        self.stripe.invoices[0]["status"] = "draft"  # Skip the invoice so its receipt stays missing.
        summary = self.run_worker()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(self.moneybird.mutations["30"]["state"], "unprocessed")

    def test_refunds_disputes_and_mismatches_stay_manual(self):
        self.stripe.transactions.append(
            {
                "type": "refund",
                "reporting_category": "refund",
                "currency": "eur",
                "amount": -100,
                "fee": 0,
                "net": -100,
            }
        )
        self.assertEqual(self.run_worker()["failed"], 1)
        self.stripe.transactions.pop()
        self.stripe.payouts[0]["amount"] += 1
        self.assertEqual(self.run_worker()["failed"], 1)
        self.assertEqual(self.moneybird.mutations["30"]["state"], "unprocessed")

    def test_dry_run_previews_without_writes(self):
        self.run_worker()  # Book the invoice and its receipt first.
        self.moneybird.mutations["30"] = self.moneybird.mutation("30", "118.83")
        self.moneybird.journals = {}
        self.ledger.db.execute("DELETE FROM operations WHERE reference LIKE '%:payout%'")
        actions = len(self.moneybird.actions)
        self.assertEqual(self.run_worker(apply=False)["payouts_previewed"], 1)
        self.assertEqual(len(self.moneybird.actions), actions)

    def test_preflight_needs_bank_and_documents_scope(self):
        self.moneybird.scopes = False
        with self.assertRaisesRegex(SyncError, "moneybird_token_needs_bank_and_documents_scope"):
            self.run_worker()

    def test_configuration_requires_payment_import_and_accounts(self):
        from stripe_moneybird.core import load_config

        settings = {**self.settings, "stripe_account_id": "acct_test", "administration_id": "1"}
        path = Path(self.temp.name) / "config.json"
        for change, code in (
            ({"sync_payments": False}, "sync_payouts_requires_sync_payments"),
            ({"payout_fee_ledger_account_id": None}, "configure_payout_fee_ledger_account_id"),
        ):
            path.write_text(__import__("json").dumps({**settings, **change}))
            with self.assertRaisesRegex(SyncError, code):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
