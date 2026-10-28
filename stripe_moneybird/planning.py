from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal

from .core import date_of, major, object_id, require

SOURCE = "Stripe"


def reference(config, kind, source_id):
    return (
        f"stripe:{config['stripe_account_id']}:{'live' if config['livemode'] else 'test'}:{kind}:{source_id}"
    )


def source_url(config, source):
    """Deterministic Stripe dashboard URL; it carries the Stripe ID and never expires."""
    credit = source.get("object") == "credit_note"
    mode = "" if config["livemode"] else "test/"
    return f"https://dashboard.stripe.com/{mode}{'credit_notes' if credit else 'invoices'}/{source['id']}"


def is_stripe_url(url):
    return isinstance(url, str) and url.startswith("https://dashboard.stripe.com/")


def taxes_for(config, invoice, line):
    taxes = line.get("taxes")
    require(taxes is not None, "stripe_tax_schema_not_supported")
    # A Moneybird detail carries exactly one tax rate.
    require(len(taxes) <= 1, "multiple_tax_rates_per_line_require_review")
    expected, inclusive = defaultdict(int), 0
    for tax in taxes:
        source = object_id((tax.get("tax_rate_details") or {}).get("tax_rate"))
        require(source in config["tax_map"], "tax_mapping_required")
        require(tax.get("type") in (None, "tax_rate_details"), "unsupported_stripe_tax_type")
        target = config["tax_map"][source]
        expected[target] += tax["amount"]
        if tax.get("tax_behavior") == "inclusive":
            inclusive += tax["amount"]
        else:
            require(tax.get("tax_behavior") == "exclusive", "tax_behavior_required")
        return target, dict(expected), inclusive
    # A fully discounted invoice carries no tax, whatever the customer's tax status; Moneybird still
    # needs a rate on the line, so the ordinary untaxed mapping is used.
    treatment = "none" if invoice.get("total") == 0 else invoice.get("customer_tax_exempt", "none")
    require(treatment in config["no_tax"], "no_tax_treatment_mapping_required")
    return config["no_tax"][treatment], {}, 0


def period_of(period, timezone):
    start = date_of(period["start"], timezone).replace("-", "")
    # Stripe periods end at the exclusive boundary; Moneybird periods are inclusive days.
    end = date_of(max(period["end"] - 1, period["start"]), timezone).replace("-", "")
    return f"{start}..{end}"


