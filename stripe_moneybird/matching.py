"""Read-only migration matching. Persist links locally; never rewrite Moneybird records."""

import unicodedata
from collections import Counter

from .core import digest, minor, require
from .planning import is_stripe_url


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def contact_name(record):
    return record.get("company_name") or " ".join(
        part for part in (record.get("firstname"), record.get("lastname")) if part
    )


def identity(record):
    return {
        "name": normalized(contact_name(record)),
        "email": normalized(record.get("email")),
        "vat": "".join(
            character for character in normalized(record.get("tax_number")) if character.isalnum()
        ),
    }


def document_lines(document, decimals):
    return Counter(
        (
            str(detail["ledger_account_id"]),
            str(detail["tax_rate_id"]),
            minor(detail["total_price_excl_tax_with_discount"], decimals),
        )
        for detail in document["details"]
    )


class Matcher:
    def __init__(self, worker):
        self.worker = worker
        self.config, self.moneybird, self.ledger = worker.config, worker.moneybird, worker.ledger

    def contact(self, ref, values):
        expected = identity(values)
        fingerprint = digest(expected)
        linked = self.ledger.linked("contact", ref)
        if linked:
            require(linked["fingerprint"] == fingerprint, "customer_snapshot_changed_set_contact_map")
            candidate = self.moneybird.find(f"/contacts/{linked['target_id']}")
            require(candidate is not None, "linked_contact_missing")
            candidates = [candidate]
        else:
            operation = self.ledger.get(ref)
            require(
                not operation or operation["status"] == "rejected",
                "existing_contact_matching_blocked_by_prior_operation",
            )
            terms = [values.get(key) for key in ("email", "tax_number", "company_name") if values.get(key)]
            if not terms:
                return None
            candidates, seen = [], set()
            for term in terms:
                found = list(self.moneybird.list("/contacts", {"query": term, "include_archived": "true"}))
                require(len(found) < 100, "too_many_contact_candidates_use_contact_map")
                for candidate in found:
                    if str(candidate["id"]) not in seen:
                        seen.add(str(candidate["id"]))
                        candidates.append(candidate)
        matches, possible = [], []
        for candidate in candidates:
            actual = identity(candidate)
            shared = {key for key in expected if expected[key] and expected[key] == actual[key]}
            if not shared:
                continue
            possible.append(candidate)
            # A conflicting VAT number must never be overridden by a shared mailbox.
            vat_conflict = expected["vat"] and actual["vat"] and expected["vat"] != actual["vat"]
            if "name" in shared and bool(shared & {"vat", "email"}) and not vat_conflict:
                matches.append(candidate)
        require(len(matches) <= 1, "ambiguous_existing_contact_use_contact_map")
        require(matches or not possible, "existing_contact_requires_explicit_contact_map")
        require(not linked or matches, "linked_contact_identity_changed")
        if not matches:
            return None
        require(not matches[0].get("archived"), "existing_contact_archived_requires_review")
        identifier = str(matches[0]["id"])
        self.ledger.link("contact", ref, identifier, fingerprint)
        return identifier

    def signature(self, plan, expected):
        body = plan["body"]
        return digest(
            {
                "identity": {key: body.get(key) for key in ("contact_id", "date", "currency")},
                "kind": plan["kind"],
                "lines": expected["lines"],
                "amounts": {key: expected[key] for key in ("net", "total", "taxes", "decimals")},
            }
        )

    def verify(self, identifier, plan, expected):
        actual = self.worker.read(identifier, fresh=True)
        require(actual is not None, "existing_invoice_missing")
        body = plan["body"]
        require(
            str(actual["contact_id"]) == str(body["contact_id"])
            and actual["date"] == body["date"]
            and actual["currency"] == body["currency"],
            "existing_invoice_identity_mismatch",
        )
        require(
            not is_stripe_url(actual.get("source_url")) or actual["source_url"] == body["source_url"],
            "existing_invoice_owned_by_other_source",
        )
        decimals = expected["decimals"]
        require(
            minor(actual["total_price_incl_tax"], decimals) == expected["total"]
            and minor(actual["total_price_excl_tax"], decimals) == expected["net"],
            "existing_invoice_amount_mismatch",
        )
        require(
            document_lines(actual, decimals) == Counter(tuple(line) for line in expected["lines"]),
            "existing_invoice_line_mapping_mismatch",
        )

    def document(self, source, plan, expected):
        ref, body = plan["ref"], plan["body"]
        linked = self.ledger.linked("document", ref)
        fingerprint = self.signature(plan, expected)
        mapped = self.config.get("document_map", {}).get(source["id"])
        if linked:
            require(not mapped or mapped == linked["target_id"], "existing_link_target_changed")
            require(linked["fingerprint"] == fingerprint, "existing_link_payload_changed")
            identifier = linked["target_id"]
        else:
            operation = self.ledger.get(ref)
            require(
                not operation or operation["status"] == "rejected",
                "existing_invoice_matching_blocked_by_prior_operation",
            )
            if mapped:
                identifier = mapped
            else:
                if body["contact_id"] is None:
                    return None
                day = body["date"].replace("-", "")
                candidates = list(
                    self.moneybird.list(
                        "/external_sales_invoices",
                        {"filter": f"period:{day}..{day},contact_id:{body['contact_id']},state:all"},
                    )
                )
                require(len(candidates) < 100, "too_many_existing_invoice_candidates")
                decimals = expected["decimals"]
                matches = [
                    item
                    for item in candidates
                    if item["currency"] == body["currency"]
                    and minor(item["total_price_incl_tax"], decimals) == expected["total"]
                    and minor(item["total_price_excl_tax"], decimals) == expected["net"]
                ]
                require(len(matches) <= 1, "ambiguous_existing_invoice_use_document_map")
                if not matches:
                    return None
                identifier = str(matches[0]["id"])
        self.verify(identifier, plan, expected)
        self.ledger.link("document", ref, identifier, fingerprint)
        return identifier
