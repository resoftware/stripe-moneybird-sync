# Stripe to Moneybird sync

Stripe issues our invoices and collects payments; Moneybird is where we keep the
accounts. This project moves the issued documents into Moneybird without having
to enter the same invoice twice. It also recognises invoices and contacts that
were entered manually before the integration existed.

It started life as Gerwin's [stripe-odoo-sync](https://github.com/gwku/stripe-odoo-sync)
and keeps its operating model; the Moneybird-specific choices are written up in
[the design note](docs/superpowers/specs/2026-10-20-stripe-moneybird-design.md).

It is a Python 3.13 command-line worker, packaged as a Docker image. A run checks
Stripe for invoices and credit notes, validates their accounting mappings, reuses
or creates the corresponding Moneybird records, saves the original PDFs, and
exits. SQLite keeps the links and operation history between runs.

We run it as a **weekly Kubernetes CronJob**, with a persistent volume for SQLite
and secrets supplied by a secret manager. That is one way to operate it: you can
run the same worker directly with Python, Docker, cron, a systemd timer, or
another job runner with persistent state.

This repository contains the worker, Dockerfile, tests, image-build workflow, and
a [generic Kubernetes example](examples/kubernetes/README.md). Production
manifests, secret-manager integration and monitoring rules live in the
infrastructure repository and are not included here.

## Quick start with Python

Requirements: Python 3.13 or newer on Linux/macOS, Stripe API access, and a
Moneybird administration with an API token (a personal API token or an OAuth
access token with the `sales_invoices` and `settings` scopes). The worker uses
Stripe's REST API and Moneybird's API v2, with only the Python standard library.

```sh
git clone https://github.com/resoftware/stripe-moneybird-sync.git
cd stripe-moneybird-sync
python3 -m venv .venv
. .venv/bin/activate
cp config.example.json config.local.json
```

Fill in `config.local.json` with your own account, administration, ledger
account, currency, and tax mappings. Supply `STRIPE_API_KEY` and
`MONEYBIRD_API_TOKEN` through your environment or secret manager. Credentials do
not belong in the JSON file.

```sh
python -m stripe_moneybird sync --mode dry-run \
  --config config.local.json --state ./state/ledger.sqlite3

# Create real Moneybird records after checking your mappings.
python -m stripe_moneybird sync --mode apply \
  --config config.local.json --state ./state/ledger.sqlite3
```

Moneybird external sales invoices have no draft state: `apply` books them. A dry
run makes no Moneybird writes, but can persist verified links in the local
ledger. Use the **same ledger** for subsequent runs and back it up.

## Run with Docker

The image is published as `ghcr.io/resoftware/stripe-moneybird-sync` for amd64
and arm64. Registry access is required if the package is private. You can also
build it:

```sh
docker build -t stripe-moneybird-sync:local .
mkdir -p state
docker run --rm --read-only \
  --user "$(id -u):$(id -g)" \
  -e STRIPE_API_KEY -e MONEYBIRD_API_TOKEN \
  --mount "type=bind,src=$PWD/config.local.json,dst=/config/config.json,readonly" \
  --mount "type=bind,src=$PWD/state,dst=/state" \
  stripe-moneybird-sync:local sync --mode apply
```

The default image user is UID/GID `10001`. The local example above uses your host
UID/GID to write the mounted state directory. In a scheduler, give the container
write access to `/state` and mount `/config/config.json` read-only. Keep API keys
injected at runtime. Pin a published commit tag or image digest for deployments.

## Run in Kubernetes

The [example setup](examples/kubernetes/README.md) supplies a namespace, PVC,
ConfigMap generation, and CronJob. It explains how to supply credentials and
activate a schedule using your own configuration. It is deliberately generic:
it does not contain accounting mappings, secrets, or cloud storage settings.

The operational contract is the same everywhere: retain the ledger, allow one
writer at a time, preserve the configuration, and keep a restorable backup
outside the worker's machine.

## Behavior

