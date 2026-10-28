import copy
import io
import re
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from unittest.mock import patch

from stripe_moneybird.__main__ import main
from stripe_moneybird.core import (
    Http,
    Ledger,
    Moneybird,
    Rejected,
    Stripe,
    SyncError,
    UnknownOutcome,
    major,
    minor,
    state_lock,
)
from stripe_moneybird.planning import document_plan
from stripe_moneybird.worker import Worker


def config():
    return {
        "stripe_account_id": "acct_test",
        "livemode": False,
        "start_date": "2026-01-01",
        "timezone": "Europe/Amsterdam",
        "administration_id": "1",
        "revenue_ledger_account_id": "3",
        "currencies": {"EUR": 2},
        "tax_map": {"txr_vat": "5"},
        "no_tax": {"none": "9"},
        "contact_map": {"cus_test": "6"},
        "sync_pdfs": False,
    }


def invoice():
    return {
        "id": "in_test",
        "object": "invoice",
        "customer": "cus_test",
        "customer_name": "Example",
        "customer_address": {"line1": "Main 1", "postal_code": "1234 AB", "city": "Utrecht", "country": "NL"},
        "currency": "eur",
        "status": "paid",
        "number": "EX-1",
        "created": 1767222000,
        "status_transitions": {"finalized_at": 1767222000},
        "customer_tax_exempt": "none",
        "total": 12100,
        "total_excluding_tax": 10000,
        "amount_remaining": 0,
        "amount_paid": 12100,
        "amount_shipping": 0,
        "starting_balance": 0,
    }


def line():
    return {
        "id": "il_test",
        "currency": "eur",
        "description": "Service",
        "amount": 10000,
        "quantity": 1,
        "discount_amounts": [],
        "taxes": [
            {
                "amount": 2100,
                "tax_behavior": "exclusive",
                "type": "tax_rate_details",
                "tax_rate_details": {"tax_rate": "txr_vat"},
            }
        ],
    }


class FakeStripe:
    def __init__(self):
        self.invoices = [invoice()]
        self.lines = [line()]
        self.credits = []
        self.allocations = []
        self.params = []

    def check(self):
        pass

    def get(self, path, params=None):
        if path.startswith("/v1/tax_rates/"):
            return {"id": path.rsplit("/", 1)[1], "country": "NL", "percentage": 21.0}
        return copy.deepcopy(next(item for item in self.invoices if path.endswith("/" + item["id"])))

    def list(self, path, params=None):
        self.params.append((path, params))
        if path == "/v1/invoices":
            return iter(copy.deepcopy(self.invoices))
        if path.endswith("/lines"):
            return iter(copy.deepcopy(self.lines))
        if path == "/v1/credit_notes":
            return iter(copy.deepcopy(self.credits))
        if path == "/v1/invoice_payments":
            return iter(copy.deepcopy(self.allocations))
        return iter([])


RATES = {"5": Decimal(21), "9": Decimal(0)}


