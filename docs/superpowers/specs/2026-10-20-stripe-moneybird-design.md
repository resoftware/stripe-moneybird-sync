# Stripe to Moneybird sync: design

Date: 2026-10-20. Status: implemented on branch `feature/moneybird`.

## Intent

Gerwin built `stripe-odoo-sync`: a weekly, stateful, "stop for review instead of
guessing" worker that copies Stripe invoices, credit notes, PDFs and (optionally)
payments into Odoo. Re: Software keeps its books in Moneybird, so the worker is
rewritten against the Moneybird API v2. Everything that made the Odoo version
trustworthy stays: the SQLite ledger with durable write intents, replay
verification, migration matching of pre-existing records, dry-run by default,
and partial failures that keep the run visible.

Trevi was asleep while this was designed; every choice below is Claude's call
unless marked otherwise, and is open for revision before the first `apply` run.

## Goals

- Every finalized Stripe invoice and issued credit note since `start_date`
  exists exactly once in Moneybird, with the original PDF attached.
- Replays never duplicate, never silently rewrite, and recover lost responses.
- Invoices or contacts that were entered by hand before the integration are
  adopted when they match unambiguously, otherwise stopped for review.
- The worker runs unattended as a Kubernetes CronJob on the Re: Software cluster.

## Non-goals and anti-goals

- No bank reconciliation of Stripe payouts, fees, refunds or disputes. The
  `reconcile` report stays read-only.
- No Moneybird-native sales invoices. Stripe is the issuing system; Moneybird
  receives *external sales invoices* (`external_sales_invoices`), which is the
  Moneybird object meant for invoices issued elsewhere.
- Never delete or edit a Moneybird document the worker did not create, and never
  invent balancing lines, fallback tax rates or write-offs.

## Mapping decisions

| Stripe | Moneybird | Notes |
| --- | --- | --- |
| Customer | Contact | `customer_id` = Stripe customer ID (`cus_…`) on contacts the worker creates. That is the durable, exact lookup (`GET /contacts/customer_id/{id}`) used to recover a lost create. Existing contacts are matched on normalized name plus VAT number or e-mail, same rules as before, and keep their own customer number. |
| Finalized invoice | External sales invoice | `reference` = Stripe invoice number, `source` = `Stripe`, `source_url` = deterministic Stripe dashboard URL containing the Stripe ID. The URL is the durable identity used for lookups; the number is what the accountant sees. |
| Issued credit note | External sales invoice with negative line prices | Moneybird has no external credit note type. `reference` = credit note number, description names the original invoice number. |
| Line item | `details_attributes` entry | One tax rate per line (Moneybird limitation; multiple Stripe tax rates on one line stop for review). Quantity is kept when net ÷ quantity is exact in currency units, otherwise the line is collapsed to quantity 1 with `n ×` in the description so totals stay exact. |
| Tax rate | `tax_rate_id` | `tax_map` as before. Preflight checks the Moneybird rate is active, `sales_invoice` type and has the same percentage as the Stripe tax rate. `no_tax` maps a Stripe exemption treatment to a single zero-percentage rate. |
| Product | `ledger_account_id` | `revenue_ledger_account_id` with optional `ledger_account_map` per Stripe product. Preflight requires `account_type: revenue` and `sales_invoice` in `allowed_document_types`. |
| Line period | detail `period` (`YYYYMMDD..YYYYMMDD`) | Opt-in via `line_periods`. Moneybird then defers the revenue over the period in the P&L, which is a bookkeeping policy, so off by default. |
| PDF | Attachment on the external sales invoice | Uploaded once; recognised again by filename `Stripe-<id>.pdf`, content type and size. |
| Successful PaymentIntent | Payment on the external sales invoice | Opt-in `sync_payments`. Registered on a dedicated Stripe financial account (`payment_financial_account_id`), `transaction_identifier` = Stripe invoice payment ID so a lost response is recoverable. Requires `receipt_ownership_confirmed` because a bank feed on the same account would double the money. Only base-currency invoices, because Moneybird needs `price_base` and the worker refuses to invent an exchange rate. |
| Void invoice / credit | `void_action` | `review` (default) stops for review every run. `delete` removes an external sales invoice the worker created itself, has no payments, and was not adopted. Moneybird refuses deletes in locked periods, which surfaces as a rejection. |
| Uncollectible invoice | `uncollectible_action` | `review` (default) or `mark`, which calls `mark_as_uncollectible` once and verifies `marked_uncollectible_on`. |
| Pre-payment credit applied to an open invoice | Review | Moneybird settlement of a negative external invoice against its original is not automated in this version. |

