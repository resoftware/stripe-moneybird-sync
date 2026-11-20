from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .core import Rejected, SyncError, date_of, digest, log, major, minor, object_id, require
from .planning import reference, source_url


class Payouts:
    """Settle Stripe payouts against the bank line they produce.

    The payment import parks each gross receipt on the clearing ledger account. A payout then
    arrives net of Stripe's fees, so the bank line is booked to the same clearing account and a
    journal entry moves the fees from clearing to the fee account. Once both are booked the
    clearing account nets to zero for that payout."""

    def __init__(self, worker):
        self.worker = worker
        self.config, self.stripe, self.moneybird = worker.config, worker.stripe, worker.moneybird

    def preflight(self):
        config = self.config
        require(config.get("sync_payments"), "sync_payouts_requires_sync_payments")
        accounts = {str(item["id"]): item for item in self.moneybird.get("/financial_accounts")}
        bank = accounts.get(config["payout_bank_account_id"])
        require(
            bank is not None
            and bank.get("active", True)
            and bank["currency"] == self.worker.administration["currency"],
            "invalid_payout_bank_account",
        )
        ledger_accounts = {str(item["id"]): item for item in self.moneybird.get("/ledger_accounts")}
        for key in ("payout_clearing_ledger_account_id", "payout_fee_ledger_account_id"):
            account = ledger_accounts.get(config[key])
            require(account is not None and account.get("active", True), f"invalid_{key}")
        today = dt.date.today().strftime("%Y%m%d")
        # Both reads need token scopes the invoice import does not: bank and documents.
        try:
            self.moneybird.get(
                "/financial_mutations",
                {
                    "filter": f"period:{today}..{today},financial_account_id:{config['payout_bank_account_id']}"
                },
            )
            self.moneybird.get("/documents/general_journal_documents", {"filter": f"period:{today}..{today}"})
        except Rejected as error:
            raise SyncError("moneybird_token_needs_bank_and_documents_scope") from error

    def run(self):
        start = int(
            dt.datetime.combine(
                dt.date.fromisoformat(self.config["payout_start_date"]),
                dt.time(),
                ZoneInfo(self.config["timezone"]),
            ).timestamp()
        )
        counts = self.worker.counts
        for payout in self.stripe.list("/v1/payouts", {"status": "paid", "arrival_date[gte]": start}):
            try:
                counts["payouts_" + self.payout(payout)] += 1
            except SyncError as error:
                counts["failed"] += 1
                log("payout_failed", source_id=payout["id"], code=str(error))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                counts["failed"] += 1
                log("payout_failed", source_id=payout["id"], code="unsupported_source_or_target_schema")

    def payout(self, payout):
        currency = payout["currency"].upper()
        require(currency == self.worker.administration["currency"], "foreign_currency_payout_requires_review")
        decimals = self.config["currencies"][currency]
        amount = payout["amount"]
        require(type(amount) is int and amount > 0, "payout_amount_requires_review")
        gross, card_fees, net, charges = 0, 0, 0, []
        for item in self.stripe.list("/v1/balance_transactions", {"payout": payout["id"]}):
            if item["type"] == "payout":
                continue
            require(item["currency"] == payout["currency"], "foreign_currency_payout_requires_review")
            category = item.get("reporting_category")
            # Refunds, disputes and adjustments have no receipt to settle against; they stay manual.
            require(category in ("charge", "fee"), "payout_transaction_requires_review")
            if category == "charge":
                gross += item["amount"]
                card_fees += item["fee"]
                charges.append(item)
            net += item["net"]
        require(net == amount and gross > 0, "payout_does_not_add_up")
        fees = gross - amount
        numbers = [self.receipt(charge) for charge in charges]
        paid_on = date_of(payout["arrival_date"], self.config["timezone"])
        line = self.bank_line(payout, paid_on, decimals)
        if line is None:
            log("payout_bank_line_pending", source_id=payout["id"], amount_minor=amount)
            return "pending"
        if not self.worker.apply:
            log(
                "payout_preview",
                source_id=payout["id"],
                target_id=line,
                net_minor=amount,
                fee_minor=fees,
            )
            return "previewed"
        if fees:
            self.fees(payout, paid_on, fees, card_fees, decimals)
        self.link(payout, line, ", ".join(sorted(set(numbers))), decimals)
        log("payout_settled", source_id=payout["id"], target_id=line, net_minor=amount, fee_minor=fees)
        return "settled"

    def receipt(self, charge):
        """The gross must already sit on the clearing account as an imported invoice payment."""
        source = charge["source"]
        intent = object_id(self.stripe.get(f"/v1/charges/{object_id(source)}").get("payment_intent"))
        require(bool(intent), "payout_charge_without_invoice_requires_review")
        allocations = list(
            self.stripe.list(
                "/v1/invoice_payments",
                {"payment[type]": "payment_intent", "payment[payment_intent]": intent, "status": "paid"},
            )
        )
        require(
            len(allocations) == 1 and allocations[0]["amount_paid"] == charge["amount"],
            "payout_charge_without_invoice_requires_review",
        )
        allocation = allocations[0]
        invoice = {"id": object_id(allocation["invoice"]), "object": "invoice"}
        document_id = self.worker.find_document(source_url(self.config, invoice))
        document = self.worker.read(document_id) if document_id else None
        require(
            document is not None
            and any(
                item.get("transaction_identifier") == allocation["id"]
                and str(item.get("financial_account_id")) == self.config["payment_financial_account_id"]
                for item in document.get("payments") or []
            ),
            "payout_payment_not_booked",
        )
        return document["reference"]

    def mutations(self, paid_on):
        day = dt.date.fromisoformat(paid_on)
        # Bank feeds book a payout on its arrival date, give or take a weekend.
        start = (day - dt.timedelta(days=2)).strftime("%Y%m%d")
        end = (day + dt.timedelta(days=5)).strftime("%Y%m%d")
        account = self.config["payout_bank_account_id"]
        return self.moneybird.get(
            "/financial_mutations", {"filter": f"period:{start}..{end},financial_account_id:{account}"}
        )

    def booked(self, payout, paid_on):
        found = [
            str(item["id"])
            for item in self.mutations(paid_on)
            if any(
                payout["id"] in (booking.get("description") or "")
                for booking in item["ledger_account_bookings"]
            )
        ]
        require(len(found) <= 1, "payout_booked_on_several_bank_lines")
        return found[0] if found else None

    def bank_line(self, payout, paid_on, decimals):
        booked = self.booked(payout, paid_on)
        if booked:
            return booked
        candidates = [
            str(item["id"])
            for item in self.mutations(paid_on)
            if item["state"] == "unprocessed"
            and minor(item["amount"], decimals) == payout["amount"]
            and "STRIPE" in (item.get("contra_account_name") or "").upper()
        ]
        require(len(candidates) <= 1, "ambiguous_payout_bank_line_requires_review")
        return candidates[0] if candidates else None

    def fees(self, payout, paid_on, fees, card_fees, decimals):
        title = f"Stripe fees payout {payout['id']}"
        other = fees - card_fees
        detail = f"Stripe fees {major(card_fees, decimals)}" + (
            f" + other Stripe fees {major(other, decimals)}" if other else ""
        )
        entries = [
            (self.config["payout_fee_ledger_account_id"], fees, 0, detail),
            (self.config["payout_clearing_ledger_account_id"], 0, fees, f"Gross less payout {payout['id']}"),
        ]
        body = {
            "reference": title,
            "date": paid_on,
            # Moneybird takes nested entries as a hash keyed by row number.
            "general_journal_document_entries_attributes": {
                str(index): {
                    "ledger_account_id": ledger,
                    "debit": major(debit, decimals),
                    "credit": major(credit, decimals),
                    "description": text,
                }
                for index, (ledger, debit, credit, text) in enumerate(entries)
            },
        }

        def lookup():
            found = [
                item
                for item in self.moneybird.get(
                    "/documents/general_journal_documents", {"filter": f"reference:{title}"}
                )
                if item.get("reference") == title
            ]
            require(len(found) <= 1, "duplicate_payout_fee_entry_requires_review")
            return str(found[0]["id"]) if found else None

        def verify(identifier):
            actual = self.moneybird.find(f"/documents/general_journal_documents/{identifier}")
            require(actual is not None and actual.get("date") == paid_on, "payout_fee_entry_mismatch")
            stored = sorted(
                (
                    str(item["ledger_account_id"]),
                    minor(item["debit"], decimals),
                    minor(item["credit"], decimals),
                )
                for item in actual["general_journal_document_entries"]
            )
            require(stored == sorted(entry[:3] for entry in entries), "payout_fee_entry_mismatch")

        def create():
            return self.moneybird.post(
                "/documents/general_journal_documents", {"general_journal_document": body}
            )["id"]

        self.worker.ledger.ensure(
            reference(self.config, "payout_fees", payout["id"]), digest(body), lookup, create, verify
        )

    def link(self, payout, line, numbers, decimals):
        paid_on = date_of(payout["arrival_date"], self.config["timezone"])
        clearing = self.config["payout_clearing_ledger_account_id"]
        values = {
            "booking_type": "LedgerAccount",
            "booking_id": clearing,
            "price_base": major(payout["amount"], decimals),
            "description": f"Stripe payout {payout['id']} ({numbers})",
        }

        def verify(identifier):
            actual = self.moneybird.find(f"/financial_mutations/{identifier}")
            bookings = [
                item
                for item in (actual or {}).get("ledger_account_bookings") or []
                if payout["id"] in (item.get("description") or "")
            ]
            require(
                actual is not None
                and actual["state"] == "processed"
                and len(bookings) == 1
                and str(bookings[0]["ledger_account_id"]) == clearing
                and minor(bookings[0]["price"], decimals) == payout["amount"],
                "payout_bank_booking_mismatch",
            )

        def create():
            self.moneybird.patch(f"/financial_mutations/{line}/link_booking", values)
            return line

        self.worker.ledger.ensure(
            reference(self.config, "payout", payout["id"]),
            digest({**values, "line": line}),
            lambda: self.booked(payout, paid_on),
            create,
            verify,
        )