def money(value):
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class FakeMoneybird:
    """In-memory Moneybird: contacts and external sales invoices with Moneybird-style totals."""

    def __init__(self):
        self.contacts, self.invoices = {}, {}
        self.creates = 0
        self.actions = []
        self.lose_response = False
        self.lose_upload_response = False
        self.lose_payment_response = False
        self.amount_delta = Decimal(0)
        self.ledger_accounts = [
            {
                "id": "3",
                "account_type": "revenue",
                "active": True,
                "allowed_document_types": ["sales_invoice"],
            }
        ]
        self.tax_rates = [
            {"id": "5", "percentage": "21.0", "tax_rate_type": "sales_invoice", "active": True},
            {"id": "9", "percentage": "0.0", "tax_rate_type": "sales_invoice", "active": True},
        ]
        self.financial_accounts = [{"id": "7", "currency": "EUR", "active": True, "type": "PspAccount"}]

    def administration(self):
        return {"id": "1", "currency": "EUR", "country": "NL", "suspended": False}

    def next_id(self):
        self.creates += 1
        return str(self.creates + 10)

    def get(self, path, params=None):
        if path == "/ledger_accounts":
            return copy.deepcopy(self.ledger_accounts)
        if path == "/financial_accounts":
            return copy.deepcopy(self.financial_accounts)
        parts = path.strip("/").split("/")
        if parts[0] == "contacts" and len(parts) == 3 and parts[1] == "customer_id":
            found = [item for item in self.contacts.values() if item.get("customer_id") == parts[2]]
            if found:
                return copy.deepcopy(found[0])
            raise Rejected("remote_http_404")
        store = {"contacts": self.contacts, "external_sales_invoices": self.invoices}.get(parts[0])
        if store is not None and len(parts) == 2:
            if parts[1] in store:
                return copy.deepcopy(store[parts[1]])
            raise Rejected("remote_http_404")
        raise AssertionError(path)

    def find(self, path, params=None):
        try:
            return self.get(path, params)
        except Rejected as error:
            if str(error) == "remote_http_404":
                return None
            raise

    def list(self, path, params=None):
        params = params or {}
        if path == "/tax_rates":
            return iter(copy.deepcopy(self.tax_rates))
        if path == "/contacts":
            query = str(params.get("query", "")).casefold()
            fields = ("company_name", "firstname", "lastname", "email", "customer_id", "tax_number")
            return iter(
                copy.deepcopy(
                    [
                        item
                        for item in self.contacts.values()
                        if any(query in str(item.get(field) or "").casefold() for field in fields)
                        and (params.get("include_archived") == "true" or not item.get("archived"))
                    ]
                )
            )
        if path == "/external_sales_invoices":
            terms = dict(term.split(":", 1) for term in params["filter"].split(","))
            start, end = terms["period"].split("..")
            results = [
                item
                for item in self.invoices.values()
                if start <= item["date"].replace("-", "") <= end
                and ("contact_id" not in terms or str(item["contact_id"]) == terms["contact_id"])
            ]
            return iter(copy.deepcopy(results))
        raise AssertionError(path)

    def create_invoice(self, body):
        identifier = self.next_id()
        details, taxes, net = [], {}, Decimal(0)
        inclusive = bool(body.get("prices_are_incl_tax"))
        for index, detail in enumerate(body["details_attributes"]):
            subtotal = Decimal(detail["price"]) * Decimal(detail["amount"])
            if inclusive:
                subtotal = (subtotal * 100 / (100 + RATES[detail["tax_rate_id"]])).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            net += subtotal
            taxes[detail["tax_rate_id"]] = taxes.get(detail["tax_rate_id"], Decimal(0)) + subtotal
            details.append(
                {
                    "id": f"{identifier}0{index}",
                    "description": detail["description"],
                    "price": money(detail["price"]),
                    "amount_decimal": detail["amount"],
                    "tax_rate_id": detail["tax_rate_id"],
                    "ledger_account_id": detail["ledger_account_id"],
                    "period": detail.get("period"),
                    "total_price_excl_tax_with_discount": money(subtotal),
                }
            )
        tax = sum(
            (amount * RATES[rate] / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            for rate, amount in taxes.items()
        )
        total = net + tax + self.amount_delta
        if inclusive:
            total = sum(Decimal(d["price"]) * Decimal(d["amount"]) for d in body["details_attributes"])
            total += self.amount_delta
        record = {
            key: body.get(key)
            for key in ("contact_id", "reference", "date", "due_date", "currency", "source", "source_url")
        }
        record.update(
            id=identifier,
            state="open",
            prices_are_incl_tax=bool(body.get("prices_are_incl_tax")),
            details=details,
            total_price_excl_tax=money(net),
            total_price_incl_tax=money(total),
            total_paid="0.00",
            total_unpaid=money(total),
            payments=[],
            attachments=[],
            marked_uncollectible_on=None,
        )
        self.invoices[identifier] = record
        return copy.deepcopy(record)

    def post(self, path, payload):
        self.actions.append(("POST", path))
        parts = path.strip("/").split("/")
        if path == "/contacts":
            identifier = self.next_id()
            self.contacts[identifier] = {"id": identifier, "archived": False, **payload["contact"]}
            result = copy.deepcopy(self.contacts[identifier])
        elif path == "/external_sales_invoices":
            result = self.create_invoice(payload["external_sales_invoice"])
        elif parts[0] == "external_sales_invoices" and parts[-1] == "payments":
            record = self.invoices[parts[1]]
            payment = {"id": self.next_id(), **payload["payment"]}
            record["payments"].append(payment)
            paid = Decimal(record["total_paid"]) + Decimal(payment["price"])
            record["total_paid"], record["total_unpaid"] = (
                money(paid),
                money(Decimal(record["total_price_incl_tax"]) - paid),
            )
            if self.lose_payment_response:
                self.lose_payment_response = False
                raise TimeoutError()
            return copy.deepcopy(payment)
        else:
            raise AssertionError(path)
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("committed but response lost")
        return result

    def patch(self, path, payload=None):
        self.actions.append(("PATCH", path))
        parts = path.strip("/").split("/")
        assert parts[-1] == "mark_as_uncollectible", path
        self.invoices[parts[1]]["marked_uncollectible_on"] = "2026-02-01"
        return {}

    def delete(self, path):
        self.actions.append(("DELETE", path))
        parts = path.strip("/").split("/")
        del self.invoices[parts[1]]
        return None

    def upload(self, path, filename, data):
        self.actions.append(("UPLOAD", path))
        parts = path.strip("/").split("/")
        self.creates += 1
        self.invoices[parts[1]]["attachments"].append(
            {
                "id": str(self.creates + 10),
                "filename": filename,
                "content_type": "application/pdf",
                "size": len(data),
            }
        )
        if self.lose_upload_response:
            self.lose_upload_response = False
            raise TimeoutError()
        return None


class PlanningTests(unittest.TestCase):
    def plan(self, source=None, lines=None, settings=None, original=None):
        return document_plan(
            settings or config(), source or invoice(), invoice(), lines or [line()], "6", original
        )

    def test_tax_and_quantity_and_minor_currency(self):
        plan, expected = self.plan()
        detail = plan["body"]["details_attributes"][0]
        self.assertEqual((detail["price"], detail["amount"], detail["tax_rate_id"]), ("100.00", "1", "5"))
        self.assertEqual(
            expected,
            {
                "net": 10000,
                "total": 12100,
                "taxes": {"5": 2100},
                "details": [("Service", "3", "5", 10000)],
                "lines": [("3", "5", 10000)],
                "decimals": 2,
            },
        )
        self.assertEqual(plan["body"]["source_url"], "https://dashboard.stripe.com/test/invoices/in_test")
        self.assertEqual(minor("123", 0), 123)
        self.assertEqual(major(-1050, 2), "-10.50")
        self.assertEqual(major(5, 0), "5")

    def test_fractional_quantity_is_preserved_when_unit_price_is_exact(self):
        item = line()
        item["quantity_decimal"] = "2.5"
        plan, _ = self.plan(lines=[item])
        detail = plan["body"]["details_attributes"][0]
        self.assertEqual((detail["amount"], detail["price"]), ("2.5", "40.00"))

    def test_inexact_unit_price_collapses_to_one_line_with_exact_total(self):
        item = line()
        item["quantity"] = 3
        plan, expected = self.plan(lines=[item])
        detail = plan["body"]["details_attributes"][0]
        self.assertEqual(
            (detail["amount"], detail["price"], detail["description"]), ("1", "100.00", "3 × Service")
        )
        self.assertEqual(expected["lines"], [("3", "5", 10000)])

    def test_zero_quantity_metered_lines_are_dropped(self):
        empty = {
            **line(),
            "id": "il_tier",
            "description": "Email Overage",
            "amount": 0,
            "quantity": 0,
            "taxes": [],
        }
        plan, expected = self.plan(lines=[line(), empty])
        self.assertEqual(len(plan["body"]["details_attributes"]), 1)
        self.assertEqual(expected["total"], 12100)
        empty["amount"] = 100
        with self.assertRaisesRegex(SyncError, "zero_quantity_line_with_amount"):
            self.plan(lines=[line(), empty])

    def test_inclusive_tax_is_normalized_to_net(self):
        item = line()
        item["amount"] = 12100
        item["taxes"][0]["tax_behavior"] = "inclusive"
        self.assertEqual(self.plan(lines=[item])[1]["net"], 10000)

    def test_discount_not_applied_twice(self):
        item, source = line(), invoice()
        item["discount_amounts"] = [{"amount": 1000}]
        item["taxes"][0]["amount"] = 1890
        source.update(total=10890, total_excluding_tax=9000)
        self.assertEqual(self.plan(source, [item])[1]["net"], 9000)

    def test_unknown_tax_blocks_before_create(self):
        settings = config()
        settings["tax_map"] = {}
        with self.assertRaisesRegex(SyncError, "tax_mapping_required"):
            self.plan(settings=settings)

    def test_multiple_tax_rates_on_one_line_require_review(self):
        item = line()
        item["taxes"].append(copy.deepcopy(item["taxes"][0]))
        with self.assertRaisesRegex(SyncError, "multiple_tax_rates_per_line"):
            self.plan(lines=[item])

    def test_source_total_mismatch_blocks(self):
        source = invoice()
        source["total"] += 1
        with self.assertRaisesRegex(SyncError, "gross_total_mismatch"):
            self.plan(source)

    def test_credit_is_negative_and_names_the_original_invoice(self):
        source = invoice()
        source.update(id="cn_test", object="credit_note", status="issued", number="CN-1")
        plan, expected = self.plan(source, original="EX-1")
        detail = plan["body"]["details_attributes"][0]
        self.assertEqual(plan["kind"], "credit")
        self.assertEqual(detail["price"], "-100.00")
        self.assertEqual(detail["description"], "Credit note for invoice EX-1: Service")
        self.assertEqual(plan["body"]["source_url"], "https://dashboard.stripe.com/test/credit_notes/cn_test")
        self.assertEqual(
            (expected["net"], expected["total"], expected["taxes"]), (-10000, -12100, {"5": -2100})
        )
        with self.assertRaisesRegex(SyncError, "original_invoice_number_required"):
            self.plan(source)

    def test_exemption_needs_explicit_reporting_mapping(self):
        source = invoice()
        source.update(customer_tax_exempt="reverse", total=10000)
        item = line()
        item["taxes"] = []
        with self.assertRaisesRegex(SyncError, "no_tax_treatment_mapping_required"):
            document_plan(config(), source, source, [item], "6")
        settings = config()
        settings["no_tax"]["reverse"] = "9"
        plan, expected = document_plan(settings, source, source, [item], "6")
        self.assertEqual(plan["body"]["details_attributes"][0]["tax_rate_id"], "9")
        self.assertEqual(expected["taxes"], {})

    def test_zero_total_does_not_imply_payment(self):
        source, item = invoice(), line()
        source.update(total=0, total_excluding_tax=0)
        item.update(amount=0, taxes=[])
        self.assertEqual(self.plan(source, [item])[1]["total"], 0)

    def test_shipping_and_credit_grants_require_review(self):
        source = invoice()
        source["amount_shipping"] = 20
        with self.assertRaisesRegex(SyncError, "shipping"):
            self.plan(source)
        item = line()
        item["pretax_credit_amounts"] = [{"type": "credit_balance_transaction", "amount": 20}]
        with self.assertRaisesRegex(SyncError, "credit_grant"):
            self.plan(lines=[item])

    def test_line_period_is_opt_in_and_inclusive(self):
        item = line()
        item["parent"] = {"type": "subscription_item_details"}
        item["period"] = {"start": 1767222000, "end": 1769900400}  # 2026-01-01 .. 2026-02-01 00:00 CET
        detail = self.plan(lines=[item])[0]["body"]["details_attributes"][0]
        self.assertNotIn("period", detail)
        self.assertEqual(detail["description"], "Service [2026-01-01 to 2026-02-01]")
        settings = config()
        settings["line_periods"] = True
        detail = self.plan(lines=[item], settings=settings)[0]["body"]["details_attributes"][0]
        self.assertEqual(detail["period"], "20260101..20260131")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.ledger = Ledger(self.path)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.ledger.close)

    def test_committed_write_with_lost_response_recovers_without_duplicate(self):
        records, calls = {}, []

        def create():
            calls.append(1)
            records["id"] = "23"
            raise TimeoutError()

        with self.assertRaises(UnknownOutcome):
            self.ledger.ensure("ref", "hash", lambda: records.get("id"), create, lambda _: None)
        target = self.ledger.ensure("ref", "hash", lambda: records.get("id"), create, lambda _: None)
        self.assertEqual(target, "23")
        self.assertEqual(len(calls), 1)

    def test_missing_unknown_write_never_retries(self):
        self.ledger.put("ref", "hash", "submitted")
        with self.assertRaises(UnknownOutcome):
            self.ledger.ensure("ref", "hash", lambda: None, lambda: self.fail("unsafe retry"), lambda _: None)

    def test_definitive_rejection_can_retry(self):
        with self.assertRaises(Rejected):
            self.ledger.ensure(
                "ref",
                "hash",
                lambda: None,
                lambda: (_ for _ in ()).throw(Rejected("invalid")),
                lambda _: None,
            )
        self.assertEqual(self.ledger.ensure("ref", "hash", lambda: None, lambda: "9", lambda _: None), "9")

    def test_corrected_payload_can_retry_after_confirmed_rejection(self):
        self.ledger.put("ref", "invalid_payload", "rejected")
        self.assertEqual(
            self.ledger.ensure("ref", "corrected_payload", lambda: None, lambda: "9", lambda _: None), "9"
        )

    def test_manual_retry_requires_confirmation_and_retains_audit_reason(self):
        self.ledger.put("ref", "hash", "unknown")
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "resolve",
                        "--state",
                        str(self.path),
                        "--reference",
                        "ref",
                        "--reason",
                        "Checked Moneybird",
                    ]
                ),
                1,
            )
            self.assertEqual(
                main(
                    [
                        "resolve",
                        "--state",
                        str(self.path),
                        "--reference",
                        "ref",
                        "--reason",
                        "Checked Moneybird",
                        "--confirm-request-finished-without-effect",
                    ]
                ),
                0,
            )
        self.assertEqual(self.ledger.get("ref")["status"], "rejected")
        self.assertEqual(
            self.ledger.db.execute("SELECT reason FROM resolutions").fetchone()[0], "Checked Moneybird"
        )

    def test_changed_payload_and_different_scope_block(self):
        self.ledger.put("ref", "old", "succeeded", "4")
        with self.assertRaisesRegex(SyncError, "payload_changed"):
            self.ledger.ensure("ref", "new", lambda: "4", lambda: "5", lambda _: None)
        self.ledger.bind({"administration_id": "1"})
        with self.assertRaisesRegex(SyncError, "scope_mismatch"):
            self.ledger.bind({"administration_id": "2"})

    def test_process_lock_blocks_manual_overlap(self):
        with state_lock(self.path), self.assertRaisesRegex(SyncError, "holds_state_lock"):
            with state_lock(self.path):
                self.fail("second lock acquired")

    def test_sqlite_backup_and_status_without_credentials(self):
        self.ledger.put("ref", "hash", "unknown")
        backup = Path(self.temp.name) / "backup.sqlite3"
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(main(["backup", "--state", str(self.path), "--output", str(backup)]), 0)
            self.assertEqual(main(["status", "--state", str(self.path)]), 0)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("SELECT status FROM operations").fetchone()[0], "unknown")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("sys.stdout", new=io.StringIO()))
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.ledger.close)
        self.settings, self.stripe, self.moneybird = config(), FakeStripe(), FakeMoneybird()
        self.moneybird.contacts["6"] = {
            "id": "6",
            "company_name": "Example",
            "email": "billing@example.test",
            "tax_number": "NL123456789B01",
            "customer_id": "K1",
            "archived": False,
        }

    def worker(self, apply=True):
        return Worker(self.settings, self.stripe, self.moneybird, self.ledger, apply)

    def test_replay_and_later_payment_do_not_duplicate_invoice(self):
        self.assertEqual(self.worker().run()["verified"], 1)
        self.stripe.invoices[0]["amount_paid"] = 0
        self.worker().run()
        self.assertEqual(self.moneybird.creates, 1)
        self.assertEqual(self.stripe.params[0][1]["created[gte]"], 1767222000)
        document = self.moneybird.invoices["11"]
        self.assertEqual(
            (document["reference"], document["source"], document["contact_id"]), ("EX-1", "Stripe", "6")
        )
        self.assertEqual(document["total_price_incl_tax"], "121.00")

    def test_customer_creation_is_stable_and_snapshot_changes_are_blocked(self):
        self.settings["contact_map"] = {}
        self.moneybird.contacts.clear()
        self.worker().run()
        self.worker().run()
        self.assertEqual(self.moneybird.creates, 2)
        contact = self.moneybird.contacts["11"]
        self.assertEqual(
            (contact["customer_id"], contact["country"], contact["zipcode"]), ("cus_test", "NL", "1234 AB")
        )
        self.stripe.invoices[0]["customer_name"] = "Changed customer"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 2)

    def test_nameless_customer_uses_its_mailbox_as_contact_name_and_is_resolved_once(self):
        self.settings["contact_map"] = {}
        self.moneybird.contacts.clear()
        self.stripe.invoices[0].update(customer_name=None, customer_email="billing@example.test")
        self.stripe.invoices.append({**self.stripe.invoices[0], "id": "in_other", "number": "EX-2"})
        self.assertEqual(self.worker().run()["verified"], 2)
        self.assertEqual(self.moneybird.creates, 3)  # One contact, two invoices.
        self.assertEqual(self.moneybird.contacts["11"]["company_name"], "billing@example.test")

    def test_zero_total_invoices_can_be_skipped(self):
        self.stripe.invoices[0].update(total=0, total_excluding_tax=0, amount_paid=0)
        self.stripe.lines[0].update(amount=0, taxes=[])
        self.assertEqual(self.worker().run()["verified"], 1)
        self.settings["skip_zero_total"] = True
        self.stripe.invoices[0]["id"] = "in_free"
        self.assertEqual(self.worker().run()["skipped"], 1)
        self.assertEqual(self.moneybird.creates, 1)

    def test_customer_without_country_needs_a_default(self):
        self.settings["contact_map"] = {}
        self.moneybird.contacts.clear()
        del self.stripe.invoices[0]["customer_address"]
        self.assertEqual(self.worker().run()["failed"], 1)
        self.settings["default_contact_country"] = "NL"
        self.assertEqual(self.worker().run()["verified"], 1)

    def test_dry_run_has_no_remote_writes_or_operation_intents(self):
        self.worker(False).run()
        self.assertEqual(self.moneybird.creates, 0)
        self.assertEqual(self.moneybird.actions, [])
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM operations").fetchone()[0], 0)

    def test_loss_after_commit_is_recovered_on_next_run(self):
        self.moneybird.lose_response = True
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.worker().run()["verified"], 1)
        self.assertEqual(self.moneybird.creates, 1)

    def test_amount_mismatch_never_recreates(self):
        self.moneybird.amount_delta = Decimal("0.01")
        self.assertEqual(self.worker().run()["failed"], 1)
        self.worker().run()
        self.assertEqual(self.moneybird.creates, 1)
        self.assertEqual(len(self.moneybird.actions), 1)

    def test_void_requires_review_unless_deletion_is_enabled(self):
        self.worker().run()
        self.stripe.invoices[0]["status"] = "void"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertIn("11", self.moneybird.invoices)
        self.settings["void_action"] = "delete"
        self.assertEqual(self.worker(False).run()["previewed"], 1)
        self.assertIn("11", self.moneybird.invoices)
        self.assertEqual(self.worker().run()["deleted"], 1)
        self.assertNotIn("11", self.moneybird.invoices)
        self.assertEqual(self.worker().run()["skipped"], 1)

    def test_void_never_deletes_a_document_with_payments(self):
        self.settings["void_action"] = "delete"
        self.worker().run()
        self.moneybird.invoices["11"]["payments"].append({"id": "90", "price": "121.00"})
        self.stripe.invoices[0]["status"] = "void"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertIn("11", self.moneybird.invoices)

    def test_paid_credit_only_does_not_generate_cash(self):
        source = invoice()
        source.update(
            id="cn_test", object="credit_note", status="issued", number="CN-1", pre_payment_amount=12100
        )
        self.stripe.credits = [source]
        result = self.worker().run()
        self.assertEqual(self.moneybird.creates, 2)
        self.assertEqual(result["review"], 1)
        self.assertEqual(self.moneybird.invoices["12"]["total_price_incl_tax"], "-121.00")
        self.assertEqual(self.moneybird.invoices["12"]["payments"], [])

    def test_uncollectible_is_reviewed_or_marked_once(self):
        self.stripe.invoices[0]["status"] = "uncollectible"
        self.assertEqual(self.worker().run()["review"], 1)
        self.settings["uncollectible_action"] = "mark"
        self.assertEqual(self.worker().run()["uncollectible"], 1)
        self.worker().run()
        self.assertEqual(
            [action for action in self.moneybird.actions if action[0] == "PATCH"],
            [("PATCH", "/external_sales_invoices/11/mark_as_uncollectible")],
        )

    def test_existing_line_tampering_is_detected_even_when_totals_agree(self):
        self.worker().run()
        self.moneybird.invoices["11"]["details"][0]["ledger_account_id"] = "99"
        self.assertEqual(self.worker().run()["failed"], 1)

    def test_payment_and_external_settlement_guards(self):
        source = invoice()
        source["starting_balance"] = -12100
        with self.assertRaisesRegex(SyncError, "external_payment_requires_review"):
            self.worker().payments(source, "11")

    def test_partial_failure_still_processes_other_invoices(self):
        bad = invoice()
        bad["id"], bad["total"] = "in_bad", 999
        self.stripe.invoices.append(bad)
        result = self.worker().run()
        self.assertEqual(result["verified"], 1)
        self.assertEqual(result["failed"], 1)

    def test_preflight_rejects_wrong_mappings(self):
        self.moneybird.tax_rates[0]["percentage"] = "9.0"
        with self.assertRaisesRegex(SyncError, "tax_rate_percentage_mismatch"):
            self.worker().run()
        self.moneybird.tax_rates[0]["percentage"] = "21.0"
        self.moneybird.ledger_accounts[0]["account_type"] = "expenses"
        with self.assertRaisesRegex(SyncError, "invalid_revenue_ledger_account"):
            self.worker().run()
        self.moneybird.ledger_accounts[0]["account_type"] = "revenue"
        self.moneybird.tax_rates[1]["percentage"] = "21.0"
        with self.assertRaisesRegex(SyncError, "no_tax_mapping_must_have_zero_rate"):
            self.worker().run()


