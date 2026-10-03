# Importe

Phase 2A stellt die sichere lokale Importgrenze bereit. Sparda-, PayPal- und
American-Express-CSV besitzen produktive Zahlungsadapter. Amazon besitzt einen
produktiven ZIP-Enrichment-Adapter, der niemals eigene Economic Events erzeugt.

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
erlaubt Vorschauen, sperrt aber jeden produktiven Import. Im Privat-Profil
liegen die entsprechenden Dateien getrennt unter
`data/private/profiles/private/imports/staging/` beziehungsweise `quarantine/`;
seine Datenbank ist `data/private/profiles/private/moneyos.db`. Die Dateien tragen
einen Hashnamen. Eine Quarantäne speichert maschinenlesbare Fehlercodes am Batch;
es findet kein Teilimport statt. Die UI gibt weder Rohinhalt noch Dateinamen aus.

Unterstützte Staging-Formate:

- Sparda/Bank: `.csv` (produktiv)
- PayPal: `.csv` (produktiv)
- American Express: `.csv` (produktiv; PDF wird nicht importiert)
- Amazon: `.zip` (produktiv, reine Anreicherung)

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
- PayPal Europe: Transfer/Funding-Leg; die Händlerausgabe darf nur aus dem
  PayPal-Adapter entstehen.
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

Dasselbe Muster gilt für Klarna und PayPal: Der Provider bleibt als Gegenpartei und
`processor` erhalten, während ein klarer Ausdruck wie `Purchase at H+M` vor einer
`EREF`-Referenz den kanonischen Händler bilden kann. Referenzwerte, Hashes und
generische Funding-Texte werden verworfen. PayPal-Funding und Settlement bleiben
Transfers und erzeugen keine zweite Händlerausgabe.

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

## Produktiver PayPal-Adapter

Der Adapter `app/importers/paypal.py` liest UTF-8/UTF-8-BOM und erkennt Komma-
beziehungsweise Semikolon-CSV. Geldwerte werden ausschließlich als `Decimal`
normalisiert. Jede neue Zeile erzeugt einen unveränderlichen Raw Record und eine
normalisierte Source Transaction; Transaktionscode und zugehöriger
Transaktionscode bilden stabile Gruppen und Fingerprints.
Da PayPal denselben Transaktionscode in mehreren technisch unterschiedlichen
Exportzeilen verwenden kann, besteht die persistierte Source-ID aus dem
zeilenspezifischen stabilen Fingerprint und nicht aus dem Transaktionscode allein.

Abgeschlossene Händlerzahlungen erzeugen genau ein Expense Event pro Händlerzeile.
Rückzahlungen erzeugen Refund Events und werden bei eindeutiger Referenz additiv
mit dem Ursprung verknüpft. Funding-, Kreditkarten-, Autorisierungs-, Hold- und
Release-Zeilen sind technische Quellen: Sie erzeugen niemals eine zweite Ausgabe.
Unbekannte oder mehrdeutige Zeilen erzeugen nur ein Review Item.

PayPal-Funding wird konservativ mit bestehenden Sparda-Quellen abgeglichen.
Explizite Referenz plus Betrag oder ein eindeutiger Betrag im engen Datumsfenster
gilt als hohe Konfidenz und wird nur als zusätzlicher Source Link an das bereits
existierende PayPal-Händlerereignis angehängt. Mittlere und mehrdeutige Treffer
bleiben im Review. Die Quelltransaktionen werden nicht verändert.

Vor der bestätigten Ausführung erzeugt die Webroute automatisch ein lokales
Safety-Backup. Der rein lesende Vorabtest ist im Privat-Profil möglich mit
`python -m app.ops audit-paypal PFAD-ZUR-DATEI.CSV`. Er meldet ausschließlich
Aggregate, Datumsbereich, Duplikate und Match-Stufen; er legt weder Batch noch
Staging-Datei oder Finanzdatensatz an.

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

Jeder produktive Parser wird ausschließlich über den atomaren Importservice aufgerufen.
Entweder werden Raw Records, Source Transactions und Economic Events gemeinsam
committed oder vollständig zurückgerollt. Ein fehlgeschlagener Batch darf für die
Diagnose bestehen bleiben. Hash plus eindeutige Source-Fingerprints verhindern
Datei- und Transaktionsduplikate.

