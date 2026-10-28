from __future__ import annotations

import datetime as dt
import time
from collections import Counter, defaultdict
from decimal import Decimal

from .core import SyncError, date_of, digest, log, major, minor, object_id, require
from .matching import Matcher, identity
from .pdf import PDFStore, validate_pdf
from .planning import document_plan, reference, source_url


class Worker:
    def __init__(self, config, stripe, moneybird, ledger, apply=False):
        self.config, self.stripe, self.moneybird, self.ledger = config, stripe, moneybird, ledger
        self.apply = apply
        self.counts = defaultdict(int)
        self.cache, self.index, self.administration, self.contacts = {}, {}, None, {}

    # Moneybird reads are cached per run; writes evict the document they touch.
    def read(self, identifier, fresh=False):
        identifier = str(identifier)
        if fresh or identifier not in self.cache:
            self.cache[identifier] = self.moneybird.find(f"/external_sales_invoices/{identifier}")
        return self.cache[identifier]

    def evict(self, identifier):
        self.cache.pop(str(identifier), None)

    def find_document(self, url):
        return self.index.get(url)

    def preflight(self):
        self.stripe.check()
        config = self.config
        self.administration = self.moneybird.administration()
        require(not self.administration.get("suspended"), "moneybird_administration_suspended")
        ledger_accounts = {str(item["id"]): item for item in self.moneybird.get("/ledger_accounts")}
        for identifier in {
            config["revenue_ledger_account_id"],
            *config.get("ledger_account_map", {}).values(),
        }:
            account = ledger_accounts.get(identifier)
            require(
                account is not None
                and account.get("active", True)
                and account["account_type"] == "revenue"
                and "sales_invoice" in account.get("allowed_document_types", []),
                "invalid_revenue_ledger_account",
            )
        tax_rates = {str(item["id"]): item for item in self.moneybird.list("/tax_rates")}
        for stripe_rate, identifier in config["tax_map"].items():
            rate = tax_rates.get(identifier)
            require(
                rate is not None and rate["active"] and rate["tax_rate_type"] == "sales_invoice",
                "unsupported_moneybird_tax_rate",
            )
            percentage = self.stripe.get(f"/v1/tax_rates/{stripe_rate}")["percentage"]
            require(
                Decimal(str(percentage)) == Decimal(str(rate["percentage"])), "tax_rate_percentage_mismatch"
            )
        for key, identifier in config.get("tax_rate_map", {}).items():
            rate = tax_rates.get(identifier)
            require(
                rate is not None
                and rate["active"]
                and rate["tax_rate_type"] == "sales_invoice"
                and Decimal(key.split(":", 1)[1]) == Decimal(str(rate["percentage"] or 0)),
                "tax_rate_rule_percentage_mismatch",
            )
        if config.get("untaxed_eu_tax_rate_id"):
            rate = tax_rates.get(config["untaxed_eu_tax_rate_id"])
            require(
                rate is not None
                and rate["active"]
                and rate["tax_rate_type"] == "sales_invoice"
                and Decimal(str(rate["percentage"] or 0)) > 0,
                "untaxed_eu_tax_rate_must_be_a_positive_sales_rate",
            )
            self.config = {**self.config, "_untaxed_eu_percentage": str(rate["percentage"])}
        for identifier in set(config["no_tax"].values()):
            rate = tax_rates.get(identifier)
            require(
                rate is not None
                and rate["active"]
                and rate["tax_rate_type"] == "sales_invoice"
                and (rate["percentage"] is None or Decimal(str(rate["percentage"])) == 0),
                "no_tax_mapping_must_have_zero_rate",
            )
        if config.get("sync_payments"):
            accounts = {str(item["id"]): item for item in self.moneybird.get("/financial_accounts")}
            account = accounts.get(config["payment_financial_account_id"])
            require(
                account is not None
                and account.get("active", True)
                and account["currency"] == self.administration["currency"],
                "invalid_payment_financial_account",
            )
        self.index = {}
        start = self.config["start_date"].replace("-", "")
        today = dt.date.today().isoformat().replace("-", "")
        for item in self.moneybird.list(
            "/external_sales_invoices", {"filter": f"period:{start}..{today},state:all"}
        ):
            url = item.get("source_url")
            if url and url.startswith("https://dashboard.stripe.com/"):
                require(url not in self.index, "duplicate_moneybird_source_url_requires_review")
                self.index[url] = str(item["id"])

    def contact(self, invoice):
        source_id = object_id(invoice.get("customer"))
        require(bool(source_id), "stripe_customer_required")
        # Subscriptions invoice the same customer every period; resolve each customer once per run.
        if source_id not in self.contacts:
            self.contacts[source_id] = self.resolve_contact(invoice, source_id)
        return self.contacts[source_id]

    def resolve_contact(self, invoice, source_id):
        mapped = self.config.get("contact_map", {}).get(source_id)
        ref = reference(self.config, "customer", source_id)
        if mapped:
            linked = self.ledger.linked("contact", ref)
            require(not linked or linked["target_id"] == mapped, "existing_link_target_changed")
            contact = self.moneybird.find(f"/contacts/{mapped}")
            require(contact is not None and not contact.get("archived"), "mapped_contact_missing_or_archived")
            return mapped
        address = invoice.get("customer_address") or {}
        country = address.get("country") or self.config.get("default_contact_country")
        require(bool(country), "customer_country_required_set_default_contact_country")
        values = {
            # Product customers often have only a mailbox; a readable contact name beats the Stripe ID.
            "company_name": invoice.get("customer_name") or invoice.get("customer_email") or source_id,
            "customer_id": source_id,
            "email": invoice.get("customer_email") or "",
            "address1": address.get("line1") or "",
            "address2": address.get("line2") or "",
            "zipcode": address.get("postal_code") or "",
            "city": address.get("city") or "",
            "country": country,
        }
        vats = [item["value"] for item in invoice.get("customer_tax_ids", []) if item["type"] == "eu_vat"]
        require(len(vats) <= 1, "multiple_customer_vat_ids_require_review")
        values["tax_number"] = vats[0] if vats else ""

        def lookup():
            contact = self.moneybird.find(f"/contacts/customer_id/{source_id}")
            return str(contact["id"]) if contact else None

        existing = lookup()
        linked = self.ledger.linked("contact", ref)
        require(not existing or not linked or existing == linked["target_id"], "existing_link_target_changed")
        if not existing:
            matched = Matcher(self).contact(ref, values, named=bool(invoice.get("customer_name")))
            if matched:
                self.counts["contacts_linked"] += 1
                log("existing_contact_linked", source_id=source_id, target_id=matched)
                return matched
        if not self.apply:
            return existing
        # Freeze the first imported contact snapshot. Later snapshot changes require an explicit contact_map.
        fingerprint = digest(values)

        def verify(identifier):
            actual = self.moneybird.find(f"/contacts/{identifier}")
            require(actual is not None and not actual.get("archived"), "contact_missing_or_archived")
            require(identity(actual) == identity(values), "customer_snapshot_changed_set_contact_map")
            for key in ("customer_id", "address1", "address2", "zipcode", "city", "country"):
                require((actual.get(key) or "") == values[key], "customer_snapshot_changed_set_contact_map")

        def create():
            return self.moneybird.post("/contacts", {"contact": values})["id"]

        return self.ledger.ensure(ref, fingerprint, lookup, create, verify)

    def verify_document(self, identifier, plan, expected):
        actual = self.read(identifier, fresh=True)
        require(actual is not None, "moneybird_document_missing")
        body = plan["body"]
        for field in ("reference", "date", "currency", "source", "source_url"):
            require(actual.get(field) == body[field], "moneybird_document_identity_mismatch")
        require(str(actual["contact_id"]) == str(body["contact_id"]), "moneybird_document_identity_mismatch")
        require(
            actual.get("due_date") == body.get("due_date")
            and bool(actual.get("prices_are_incl_tax")) == body["prices_are_incl_tax"],
            "moneybird_document_due_date_or_tax_mode_mismatch",
        )
        decimals = expected["decimals"]
        require(
            len(actual["details"]) == len(body["details_attributes"]), "moneybird_invoice_line_count_mismatch"
        )
        planned = Counter(expected["details"])
        stored = Counter(
            (
                detail["description"],
                str(detail["ledger_account_id"]),
                str(detail["tax_rate_id"]),
                minor(detail["total_price_excl_tax_with_discount"], decimals),
            )
            for detail in actual["details"]
        )
        require(planned == stored, "moneybird_invoice_line_mapping_mismatch")
        require(
            minor(actual["total_price_incl_tax"], decimals) == expected["total"]
            and minor(actual["total_price_excl_tax"], decimals) == expected["net"],
            "moneybird_document_amount_mismatch",
        )

    def action(self, ref, fingerprint, target, complete, perform):
        return self.ledger.ensure(
            ref,
            fingerprint,
            lambda: target if complete() else None,
            lambda: (perform(), target)[1],
            lambda _: require(complete(), "action_not_complete"),
        )

    def configured_taxes(self, lines):
        """Stripe Tax creates a new tax-rate ID per invoice; map those by country and percentage."""
        rules = self.config.get("tax_rate_map", {})
        tax_map = dict(self.config["tax_map"])
        for line in lines:
            for tax in line.get("taxes") or []:
                identifier = object_id((tax.get("tax_rate_details") or {}).get("tax_rate"))
                if not identifier or identifier in tax_map or not rules:
                    continue
                rate = self.stripe.get(f"/v1/tax_rates/{identifier}")
                key = f"{rate.get('country')}:{Decimal(str(rate.get('percentage'))).normalize():f}"
                require(key in rules, "tax_rate_mapping_required")
                tax_map[identifier] = rules[key]
        return {**self.config, "tax_map": tax_map}

    def discount_labels(self, lines):
        """Label discount lines like Stripe's PDF, "<coupon name> (<n>% off)"; fall back to "Discount"."""
        labels = {}
        for line in lines:
            for discount in line.get("discounts") or []:
                if not isinstance(discount, dict) or discount["id"] in labels:
                    continue
                # The pinned API version embeds the coupon; newer ones reference it from "source".
                coupon = discount.get("coupon") or (discount.get("source") or {}).get("coupon")
                label = "Discount"
                try:
                    if coupon and not isinstance(coupon, dict):
                        coupon = self.stripe.get(f"/v1/coupons/{object_id(coupon)}")
                except SyncError:
                    coupon = None  # Restricted API keys may not read coupons.
                if coupon and coupon.get("name"):
                    label = coupon["name"]
                    if coupon.get("percent_off"):
                        label += f" ({Decimal(str(coupon['percent_off'])).normalize():f}% off)"
                labels[discount["id"]] = label
        return labels

    def document(self, source, invoice, lines, contact, original_number=None):
        plan, expected = document_plan(
            self.configured_taxes(lines),
            source,
            invoice,
            lines,
            contact,
            original_number,
            invoice.get("_discount_labels"),
        )
        ref, body = plan["ref"], plan["body"]
        fingerprint = digest({"body": body, "expected": expected})
        managed = self.find_document(body["source_url"])
        linked = self.ledger.linked("document", ref)
        mapped = self.config.get("document_map", {}).get(source["id"])
        require(not managed or not linked or managed == linked["target_id"], "existing_link_target_changed")
        require(not managed or not mapped or managed == mapped, "existing_link_target_changed")
        if linked or not managed:
            existing = Matcher(self).document(source, plan, expected)
            if existing:
                self.counts["linked"] += 1
                log(
                    "existing_document_linked",
                    source_id=source["id"],
                    target_id=existing,
                    mode="apply" if self.apply else "dry-run",
                )
                return existing
        if not self.apply:
            self.counts["previewed"] += 1
            log(
                "document_preview",
                source_id=source["id"],
                total_minor=expected["total"],
                currency=source["currency"],
            )
            return managed

        def create():
            created = self.moneybird.post("/external_sales_invoices", {"external_sales_invoice": body})
            self.index[body["source_url"]] = str(created["id"])
            return created["id"]

        identifier = self.ledger.ensure(
            ref,
            fingerprint,
            lambda: self.find_document(body["source_url"]),
            create,
            lambda identifier: self.verify_document(identifier, plan, expected),
        )
        self.counts["verified"] += 1
        return identifier

    def void(self, source, kind):
        ref = reference(self.config, kind, source["id"])
        require(not self.ledger.linked("document", ref), "linked_document_void_requires_review")
        identifier = self.find_document(source_url(self.config, source))
        if not identifier:
            deleted = self.ledger.get(ref + ":void")
            require(
                (deleted is not None and deleted["status"] == "succeeded") or not self.ledger.get(ref),
                "voided_document_result_missing",
            )
            self.counts["skipped"] += 1
            return
        document = self.read(identifier)
        require(
            document is not None and not document.get("payments"),
            "voided_document_with_payments_requires_review",
        )
        require(
            self.config.get("void_action") == "delete", "voided_source_requires_manual_deletion_or_credit"
        )
        if not self.apply:
            self.counts["previewed"] += 1
            return

        def complete():
            return self.read(identifier, fresh=True) is None

        def perform():
            self.moneybird.delete(f"/external_sales_invoices/{identifier}")
            self.index.pop(source_url(self.config, source), None)

        self.action(ref + ":void", digest({"id": identifier}), identifier, complete, perform)
        self.counts["deleted"] += 1

    def uncollectible(self, invoice, identifier):
        if self.config.get("uncollectible_action") != "mark":
            log("accountant_review", source_id=invoice["id"], code="bad_debt_policy_required")
            self.counts["review"] += 1
            return
        require(
            not self.ledger.linked("document", reference(self.config, "invoice", invoice["id"])),
            "linked_invoice_uncollectible_requires_review",
        )
        if not self.apply or identifier is None:
            return

        def complete():
            return bool((self.read(identifier, fresh=True) or {}).get("marked_uncollectible_on"))

        self.action(
            reference(self.config, "uncollectible", invoice["id"]),
            digest({"id": identifier}),
            identifier,
            complete,
            lambda: self.moneybird.patch(f"/external_sales_invoices/{identifier}/mark_as_uncollectible"),
        )
        self.counts["uncollectible"] += 1

    def payments(self, invoice, document_id, credited=0):
        require(
            not invoice.get("starting_balance")
            and not invoice.get("amount_overpaid")
            and not invoice.get("amount_paid_off_stripe"),
            "customer_balance_or_external_payment_requires_review",
        )
        currency = invoice["currency"].upper()
        require(
            self.administration is None or currency == self.administration["currency"],
            "foreign_currency_payment_requires_review",
        )
        decimals = self.config["currencies"][currency]
        payments = list(
            self.stripe.list("/v1/invoice_payments", {"invoice": invoice["id"], "status": "paid"})
        )
        for allocation in payments:
            payment = allocation["payment"]
            require(payment["type"] == "payment_intent", "unsupported_payment_type_requires_review")
            intent_id = object_id(payment["payment_intent"])
            intent = self.stripe.get(f"/v1/payment_intents/{intent_id}")
            require(
                intent["status"] == "succeeded" and intent["currency"] == invoice["currency"],
                "payment_not_settled",
            )
            amount = allocation["amount_paid"]
            require(
                type(amount) is int and amount > 0 and intent["amount_received"] == amount,
                "payment_amount_or_shared_allocation_requires_review",
            )
            shared = list(
                self.stripe.list(
                    "/v1/invoice_payments",
                    {
                        "payment[type]": "payment_intent",
                        "payment[payment_intent]": intent_id,
                        "status": "paid",
                    },
                )
            )
            require(
                len(shared) == 1 and shared[0]["id"] == allocation["id"], "shared_payment_requires_review"
            )
            ref = reference(self.config, "receipt", allocation["id"])
            paid_on = date_of(allocation["status_transitions"]["paid_at"], self.config["timezone"])
            values = {
                "payment_date": paid_on,
                "price": major(amount, decimals),
                "price_base": major(amount, decimals),
                "financial_account_id": self.config["payment_financial_account_id"],
                "transaction_identifier": allocation["id"],
            }

            def lookup(allocation=allocation):
                document = self.read(document_id, fresh=True)
                require(document is not None, "moneybird_document_missing")
                found = [
                    item
                    for item in document.get("payments") or []
                    if item.get("transaction_identifier") == allocation["id"]
                ]
                require(len(found) <= 1, "duplicate_moneybird_payment_requires_review")
                return str(found[0]["id"]) if found else None

            def verify(identifier, values=values, amount=amount):
                document = self.read(document_id, fresh=True)
                actual = next(
                    (item for item in document.get("payments") or [] if str(item["id"]) == identifier), None
                )
                require(
                    actual is not None
                    and minor(actual["price"], decimals) == amount
                    and actual["payment_date"] == values["payment_date"]
                    and str(actual.get("financial_account_id")) == values["financial_account_id"]
                    and actual.get("transaction_identifier") == values["transaction_identifier"],
                    "receipt_verification_failed",
                )

            def create(values=values, amount=amount):
                document = self.read(document_id, fresh=True)
                require(
                    minor(document["total_unpaid"], decimals) >= amount, "payment_exceeds_moneybird_unpaid"
                )
                self.moneybird.post(f"/external_sales_invoices/{document_id}/payments", {"payment": values})
                result = lookup()
                require(bool(result), "receipt_result_not_found")
                return result

            if self.apply:
                self.ledger.ensure(
                    ref, digest({**values, "document_id": document_id}), lookup, create, verify
                )
            self.counts["payment_allocations"] += 1
        if self.apply:
            document = self.read(document_id, fresh=True)
            # Moneybird does not net a credit against its invoice; Stripe's remaining amount already does.
            require(
                minor(document["total_unpaid"], decimals) == invoice["amount_remaining"] + credited,
                "invoice_unpaid_amount_requires_review",
            )

    def invoice(self, invoice):
        if invoice["status"] == "draft":
            self.counts["skipped"] += 1
            return
        if invoice["total"] == 0 and self.config.get("skip_zero_total"):
            # Free trials and fully discounted periods carry no revenue, tax or payment.
            self.counts["skipped"] += 1
            return
        if invoice["status"] == "void":
            self.void(invoice, "invoice")
            return
        require(invoice["status"] in ("open", "paid", "uncollectible"), "unsupported_invoice_status")
        pdf = self.fetch_pdf(invoice)
        invoice["_lines"] = list(
            self.stripe.list(f"/v1/invoices/{invoice['id']}/lines", {"expand[]": "data.discounts"})
        )
        invoice["_discount_labels"] = self.discount_labels(invoice["_lines"])
        contact = self.contact(invoice)
        document_id = self.document(invoice, invoice, invoice["_lines"], contact)
        if pdf is not None:
            self.attach_pdf(invoice, document_id, pdf)
        require(
            not self.config.get("sync_payments")
            or not self.ledger.linked("document", reference(self.config, "invoice", invoice["id"])),
            "linked_invoice_settlements_require_review",
        )
        credited = 0
        for credit in self.stripe.list("/v1/credit_notes", {"invoice": invoice["id"]}):
            if credit["status"] == "void":
                self.void(credit, "credit")
                continue
            require(credit["status"] == "issued", "unsupported_credit_status")
            pdf = self.fetch_pdf(credit)
            lines = list(self.stripe.list(f"/v1/credit_notes/{credit['id']}/lines"))
            credit_id = self.document(credit, invoice, lines, contact, invoice.get("number"))
            if pdf is not None:
                self.attach_pdf(credit, credit_id, pdf)
            if credit.get("pre_payment_amount"):
                credited += credit["pre_payment_amount"]
                log(
                    "accountant_review",
                    source_id=credit["id"],
                    code="credit_settlement_against_invoice_required",
                )
                self.counts["review"] += 1
        if self.config.get("sync_payments"):
            self.payments(invoice, document_id, credited)
        if invoice["status"] == "uncollectible":
            self.uncollectible(invoice, document_id)

    def attach_pdf(self, source, document_id, data):
        if document_id is None:
            self.counts["pdfs_previewed"] += 1
            return
        identifier, status = PDFStore(self).sync(source, document_id, data, self.apply)
        self.counts["pdfs_" + status] += 1
        log("source_pdf_" + status, source_id=source["id"], target_id=identifier)

    def fetch_pdf(self, source):
        if not self.config.get("sync_pdfs", True):
            return None
        data = self.stripe.pdf(source)
        validate_pdf(data)
        return data

    def run(self, cutoff=None):
        self.preflight()
        cutoff = cutoff or self.config["start_date"]
        require(cutoff >= self.config["start_date"], "cutoff_precedes_configured_migration_boundary")
        from zoneinfo import ZoneInfo

        start = int(
            dt.datetime.combine(
                dt.date.fromisoformat(cutoff), dt.time(), ZoneInfo(self.config["timezone"])
            ).timestamp()
        )
        boundary = int(time.time())
        # A full bounded scan is deliberate: older finalized invoices can gain payments and credits.
        for invoice in self.stripe.list("/v1/invoices", {"created[gte]": start, "created[lte]": boundary}):
            try:
                self.invoice(self.stripe.get(f"/v1/invoices/{invoice['id']}"))
            except SyncError as error:
                self.counts["failed"] += 1
                log("document_failed", source_id=invoice["id"], code=str(error))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                self.counts["failed"] += 1
                log("document_failed", source_id=invoice["id"], code="unsupported_source_or_target_schema")
        return dict(self.counts)

    def reconcile(self):
        self.preflight()
        from zoneinfo import ZoneInfo

        start = int(
            dt.datetime.combine(
                dt.date.fromisoformat(self.config["start_date"]), dt.time(), ZoneInfo(self.config["timezone"])
            ).timestamp()
        )
        totals = {}
        for item in self.stripe.list(
            "/v1/balance_transactions", {"created[gte]": start, "created[lte]": int(time.time())}
        ):
            key = (item["currency"], item.get("reporting_category") or item["type"])
            row = totals.setdefault(key, {"count": 0, "gross_minor": 0, "fee_minor": 0, "net_minor": 0})
            row["count"] += 1
            row["gross_minor"] += item["amount"]
            row["fee_minor"] += item["fee"]
            row["net_minor"] += item["net"]
        for (currency, category), amounts in sorted(totals.items()):
            log("settlement_summary", currency=currency, category=category, **amounts)
        log("settlement_review", code="fees_payouts_refunds_disputes_are_not_posted_by_this_worker")