class DescriptionTests(unittest.TestCase):
    def names(self, settings=None, **changes):
        item = {**line(), "period": {"start": 1767222000, "end": 1769900400}, **changes}
        plan, _ = document_plan(settings or config(), invoice(), invoice(), [item], "6")
        return [detail["description"] for detail in plan["body"]["details_attributes"]]

    def test_one_off_line_keeps_stripe_description_without_period(self):
        self.assertEqual(
            self.names(description="Hours (uren, 24 oktober 2026)"), ["Hours (uren, 24 oktober 2026)"]
        )

    def test_subscription_line_uses_pdf_name_and_adds_period(self):
        parent = {"parent": {"type": "subscription_item_details"}}
        for description in ("1 × Zorgeloos (voor €139.00 / maand)", "1 × Zorgeloos (at €139.00 / month)"):
            self.assertEqual(
                self.names(description=description, **parent), ["Zorgeloos [2026-01-01 to 2026-02-01]"]
            )
        self.assertEqual(
            self.names({**config(), "period_separator": "tot"}, **parent),
            ["Service [2026-01-01 tot 2026-02-01]"],
        )
        self.assertEqual(
            self.names(description="Custom subscription text", **parent),
            ["Custom subscription text [2026-01-01 to 2026-02-01]"],
        )


