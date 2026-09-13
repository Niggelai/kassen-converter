import csv
import tempfile
import unittest
import zipfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kassen_converter as kc


DATEV_XML = """<?xml version="1.0" encoding="utf-8"?>
<LedgerImport xmlns="http://xml.datev.de/bedi/tps/document/v05.0" version="5.0">
  <consolidate consolidatedCurrencyCode="EUR" consolidatedDate="2026-06-01" consolidatedAmount="10.00">
    <accountsReceivableLedger>
      <date>2026-06-01T12:34:56</date>
      <amount>10.00</amount>
      <accountNo>8401</accountNo>
      <tax>19.00</tax>
      <information>Testartikel</information>
      <currencyCode>EUR</currencyCode>
      <invoiceId>2-1</invoiceId>
      <bookingText>Bar</bookingText>
    </accountsReceivableLedger>
  </consolidate>
</LedgerImport>
"""

DOCUMENT_XML = """<?xml version="1.0" encoding="utf-8"?>
<archive xmlns="http://xml.datev.de/bedi/tps/document/v05.0" version="5.0">
  <content />
</archive>
"""


class ConverterTests(unittest.TestCase):
    def test_next_available_uses_windows_style_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            base = directory / "test.csv"
            base.write_text("x", encoding="utf-8")
            (directory / "test (1).csv").write_text("x", encoding="utf-8")
            self.assertEqual(kc.next_available(base).name, "test (2).csv")

    def test_end_to_end_with_tip(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            datev_zip = directory / "DATEV_XML_Export_test.zip"
            with zipfile.ZipFile(datev_zip, "w") as zf:
                zf.writestr("document.xml", DOCUMENT_XML)
                zf.writestr("receipt.xml", DATEV_XML)

            extf_zip = directory / "EXTF_Buchungsstapel_test.zip"
            extf_lines = [
                '"EXTF";510;21;"Buchungsstapel"',
                '"Umsatz (ohne Soll/Haben-Kz)";"Soll/Haben";"Konto";"Belegfeld 1";"Buchungstext"',
                '"12,00";"S";"1362";"2-1";"Bar"',
                '"2,00";"H";"1369";"2-1";"Trinkgeld"',
                '"10,00";"H";"8401";"2-1";"Testartikel"',
            ]
            with zipfile.ZipFile(extf_zip, "w") as zf:
                zf.writestr(
                    "EXTF_Einzel_Buchungsstapel_test.csv",
                    "\r\n".join(extf_lines).encode("cp1252"),
                )

            full_path, receipt_path = kc.run(directory)

            self.assertTrue(full_path.exists())
            self.assertTrue(receipt_path.exists())
            with receipt_path.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh, delimiter=";"))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["Brutto gesamt"], "10,00")
            self.assertEqual(row["Trinkgeld"], "2,00")
            self.assertEqual(row["Bar"], "12,00")
            self.assertEqual(row["Karte"], "0,00")
            self.assertEqual(row["Differenz"], "0,00")
            self.assertEqual(row["Pruefhinweis"], "")


if __name__ == "__main__":
    unittest.main()