Der Dateihash unterscheidet außerdem den Lebenszyklus: Nur ein Batch mit Status
`imported` gilt als abgeschlossenes Dateiduplikat. Ein `valid`er oder `failed`er
Batch bleibt unter derselben Batch-ID vorschau- und ausführbar. Der Retry prüft den
unveränderten Staging-Hash erneut und läuft wieder vollständig atomar; ein bereits
abgeschlossener Batch kann dagegen nicht erneut ausgeführt werden.

Fehlgeschlagene Versuche bleiben unter `metadata_json.attempt_history` mit neutralem
Fehlercode und Zeitpunkt auditierbar. Nach einem erfolgreichen Retry beschreibt
`validation_json` nur noch das aktuelle Ergebnis `imported`; die Importtabelle zeigt
historische Fehler getrennt vom aktuellen Status und nicht mehr als aktive Prüfung.

Aufbewahrungsregeln sind in `.env` konfigurierbar. Sie laufen nie stillschweigend:
`python -m app.ops retention` zeigt Kandidaten, erst `--apply --confirm` löscht.
Mindestens die konfigurierten neuesten Backups und immer wenigstens ein Backup
bleiben geschützt. Eine einzelne Staging-/Quarantänedatei wird mit
`python -m app.ops delete-staged BATCH_ID --confirm` entfernt; der technische
Batch-Nachweis bleibt erhalten.

# American Express CSV

American Express uses the hardened upload → hash → validation → preview → atomic
execute lifecycle. Productive parsing accepts UTF-8 CSV (with or without BOM)
and comma, semicolon or tab delimiters. Required logical columns are booking
date, description and booked amount; German and English header aliases and
flexible column order are supported. PDF statements are not productive import
sources and are rejected cleanly.

Row identity is based on a source-scoped natural key plus a deterministic content
hash over stable row fields. Truly identical repeated rows are skipped, while
different rows sharing a transaction/reference identifier remain distinct. Full
card-number-shaped values are redacted before any
raw or normalized row is persisted. Foreign amount, currency and supplied
exchange rate are retained without deriving a missing rate.

Statement payments are clearing movements, not merchant expenses. High-
confidence Sparda matches create only an additive `settlement_leg`; medium and
unmatched settlements create review items. Failed batches roll back all business
rows and can be retried, while completed file hashes remain duplicate-protected.

## Amazon „Your Orders“ ZIP

Der Amazon-Adapter verarbeitet das Export-ZIP direkt und extrahiert keine Dateien.
Relevante CSVs sind Order History, Digital Content Orders, Refund Details,
Return Requests/Status, Digital Returns, Returns.2 und Replacement Orders.
PDFs, Bilder, Skripte und sonstige Archivbestandteile werden nicht ausgeführt.

Amazon-Daten sind reine Anreicherung. Neue Bestellpositionen, Refunds, Returns und
Ersatzlieferungen erzeugen `AmazonEnrichmentRecord`-Zeilen, aber weder
`SourceTransaction` noch `EconomicEvent`. Konservative Betrags-/Datums-/Zahlungs-
hinweise dürfen nur einen additiven `AmazonPaymentMatch` zu einem vorhandenen
kanonischen Ereignis erzeugen. Gesplittete Zahlungen, Gutscheine, mehrere
Kandidaten und unvollständige Quellen bleiben mittlere oder ungeklärte Vorschläge.

Die Vorschau trennt `duplicate_within_file`, `existing_exact`,
`existing_conflict`, `unique_source_rows` und `new_rows`. Dieselbe natürliche
Identität mit abweichendem Inhalt wird als Konflikt gespeichert und nie still
überschrieben. Der read-only Audit ist:

```powershell
$env:MONEYOS_DEMO_MODE = "false"
python -m app.ops audit-amazon "C:\Pfad\Your Orders.zip"
```

Vor der bestätigten Ausführung erzeugt die Webroute ein privates Safety-Backup.
`FileDescriptions.csv` dient nur zur Dokumentation des Exports und wird nicht als
produktive Importquelle benötigt.

Die explizite produktive Zeitgrenze ist standardmäßig `2026-01-01`
(`MONEYOS_AMAZON_IMPORT_START_DATE`). Die Vorschau weist Vollarchiv und produktiven
Scope getrennt aus. Historische oder nicht sicher datierbare Zeilen bleiben Teil
der Parserstatistik, erzeugen aber weder Enrichment Records noch Matches, Reviews,
Konflikte, Source Transactions oder Economic Events.