class DiscountTests(unittest.TestCase):
    def plan(self, labels=None, **changes):
        source = {**invoice(), "total_excluding_tax": 7500, "total": 9075}
        item = {**line(), "discount_amounts": [{"amount": 2500, "discount": "di_1"}], **changes}
        item["taxes"] = [{**item["taxes"][0], "amount": 1575}]
        plan, expected = document_plan(config(), source, source, [item], "6", discount_labels=labels)
        return plan["body"]["details_attributes"], expected

    def test_discount_becomes_a_negative_line_with_the_same_tax(self):
        details, expected = self.plan({"di_1": "WELCOME (25% off)"})
        self.assertEqual([d["description"] for d in details], ["Service", "WELCOME (25% off)"])
        self.assertEqual([d["price"] for d in details], ["100.00", "-25.00"])
        self.assertEqual(details[0]["tax_rate_id"], details[1]["tax_rate_id"])
        self.assertEqual(expected["net"], 7500)
        self.assertEqual(expected["lines"], [("3", "5", -2500), ("3", "5", 10000)])

    def test_discount_on_credit_note_flips_sign(self):
        source = {**invoice(), "id": "cn_test", "object": "credit_note", "number": "CN-1"}
        source.update(total_excluding_tax=7500, total=9075)
        item = {**line(), "discount_amounts": [{"amount": 2500, "discount": "di_1"}]}
        item["taxes"] = [{**item["taxes"][0], "amount": 1575}]
        plan, expected = document_plan(config(), source, invoice(), [item], "6", "EX-1")
        self.assertEqual([d["price"] for d in plan["body"]["details_attributes"]], ["-100.00", "25.00"])
        self.assertEqual(expected["total"], -9075)

    def test_unlabelled_discount_falls_back_to_a_generic_name(self):
        details, _ = self.plan()
        self.assertEqual(details[1]["description"], "Discount")

    def test_worker_labels_discounts_from_the_coupon_and_tolerates_unreadable_coupons(self):
        class Stripe:
            def get(self, path):
                if path == "/v1/coupons/ok":
                    return {"name": "FREE", "percent_off": 100.0}
                raise Rejected("remote_http_403")

        worker = Worker.__new__(Worker)
        worker.stripe = Stripe()
        lines = [
            {
                "discounts": [
                    {"id": "di_ok", "source": {"coupon": "ok"}},
                    {"id": "di_hidden", "source": {"coupon": "hidden"}},
                    {"id": "di_embedded", "coupon": {"name": "HALF", "percent_off": 50}},
                    "di_not_expanded",
                ]
            }
        ]
        self.assertEqual(
            worker.discount_labels(lines),
            {"di_ok": "FREE (100% off)", "di_hidden": "Discount", "di_embedded": "HALF (50% off)"},
        )

    def test_inclusive_tax_discounts_stay_netted(self):
        source = {**invoice(), "total_excluding_tax": 6198, "total": 7500}
        item = {**line(), "amount": 10000, "discount_amounts": [{"amount": 2500, "discount": "di_1"}]}
        item["taxes"] = [{**item["taxes"][0], "amount": 1302, "tax_behavior": "inclusive"}]
        plan, _ = document_plan(config(), source, source, [item], "6")
        self.assertEqual(len(plan["body"]["details_attributes"]), 1)

    def test_discounted_invoice_round_trips_through_the_worker(self):
        with patch("sys.stdout", new=io.StringIO()):
            settings, stripe, moneybird = config(), FakeStripe(), FakeMoneybird()
            moneybird.contacts["6"] = {"id": "6", "company_name": "Example", "archived": False}
            stripe.invoices[0].update(total_excluding_tax=7500, total=9075, amount_paid=9075)
            stripe.lines[0]["discount_amounts"] = [{"amount": 2500, "discount": "di_1"}]
            stripe.lines[0]["taxes"][0]["amount"] = 1575
            with tempfile.TemporaryDirectory() as temp, closing(Ledger(Path(temp) / "s.sqlite3")) as ledger:
                for _ in range(2):
                    worker = Worker(settings, stripe, moneybird, ledger, apply=True)
                    self.assertEqual(worker.run()["verified"], 1)
        self.assertEqual(moneybird.invoices["11"]["total_price_incl_tax"], "90.75")
        self.assertEqual(len(moneybird.invoices["11"]["details"]), 2)