Each run lists every invoice created from the approved `start_date` through the
run's fixed discovery boundary. This deliberately revisits older invoices for
later finalization, credits, and settlements. A missed week does not lose changes.
All line, credit-note, and payment collections are paginated. Stripe requests use
the explicit `2025-03-31.basil` API version; schema changes require fixture updates
and sandbox verification before changing that version.

The default `dry-run` mode makes no Moneybird writes. It checks connectivity,
administration access, ledger accounts, tax rates (including the percentage
against the Stripe tax rate), and the payment account, then validates source
document totals and produces amount previews. `apply` creates contacts and
external sales invoices, then reads back their contact, dates, currency, lines,
ledger accounts, tax rates, and totals. It does not send e-mails or collect
payments.

Stripe invoices become Moneybird **external sales invoices**: `reference` holds
the Stripe invoice number, `source` is `Stripe`, and `source_url` is the Stripe
dashboard URL carrying the Stripe ID. Credit notes become external sales invoices
with negative lines whose descriptions name the original invoice. Each line keeps
its Stripe tax rate and the configured revenue ledger account. A quantity is kept
when the unit price is exact in currency units; otherwise the line is collapsed to
quantity one with the original quantity in the description so totals stay exact.
With `line_periods: true` the Stripe service period is passed as the Moneybird
detail period, which defers the revenue over that period in the profit and loss
report; this is off by default because it is a bookkeeping policy.

Optional `sync_payments: true` records supported successful Stripe PaymentIntent
allocations as payments on the external sales invoice, on the configured
`payment_financial_account_id` with the Stripe invoice payment ID as transaction
identifier. It requires `receipt_ownership_confirmed: true`: use a dedicated
Stripe financial account in Moneybird and confirm no bank feed also books those
receipts. Payments are only supported in the administration's base currency,
because Moneybird needs a base amount and the worker never invents an exchange rate.

Payment import handles full and partial payments and verifies the remaining
unpaid amount. It rejects a PaymentIntent shared across invoices, overpayments,
customer balances, unsupported payment types, and unmatched external payments.
A credit note applied to an open invoice is imported but its settlement against
the invoice is left for accountant review. Post-payment credits, refunds,
disputes, and bad-debt treatment also require accountant review; the worker
imports credit documents but never invents refund cash movements.

Voided Stripe invoices and credit notes stop for review by default. With
`void_action: "delete"` the worker deletes an external sales invoice it created
itself, provided no payment is registered on it and it was not adopted from a
manual entry. Uncollectible invoices stop for review by default; with
`uncollectible_action: "mark"` the worker marks the external sales invoice as
uncollectible once.

`reconcile` emits Stripe balance-transaction totals by currency and reporting
category. It is a read-only settlement report, not an automatic Moneybird bank
reconciler. Fees, payouts, refund cash, and disputes remain owned by the existing
accounting process. No fallback tax, write-off, or balancing line is invented.

Supported taxes are mapped Moneybird sales tax rates with the same percentage as
the Stripe tax rate. Stripe inclusive prices are normalized to net values. A line
with more than one Stripe tax rate, shipping, billing credit grants, Connect
issuers/transfers, unsupported rounding/precision, and unknown tax treatments
stop the affected invoice for review. Other invoices continue, but the run exits
unsuccessfully so partial failure remains visible.

Moneybird allows 150 requests per five minutes per administration. The worker
waits for `Retry-After` on 429 responses (a throttled request is never
committed), indexes the Stripe-sourced external sales invoices once per run, and
caches each document read within a run.

## Configuration

Copy `config.example.json` to ignored `config.local.json` for local work. The
example's IDs and date are placeholders, not an accounting configuration.
Moneybird IDs are strings. Fill in:

