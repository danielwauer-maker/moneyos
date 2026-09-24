# Importe

Phase 2A stellt die sichere lokale Importgrenze bereit. Seit Phase 2B ist Sparda CSV
der einzige produktive Adapter. PayPal CSV, Amex CSV/PDF und Amazon-Daten werden
weiterhin nur bereitgestellt und validiert; für sie existieren keine produktiven
Parser.

## Staging und Validierung

Vor jeder Inhaltsverarbeitung wird die Datei unverändert geschrieben, während
SHA-256 und Größe berechnet werden. Danach existiert ein Import-Batch. Exakt gleiche
Dateien werden über den eindeutigen Hash erkannt und nicht erneut angelegt.
Erlaubte Endungen werden je Quelltyp geprüft; zusätzlich werden Text/Binärinhalt,
PDF-Signatur und aktive PDF-Marker, JSON-Syntax sowie ZIP-Struktur geprüft. ZIPs
werden nicht extrahiert. Makros, Skripte, ausführbare Inhalte, Pfadtraversal und
verschlüsselte Archive führen zur Quarantäne. Eingebetteter Inhalt wird nie ausgeführt.

Im Demo-Profil liegen valide Originale unter `data/private/imports/staging/` und
abgewiesene Originale unter `data/private/imports/quarantine/`. Dieses Profil
erlaubt Vorschauen, sperrt aber jeden produktiven Sparda-Import. Im Privat-Profil
liegen die entsprechenden Dateien getrennt unter
`data/private/profiles/private/imports/staging/` beziehungsweise `quarantine/`;
seine Datenbank ist `data/private/profiles/private/moneyos.db`. Die Dateien tragen
einen Hashnamen. Eine Quarantäne speichert maschinenlesbare Fehlercodes am Batch;
es findet kein Teilimport statt. Die UI gibt weder Rohinhalt noch Dateinamen aus.

Unterstützte Staging-Formate (nur Sparda ist bereits produktiv):

- Sparda/Bank: `.csv`
- PayPal: `.csv`
- American Express: `.csv`, `.pdf`
- Amazon: `.csv`, `.json`, `.zip`

Die Maximalgröße wird mit `MONEYOS_MAX_IMPORT_FILE_SIZE_BYTES` konfiguriert. Leere,
zu große oder nicht unterstützte Dateien werden sauber quarantänisiert.

## Produktiver Sparda-Adapter

Sparda-Dateien werden als UTF-8/UTF-8-BOM, Semikolon-CSV mit deutschen Datums- und
Decimalwerten gelesen. Die Spaltenreihenfolge ist beliebig. Erforderlich sind
`Buchungstag`, `Valutadatum`, `Name Zahlungsbeteiligter`, `Buchungstext`,
`Verwendungszweck`, `Betrag` und `Waehrung`; einzelne Werte wie Valutadatum oder
Zahlungsbeteiligter dürfen leer sein. Fehlende Spalten oder fehlerhafte Zeilen
quarantänisieren die gesamte Datei, damit kein Teilimport entsteht.

Jede Quellzeile behält geschützten Raw-Text und das ursprüngliche Feld-Dictionary.
Geld wird ausschließlich über `Decimal` verarbeitet. Der stabile SHA-256-
Fingerprint umfasst Buchungs-/Valutadatum, Betrag, Währung, Saldo, Gegenpartei,
Verwendungszweck sowie – falls vorhanden – Konto-/Gegenkonto-IBAN,
Gläubiger-ID und Mandatsreferenz. Die Zeilenposition gehört bewusst nicht zum
Fingerprint, sodass eine geänderte Spalten- oder Zeilenreihenfolge keine Duplikate
erzeugt. Saldo und starke Quellfelder unterscheiden ansonsten gleichartige Käufe.

Deterministische Klassifikation:

- American Express und Amazon Visa-Abrechnungen: Transfer/Settlement, niemals
  gewöhnliche Ausgabe; Amazon Visa erzeugt zusätzlich `missing_card_source`.
- PayPal Europe: Transfer/Funding-Leg; die spätere Händlerausgabe darf nur aus dem
  noch nicht implementierten PayPal-Adapter entstehen.
- Bargeldabhebung: Transfer; ohne eindeutiges Ziel Review für Portemonnaie/Tresor.
- Gehalt, Bundesagentur, Familienkasse/Kindergeld und klar erkennbares Finanzamt:
  Einkommen.
