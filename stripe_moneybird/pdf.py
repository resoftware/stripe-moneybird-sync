"""Preserve original PDFs as Moneybird attachments, avoiding repeat uploads."""

import re
import urllib.error
import urllib.parse
import urllib.request

from .core import USER_AGENT, SyncError, UnknownOutcome, digest, require
from .planning import reference

MAX_PDF_BYTES = 8 * 1024 * 1024


def allowed_url(url, redirect=False):
    parsed = urllib.parse.urlsplit(url or "")
    host = parsed.hostname or ""
    stripe = host == "stripe.com" or host.endswith(".stripe.com")
    storage = bool(re.fullmatch(r"stripe-upload-api\.s3\.[a-z0-9-]+\.amazonaws\.com", host))
    require(
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and parsed.port in (None, 443)
        and (stripe or redirect and storage),
        "untrusted_stripe_pdf_url",
    )


def download_pdf(http, url):
    require(isinstance(url, str) and bool(url), "source_pdf_not_available")
    for hop in range(5):
        allowed_url(url, redirect=hop > 0)
        for attempt in range(3):
            try:
                # Use the no-redirect opener; validate each hop and never send API credentials.
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with http.opener.open(request, timeout=45) as response:
                    data = response.read(MAX_PDF_BYTES + 1)
                require(len(data) <= MAX_PDF_BYTES, "pdf_too_large")
                require(data.startswith(b"%PDF-"), "response_is_not_pdf")
                return data
            except urllib.error.HTTPError as error:
                if error.code in (301, 302, 303, 307, 308):
                    location = error.headers.get("Location")
                    require(bool(location), "pdf_redirect_without_location")
                    url = urllib.parse.urljoin(url, location)
                    break
                if error.code in (429, 500, 502, 503, 504) and attempt < 2:
                    http.sleep(2**attempt)
                    continue
                raise SyncError(f"pdf_http_{error.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < 2:
                    http.sleep(2**attempt)
                    continue
                raise UnknownOutcome("pdf_download_failed") from None
    raise SyncError("too_many_pdf_redirects")


def validate_pdf(data):
    require(len(data) <= MAX_PDF_BYTES and data.startswith(b"%PDF-"), "invalid_pdf_document")


class PDFStore:
    def __init__(self, worker):
        self.worker = worker
        self.config, self.moneybird, self.ledger = worker.config, worker.moneybird, worker.ledger

    def attachments(self, document_id, filenames):
        document = self.worker.read(document_id, fresh=True)
        require(document is not None, "pdf_document_missing")
        found = [
            item
            for item in document.get("attachments") or []
            if item.get("filename") in filenames and item.get("content_type") == "application/pdf"
        ]
        require(len(found) <= 1, "ambiguous_existing_pdf_attachment")
        return found[0] if found else None

    def sync(self, source, document_id, data, apply):
        validate_pdf(data)
        kind = "credit-pdf" if source.get("object") == "credit_note" else "invoice-pdf"
        ref = reference(self.config, kind, source["id"])
        operation = self.ledger.get(ref)
        own = f"Stripe-{source['id']}.pdf"
        # Stripe's own download name is reused on adopted invoices with a manually saved PDF.
        filenames = (own, f"Invoice-{source['number']}.pdf")

        def read(identifier):
            attachment = self.attachments(document_id, filenames)
            require(attachment is not None and str(attachment["id"]) == identifier, "pdf_attachment_missing")
            return attachment

        existing = None
        if operation and operation["target_id"]:
            existing = read(operation["target_id"])
        elif document_id is not None:
            existing = self.attachments(document_id, filenames)
        # Keep the original attachment; Stripe can regenerate PDF metadata between runs.
        size = existing.get("size") if existing else len(data)
        fingerprint = digest({"document_id": document_id, "size": size})
        if not apply:
            return (str(existing["id"]) if existing else None), "reused" if existing else "previewed"

        def verify(identifier):
            require(read(identifier).get("size") == size, "pdf_attachment_content_mismatch")

        def create():
            self.moneybird.upload(f"/external_sales_invoices/{document_id}/attachment", own, data)
            attachment = self.attachments(document_id, (own,))
            require(attachment is not None, "pdf_attachment_result_not_found")
            return attachment["id"]

        identifier = self.ledger.ensure(
            ref, fingerprint, lambda: existing["id"] if existing else None, create, verify
        )
        return identifier, "reused" if existing else "saved"
