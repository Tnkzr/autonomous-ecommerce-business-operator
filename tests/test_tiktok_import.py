"""Credential abstraction and Seller Center CSV import.

Two things these tests exist to protect:

  - The whole system must be constructible and testable with no credentials at
    all. "Not provisioned yet" is a state to wait in, not an error to handle.
  - An importer that silently reads zero rows after TikTok renames a column is
    worse than one that stops. Header drift must be loud.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.tiktok import (
    ChainedCredentials,
    CredentialsUnavailable,
    EnvCredentials,
    FileCredentials,
    StaticCredentials,
    TikTokCredentials,
    TikTokShopConnector,
    UnavailableCredentials,
    daily_performance_from_orders,
    default_source,
    detect_export_kind,
    import_orders,
    import_products,
    import_settlements,
    parse_money,
    parse_int,
)

FULL_ENV = {
    "TIKTOK_APP_KEY": "k", "TIKTOK_APP_SECRET": "s",
    "TIKTOK_REFRESH_TOKEN": "r", "TIKTOK_SHOP_ID": "7000",
}


def write_csv(rows: list[dict], headers: list[str] | None = None) -> Path:
    path = Path(tempfile.mkdtemp()) / "export.csv"
    headers = headers or list(rows[0])
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)
    return path


class TestCredentialSources(unittest.TestCase):
    def test_unavailable_is_a_first_class_state(self):
        # The system must be constructible without credentials existing.
        src = UnavailableCredentials("API registration is gated.")
        self.assertFalse(src.available)
        self.assertIn("gated", src.status().detail)
        with self.assertRaises(CredentialsUnavailable):
            src.resolve()

    def test_unavailable_names_the_remedy(self):
        with self.assertRaises(CredentialsUnavailable) as ctx:
            UnavailableCredentials().resolve()
        self.assertIn("CSV export", str(ctx.exception))

    def test_env_source_reports_missing_fields(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            st = EnvCredentials().status()
        self.assertFalse(st.available)
        self.assertIn("TIKTOK_APP_KEY", st.detail)

    def test_env_source_resolves_when_complete(self):
        with mock.patch.dict(os.environ, FULL_ENV):
            creds = EnvCredentials().resolve()
        self.assertEqual(creds.app_key, "k")

    def test_file_source_round_trips(self):
        path = Path(tempfile.mkdtemp()) / "creds.json"
        path.write_text(json.dumps({
            "app_key": "k", "app_secret": "s",
            "refresh_token": "old", "shop_id": "7000"}))
        src = FileCredentials(path)
        self.assertTrue(src.available)
        self.assertEqual(src.resolve().refresh_token, "old")

    def test_file_source_persists_rotated_token(self):
        # TikTok rotates on every refresh; a lost rotation strands the
        # integration months later with no deploy to correlate against.
        path = Path(tempfile.mkdtemp()) / "creds.json"
        path.write_text(json.dumps({
            "app_key": "k", "app_secret": "s",
            "refresh_token": "old", "shop_id": "7000"}))
        src = FileCredentials(path)
        src.persist_refresh_token("new")
        self.assertEqual(FileCredentials(path).resolve().refresh_token, "new")

    def test_malformed_file_is_not_treated_as_absent(self):
        # Falling back to "no credentials" would silently drop access.
        path = Path(tempfile.mkdtemp()) / "creds.json"
        path.write_text("{not json")
        with self.assertRaises(CredentialsUnavailable):
            FileCredentials(path).status()

    def test_chain_prefers_the_first_available(self):
        good = StaticCredentials(TikTokCredentials("k", "s", "r", "7000"))
        chain = ChainedCredentials(UnavailableCredentials(), good)
        self.assertTrue(chain.available)
        self.assertEqual(chain.resolve().app_key, "k")

    def test_chain_reports_every_attempt_when_none_work(self):
        chain = ChainedCredentials(EnvCredentials(), UnavailableCredentials())
        with mock.patch.dict(os.environ, {}, clear=True):
            detail = chain.status().detail
        self.assertIn("environment", detail)
        self.assertIn("unavailable", detail)

    def test_default_source_never_raises_at_construction(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            src = default_source()
            self.assertFalse(src.available)

    def test_connector_accepts_an_injected_source(self):
        # Swapping where credentials come from must not touch the connector.
        conn = TikTokShopConnector(
            credentials=StaticCredentials(TikTokCredentials("k", "s", "r", "7000")))
        self.assertTrue(conn._credentials.available)

    def test_verify_reports_credential_state_not_a_crash(self):
        conn = TikTokShopConnector(credentials=UnavailableCredentials())
        result = conn.verify_connection()
        self.assertFalse(result["ok"])
        self.assertEqual(result["credential_source"], "unavailable")
        self.assertIn("remedy", result)

    def test_secrets_absent_from_repr(self):
        src = StaticCredentials(TikTokCredentials("key", "SUPERSECRET", "RT", "1"))
        self.assertNotIn("SUPERSECRET", repr(src))


class TestMoneyParsing(unittest.TestCase):
    def test_currency_symbols_stripped(self):
        self.assertEqual(parse_money("US$12.34"), 12.34)
        self.assertEqual(parse_money("£9.99"), 9.99)

    def test_thousands_separator(self):
        self.assertEqual(parse_money("1,234.56"), 1234.56)

    def test_european_decimal_comma(self):
        self.assertEqual(parse_money("12,34"), 12.34)

    def test_dot_thousands_for_idr_and_de(self):
        # TikTok Shop operates in Indonesia and Germany, both dot-separated.
        self.assertEqual(parse_money("IDR 1.234.567"), 1234567.0)
        self.assertEqual(parse_money("1.234.567,89"), 1234567.89)

    def test_parenthesised_negative(self):
        self.assertEqual(parse_money("(4.50)"), -4.5)

    def test_empty_is_zero(self):
        for blank in ("", None, "-", "N/A"):
            self.assertEqual(parse_money(blank), 0.0)

    def test_garbage_raises_rather_than_zeroing(self):
        # A silently dropped amount puts a settlement reconciliation out by a
        # month of fees.
        with self.assertRaises(ValueError):
            parse_money("twelve dollars and one cent")

    def test_prose_between_digits_raises(self):
        # "1 of 2" is a column that isn't money at all — usually a header drift
        # that pointed the money field at the wrong column.
        with self.assertRaises(ValueError):
            parse_money("1 of 2")

    def test_currency_suffix_form(self):
        self.assertEqual(parse_money("12.34 USD"), 12.34)

    def test_quantity_garbage_raises(self):
        # A quantity that reads as 0 deletes a real sale from velocity.
        with self.assertRaises(ValueError):
            parse_int("two units")
        self.assertEqual(parse_int(""), 0)
        self.assertEqual(parse_int("1,024"), 1024)


class TestOrderImport(unittest.TestCase):
    def _rows(self, **overrides):
        base = {
            "Order ID": "577001", "Created Time": "2026-07-10 12:00:00",
            "Order Status": "Completed", "Seller SKU": "SKU-1",
            "Product Name": "Widget", "Quantity": "2",
            "Order Amount": "US$69.98", "Buyer Username": "buyer_a",
            "Currency": "USD",
        }
        base.update(overrides)
        return base

    def test_basic_import(self):
        path = write_csv([self._rows(), self._rows(**{"Order ID": "577002"})])
        result = import_orders(path)
        self.assertEqual(result.count, 2)
        self.assertEqual(result.rows[0]["quantity"], 2)
        self.assertAlmostEqual(result.rows[0]["order_total"], 69.98)

    def test_cancelled_orders_flagged_not_counted(self):
        path = write_csv([
            self._rows(),
            self._rows(**{"Order ID": "577002", "Order Status": "Cancelled"}),
        ])
        result = import_orders(path)
        self.assertTrue(result.rows[0]["is_sale"])
        self.assertFalse(result.rows[1]["is_sale"])
        self.assertTrue(any("inflate revenue" in w for w in result.warnings))

    def test_missing_required_column_is_loud(self):
        # Silently reading zero rows after a rename is worse than stopping.
        path = write_csv([{"Some Column": "x", "Another": "y"}])
        with self.assertRaises(ValueError) as ctx:
            import_orders(path)
        message = str(ctx.exception)
        self.assertIn("missing required column", message.lower())
        self.assertIn("Headers found", message)

    def test_header_aliases_are_accepted(self):
        # TikTok renames columns between export versions and regions.
        path = write_csv([{
            "order_id": "1", "create time": "2026-07-10", "status": "Completed",
            "sku": "SKU-1", "qty": "1", "total amount": "10.00",
        }])
        result = import_orders(path)
        self.assertEqual(result.count, 1)
        self.assertEqual(result.rows[0]["sku"], "SKU-1")

    def test_missing_buyer_column_is_reported(self):
        path = write_csv([{
            "Order ID": "1", "Created Time": "2026-07-10", "Order Status": "Completed",
            "Seller SKU": "S", "Quantity": "1", "Order Amount": "10",
        }])
        result = import_orders(path)
        self.assertTrue(any("repeat-purchase" in w for w in result.warnings))

    def test_bom_and_encoding_handled(self):
        path = write_csv([self._rows()])
        self.assertEqual(import_orders(path).count, 1)

    def test_daily_series_excludes_non_sales(self):
        path = write_csv([
            self._rows(),
            self._rows(**{"Order ID": "2", "Order Status": "Unpaid"}),
        ])
        series = daily_performance_from_orders(import_orders(path).rows)
        self.assertEqual(series["SKU-1"][0]["units"], 2)

    def test_daily_series_leaves_page_views_unmeasured(self):
        # An order export cannot know them; inventing them would fabricate a
        # conversion rate.
        path = write_csv([self._rows()])
        series = daily_performance_from_orders(import_orders(path).rows)
        self.assertEqual(series["SKU-1"][0]["page_views"], 0)


class TestSettlementImport(unittest.TestCase):
    def test_take_rate_computed(self):
        path = write_csv([{
            "Statement ID": "S1", "Statement Time": "2026-07-10",
            "Revenue": "1000.00", "Platform Commission": "(80.00)",
            "Affiliate Commission": "(120.00)", "Settlement Amount": "800.00",
            "Currency": "USD",
        }])
        result = import_settlements(path)
        self.assertEqual(result.rows[0]["fees"], 80.0)
        self.assertEqual(result.rows[0]["affiliate_commission"], 120.0)
        self.assertTrue(any("take rate" in w for w in result.warnings))

    def test_negative_fees_normalised_to_positive(self):
        path = write_csv([{
            "Statement ID": "S1", "Revenue": "100", "Fee": "-8",
            "Settlement Amount": "92",
        }])
        self.assertGreaterEqual(import_settlements(path).rows[0]["fees"], 0)


class TestProductImport(unittest.TestCase):
    def test_zero_stock_warned(self):
        path = write_csv([
            {"Product ID": "1", "Seller SKU": "A", "Product Name": "X",
             "Status": "Live", "Price": "10", "Stock": "0"},
        ])
        result = import_products(path)
        self.assertTrue(any("suppresses out-of-stock" in w for w in result.warnings))


class TestExportDetection(unittest.TestCase):
    def test_detects_orders(self):
        path = write_csv([{
            "Order ID": "1", "Created Time": "2026-07-10", "Seller SKU": "A",
            "Quantity": "1", "Order Amount": "10"}])
        self.assertEqual(detect_export_kind(path), "orders")

    def test_detects_settlements(self):
        path = write_csv([{
            "Statement ID": "1", "Statement Time": "2026-07-10",
            "Revenue": "100", "Settlement Amount": "80"}])
        self.assertEqual(detect_export_kind(path), "settlements")

    def test_unknown_file_is_unknown(self):
        path = write_csv([{"Colour": "red", "Shape": "round"}])
        self.assertEqual(detect_export_kind(path), "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