External sales invoices have no draft state: a create is booked immediately.
The worker therefore verifies the created document (totals, lines, contact,
date, currency) right after creation and stops for review on any mismatch; it
never deletes to "fix" its own mistake. Dry-run is the place to catch mapping
errors.

## Changes to the operating model

- Ledger `target_id` becomes text because Moneybird IDs are 64-bit integers
  that the API recommends treating as strings. Scope binds to Stripe account,
  mode and `administration_id`.
- Moneybird allows 150 requests per 5 minutes per administration. The worker
  honours `Retry-After` on 429 for reads and writes (a 429 is never committed),
  builds one index of Stripe-sourced external sales invoices per run instead of
  searching per invoice, and caches each Moneybird document read within a run.
  The example CronJob deadline grows to 30 minutes.
- Credentials: `STRIPE_API_KEY` and `MONEYBIRD_API_TOKEN` (a personal API token
  or OAuth access token with `sales_invoices` and `settings` scope).
- The package is `stripe_moneybird`, the image `ghcr.io/resoftware/stripe-moneybird-sync`.

## Configuration

```json
{
  "stripe_account_id": "acct_REPLACE",
  "livemode": false,
  "start_date": "2026-01-01",
  "timezone": "Europe/Amsterdam",
  "administration_id": "REPLACE",
  "revenue_ledger_account_id": "REPLACE",
  "currencies": {"EUR": 2},
  "tax_map": {},
  "no_tax": {"none": "REPLACE"},
  "contact_map": {},
  "document_map": {},
  "ledger_account_map": {},
  "default_contact_country": null,
  "line_periods": false,
  "sync_pdfs": true,
  "sync_payments": false,
  "receipt_ownership_confirmed": false,
  "payment_financial_account_id": null,
  "void_action": "review",
  "uncollectible_action": "review"
}
```

Moneybird requires a country on every contact. Stripe customers without a
billing country stop for review unless `default_contact_country` is set.

## Verified against the real administration (2026-10-28)

A dry run with the live Stripe key and the Re: Software administration confirmed:
list responses of `external_sales_invoices` carry `details` (with `amount`,
`amount_decimal`, `total_price_excl_tax_with_discount`, `period`), `payments`,
`attachments` (`filename`, `content_type`, `size`) and `tax_totals`; `source`
and `source_url` come back as empty strings on manual entries. Four invoices
that had been entered by hand were linked, not duplicated, once the three
contacts without an e-mail address were mapped in `contact_map`. Still open:
the 422 body on locked periods and the shape of a create response.

Findings that shaped the configuration: the product issues many € 0 trial
invoices (`skip_zero_total`), customers usually have only an e-mail address
(contact name falls back to the e-mail, `default_contact_country` needed),
metered tiers produce zero-quantity lines (dropped), and the manual entries
used the "Geen btw" rate and the "Ongecategoriseerde inkomsten" ledger with
net-of-fee payments on the ING account.

## Upstream 1.0.6 merged (2026-10-28)

Gerwin's commits up to `cdd8b56` are ported: Stripe Tax per-invoice rates via
`tax_rate_map` (`"<country>:<percentage>"`, percentage checked against the
Moneybird rate in preflight), discounts as separate negative lines labelled after
the coupon, subscription lines named like Stripe's PDF with a configurable
`period_separator`, nameless customers matched on a unique e-mail, and fully
discounted invoices needing no exemption mapping. Because a Moneybird line
always needs a tax rate, a zero-total untaxed invoice uses the `none` rate. The
Odoo-specific attachment fix (`raw` instead of `datas`) does not apply.