class ImpliedTaxTests(unittest.TestCase):
    """Stripe collected no tax; the price to an EU customer includes 21% VAT."""

    def settings(self):
        return {**config(), "untaxed_eu_tax_rate_id": "5", "_untaxed_eu_percentage": "21.0"}

    def untaxed(self, total, country="FR", **changes):
        source = {**invoice(), "total": total, "total_excluding_tax": total, "amount_paid": total}
        source["customer_address"] = {"country": country} if country else None
        source.update(changes)
        item = {**line(), "amount": total, "taxes": []}
        return source, item

    def test_eu_customer_is_booked_gross_with_inclusive_vat(self):
        source, item = self.untaxed(900)
        plan, expected = document_plan(self.settings(), source, source, [item], "6")
        detail = plan["body"]["details_attributes"][0]
        self.assertTrue(plan["body"]["prices_are_incl_tax"])
        self.assertEqual((detail["price"], detail["tax_rate_id"]), ("9.00", "5"))
        self.assertEqual((expected["net"], expected["total"], expected["taxes"]), (744, 900, {"5": 156}))
        self.assertEqual(expected["details"], [("Service", "3", "5", 744)])

    def test_rounding_matches_moneybird_per_line(self):
        source, item = self.untaxed(28800, country="NL")
        _, expected = document_plan(self.settings(), source, source, [item], "6")
        self.assertEqual((expected["net"], expected["taxes"]), (23802, {"5": 4998}))

    def test_missing_country_uses_the_default_contact_country(self):
        source, item = self.untaxed(900, country=None)
        _, expected = document_plan(
            {**self.settings(), "default_contact_country": "NL"}, source, source, [item], "6"
        )
        self.assertEqual(expected["net"], 744)
        _, expected = document_plan(
            {**self.settings(), "default_contact_country": "US"}, source, source, [item], "6"
        )
        self.assertEqual(expected["net"], 900)

    def test_non_eu_exempt_taxed_and_zero_invoices_are_unaffected(self):
        for country, changes in (("GB", {}), ("CA", {}), ("FR", {"customer_tax_exempt": "reverse"})):
            with self.subTest(country=country, changes=changes):
                settings = self.settings()
                settings["no_tax"]["reverse"] = "9"
                source, item = self.untaxed(900, country=country, **changes)
                plan, expected = document_plan(settings, source, source, [item], "6")
                self.assertFalse(plan["body"]["prices_are_incl_tax"])
                self.assertEqual(expected["net"], 900)
        plan, expected = document_plan(self.settings(), invoice(), invoice(), [line()], "6")
        self.assertFalse(plan["body"]["prices_are_incl_tax"])
        self.assertEqual(expected["taxes"], {"5": 2100})
        source, item = self.untaxed(0)
        self.assertEqual(document_plan(self.settings(), source, source, [item], "6")[1]["total"], 0)

    def test_credit_note_keeps_the_sign(self):
        source, item = self.untaxed(500)
        credit = {**source, "id": "cn_test", "object": "credit_note", "number": "CN-1"}
        _, expected = document_plan(self.settings(), credit, source, [item], "6", "EX-1")
        self.assertEqual((expected["net"], expected["total"], expected["taxes"]), (-413, -500, {"5": -87}))

    def test_worker_creates_and_verifies_inclusive_documents(self):
        with patch("sys.stdout", new=io.StringIO()):
            settings, stripe, moneybird = config(), FakeStripe(), FakeMoneybird()
            settings["untaxed_eu_tax_rate_id"] = "5"
            moneybird.contacts["6"] = {"id": "6", "company_name": "Example", "archived": False}
            stripe.invoices[0].update(total=900, total_excluding_tax=900, amount_paid=900)
            stripe.lines[0].update(amount=900, taxes=[])
            with tempfile.TemporaryDirectory() as temp, closing(Ledger(Path(temp) / "s.sqlite3")) as ledger:
                for _ in range(2):
                    self.assertEqual(
                        Worker(settings, stripe, moneybird, ledger, apply=True).run()["verified"], 1
                    )
                document = moneybird.invoices["11"]
                self.assertEqual(
                    (document["total_price_excl_tax"], document["total_price_incl_tax"]), ("7.44", "9.00")
                )
                settings["untaxed_eu_tax_rate_id"] = "9"  # The zero-percentage rate.
                with self.assertRaisesRegex(SyncError, "positive_sales_rate"):
                    Worker(settings, stripe, moneybird, ledger, apply=True).run()


