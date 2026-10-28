from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

USER_AGENT = "stripe-moneybird-sync/1.0"


class SyncError(Exception):
    pass


class Rejected(SyncError):
    """The server explicitly rejected a request without committing it."""


class UnknownOutcome(SyncError):
    pass


def require(condition, code):
    if not condition:
        raise SyncError(code)


def log(event, **fields):
    print(json.dumps({"time": dt.datetime.now(dt.UTC).isoformat(), "event": event, **fields}), flush=True)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def object_id(value):
    return value.get("id") if isinstance(value, dict) else value


def date_of(timestamp, timezone):
    from zoneinfo import ZoneInfo

    return dt.datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).date().isoformat()


def minor(value, decimals):
    """Convert a Moneybird decimal string to integer currency units, refusing sub-unit values."""
    try:
        amount = Decimal(str(value)) * (Decimal(10) ** decimals)
    except InvalidOperation:
        raise SyncError("moneybird_amount_not_decimal") from None
    rounded = amount.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    require(abs(amount - rounded) < Decimal("0.001"), "moneybird_amount_not_in_currency_units")
    return int(rounded)


def major(value, decimals):
    """Render integer currency units as the decimal string Moneybird expects."""
    return str(Decimal(value).scaleb(-decimals).quantize(Decimal(1).scaleb(-decimals)))


