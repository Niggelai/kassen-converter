# kassen-converter

Kleine lokale Windows-Anwendung zur Aufbereitung von Kassensystem-Exporten in Excel-kompatible CSV-Dateien.

## Zweck

Der Konverter ersetzt weder DATEV noch ein Buchhaltungssystem. Er erzeugt aus vorhandenen Originalexporten eine zusätzliche, nachvollziehbare Arbeits- und Übergabeansicht für die Steuerberatung sowie positionsgenaue Artikeldaten für Excel-Auswertungen.

Die Original-ZIP-Dateien werden nicht verändert, verschoben oder überschrieben.

## Eingaben

Die EXE arbeitet im Verzeichnis, in dem sie selbst liegt, und erkennt dort anhand von Inhalt und Struktur:

- einen DATEV-XML-Export als Primärquelle für Bons und Artikelpositionen,
- optional einen EXTF-Einzelbuchungsstapel als Kontrollquelle für Zahlungsarten, Trinkgeld und Plausibilitätsprüfungen.

## Ausgaben

- `Kasse_Vollstaendig_YYYY-MM.csv`: eine Zeile je Artikelposition.
- `Kasse_Bons_YYYY-MM.csv`: eine Zeile je Bon mit Bruttosummen, 7 % / 19 %, Trinkgeld, Bar, Karte, sonstigen Zahlungsarten und Prüfdifferenz.
- `KassenConverter.log`: Verarbeitung, Warnungen und erkannte Abweichungen.

Vorhandene CSVs werden nie überschrieben. Wiederholte Läufe erzeugen Namen wie `Kasse_Bons_2026-06 (1).csv`.

## Lokaler Test

```powershell
python -m unittest discover -s tests -v
```

## EXE bauen

Auf dem Windows-Rechner mit installiertem PyInstaller genügt ein Doppelklick auf `build_exe.bat` oder:

```powershell
python -m PyInstaller --clean --onefile --name KassenConverter kassen_converter.py
```

Die fertige Datei liegt anschließend unter:

```text
dist\KassenConverter.exe
```

## Nutzung

Beispiel Monatsordner:

```text
2026\
  Juni\
    KassenConverter.exe
    DATEV_XML_Export_....zip
    EXTF_Buchungsstapel_....zip
```

`KassenConverter.exe` doppelklicken. Die beiden CSV-Dateien und das Log werden im selben Ordner erzeugt.

## Spezifikation

Die verbindlichen V0-Anforderungen stehen in `KASSEN_CONVERTER_SPEC.yaml`.
