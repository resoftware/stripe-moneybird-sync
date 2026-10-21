import io
import unittest
import urllib.error
from unittest.mock import Mock

import test_sync as fixtures

from stripe_moneybird.core import Http, SyncError
from stripe_moneybird.pdf import MAX_PDF_BYTES, download_pdf


def pdf(number="EX-1", metadata="first"):
    return f"%PDF-1.4\n% fixture {number} {metadata}\n%%EOF".encode()


class PDFDownloadTests(unittest.TestCase):
    def test_signed_stripe_storage_redirect_is_allowed_without_api_credentials(self):
        opener = Mock()
        opener.open.side_effect = [
            urllib.error.HTTPError(
                "https://pay.stripe.com/pdf",
                302,
                "redirect",
                {
                    "Location": "https://stripe-upload-api.s3.us-west-1.amazonaws.com/document.pdf?signature=test"
                },
                None,
            ),
            io.BytesIO(pdf()),
        ]
        self.assertTrue(download_pdf(Http(opener), "https://pay.stripe.com/pdf").startswith(b"%PDF-"))
        for call in opener.open.call_args_list:
            self.assertFalse(call.args[0].has_header("Authorization"))

    def test_foreign_redirect_and_non_https_sources_are_refused(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://pay.stripe.com/pdf", 302, "redirect", {"Location": "https://evil.test/pdf"}, None
        )
        with self.assertRaisesRegex(SyncError, "untrusted"):
            download_pdf(Http(opener), "https://pay.stripe.com/pdf")
        self.assertEqual(opener.open.call_count, 1)
        with self.assertRaisesRegex(SyncError, "untrusted"):
            download_pdf(Http(opener), "http://pay.stripe.com/pdf")

    def test_html_and_oversized_downloads_are_refused(self):
        for data in (b"<html>login</html>", b"%PDF-" + b"x" * MAX_PDF_BYTES):
            with self.subTest(size=len(data)):
                opener = Mock()
                opener.open.return_value = io.BytesIO(data)
                with self.assertRaises(SyncError):
                    download_pdf(Http(opener), "https://pay.stripe.com/pdf")


class PDFStoreTests(unittest.TestCase):
    worker = fixtures.WorkerTests.worker

    def setUp(self):
        fixtures.WorkerTests.setUp(self)
        self.settings["sync_pdfs"] = True
        self.stripe.pdf = lambda source: pdf(source["number"])

    def test_original_pdf_is_uploaded_once_and_verified_on_replay(self):
        result = self.worker().run()
        self.assertEqual(result["pdfs_saved"], 1)
        self.assertEqual(self.moneybird.creates, 2)  # Invoice plus original PDF.
        result = self.worker().run()
        self.assertEqual(result["pdfs_reused"], 1)
        self.assertEqual(self.moneybird.creates, 2)
        attachment = self.moneybird.invoices["11"]["attachments"][0]
        self.assertEqual(attachment["filename"], "Stripe-in_test.pdf")
        self.assertEqual(attachment["size"], len(pdf()))

    def test_existing_manual_pdf_with_different_metadata_is_reused_without_overwriting(self):
        fixtures.MatchingTests.setUp(self)
        self.settings["sync_pdfs"] = True
        self.stripe.pdf = lambda source: pdf(source["number"])
        target = fixtures.MatchingTests.external_invoice(self)
        self.moneybird.invoices[target]["attachments"].append(
            {
                "id": "77",
                "filename": "Invoice-EX-1.pdf",
                "content_type": "application/pdf",
                "size": len(pdf(metadata="manual-original")),
            }
        )
        before = dict(self.moneybird.invoices[target]["attachments"][0])
        self.assertEqual(self.worker().run()["pdfs_reused"], 1)
        self.assertEqual(self.moneybird.creates, 1)
        self.assertEqual(self.moneybird.invoices[target]["attachments"], [before])

    def test_dry_run_validates_pdf_without_uploading_or_recording_write_intents(self):
        self.assertEqual(self.worker(False).run()["pdfs_previewed"], 1)
        self.assertEqual(self.moneybird.creates, 0)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM operations").fetchone()[0], 0)

    def test_invalid_source_pdf_stops_before_contact_or_invoice_creation(self):
        self.stripe.pdf = lambda source: b"<html>expired link</html>"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 0)

    def test_lost_upload_response_recovers_without_another_attachment(self):
        self.moneybird.lose_upload_response = True
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.worker().run()["pdfs_reused"], 1)
        self.assertEqual(self.moneybird.creates, 2)
        self.assertEqual(len(self.moneybird.invoices["11"]["attachments"]), 1)

    def test_deleted_or_changed_saved_pdf_does_not_create_a_replacement(self):
        self.worker().run()
        self.moneybird.invoices["11"]["attachments"][0]["size"] += 1
        self.assertEqual(self.worker().run()["failed"], 1)
        self.moneybird.invoices["11"]["attachments"].clear()
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.moneybird.creates, 2)
