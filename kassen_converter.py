from __future__ import annotations

import csv
import logging
import re
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import xml.etree.ElementTree as ET

DATEV_NS = "http://xml.datev.de/bedi/tps/document/v05.0"
NS = {"d": DATEV_NS}
MONEY_ZERO = Decimal("0.00")
CARD_HINTS = (
    "visa",
    "mastercard",
    "maestro",
    "girocard",
    "ec",
    "v pay",
    "vpay",
    "american express",
    "amex",
)
INVOICE_HINTS = ("auf rechnung", "rechnung")
EX_REFERENCE_RE = re.compile(r"\(\s*ex-(\d+)\s*\)", re.IGNORECASE)
GERMAN_WEEKDAYS = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)


@dataclass(frozen=True)
class Position:
    date: str
    time: str
    receipt_id: str
    item: str
    amount: Decimal
    vat: Decimal
    account: str
    payment_method: str


@dataclass
class Receipt:
    date: str
    time: str
    receipt_id: str
    total: Decimal
    positions: list[Position] = field(default_factory=list)
    vat_7: Decimal = MONEY_ZERO
    vat_19: Decimal = MONEY_ZERO
    tips: Decimal = MONEY_ZERO
    cash: Decimal = MONEY_ZERO
    card: Decimal = MONEY_ZERO
    on_account: Decimal = MONEY_ZERO
    other_payment: Decimal = MONEY_ZERO
    payment_sum: Decimal = MONEY_ZERO
    difference: Decimal = MONEY_ZERO
    warnings: list[str] = field(default_factory=list)


class ConverterError(RuntimeError):
    pass


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def money(value: str) -> Decimal:
    value = value.strip()
    try:
        return Decimal(value).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ConverterError(f"Ungültiger Geldbetrag: {value!r}") from exc


def german_money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}".replace(".", ",")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def normalize_label(label: str) -> str:
    return " ".join(label.casefold().strip().split())


def is_card_label(label: str) -> bool:
    text = normalize_label(label)
    return any(text == hint or text.startswith(hint + " ") for hint in CARD_HINTS)


def is_invoice_label(label: str) -> bool:
    return normalize_label(label) in INVOICE_HINTS