- Händlererstattung: Refund statt Einkommen.
- Nur bekannte, hochkonfidente Händler-/Zweckmuster werden automatisch als Ausgabe
  mit Kategorie vorgeschlagen. Fehlende Standard-Kategoriepfade werden innerhalb
  derselben atomaren Transaktion ergänzt. Kategorie, Umschlag und Projekt bleiben
  getrennt; der Importer weist niemals automatisch einen Umschlag zu.
- Unklare Belastungen/Gutschriften erzeugen eine Source Transaction und Review,
  aber kein geratenes Economic Event.

Bei Kartenumsätzen ist `Name Zahlungsbeteiligter` häufig nur ein Prozessor. Der
Adapter extrahiert deshalb einen strukturierten Händler vor dem ersten Detailtrenner
aus `Verwendungszweck`; erst danach folgen Zweck und Gegenpartei als Fallback.
`DZ BANK AG` ohne spezifisches Detail ist niemals eine Kategorie-Evidenz. Vorschau
und Review zeigen redigiert sowohl den rohen Gegenpart als auch den kanonischen
Händler samt Confidence und Regelgrund.

Vorhandene private Sparda-Zeilen werden mit `python -m app.ops reclassify-sparda`
read-only ausgewertet. `--apply --confirm` legt zuerst ein Safety-Backup an und
schreibt atomar nur hochkonfidente Kategorien an ungeschützte Economic Events sowie
Vorschläge an Reviews. Raw Records und Source Transactions bleiben unverändert;
eine `category_assignment_decision` sperrt jede automatische Überschreibung.

Die Importseite führt nach erfolgreichem Staging zuerst in eine redigierte
Vorschau. Erst die ausdrückliche Bestätigung startet den atomaren Import. Die
Zusammenfassung nennt Quellzeilen, neue Raw-/Source-Datensätze, Economic Events,
Typzählungen, Reviews, Zeilen-/Dateiduplikate und Fehlzeilen, niemals IBAN/BIC,
Gläubiger-ID, Mandatsreferenz oder Raw-Zahlungsidentifikatoren.

Vor privaten Daten muss die App mit `MONEYOS_DEMO_MODE=false` neu gestartet
werden. Die Seitenleiste muss `Privat-Profil` anzeigen. Eine bereits im
Demo-Profil bereitgestellte Datei wird nicht übernommen: Das Original wird im
Privat-Profil bewusst erneut ausgewählt, validiert und gehasht. Dadurch können
Demo-Batches und private Economic Events nicht in derselben Datenbank landen.

Importer dürfen niemals direkt Präsentationswerte erzeugen. Wiederholte Ausführung
muss durch Hashes/Fingerprints idempotent sein. PayPal-Zeilen werden möglichst über
den zugehörigen Transaktionscode gruppiert; Amazon reichert vorhandene Käufe an und
erzeugt nicht automatisch eine zweite Ausgabe.

Alle Importer müssen vor dem Erzeugen eines wirtschaftlichen Ereignisses die
Klassifizierer in `app/domain/accounting.py` beziehungsweise
`app/domain/paypal.py` verwenden. Unbekannte PayPal-Typen gehen fehlersicher in
Review statt automatisch als Ausgabe gezählt zu werden. Kartenabrechnungen,
Bargeldbewegungen zwischen eigenen Konten und sonstige Eigenüberträge werden als
`transfer` klassifiziert. Ein Source-Datensatz darf durch den partiell eindeutigen
kanonischen Link höchstens ein wirtschaftliches Ereignis begründen.

## Atomarität und Löschen

Der künftige Parser wird ausschließlich über den atomaren Importservice aufgerufen.
Entweder werden Raw Records, Source Transactions und Economic Events gemeinsam
committed oder vollständig zurückgerollt. Ein fehlgeschlagener Batch darf für die
Diagnose bestehen bleiben. Hash plus eindeutige Source-Fingerprints verhindern
Datei- und Transaktionsduplikate.

Aufbewahrungsregeln sind in `.env` konfigurierbar. Sie laufen nie stillschweigend:
`python -m app.ops retention` zeigt Kandidaten, erst `--apply --confirm` löscht.
Mindestens die konfigurierten neuesten Backups und immer wenigstens ein Backup
bleiben geschützt. Eine einzelne Staging-/Quarantänedatei wird mit
`python -m app.ops delete-staged BATCH_ID --confirm` entfernt; der technische
Batch-Nachweis bleibt erhalten.