@contextlib.contextmanager
def state_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("another_run_holds_state_lock") from None
        yield


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
                reference TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, target_id TEXT, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, started_at INTEGER NOT NULL,
                finished_at INTEGER, mode TEXT NOT NULL, status TEXT NOT NULL,
                summary TEXT);
            CREATE TABLE IF NOT EXISTS resolutions (
                id INTEGER PRIMARY KEY, reference TEXT NOT NULL,
                resolved_at INTEGER NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS document_links (
                reference TEXT PRIMARY KEY, target_id TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL, linked_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS contact_links (
                reference TEXT PRIMARY KEY, target_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, linked_at INTEGER NOT NULL);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def bind(self, scope):
        encoded = json.dumps(scope, sort_keys=True)
        row = self.db.execute("SELECT value FROM settings WHERE key='scope'").fetchone()
        if row:
            require(row[0] == encoded, "ledger_scope_mismatch")
        else:
            with self.db:
                self.db.execute("INSERT INTO settings VALUES ('scope', ?)", (encoded,))

    def get(self, ref):
        return self.db.execute("SELECT * FROM operations WHERE reference=?", (ref,)).fetchone()

    def link(self, kind, ref, target_id, fingerprint):
        require(kind in ("document", "contact"), "invalid_link_kind")
        target_id = str(target_id)
        previous = self.linked(kind, ref)
        if previous:
            require(previous["target_id"] == target_id, "existing_link_target_changed")
            require(previous["fingerprint"] == fingerprint, "existing_link_payload_changed")
            return
        if kind == "document":
            claimed = self.db.execute(
                "SELECT reference FROM document_links WHERE target_id=?", (target_id,)
            ).fetchone()
            require(not claimed, "moneybird_document_already_linked_to_other_source")
            operations = self.db.execute(
                "SELECT reference FROM operations WHERE target_id=?", (target_id,)
            ).fetchall()
            require(
                not any(
                    (":invoice:" in row[0] or ":credit:" in row[0])
                    and row[0] != ref
                    and not row[0].startswith(ref + ":")
                    for row in operations
                ),
                "moneybird_document_already_owned_by_other_source",
            )
        with self.db:
            self.db.execute(
                f"INSERT INTO {kind}_links VALUES (?, ?, ?, ?)",
                (ref, target_id, fingerprint, int(time.time())),
            )

    def linked(self, kind, ref):
        require(kind in ("document", "contact"), "invalid_link_kind")
        return self.db.execute(f"SELECT * FROM {kind}_links WHERE reference=?", (ref,)).fetchone()

    def put(self, ref, fingerprint, status, target_id=None):
        with self.db:
            self.db.execute(
                """INSERT INTO operations VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(reference) DO UPDATE SET fingerprint=excluded.fingerprint,
                status=excluded.status, target_id=excluded.target_id, updated_at=excluded.updated_at""",
                (ref, fingerprint, status, None if target_id is None else str(target_id), int(time.time())),
            )

    def ensure(self, ref, fingerprint, lookup, create, verify):
        operation = self.get(ref)
        if operation and operation["status"] != "rejected":
            require(operation["fingerprint"] == fingerprint, "operation_payload_changed")
        target = lookup()
        if target:
            target = str(target)
            verify(target)
            if operation and operation["target_id"]:
                require(target == operation["target_id"], "operation_target_changed")
            self.put(ref, fingerprint, "succeeded", target)
            return target
        if operation and operation["status"] != "rejected":
            raise UnknownOutcome("operation_result_missing_review_required")
        self.put(ref, fingerprint, "submitted")
        try:
            target = str(create())
        except Rejected:
            self.put(ref, fingerprint, "rejected")
            raise
        except Exception:
            self.put(ref, fingerprint, "unknown")
            raise UnknownOutcome("moneybird_write_outcome_unknown") from None
        # Persist the ID before verification so an amount mismatch is recoverable without another create.
        self.put(ref, fingerprint, "created", target)
        verify(target)
        self.put(ref, fingerprint, "succeeded", target)
        return target

    def permit_retry(self, ref, reason):
        operation = self.get(ref)
        require(
            operation is not None
            and operation["status"] in ("unknown", "submitted")
            and operation["target_id"] is None,
            "only_unresolved_writes_can_be_reset",
        )
        require(bool(reason.strip()), "resolution_reason_required")
        with self.db:
            self.db.execute(
                "INSERT INTO resolutions (reference, resolved_at, reason) VALUES (?, ?, ?)",
                (ref, int(time.time()), reason),
            )
            self.db.execute(
                "UPDATE operations SET status='rejected', updated_at=? WHERE reference=?",
                (int(time.time()), ref),
            )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_after(headers, attempt):
    try:
        value = int(headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        value = 2**attempt
    return min(max(value, 1), 300)


class Http:
    def __init__(self, opener=None, sleep=time.sleep):
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.sleep = sleep

    def request(self, url, headers, payload=None, safe=False, method=None, body=None):
        if body is None and payload is not None:
            body = json.dumps(payload, allow_nan=False).encode()
            headers = {**headers, "Content-Type": "application/json"}
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
        attempts = 3 if safe else 1
        # A 429 is never committed, so writes may also wait and retry a few times.
        throttled = 0
        attempt = 0
        while True:
            try:
                request = urllib.request.Request(url, data=body, headers=headers, method=method)
                with self.opener.open(request, timeout=45) as response:
                    data = response.read(16 * 1024 * 1024 + 1)
                require(len(data) <= 16 * 1024 * 1024, "response_too_large")
                return json.loads(data) if data.strip() else None
            except urllib.error.HTTPError as error:
                # Never include a response body: it can contain invoice PII, API keys, or tracebacks.
                if error.code == 429 and throttled < 5:
                    throttled += 1
                    self.sleep(retry_after(error.headers, throttled))
                    continue
                if safe and error.code in (500, 502, 503, 504) and attempt < attempts - 1:
                    attempt += 1
                    self.sleep(2**attempt)
                    continue
                if 400 <= error.code < 500 and error.code != 408:
                    raise Rejected(f"remote_http_{error.code}") from None
                raise UnknownOutcome(f"remote_http_{error.code}_outcome_unknown") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if safe and attempt < attempts - 1:
                    attempt += 1
                    self.sleep(2**attempt)
                    continue
                raise UnknownOutcome("remote_transport_outcome_unknown") from None
            except (json.JSONDecodeError, ValueError):
                raise UnknownOutcome("remote_response_invalid") from None


class Stripe:
    def __init__(self, key, account_id, live, http):
        self.http, self.account_id, self.live = http, account_id, live
        self.headers = {"Authorization": f"Bearer {key}", "Stripe-Version": "2025-03-31.basil"}

    def get(self, path, params=None):
        require(path.startswith("/v1/") and "?" not in path and ".." not in path, "invalid_stripe_path")
        query = urllib.parse.urlencode(params or {}, doseq=True)
        result = self.http.request(
            "https://api.stripe.com" + path + ("?" + query if query else ""), self.headers, safe=True
        )
        if "livemode" in result:
            require(result["livemode"] == self.live, "stripe_mode_mismatch")
        return result

    def list(self, path, params=None):
        params = {**(params or {}), "limit": 100}
        previous = None
        while True:
            page = self.get(path, params)
            require(isinstance(page.get("data"), list), "invalid_stripe_page")
            for item in page["data"]:
                if "livemode" in item:
                    require(item["livemode"] == self.live, "stripe_mode_mismatch")
                yield item
            if not page.get("has_more"):
                break
            require(bool(page["data"]), "empty_stripe_page_with_more")
            cursor = page["data"][-1]["id"]
            require(cursor != previous, "stripe_pagination_not_advancing")
            params["starting_after"] = cursor
            previous = cursor

    def check(self):
        require(self.get("/v1/account")["id"] == self.account_id, "stripe_account_mismatch")
        # Account objects need not expose livemode. Balance does and verifies even an empty account.
        require(self.get("/v1/balance")["livemode"] == self.live, "stripe_mode_mismatch")

    def pdf(self, source):
        from .pdf import download_pdf

        return download_pdf(self.http, source.get("pdf") or source.get("invoice_pdf"))


def multipart(fields, filename, data):
    boundary = "----stripe-moneybird-" + secrets.token_hex(16)
    body = b""
    for key, value in fields.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode()
    body += data + f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


class Moneybird:
    """Thin Moneybird API v2 client. Paths are relative to the administration."""

    base = "https://moneybird.com/api/v2"

    def __init__(self, administration_id, token, http):
        require(type(administration_id) is str and administration_id.isdigit(), "configure_administration_id")
        self.administration_id, self.http = administration_id, http
        self.headers = {"Authorization": f"Bearer {token}"}

    def url(self, path, params=None):
        require(
            path.startswith("/") and "?" not in path and ".." not in path and not path.endswith(".json"),
            "invalid_moneybird_path",
        )
        query = urllib.parse.urlencode(params or {})
        return f"{self.base}/{self.administration_id}{path}.json" + ("?" + query if query else "")

    def get(self, path, params=None):
        return self.http.request(self.url(path, params), self.headers, safe=True)

    def find(self, path, params=None):
        """GET that treats 404 as absence; other rejections still propagate."""
        try:
            return self.get(path, params)
        except Rejected as error:
            if str(error) == "remote_http_404":
                return None
            raise

    def list(self, path, params=None):
        page = 1
        while True:
            items = self.get(path, {**(params or {}), "page": page, "per_page": 100})
            require(isinstance(items, list), "invalid_moneybird_page")
            yield from items
            if len(items) < 100:
                break
            page += 1
            require(page <= 1000, "moneybird_pagination_runaway")

    def post(self, path, payload):
        return self.http.request(self.url(path), self.headers, payload, method="POST")

    def patch(self, path, payload=None):
        return self.http.request(self.url(path), self.headers, payload or {}, method="PATCH")

    def delete(self, path):
        return self.http.request(self.url(path), self.headers, method="DELETE")

    def upload(self, path, filename, data):
        content_type, body = multipart({}, filename, data)
        return self.http.request(
            self.url(path), {**self.headers, "Content-Type": content_type}, method="POST", body=body
        )

    def administration(self):
        administrations = self.http.request(f"{self.base}/administrations.json", self.headers, safe=True)
        require(isinstance(administrations, list), "invalid_moneybird_administrations")
        matches = [item for item in administrations if str(item.get("id")) == self.administration_id]
        require(len(matches) == 1, "moneybird_administration_not_accessible")
        return matches[0]


def identifier(value):
    return type(value) is str and value.isdigit()


def load_config(path):
    config = json.loads(Path(path).read_text())
    required = (
        "stripe_account_id",
        "livemode",
        "start_date",
        "timezone",
        "administration_id",
        "revenue_ledger_account_id",
        "currencies",
        "tax_map",
        "no_tax",
    )
    require(all(key in config for key in required), "configuration_missing_fields")
    require(type(config["livemode"]) is bool, "invalid_livemode")
    require(
        config["stripe_account_id"].startswith("acct_") and "REPLACE" not in config["stripe_account_id"],
        "configure_stripe_account_id",
    )
    for key in ("administration_id", "revenue_ledger_account_id"):
        require(identifier(config[key]), f"configure_{key}")
    for key in ("tax_map", "tax_rate_map", "no_tax", "contact_map", "document_map", "ledger_account_map"):
        mapping = config.get(key, {})
        require(
            isinstance(mapping, dict) and all(identifier(value) for value in mapping.values()),
            f"invalid_{key}",
        )
    require(set(config["no_tax"]) <= {"none", "exempt", "reverse"}, "invalid_no_tax_treatment")
    require(
        all(re.fullmatch(r"[A-Z]{2}:\d+(\.\d+)?", key) for key in config.get("tax_rate_map", {})),
        "invalid_tax_rate_map_key",
    )
    require(type(config.get("period_separator", "to")) is str, "invalid_period_separator")
    rate = config.get("untaxed_eu_tax_rate_id")
    require(rate is None or identifier(rate), "invalid_untaxed_eu_tax_rate_id")
    dt.date.fromisoformat(config["start_date"])
    from zoneinfo import ZoneInfo

    ZoneInfo(config["timezone"])
    require(
        isinstance(config["currencies"], dict)
        and config["currencies"]
        and all(
            len(code) == 3 and code.isupper() and type(decimals) is int and 0 <= decimals <= 3
            for code, decimals in config["currencies"].items()
        ),
        "configure_currencies",
    )
    for key in (
        "sync_payments",
        "receipt_ownership_confirmed",
        "sync_pdfs",
        "line_periods",
        "skip_zero_total",
    ):
        require(type(config.get(key, key == "sync_pdfs")) is bool, f"invalid_{key}")
    require(config.get("void_action", "review") in ("review", "delete"), "invalid_void_action")
    require(
        config.get("uncollectible_action", "review") in ("review", "mark"), "invalid_uncollectible_action"
    )
    country = config.get("default_contact_country")
    require(
        country is None or (type(country) is str and len(country) == 2), "invalid_default_contact_country"
    )
    if config.get("sync_payments"):
        require(config.get("receipt_ownership_confirmed") is True, "confirm_receipt_import_ownership")
        require(
            identifier(config.get("payment_financial_account_id")), "configure_payment_financial_account_id"
        )
    return config