| Setting | Meaning |
| --- | --- |
| `stripe_account_id`, `livemode` | Expected Stripe account and test/live mode; both are checked even for an empty account. |
| `start_date` | Approved invoice **creation-date** cutoff. Do not move it forward to hide failures. Earlier issued invoices require a separate migration decision. |
| `timezone` | Time zone used to turn Stripe timestamps into Moneybird dates. |
| `administration_id` | The Moneybird administration the token must have access to. |
| `revenue_ledger_account_id` | Default revenue ledger account for invoice lines. |
| `currencies` | Currency code to number of decimals, e.g. `{"EUR": 2}`. Only explicitly configured currencies are accepted. |
| `tax_map` | Stripe tax-rate ID to Moneybird sales tax-rate ID. |
| `no_tax` | Stripe exemption treatment (`none`, `exempt`, `reverse`) to a zero-percentage Moneybird tax rate. `none` covers ordinary untaxed sales. |
| `contact_map` | Optional Stripe customer ID to an existing Moneybird contact ID. Use for migration matches and changed historical customer snapshots. |
| `document_map` | Optional Stripe invoice/credit-note ID to an existing external sales invoice ID, to resolve ambiguous migration matches. Financial verification still applies. |
| `ledger_account_map` | Optional Stripe product ID to a revenue ledger account ID; otherwise the default account is used. |
| `default_contact_country` | Moneybird requires a country on each contact. Stripe customers without a billing country stop for review unless this two-letter code is set. |
| `line_periods` | Pass Stripe service periods as Moneybird detail periods; false by default. |
| `sync_pdfs` | Preserve original invoice and credit-note PDFs as attachments; true by default. |
| `sync_payments`, `receipt_ownership_confirmed`, `payment_financial_account_id` | Payment import opt-in, its ownership confirmation and the Stripe financial account. |
| `void_action` | `review` (default) or `delete`. |
| `uncollectible_action` | `review` (default) or `mark`. |

Contacts first use explicit mappings and the Stripe customer ID stored as the
Moneybird `customer_id` of contacts the worker created. Otherwise, the worker
searches contacts, including archived ones, by e-mail, VAT number and name. A
unique match requires the same normalized name plus VAT number or e-mail, without
conflicting VAT numbers. Name-only, e-mail-only, conflicting, ambiguous, and
archived matches stop for review; no replacement contact is created. It creates a
new contact only when no possible existing contact is found.

Auto-created contacts use the finalized invoice's name, address, e-mail and EU
VAT ID as company contacts. The first snapshot is frozen. A later differing
snapshot requires an explicit `contact_map`. Existing contacts are reused without
overwriting their details. Original PDF bytes are uploaded as attachments on the
corresponding external sales invoice, including matched existing invoices. A
saved attachment is recognised by its filename `Stripe-<Stripe ID>.pdf`; an
existing attachment named `Invoice-<Stripe invoice number>.pdf` on a matched
invoice is reused. Downloads are limited to 8 MiB and checked for a PDF header
before creating accounting records. Missing or altered saved attachments stop for
review.

Existing documents first use saved links or the deterministic `source_url`. For
manually entered external sales invoices, automatic matching requires a unique
match on contact, issue date, currency, net amount, and total. The worker then
verifies the complete multiset of line net amounts, ledger accounts and tax
rates. It permits different descriptions, line order, and collapsed quantities
in a manual invoice when those financial details agree. Ambiguous or incompatible
candidates stop for review instead of creating another invoice. An explicit
`document_map` resolves ambiguity but cannot bypass financial verification.
Moneybird's own sales invoices (issued by Moneybird) are never adopted.

Contact and document links are stored in SQLite, including during dry runs.
Each Moneybird document can belong to only one Stripe document within this
ledger. Every replay revalidates the linked target; a missing or changed target
never falls back to creating a replacement. Uncertain write intents cannot be
overridden by migration matching. Preserve and back up the ledger for scheduled
runs. Adopted invoices and credits are never automatically paid, deleted or
marked uncollectible.