class TaxRuleTests(unittest.TestCase):
    worker = WorkerTests.worker

    def setUp(self):
        WorkerTests.setUp(self)
        self.stripe.lines[0]["taxes"][0]["tax_rate_details"]["tax_rate"] = "txr_per_invoice"

    def test_unknown_tax_rate_id_is_mapped_by_country_and_percentage(self):
        self.settings["tax_rate_map"] = {"NL:21": "5"}
        self.assertEqual(self.worker().run()["verified"], 1)
        self.assertEqual(self.moneybird.creates, 1)

    def test_unknown_tax_rate_without_matching_rule_fails_without_writing(self):
        self.assertEqual(self.worker().run()["failed"], 1)
        self.settings["tax_rate_map"] = {"NL:0": "9"}
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 0)

    def test_rule_percentage_must_match_the_moneybird_rate(self):
        self.settings["tax_rate_map"] = {"NL:9": "5"}
        with self.assertRaisesRegex(SyncError, "tax_rate_rule_percentage_mismatch"):
            self.worker().run()

    def test_fully_discounted_invoice_uses_the_untaxed_rate_without_an_exemption_mapping(self):
        self.stripe.invoices[0].update(
            total=0, total_excluding_tax=0, amount_paid=0, customer_tax_exempt="reverse"
        )
        self.stripe.lines[0].update(taxes=[], discount_amounts=[{"amount": 10000}])
        self.assertEqual(self.worker().run()["verified"], 1)
        self.assertEqual(self.moneybird.invoices["11"]["total_price_incl_tax"], "0.00")

    def test_untaxed_invoice_with_value_still_needs_a_treatment(self):
        self.settings["no_tax"] = {}
        self.stripe.lines[0]["taxes"] = []
        self.assertEqual(self.worker().run()["failed"], 1)


