import csv
import tempfile
import unittest
import zipfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kassen_converter as kc


DOCUMENT_XML = """<?xml version="1.0" encoding="utf-8"?>
<archive xmlns="http://xml.datev.de/bedi/tps/document/v05.0" version="5.0"><content /></archive>
"""


def receipt_xml(
    receipt_id: str,
    booking_text: str = "Bar",
    amount: str = "10.00",
    item: str = "Testartikel",
    time: str = "12:34:56",
    vat: str = "19.00",
) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<LedgerImport xmlns="http://xml.datev.de/bedi/tps/document/v05.0" version="5.0">
  <consolidate consolidatedCurrencyCode="EUR" consolidatedDate="2026-06-01" consolidatedAmount="{amount}">
    <accountsReceivableLedger>
      <date>2026-06-01T{time}</date>
      <amount>{amount}</amount>
      <accountNo>8401</accountNo>
      <tax>{vat}</tax>
      <information>{item}</information>
      <currencyCode>EUR</currencyCode>
      <invoiceId>{receipt_id}</invoiceId>
      <bookingText>{booking_text}</bookingText>
    </accountsReceivableLedger>
  </consolidate>
</LedgerImport>
"""


def multi_position_receipt_xml() -> str:
    return """<?xml version="1.0" encoding="utf-8"?>
<LedgerImport xmlns="http://xml.datev.de/bedi/tps/document/v05.0" version="5.0">
  <consolidate consolidatedCurrencyCode="EUR" consolidatedDate="2026-06-05" consolidatedAmount="15.00">
    <accountsReceivableLedger>
      <date>2026-06-05T18:05:00</date><amount>5.00</amount><accountNo>8401</accountNo><tax>19.00</tax>
      <information>Wasser</information><currencyCode>EUR</currencyCode><invoiceId>2-20</invoiceId><bookingText>Bar</bookingText>
    </accountsReceivableLedger>
    <accountsReceivableLedger>
      <date>2026-06-05T18:15:00</date><amount>5.00</amount><accountNo>8401</accountNo><tax>19.00</tax>
      <information>Wasser</information><currencyCode>EUR</currencyCode><invoiceId>2-20</invoiceId><bookingText>Bar</bookingText>
    </accountsReceivableLedger>
    <accountsReceivableLedger>
      <date>2026-06-05T18:45:00</date><amount>5.00</amount><accountNo>8401</accountNo><tax>19.00</tax>
      <information>FFM-Riesling</information><currencyCode>EUR</currencyCode><invoiceId>2-20</invoiceId><bookingText>Bar</bookingText>
    </accountsReceivableLedger>
  </consolidate>
</LedgerImport>
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
            with zipfile.ZipFile(directory / "DATEV_XML_Export_test.zip", "w") as zf:
                zf.writestr("document.xml", DOCUMENT_XML)
                zf.writestr("receipt.xml", receipt_xml("2-1"))
            extf_lines = [
                '"EXTF";510;21;"Buchungsstapel"',
                '"Umsatz (ohne Soll/Haben-Kz)";"Soll/Haben";"Konto";"Belegfeld 1";"Buchungstext"',
                '"12,00";"S";"1362";"2-1";"Bar"',
                '"2,00";"H";"1369";"2-1";"Trinkgeld"',
                '"10,00";"H";"8401";"2-1";"Testartikel"',
            ]
            with zipfile.ZipFile(directory / "EXTF_Buchungsstapel_test.zip", "w") as zf:
                zf.writestr("EXTF_Einzel_Buchungsstapel_test.csv", "\r\n".join(extf_lines).encode("cp1252"))
            _, receipt_path, _, _ = kc.run(directory)
            with receipt_path.open(encoding="utf-8-sig", newline="") as fh:
                row = next(csv.DictReader(fh, delimiter=";"))
            self.assertEqual(row["Trinkgeld"], "2,00")
            self.assertEqual(row["Bar"], "12,00")
            self.assertEqual(row["Auf Rechnung"], "0,00")
            self.assertEqual(row["Differenz"], "0,00")

    def test_missing_extf_payment_can_be_classified_as_invoice(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with zipfile.ZipFile(directory / "DATEV_XML_Export_test.zip", "w") as zf:
                zf.writestr("document.xml", DOCUMENT_XML)
                zf.writestr("receipt.xml", receipt_xml("2-10", "Auf Rechnung"))
            _, receipt_path, _, _ = kc.run(directory)
            with receipt_path.open(encoding="utf-8-sig", newline="") as fh:
                row = next(csv.DictReader(fh, delimiter=";"))
            self.assertEqual(row["Auf Rechnung"], "10,00")
            self.assertEqual(row["Sonstige Zahlart"], "0,00")
            self.assertIn("EXTF-Zahlungsdatensatz fehlt", row["Pruefhinweis"])

    def test_ex_reference_marks_both_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with zipfile.ZipFile(directory / "DATEV_XML_Export_test.zip", "w") as zf:
                zf.writestr("document.xml", DOCUMENT_XML)
                zf.writestr("old.xml", receipt_xml("2-6819", "Auf Rechnung", "12.50"))
                zf.writestr("new.xml", receipt_xml("2-6820 (ex-6819)", "Maestro", "12.50"))
            _, receipt_path, _, _ = kc.run(directory)
            with receipt_path.open(encoding="utf-8-sig", newline="") as fh:
                rows = {r["Bonnummer"]: r for r in csv.DictReader(fh, delimiter=";")}
            self.assertIn("Möglicherweise ersetzter/stornierter Bon", rows["2-6819"]["Pruefhinweis"])
            self.assertIn("Ersatz-/Folgebon", rows["2-6820 (ex-6819)"]["Pruefhinweis"])

    def test_full_csv_has_position_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with zipfile.ZipFile(directory / "DATEV_XML_Export_test.zip", "w") as zf:
                zf.writestr("document.xml", DOCUMENT_XML)
                zf.writestr("receipt.xml", multi_position_receipt_xml())
            full_path, _, _, _ = kc.run(directory)
            with full_path.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh, delimiter=";"))
            self.assertEqual([r["Position"] for r in rows], ["1", "2", "3"])
            self.assertEqual({r["Bonnummer"] for r in rows}, {"2-20"})

    def test_article_and_hourly_summaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with zipfile.ZipFile(directory / "DATEV_XML_Export_test.zip", "w") as zf:
                zf.writestr("document.xml", DOCUMENT_XML)
                zf.writestr("receipt.xml", multi_position_receipt_xml())
            _, _, article_path, hourly_path = kc.run(directory)

            with article_path.open(encoding="utf-8-sig", newline="") as fh:
                article_rows = {r["Artikel"]: r for r in csv.DictReader(fh, delimiter=";")}
            self.assertEqual(article_rows["Wasser"]["Stk."], "2")
            self.assertEqual(article_rows["Wasser"]["Umsatz"], "10,00")
            self.assertEqual(article_rows["FFM-Riesling"]["Stk."], "1")

            with hourly_path.open(encoding="utf-8-sig", newline="") as fh:
                hourly_rows = list(csv.DictReader(fh, delimiter=";"))
            water = next(r for r in hourly_rows if r["Artikel"] == "Wasser")
            self.assertEqual(water["Tag"], "Freitag")
            self.assertEqual(water["Datum"], "05.06.2026")
            self.assertEqual(water["Stunde"], "18:00-18:59")
            self.assertEqual(water["Stk."], "2")
            self.assertEqual(water["Umsatz"], "10,00")


if __name__ == "__main__":
    unittest.main()