Set `STRIPE_API_KEY` and `MONEYBIRD_API_TOKEN` through the environment. Stripe
requires read access to Account, Balance, Invoices/lines, Credit Notes/lines,
Tax Rates, Invoice Payments, PaymentIntents when importing payments, and Balance
Transactions when running settlement reports. The Moneybird token needs the
`sales_invoices` scope for contacts, external sales invoices, tax rates and
ledger accounts, and `settings` for financial accounts when importing payments.

## Commands

Run from this directory after supplying credentials and an approved configuration:

```sh
python -m stripe_moneybird sync --config config.local.json --state ./state/ledger.sqlite3
python -m stripe_moneybird sync --mode apply --config config.local.json --state ./state/ledger.sqlite3
python -m stripe_moneybird backfill --from-date 2026-01-01 --config config.local.json --state ./state/ledger.sqlite3
python -m stripe_moneybird reconcile --config config.local.json --state ./state/ledger.sqlite3
python -m stripe_moneybird status --state ./state/ledger.sqlite3
python -m stripe_moneybird backup --state ./state/ledger.sqlite3 --output ./state/snapshot.sqlite3
```

`backfill` uses the same importer. Its optional narrower boundary cannot precede
the configured cutoff. Omit it to scan the full approved period. `status` and
`backup` require no API credentials. A nonblocking file lock protects normal runs,
manual jobs, status, recovery, and backup snapshots from concurrent state access.

## Recovery and state retention

SQLite binds the state volume to Stripe account, test/live mode and Moneybird
administration. It stores durable write intents before Moneybird mutations and
records target IDs before verification. Replays verify existing records instead
of creating duplicates. A lost create response is recovered if the exact
reference (`source_url`, contact `customer_id`, attachment filename, payment
transaction identifier) is found and its accounting fields agree. If a previous
write may still complete and no result is found, the worker stops that operation
for review. A local rollback cannot undo a Moneybird write.

Use `status` to inspect incomplete operations. Correct mappings or fix the
Moneybird document causing verification errors before rerunning. Do not delete
the ledger, remove reference fields, or reset operation rows to force a retry.

If Moneybird shows the result, rerun normally to recover it. If the operation has
no result, an operator must verify that the original request has finished without
any accounting effect before permitting another attempt:

```sh
python -m stripe_moneybird resolve --state ./state/ledger.sqlite3 \
  --reference 'EXACT_REFERENCE_FROM_STATUS' \
  --reason 'Verified the original Moneybird request finished without creating a record' \
  --confirm-request-finished-without-effect
```

The confirmation permits retry of one unresolved intent and retains an audit
reason. It does not run the sync. Created or successfully verified records cannot
be reset this way. Target-side reference searches are not a database uniqueness
constraint; preserve the ledger and ensure all invocations share its lock.

Backups use SQLite's online backup API and an integrity check. Restore while all
jobs are suspended and no pod is using the volume. Preserve the damaged state
for investigation. A restored ledger can predate Moneybird: the importer probes
target references before creating anything, and uncertain or inconsistent records
still require review. Treat the state and its backups as operational accounting data.

## Verification

From the repository root:

```sh
python3 -m unittest discover -s tests -v
uvx ruff@0.17.1 check .
uvx ruff@0.17.1 format --check .
docker build -t stripe-moneybird-sync:test .
docker run --rm --read-only stripe-moneybird-sync:test --help
```

These tests cover local accounting normalization, migration matching, contact
reuse, ambiguous/changed/deleted targets, replay and crash recovery, partial
payments, rate limiting, pagination, locks, and backup integrity. They run
against an in-memory Moneybird stand-in and do not replace accounting
verification against a real administration.

## Image releases

The included workflow checks pull requests and pushes to `main`. Publishing is
an explicit manual workflow run with `publish` enabled. It builds amd64 and arm64
images under `ghcr.io/resoftware/stripe-moneybird-sync`, tagged with `VERSION`
and `sha-<full Git commit>`. Pushes to `main` only check and build locally in CI.

Pin the production overlay to a published commit tag or image digest. The
version tag is a build alias; do not rely on it as immutable. Local tests never
push an image.