class MatchingTests(unittest.TestCase):
    worker = WorkerTests.worker

    def setUp(self):
        WorkerTests.setUp(self)
        self.settings["contact_map"] = {}
        self.stripe.invoices[0].update(
            customer_email="billing@example.test",
            customer_tax_ids=[{"type": "eu_vat", "value": "NL123456789B01"}],
        )

    def external_invoice(self):
        plan, _ = document_plan(self.settings, invoice(), invoice(), [line()], "6")
        body = {**plan["body"], "reference": "manual", "source": None, "source_url": None}
        return self.moneybird.create_invoice(body)["id"]

    def test_dry_run_links_existing_contacts_and_documents_without_remote_changes(self):
        target = self.external_invoice()
        before = copy.deepcopy((self.moneybird.contacts, self.moneybird.invoices))
        result = self.worker(False).run()
        self.assertEqual(result, {"contacts_linked": 1, "linked": 1})
        self.assertEqual((self.moneybird.contacts, self.moneybird.invoices), before)
        self.assertEqual(self.moneybird.creates, 1)
        self.assertEqual(self.moneybird.actions, [])
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM operations").fetchone()[0], 0)
        self.assertEqual(self.ledger.db.execute("SELECT target_id FROM document_links").fetchone()[0], target)
        self.assertEqual(self.ledger.db.execute("SELECT target_id FROM contact_links").fetchone()[0], "6")

    def test_apply_replays_local_links_without_creating_or_rewriting_records(self):
        self.external_invoice()
        before = copy.deepcopy((self.moneybird.contacts, self.moneybird.invoices))
        self.worker(False).run()
        self.assertEqual(self.worker().run()["linked"], 1)
        self.assertEqual(self.worker().run()["linked"], 1)
        self.assertEqual((self.moneybird.contacts, self.moneybird.invoices), before)
        self.assertEqual(self.moneybird.creates, 1)
        self.assertEqual(self.moneybird.actions, [])

    def test_restored_snapshot_reuses_contact_and_invoice_links(self):
        self.external_invoice()
        self.worker(False).run()
        backup = Path(self.temp.name) / "restored.sqlite3"
        self.assertEqual(
            main(
                [
                    "backup",
                    "--state",
                    str(Path(self.temp.name) / "state.sqlite3"),
                    "--output",
                    str(backup),
                ]
            ),
            0,
        )
        with closing(Ledger(backup)) as restored:
            result = Worker(self.settings, self.stripe, self.moneybird, restored, apply=True).run()
        self.assertEqual(result, {"contacts_linked": 1, "linked": 1})
        self.assertEqual(self.moneybird.creates, 1)
        self.assertEqual(self.moneybird.actions, [])

    def test_manual_collapsed_quantities_and_different_descriptions_can_match(self):
        target = self.external_invoice()
        self.stripe.lines[0]["quantity"] = 2
        self.moneybird.invoices[target]["details"][0]["description"] = "Manual description"
        self.assertEqual(self.worker(False).run()["linked"], 1)

    def test_duplicate_candidates_require_an_explicit_document_map(self):
        self.external_invoice()
        target = self.external_invoice()
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 2)
        self.settings["document_map"] = {"in_test": target}
        self.assertEqual(self.worker(False).run()["linked"], 1)

    def test_explicit_document_mapping_still_requires_financial_verification(self):
        target = self.external_invoice()
        self.settings["document_map"] = {"in_test": target}
        self.moneybird.invoices[target]["details"][0]["ledger_account_id"] = "99"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 1)

    def test_incompatible_candidates_block_creation_instead_of_falling_back(self):
        target = self.external_invoice()
        document = self.moneybird.invoices[target]
        mutations = [
            (document["details"][0], "ledger_account_id", "99"),
            (document["details"][0], "tax_rate_id", "9"),
            (document["details"][0], "total_price_excl_tax_with_discount", "90.00"),
        ]
        for record, field, value in mutations:
            with self.subTest(field=field):
                previous = record.get(field)
                record[field] = value
                self.assertEqual(self.worker().run()["failed"], 1)
                self.assertEqual(self.moneybird.creates, 1)
                self.assertEqual(self.moneybird.actions, [])
                record[field] = previous

    def test_document_owned_by_another_stripe_source_is_not_adopted(self):
        target = self.external_invoice()
        self.moneybird.invoices[target]["source_url"] = "https://dashboard.stripe.com/test/invoices/in_other"
        self.assertEqual(self.worker().run()["verified"], 1)
        self.assertEqual(self.moneybird.creates, 2)
        self.settings["document_map"] = {"in_test": target}
        self.assertEqual(self.worker().run()["failed"], 1)

    def test_other_contact_invoice_is_not_adopted(self):
        target = self.external_invoice()
        self.moneybird.invoices[target]["contact_id"] = "2"
        result = self.worker().run()
        self.assertEqual(result["verified"], 1)
        self.assertEqual(self.moneybird.creates, 2)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM document_links").fetchone()[0], 0)

    def test_two_source_invoices_cannot_claim_one_existing_document(self):
        self.external_invoice()
        self.stripe.invoices.append({**self.stripe.invoices[0], "id": "in_other", "number": "EX-2"})
        result = self.worker().run()
        self.assertEqual(result["linked"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.moneybird.creates, 1)

    def test_linked_record_changes_or_deletion_never_trigger_a_new_create(self):
        target = self.external_invoice()
        self.worker(False).run()
        self.moneybird.invoices[target]["details"][0]["ledger_account_id"] = "99"
        self.assertEqual(self.worker().run()["failed"], 1)
        del self.moneybird.invoices[target]
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 1)

    def test_remapping_an_existing_link_is_rejected(self):
        self.external_invoice()
        self.worker(False).run()
        self.settings["document_map"] = {"in_test": "999"}
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 1)

    def test_unknown_write_intent_cannot_be_replaced_with_a_migration_match(self):
        self.external_invoice()
        ref = "stripe:acct_test:test:invoice:in_test"
        self.ledger.put(ref, "unknown-fingerprint", "unknown")
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.ledger.get(ref)["status"], "unknown")
        self.assertEqual(self.moneybird.creates, 1)

    def test_adopted_invoices_are_not_automatically_settled_or_deleted(self):
        self.external_invoice()
        self.worker(False).run()
        self.settings.update(
            sync_payments=True, receipt_ownership_confirmed=True, payment_financial_account_id="7"
        )
        with self.assertRaisesRegex(SyncError, "settlements_require_review"):
            self.worker().invoice(self.stripe.invoices[0])
        self.settings.update(sync_payments=False, void_action="delete")
        self.stripe.invoices[0]["status"] = "void"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.actions, [])

    def test_vat_and_name_match_reuses_contact_with_a_different_email(self):
        self.moneybird.contacts["6"]["email"] = "old@example.test"
        self.assertEqual(self.worker().run()["contacts_linked"], 1)
        self.assertEqual(self.moneybird.creates, 1)  # Invoice only.

    def test_name_and_email_match_is_case_insensitive(self):
        self.stripe.invoices[0]["customer_tax_ids"] = []
        self.moneybird.contacts["6"].update(company_name="EXAMPLE", email="BILLING@example.test")
        self.assertEqual(self.worker().run()["contacts_linked"], 1)
        self.assertEqual(self.moneybird.creates, 1)

    def test_person_contacts_match_on_first_and_last_name(self):
        self.stripe.invoices[0]["customer_name"] = "Jane Doe"
        self.moneybird.contacts["6"].update(company_name="", firstname="Jane", lastname="Doe")
        self.assertEqual(self.worker().run()["contacts_linked"], 1)

    def test_shared_email_without_matching_name_does_not_merge_or_create(self):
        self.moneybird.contacts["6"]["company_name"] = "Different business"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 0)

    def test_nameless_customer_links_a_unique_mailbox_match(self):
        self.stripe.invoices[0].update(customer_name=None, customer_tax_ids=[])
        self.moneybird.contacts["6"]["tax_number"] = ""
        self.assertEqual(self.worker().run()["contacts_linked"], 1)
        self.assertEqual(self.moneybird.creates, 1)

    def test_nameless_customer_with_ambiguous_mailbox_match_is_reviewed(self):
        self.stripe.invoices[0].update(customer_name=None, customer_tax_ids=[])
        self.moneybird.contacts["7"] = {**self.moneybird.contacts["6"], "id": "7"}
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 0)

    def test_conflicting_vat_numbers_do_not_merge_or_create(self):
        self.moneybird.contacts["6"]["tax_number"] = "NL999999999B01"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 0)

    def test_formatted_vat_with_changed_name_is_reviewed_instead_of_duplicated(self):
        self.moneybird.contacts["6"].update(
            company_name="Example Limited",
            email="",
            tax_number="NL 123.456.789 B01",
        )
        self.stripe.invoices[0]["customer_tax_ids"] = [{"type": "eu_vat", "value": "NL 123.456.789 B01"}]
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 0)

    def test_unknown_contact_create_cannot_be_replaced_by_a_migration_match(self):
        self.ledger.put("stripe:acct_test:test:customer:cus_test", "unknown", "unknown")
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 0)

    def test_name_only_match_requires_review_instead_of_creating_a_contact(self):
        self.moneybird.contacts["6"].update(email="other@example.test", tax_number="")
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 0)

    def test_ambiguous_and_archived_contacts_are_not_duplicated(self):
        self.moneybird.contacts["7"] = {**self.moneybird.contacts["6"], "id": "7"}
        self.assertEqual(self.worker().run()["failed"], 1)
        del self.moneybird.contacts["7"]
        self.moneybird.contacts["6"]["archived"] = True
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 0)

    def test_linked_contact_changes_and_deleted_contact_do_not_create_replacements(self):
        self.worker(False).run()
        self.stripe.invoices[0]["customer_name"] = "Changed name"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.stripe.invoices[0]["customer_name"] = "Example"
        del self.moneybird.contacts["6"]
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 0)

    def test_new_contact_is_created_only_when_no_possible_existing_contact_is_found(self):
        self.moneybird.contacts.clear()
        result = self.worker().run()
        self.assertEqual(result["verified"], 1)
        self.assertEqual(self.moneybird.creates, 2)  # One contact, one invoice.

    def test_stripe_customer_id_lookup_recovers_a_created_contact_without_the_ledger(self):
        self.moneybird.contacts.clear()
        self.worker().run()
        with closing(Ledger(Path(self.temp.name) / "fresh.sqlite3")) as fresh:
            result = Worker(self.settings, self.stripe, self.moneybird, fresh, apply=True).run()
        self.assertEqual(result, {"verified": 1})
        self.assertEqual(self.moneybird.creates, 2)