def document_plan(config, source, invoice, lines, contact_id, original_number=None, discount_labels=None):
    credit = source.get("object") == "credit_note"
    currency = source["currency"].upper()
    require(currency in config["currencies"], "currency_mapping_required")
    decimals = config["currencies"][currency]
    require(not source.get("amount_shipping"), "shipping_requires_explicit_mapping")
    require(source.get("total_excluding_tax") is not None, "source_net_total_required")
    require(
        not invoice.get("on_behalf_of") and not invoice.get("transfer_data"),
        "connect_invoice_requires_review",
    )
    require(
        not invoice.get("issuer") or invoice["issuer"].get("type") == "self",
        "external_issuer_requires_review",
    )
    details, expected_taxes, net_total, sign = [], defaultdict(int), 0, -1 if credit else 1
    for line in lines:
        require(
            line["currency"].upper() == currency if "currency" in line else credit, "line_currency_mismatch"
        )
        # Non-discount pretax credits (e.g. billing credit grants) need their own accounting policy.
        require(
            all(item.get("type") == "discount" for item in line.get("pretax_credit_amounts", [])),
            "billing_credit_grant_requires_review",
        )
        tax_rate_id, tax_totals, inclusive = taxes_for(config, invoice, line)
        discount = sum(item["amount"] for item in line.get("discount_amounts", []))
        net = line["amount"] - discount - inclusive
        net_total += net
        for identifier, amount in tax_totals.items():
            expected_taxes[identifier] += amount
        raw_quantity = line.get("quantity_decimal")
        if raw_quantity is None:
            raw_quantity = line.get("quantity")
        quantity = Decimal(str(1 if raw_quantity is None else raw_quantity))
        if quantity == 0:
            # Metered tiers without usage report a zero quantity and amount; they carry nothing.
            require(line["amount"] == 0 and not tax_totals, "zero_quantity_line_with_amount")
            continue
        require(quantity > 0, "invalid_line_quantity")
        pricing = line.get("pricing") or {}
        product = object_id((pricing.get("price_details") or {}).get("product"))
        if credit and line.get("invoice_line_item"):
            original_line = next(
                (item for item in invoice.get("_lines", []) if item["id"] == line["invoice_line_item"]), None
            )
            require(original_line is not None, "original_credit_line_not_found")
            product = object_id(
                ((original_line.get("pricing") or {}).get("price_details") or {}).get("product")
            )
        ledger_account_id = config.get("ledger_account_map", {}).get(
            product, config["revenue_ledger_account_id"]
        )
        description = line.get("description") or line["id"]
        period = None
        if (line.get("parent") or {}).get("type") == "subscription_item_details":
            # Stripe's API wraps subscription lines as "1 × Name (at €9.00 / month)"; its PDF shows just the name.
            wrapped = re.fullmatch(r"\d+ × (.+) \([^()]*\)", description)
            description = wrapped.group(1) if wrapped else description
            period = line.get("period")
        if period:
            separator = config.get("period_separator", "to")
            description += (
                f" [{date_of(period['start'], config['timezone'])} {separator}"
                f" {date_of(period['end'], config['timezone'])}]"
            )
        # Show discounts as their own negative lines, like Stripe's invoice. Inclusive taxes stay netted.
        separate = (
            [item for item in line.get("discount_amounts", []) if item["amount"]] if not inclusive else []
        )
        if separate:
            net += discount
        unit = Decimal(net) / quantity
        if unit == unit.to_integral_value():
            amount, price = str(quantity.normalize()), major(sign * int(unit), decimals)
        else:
            # Keep totals exact: Moneybird multiplies price by amount, so collapse inexact unit prices.
            amount, price = "1", major(sign * net, decimals)
            description = f"{quantity.normalize()} × {description}"
        detail = {
            "description": description,
            "price": price,
            "amount": amount,
            "tax_rate_id": tax_rate_id,
            "ledger_account_id": ledger_account_id,
        }
        if period and config.get("line_periods"):
            detail["period"] = period_of(period, config["timezone"])
        details.append(detail)
        for item in separate:
            details.append(
                {
                    "description": (discount_labels or {}).get(item.get("discount"), "Discount"),
                    "price": major(-sign * item["amount"], decimals),
                    "amount": "1",
                    "tax_rate_id": tax_rate_id,
                    "ledger_account_id": ledger_account_id,
                }
            )
    require(bool(details), "empty_document_requires_review")
    require(net_total == source["total_excluding_tax"], "source_line_net_total_mismatch")
    require(net_total + sum(expected_taxes.values()) == source["total"], "source_line_gross_total_mismatch")
    kind = "credit" if credit else "invoice"
    issued = source.get("effective_at") or (
        source["created"] if credit else (source.get("status_transitions") or {}).get("finalized_at")
    )
    require(bool(issued), "source_issue_date_required")
    number = source.get("number")
    require(bool(number), "source_number_required")
    body = {
        "contact_id": contact_id,
        "reference": number,
        "date": date_of(issued, config["timezone"]),
        "currency": currency,
        "source": SOURCE,
        "source_url": source_url(config, source),
        "prices_are_incl_tax": False,
        "details_attributes": details,
    }
    if not credit and source.get("due_date"):
        body["due_date"] = date_of(source["due_date"], config["timezone"])
    if credit:
        require(bool(original_number), "original_invoice_number_required")
        for detail in details:
            detail["description"] = f"Credit note for invoice {original_number}: {detail['description']}"
    plan = {
        "ref": reference(config, kind, source["id"]),
        "kind": kind,
        "source_id": source["id"],
        "body": body,
    }
    expected = {
        "net": sign * net_total,
        "total": sign * source["total"],
        "taxes": {key: sign * value for key, value in expected_taxes.items()},
        "lines": sorted(
            (detail["ledger_account_id"], detail["tax_rate_id"], minor_of(detail, decimals))
            for detail in details
        ),
        "decimals": decimals,
    }
    return plan, expected


def minor_of(detail, decimals):
    from .core import minor

    return minor(Decimal(detail["price"]) * Decimal(detail["amount"]), decimals)