def next_available(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    n = 1
    while True:
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def setup_logger(directory: Path) -> logging.Logger:
    logger = logging.getLogger("KassenConverter")
    logger.setLevel(logging.INFO)
    close_logger(logger)
    handler = logging.FileHandler(directory / "KassenConverter.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def validate_datev_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if "document.xml" not in names:
                return False
            candidates = [n for n in names if n.lower().endswith(".xml") and Path(n).name != "document.xml"]
            if not candidates:
                return False
            root = ET.fromstring(zf.read(candidates[0]))
            return local_name(root.tag) == "LedgerImport" and root.find("d:consolidate", NS) is not None
    except (zipfile.BadZipFile, ET.ParseError, KeyError):
        return False


def decode_extf(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ConverterError("EXTF-Datei konnte weder als UTF-8 noch als Windows-1252 gelesen werden.")


def validate_extf_zip(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            matches = [
                n for n in zf.namelist()
                if Path(n).name.startswith("EXTF_Einzel_Buchungsstapel_") and n.lower().endswith(".csv")
            ]
            if not matches:
                return False
            lines = decode_extf(zf.read(matches[0])).splitlines()
            if len(lines) < 2:
                return False
            header = next(csv.reader([lines[1]], delimiter=";", quotechar='"'))
            required = {"Umsatz (ohne Soll/Haben-Kz)", "Soll/Haben", "Konto", "Belegfeld 1", "Buchungstext"}
            return required.issubset(set(header))
    except (zipfile.BadZipFile, csv.Error, KeyError, ConverterError):
        return False


def discover_inputs(directory: Path, logger: logging.Logger) -> tuple[Path, Path | None]:
    zips = sorted(directory.glob("*.zip"))
    datev = [p for p in zips if validate_datev_zip(p)]
    extf = [p for p in zips if validate_extf_zip(p)]
    if not datev:
        raise ConverterError("Kein gültiger DATEV-XML-Export im Programmverzeichnis gefunden.")
    if len(datev) > 1:
        raise ConverterError("Mehrere gültige DATEV-XML-Exporte gefunden. Bitte nur einen Export pro Ordner verwenden.")
    if len(extf) > 1:
        raise ConverterError("Mehrere gültige EXTF-Einzelbuchungsstapel gefunden. Bitte nur einen Export pro Ordner verwenden.")
    logger.info("DATEV-XML erkannt: %s", datev[0].name)
    if extf:
        logger.info("EXTF-Kontrollquelle erkannt: %s", extf[0].name)
    else:
        logger.warning("Kein EXTF-Einzelbuchungsstapel gefunden; Zahlungs-/Trinkgeldabgleich ist nicht vollständig möglich.")
    return datev[0], extf[0] if extf else None


def mark_replacement_relations(receipts: dict[str, Receipt], logger: logging.Logger) -> None:
    for receipt in receipts.values():
        match = EX_REFERENCE_RE.search(receipt.receipt_id)
        if not match:
            continue
        prefix = receipt.receipt_id.split("-", 1)[0]
        referenced_id = f"{prefix}-{match.group(1)}"
        msg = f"Ersatz-/Folgebon: Kennzeichnung verweist auf {referenced_id}"
        receipt.warnings.append(msg)
        logger.warning("%s: %s", receipt.receipt_id, msg)
        referenced = receipts.get(referenced_id)
        if referenced is not None:
            reverse = f"Möglicherweise ersetzter/stornierter Bon; Folgebon {receipt.receipt_id} vorhanden"
            referenced.warnings.append(reverse)
            logger.warning("%s: %s", referenced_id, reverse)
        else:
            missing = f"Referenzierter Vorgängerbon {referenced_id} im XML-Export nicht gefunden"
            receipt.warnings.append(missing)
            logger.warning("%s: %s", receipt.receipt_id, missing)


def parse_datev(path: Path, logger: logging.Logger) -> dict[str, Receipt]:
    receipts: dict[str, Receipt] = {}
    with zipfile.ZipFile(path) as zf:
        xml_names = sorted(
            n for n in zf.namelist()
            if n.lower().endswith(".xml") and Path(n).name != "document.xml"
        )
        for name in xml_names:
            try:
                root = ET.fromstring(zf.read(name))
            except ET.ParseError as exc:
                raise ConverterError(f"XML-Datei {name} ist nicht lesbar: {exc}") from exc
            if local_name(root.tag) != "LedgerImport":
                logger.warning("Unbekannte XML-Struktur übersprungen: %s", name)
                continue
            consolidate = root.find("d:consolidate", NS)
            if consolidate is None:
                logger.warning("XML ohne consolidate-Element: %s", name)
                continue
            ledgers = consolidate.findall("d:accountsReceivableLedger", NS)
            if not ledgers:
                logger.warning("XML ohne Buchungspositionen: %s", name)
                continue

            positions: list[Position] = []
            receipt_ids: set[str] = set()
            timestamps: list[datetime] = []
            vat_sums: defaultdict[Decimal, Decimal] = defaultdict(lambda: MONEY_ZERO)
            for node in ledgers:
                dt_text = (node.findtext("d:date", default="", namespaces=NS) or "").strip()
                try:
                    dt = datetime.fromisoformat(dt_text)
                except ValueError as exc:
                    raise ConverterError(f"Ungültiges Datum in {name}: {dt_text!r}") from exc
                timestamps.append(dt)
                receipt_id = (node.findtext("d:invoiceId", default="", namespaces=NS) or "").strip()
                receipt_ids.add(receipt_id)
                amount = money(node.findtext("d:amount", default="", namespaces=NS) or "")
                vat = money(node.findtext("d:tax", default="0", namespaces=NS) or "0")
                vat_sums[vat] += amount
                positions.append(
                    Position(
                        date=dt.strftime("%d.%m.%Y"),
                        time=dt.strftime("%H:%M:%S"),
                        receipt_id=receipt_id,
                        item=(node.findtext("d:information", default="", namespaces=NS) or "").strip(),
                        amount=amount,
                        vat=vat,
                        account=(node.findtext("d:accountNo", default="", namespaces=NS) or "").strip(),
                        payment_method=(node.findtext("d:bookingText", default="", namespaces=NS) or "").strip(),
                    )
                )

            if len(receipt_ids) != 1 or "" in receipt_ids:
                raise ConverterError(f"Uneindeutige oder fehlende Bonnummer in {name}: {sorted(receipt_ids)!r}")
            receipt_id = next(iter(receipt_ids))
            if receipt_id in receipts:
                raise ConverterError(f"Bonnummer {receipt_id} kommt mehrfach im XML-Export vor.")
            total = money(consolidate.attrib.get("consolidatedAmount", ""))
            position_sum = sum((p.amount for p in positions), MONEY_ZERO)
            first_dt = min(timestamps)
            receipt = Receipt(
                date=first_dt.strftime("%d.%m.%Y"),
                time=first_dt.strftime("%H:%M:%S"),
                receipt_id=receipt_id,
                total=total,
                positions=positions,
                vat_7=vat_sums[Decimal("7.00")],
                vat_19=vat_sums[Decimal("19.00")],
            )
            if position_sum != total:
                msg = f"Positionssumme {position_sum} weicht von Bonsumme {total} ab"
                receipt.warnings.append(msg)
                logger.warning("%s: %s", receipt_id, msg)
            unknown_vat = [rate for rate in vat_sums if rate not in (Decimal("7.00"), Decimal("19.00"))]
            if unknown_vat:
                logger.info("%s: weitere MwSt.-Sätze vorhanden: %s", receipt_id, ", ".join(map(str, unknown_vat)))
            receipts[receipt_id] = receipt

    if not receipts:
        raise ConverterError("Im DATEV-XML-Export wurden keine Bons gefunden.")
    mark_replacement_relations(receipts, logger)
    logger.info(
        "%d Bons und %d Positionen aus XML gelesen.",
        len(receipts),
        sum(len(r.positions) for r in receipts.values()),
    )
    return receipts


def fallback_payment_from_xml(receipt: Receipt, logger: logging.Logger) -> None:
    methods = {p.payment_method.strip() for p in receipt.positions if p.payment_method.strip()}
    if len(methods) != 1:
        receipt.other_payment = receipt.total
        label = ", ".join(sorted(methods)) if methods else "(keine)"
        msg = f"EXTF-Zahlungsdatensatz fehlt; uneindeutige XML-Zahlungsart(en): {label}"
        receipt.warnings.append(msg)
        logger.warning("%s: %s", receipt.receipt_id, msg)
        return
    label = next(iter(methods))
    if normalize_label(label) == "bar":
        receipt.cash = receipt.total
    elif is_card_label(label):
        receipt.card = receipt.total
    elif is_invoice_label(label):
        receipt.on_account = receipt.total
    else:
        receipt.other_payment = receipt.total
    msg = (
        f"EXTF-Zahlungsdatensatz fehlt; Zahlungsbetrag aus XML-Zahlungsart '{label}' "
        "abgeleitet, Trinkgeld nicht prüfbar"
    )
    receipt.warnings.append(msg)
    logger.warning("%s: %s", receipt.receipt_id, msg)


def parse_extf(path: Path, receipts: dict[str, Receipt], logger: logging.Logger) -> None:
    with zipfile.ZipFile(path) as zf:
        names = [
            n for n in zf.namelist()
            if Path(n).name.startswith("EXTF_Einzel_Buchungsstapel_") and n.lower().endswith(".csv")
        ]
        if len(names) != 1:
            raise ConverterError("EXTF-ZIP enthält nicht genau einen Einzel-Buchungsstapel.")
        text = decode_extf(zf.read(names[0]))
    lines = text.splitlines()
    if len(lines) < 3:
        raise ConverterError("EXTF-Einzelbuchungsstapel enthält keine Buchungszeilen.")
    header = next(csv.reader([lines[1]], delimiter=";", quotechar='"'))
    index = {name: i for i, name in enumerate(header)}
    required = ["Umsatz (ohne Soll/Haben-Kz)", "Soll/Haben", "Konto", "Belegfeld 1", "Buchungstext"]
    missing = [name for name in required if name not in index]
    if missing:
        raise ConverterError(f"EXTF-Spalten fehlen: {', '.join(missing)}")

    extf_revenue: defaultdict[str, Decimal] = defaultdict(lambda: MONEY_ZERO)
    extf_payment_seen: set[str] = set()
    extf_receipt_seen: set[str] = set()
    for row in csv.reader(lines[2:], delimiter=";", quotechar='"'):
        if len(row) < len(header):
            row += [""] * (len(header) - len(row))
        receipt_id = row[index["Belegfeld 1"]].strip()
        amount_text = row[index["Umsatz (ohne Soll/Haben-Kz)"]].strip() if receipt_id else ""
        if not amount_text:
            continue
        try:
            amount = money(amount_text.replace(".", "").replace(",", "."))
        except ConverterError:
            logger.warning("EXTF-Zeile mit ungültigem Betrag für Bon %s: %r", receipt_id, amount_text)
            continue
        side = row[index["Soll/Haben"]].strip().upper()
        label = row[index["Buchungstext"]].strip()
        account = row[index["Konto"]].strip()
        receipt = receipts.get(receipt_id)
        if receipt is None:
            logger.warning("EXTF enthält Bon %s, der im XML-Export nicht vorhanden ist.", receipt_id)
            continue
        extf_receipt_seen.add(receipt_id)

        if normalize_label(label) == "trinkgeld":
            if side != "H":
                logger.warning("%s: Trinkgeld mit unerwartetem Soll/Haben-Kennzeichen %s.", receipt_id, side)
            receipt.tips += amount
            continue
        if side == "S":
            extf_payment_seen.add(receipt_id)
            if normalize_label(label) == "bar":
                receipt.cash += amount
            elif is_card_label(label):
                receipt.card += amount
            elif is_invoice_label(label):
                receipt.on_account += amount
            else:
                receipt.other_payment += amount
                msg = f"Unbekannte/sonstige Zahlungsart im EXTF: {label or '(leer)'} (Konto {account})"
                if msg not in receipt.warnings:
                    receipt.warnings.append(msg)
                logger.warning("%s: %s", receipt_id, msg)
        elif side == "H":
            extf_revenue[receipt_id] += amount
        else:
            msg = f"Unbekanntes Soll/Haben-Kennzeichen im EXTF: {side or '(leer)'}"
            if msg not in receipt.warnings:
                receipt.warnings.append(msg)
            logger.warning("%s: %s", receipt_id, msg)

    for receipt in receipts.values():
        if receipt.receipt_id not in extf_payment_seen:
            fallback_payment_from_xml(receipt, logger)
        receipt.cash = receipt.cash.quantize(Decimal("0.01"))
        receipt.card = receipt.card.quantize(Decimal("0.01"))
        receipt.on_account = receipt.on_account.quantize(Decimal("0.01"))
        receipt.other_payment = receipt.other_payment.quantize(Decimal("0.01"))
        receipt.tips = receipt.tips.quantize(Decimal("0.01"))
        receipt.payment_sum = (
            receipt.cash + receipt.card + receipt.on_account + receipt.other_payment
        ).quantize(Decimal("0.01"))
        receipt.difference = (
            receipt.payment_sum - (receipt.total + receipt.tips)
        ).quantize(Decimal("0.01"))
        if receipt.receipt_id in extf_receipt_seen:
            revenue_sum = extf_revenue[receipt.receipt_id].quantize(Decimal("0.01"))
            if revenue_sum != receipt.total:
                msg = (
                    f"EXTF-Buchungssumme ohne Trinkgeld {revenue_sum} EUR weicht von "
                    f"XML-Bonsumme {receipt.total} EUR ab"
                )
                receipt.warnings.append(msg)
                logger.warning("%s: %s", receipt.receipt_id, msg)
        if receipt.receipt_id in extf_payment_seen and receipt.difference != MONEY_ZERO:
            msg = f"Zahlungsdifferenz {receipt.difference} EUR"
            receipt.warnings.append(msg)
            logger.warning("%s: %s", receipt.receipt_id, msg)


def period_from_receipts(receipts: dict[str, Receipt]) -> str:
    dates = [datetime.strptime(r.date, "%d.%m.%Y") for r in receipts.values()]
    months = {(d.year, d.month) for d in dates}
    if len(months) != 1:
        raise ConverterError("Der Export enthält mehrere Monate. Bitte pro Monatsordner nur einen Monats-Export verwenden.")
    year, month = next(iter(months))
    return f"{year:04d}-{month:02d}"


def sorted_receipts(receipts: dict[str, Receipt]) -> list[Receipt]:
    return sorted(
        receipts.values(),
        key=lambda r: (datetime.strptime(r.date, "%d.%m.%Y"), r.time, r.receipt_id),
    )


def all_positions(receipts: dict[str, Receipt]) -> list[Position]:
    return [p for receipt in sorted_receipts(receipts) for p in receipt.positions]


def write_full_csv(directory: Path, period: str, receipts: dict[str, Receipt]) -> Path:
    path = next_available(directory / f"Kasse_Vollstaendig_{period}.csv")
    columns = [
        "Datum", "Uhrzeit", "Bonnummer", "Position", "Artikel", "Betrag", "MwSt", "Konto", "Zahlungsart"
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
        writer.writerow(columns)
        for receipt in sorted_receipts(receipts):
            for position_no, p in enumerate(receipt.positions, start=1):
                writer.writerow([
                    p.date,
                    p.time,
                    p.receipt_id,
                    position_no,
                    p.item,
                    german_money(p.amount),
                    german_money(p.vat),
                    p.account,
                    p.payment_method,
                ])
    return path


def write_receipt_csv(directory: Path, period: str, receipts: dict[str, Receipt]) -> Path:
    path = next_available(directory / f"Kasse_Bons_{period}.csv")
    columns = [
        "Datum", "Uhrzeit", "Bonnummer", "Brutto gesamt", "Brutto 7 %", "Brutto 19 %", "Trinkgeld",
        "Bar", "Karte", "Auf Rechnung", "Sonstige Zahlart", "Abwicklungssumme", "Differenz", "Pruefhinweis",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
        writer.writerow(columns)
        for r in sorted_receipts(receipts):
            writer.writerow([
                r.date,
                r.time,
                r.receipt_id,
                german_money(r.total),
                german_money(r.vat_7),
                german_money(r.vat_19),
                german_money(r.tips),
                german_money(r.cash),
                german_money(r.card),
                german_money(r.on_account),
                german_money(r.other_payment),
                german_money(r.payment_sum),
                german_money(r.difference),
                " | ".join(r.warnings),
            ])
    return path


def write_article_summary_csv(directory: Path, period: str, receipts: dict[str, Receipt]) -> Path:
    path = next_available(directory / f"Artikel_Auswertung_{period}.csv")
    grouped: dict[tuple[str, Decimal], list[Decimal | int]] = {}
    for p in all_positions(receipts):
        key = (p.item, p.vat)
        if key not in grouped:
            grouped[key] = [0, MONEY_ZERO]
        grouped[key][0] = int(grouped[key][0]) + 1
        grouped[key][1] = Decimal(grouped[key][1]) + p.amount

    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["Artikel", "MwSt", "Stk.", "Umsatz", "Durchschnittspreis"])
        for (item, vat), values in sorted(grouped.items(), key=lambda x: (x[0][0].casefold(), x[0][1])):
            count = int(values[0])
            revenue = Decimal(values[1]).quantize(Decimal("0.01"))
            average = (revenue / count).quantize(Decimal("0.01")) if count else MONEY_ZERO
            writer.writerow([item, german_money(vat), count, german_money(revenue), german_money(average)])
    return path


def hour_bucket(time_text: str) -> str:
    hour = datetime.strptime(time_text, "%H:%M:%S").hour
    return f"{hour:02d}:00-{hour:02d}:59"


def write_article_hourly_csv(directory: Path, period: str, receipts: dict[str, Receipt]) -> Path:
    path = next_available(directory / f"Artikel_Stunden_{period}.csv")
    grouped: dict[tuple[str, str, str], list[Decimal | int]] = {}
    for p in all_positions(receipts):
        key = (p.date, hour_bucket(p.time), p.item)
        if key not in grouped:
            grouped[key] = [0, MONEY_ZERO]
        grouped[key][0] = int(grouped[key][0]) + 1
        grouped[key][1] = Decimal(grouped[key][1]) + p.amount

    rows = []
    for (date_text, hour, item), values in grouped.items():
        dt = datetime.strptime(date_text, "%d.%m.%Y")
        rows.append((dt, hour, item, int(values[0]), Decimal(values[1]).quantize(Decimal("0.01"))))
    rows.sort(key=lambda r: (r[0], r[1], r[2].casefold()))

    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["Tag", "Datum", "Stunde", "Artikel", "Stk.", "Umsatz"])
        for dt, hour, item, count, revenue in rows:
            writer.writerow([
                GERMAN_WEEKDAYS[dt.weekday()],
                dt.strftime("%d.%m.%Y"),
                hour,
                item,
                count,
                german_money(revenue),
            ])
    return path


def run(directory: Path | None = None) -> tuple[Path, Path, Path, Path]:
    directory = (directory or app_dir()).resolve()
    logger = setup_logger(directory)
    try:
        logger.info("KassenConverter gestartet. Arbeitsverzeichnis: %s", directory)
        datev_path, extf_path = discover_inputs(directory, logger)
        receipts = parse_datev(datev_path, logger)
        if extf_path:
            parse_extf(extf_path, receipts, logger)
        else:
            for receipt in receipts.values():
                fallback_payment_from_xml(receipt, logger)
                receipt.payment_sum = (
                    receipt.cash + receipt.card + receipt.on_account + receipt.other_payment
                ).quantize(Decimal("0.01"))
                receipt.difference = (receipt.payment_sum - receipt.total).quantize(Decimal("0.01"))
                receipt.warnings.append("EXTF-Kontrollquelle fehlt; Trinkgeld nicht prüfbar")

        period = period_from_receipts(receipts)
        full_path = write_full_csv(directory, period, receipts)
        receipt_path = write_receipt_csv(directory, period, receipts)
        article_path = write_article_summary_csv(directory, period, receipts)
        hourly_path = write_article_hourly_csv(directory, period, receipts)
        for output in (full_path, receipt_path, article_path, hourly_path):
            logger.info("Ausgabe erstellt: %s", output.name)
        logger.info("KassenConverter beendet.")
        return full_path, receipt_path, article_path, hourly_path
    finally:
        close_logger(logger)


def main() -> int:
    try:
        full_path, receipt_path, article_path, hourly_path = run()
        print("KassenConverter erfolgreich.")
        print(f"Vollständige Positionen: {full_path.name}")
        print(f"Bon-Übersicht:          {receipt_path.name}")
        print(f"Artikel-Auswertung:     {article_path.name}")
        print(f"Artikel nach Stunden:   {hourly_path.name}")
        print("Details: KassenConverter.log")
        input("Enter zum Schließen ...")
        return 0
    except Exception as exc:
        directory = app_dir()
        logger = None
        try:
            logger = setup_logger(directory)
            logger.exception("Abbruch: %s", exc)
        except Exception:
            pass
        finally:
            if logger is not None:
                close_logger(logger)
        print(f"FEHLER: {exc}")
        print("Details stehen, soweit möglich, in KassenConverter.log.")
        input("Enter zum Schließen ...")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