class HttpTests(unittest.TestCase):
    def test_reads_retry_and_writes_do_not(self):
        for safe, attempts in ((True, 3), (False, 1)):
            opener = unittest.mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError(
                "https://example.test", 503, "unavailable", {}, None
            )
            with self.assertRaises(UnknownOutcome):
                Http(opener, sleep=lambda _: None).request("https://example.test", {}, {}, safe=safe)
            self.assertEqual(opener.open.call_count, attempts)

    def test_rate_limited_writes_wait_for_retry_after_and_then_continue(self):
        opener, waits = unittest.mock.Mock(), []
        opener.open.side_effect = [
            urllib.error.HTTPError("https://example.test", 429, "limited", {"Retry-After": "7"}, None),
            unittest.mock.MagicMock(
                __enter__=lambda self: self, __exit__=lambda *args: None, read=lambda _: b'{"id": "1"}'
            ),
        ]
        result = Http(opener, sleep=waits.append).request("https://example.test", {}, {"a": 1}, method="POST")
        self.assertEqual(result, {"id": "1"})
        self.assertEqual(waits, [7])
        opener.open.side_effect = urllib.error.HTTPError("https://example.test", 429, "limited", {}, None)
        with self.assertRaises(Rejected):
            Http(opener, sleep=waits.append).request("https://example.test", {}, {"a": 1}, method="POST")
        self.assertEqual(len(waits), 6)

    def test_error_bodies_are_not_logged(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://example.test", 403, "PII-SECRET", {}, io.BytesIO(b"PII-SECRET")
        )
        with self.assertRaises(Rejected) as caught:
            Http(opener).request("https://example.test", {})
        self.assertNotIn("PII-SECRET", str(caught.exception))

    def test_moneybird_paths_and_administration_ids_are_validated(self):
        with self.assertRaises(SyncError):
            Moneybird("abc", "token", Http())
        client = Moneybird("1", "token", Http())
        self.assertEqual(
            client.url("/contacts", {"query": "a b"}),
            "https://moneybird.com/api/v2/1/contacts.json?query=a+b",
        )
        for path in ("contacts", "/contacts?x=1", "/../admin", "/contacts.json"):
            with self.assertRaises(SyncError):
                client.url(path)

    def test_moneybird_pagination_and_404_handling(self):
        client = Moneybird("1", "token", None)
        pages = [[{"id": str(index)} for index in range(100)], [{"id": "last"}]]
        with patch.object(client, "get", side_effect=pages) as get:
            self.assertEqual(len(list(client.list("/contacts"))), 101)
            self.assertEqual(get.call_args.args[1]["page"], 2)
        with patch.object(client, "get", side_effect=Rejected("remote_http_404")):
            self.assertIsNone(client.find("/contacts/1"))
        with patch.object(client, "get", side_effect=Rejected("remote_http_403")):
            with self.assertRaises(Rejected):
                client.find("/contacts/1")

    def test_pagination_fetches_every_page_and_checks_modes(self):
        stripe = Stripe("key", "acct_test", False, None)
        with patch.object(
            stripe,
            "get",
            side_effect=[
                {"data": [{"id": "a", "livemode": False}], "has_more": True},
                {"data": [{"id": "b", "livemode": False}], "has_more": False},
            ],
        ) as get:
            self.assertEqual([row["id"] for row in stripe.list("/v1/invoices")], ["a", "b"])
            self.assertEqual(get.call_args.args[1]["starting_after"], "a")
        with patch.object(
            stripe, "get", return_value={"data": [{"id": "a", "livemode": True}], "has_more": False}
        ):
            with self.assertRaisesRegex(SyncError, "mode_mismatch"):
                list(stripe.list("/v1/invoices"))

    def test_multipart_upload_carries_the_file_field(self):
        captured = {}

        def request(url, headers, payload=None, safe=False, method=None, body=None):
            captured.update(url=url, headers=headers, method=method, body=body)
            return None

        client = Moneybird("1", "token", unittest.mock.Mock(request=request))
        client.upload("/external_sales_invoices/5/attachment", "Stripe-in_1.pdf", b"%PDF-1.4 test")
        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["headers"]["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertRegex(captured["body"].decode(), r'name="file"; filename="Stripe-in_1.pdf"')
        self.assertIn(b"%PDF-1.4 test", captured["body"])


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("sys.stdout", new=io.StringIO()))
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.ledger.close)
        self.settings = config()
        self.settings.update(
            sync_payments=True, receipt_ownership_confirmed=True, payment_financial_account_id="7"
        )
        self.stripe, self.moneybird = FakeStripe(), FakeMoneybird()
        self.allocation = {
            "id": "inpay_test",
            "amount_paid": 6000,
            "payment": {"type": "payment_intent", "payment_intent": "pi_test"},
            "status_transitions": {"paid_at": 1767222000},
        }
        self.stripe.allocations = [self.allocation]
        self.stripe.get = lambda _: {"status": "succeeded", "currency": "eur", "amount_received": 6000}
        self.worker = Worker(self.settings, self.stripe, self.moneybird, self.ledger, apply=True)
        self.worker.administration = self.moneybird.administration()
        self.worker.document(invoice(), invoice(), [line()], "6")
        self.source = invoice()
        self.source["amount_remaining"] = 6100

    def test_partial_receipt_preserves_unpaid_amount_and_replay_does_not_repay(self):
        self.worker.payments(self.source, "11")
        self.worker.payments(self.source, "11")
        document = self.moneybird.invoices["11"]
        self.assertEqual(document["total_unpaid"], "61.00")
        self.assertEqual(len(document["payments"]), 1)
        payment = document["payments"][0]
        self.assertEqual(
            (
                payment["price"],
                payment["price_base"],
                payment["payment_date"],
                payment["financial_account_id"],
            ),
            ("60.00", "60.00", "2026-01-01", "7"),
        )
        self.assertEqual(payment["transaction_identifier"], "inpay_test")

    def test_lost_receipt_response_recovers_existing_receipt(self):
        self.moneybird.lose_payment_response = True
        with self.assertRaises(UnknownOutcome):
            self.worker.payments(self.source, "11")
        self.worker.payments(self.source, "11")
        self.assertEqual(self.moneybird.invoices["11"]["total_unpaid"], "61.00")
        self.assertEqual(len(self.moneybird.invoices["11"]["payments"]), 1)

    def test_shared_payment_is_quarantined_before_receipt(self):
        self.stripe.allocations.append({**self.allocation, "id": "inpay_other"})
        with self.assertRaisesRegex(SyncError, "shared_payment_requires_review"):
            self.worker.payments(self.source, "11")
        self.assertEqual(self.moneybird.invoices["11"]["payments"], [])

    def test_unpaid_amount_mismatch_and_foreign_currency_require_review(self):
        self.source["amount_remaining"] = 0
        with self.assertRaisesRegex(SyncError, "unpaid_amount_requires_review"):
            self.worker.payments(self.source, "11")
        self.worker.administration = {"currency": "USD"}
        with self.assertRaisesRegex(SyncError, "foreign_currency_payment_requires_review"):
            self.worker.payments(self.source, "11")

    def test_dry_run_does_not_register_payments(self):
        self.worker.apply = False
        self.worker.payments(self.source, "11")
        self.assertEqual(self.moneybird.invoices["11"]["payments"], [])


class ConfigTests(unittest.TestCase):
    def test_example_configuration_needs_real_identifiers(self):
        with patch("sys.stdout", new=io.StringIO()) as out:
            with tempfile.TemporaryDirectory() as temp:
                code = main(["sync", "--config", "config.example.json", "--state", f"{temp}/ledger.sqlite3"])
        self.assertEqual(code, 1)
        self.assertRegex(out.getvalue(), re.compile(r"configure_stripe_account_id"))


if __name__ == "__main__":
    unittest.main()
