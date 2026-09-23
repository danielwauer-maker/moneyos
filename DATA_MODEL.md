# Datenmodell

Grundprinzipien: Rohimporte sind unveränderlich; Quelltransaktionen und
wirtschaftliche Ereignisse bleiben getrennt; Verknüpfungen sind explizit;
historische Regeln haben Gültigkeitszeiträume; Geld nutzt Decimal/NUMERIC.

Kernbereiche:

- `accounts`: eigene Aktiv- und Passivkonten; Amazon ist kein Konto.
- `balance_confirmations`: append-only Historie datierter Saldo-Bestätigungen;
  die neueste Zeile mit Status `confirmed` bestimmt die aktuelle Sicht.
- `import_batches`, `raw_import_records`, `source_transactions`: Importkette.
- `source_transaction_accounts`: additive Konto-/Rollenverknüpfung für
  unveränderliche Quelltransaktionen; `source` und optional `target` werden ohne
  Änderung des Quelldatensatzes nachgetragen.
- `economic_events`, `event_source_links`: kanonische Ausgaben, Einnahmen,
  Transfers und Refunds sowie ihre Quellen.
- `categories`: hierarchische Kategorien.
- `envelopes`, `envelope_rule_periods`, `envelope_snapshots`,
  `envelope_movements`: physische Umschläge, Historie und Bewegungen.
- `projects`: unabhängige Tags/Projekte.
- `review_items`, `assignment_rules`: nachvollziehbare Entscheidungen.
- `recurring_items`, `forecast_entries`, `reconciliation_runs`: Planung.

Das vollständige SQLAlchemy-Schema ist in `app/db/models.py` definiert und über
Alembic versioniert.

## Integritätsregeln

- `raw_import_records` und `source_transactions` sind auf ORM- und Datenbankebene
  unveränderlich; Korrekturen werden als neue Datensätze beziehungsweise Links
  modelliert.
- `envelope_rule_periods` sind append-only und dürfen sich je Umschlag und Regeltyp
  zeitlich nicht überschneiden.
- Genau ein `canonical_source`-Link ist je Source Transaction möglich.
- Persistierte Geldwerte verwenden `NUMERIC(14,2)` und werden als `Decimal`
  geladen. Binäre Float-Spalten sind nicht zulässig.
- Unbestätigte Kontosalden bleiben durch `balance_confirmed=false` ausdrücklich
  unbekannt und werden nicht als erfundene 0-Euro-Werte in Vermögen eingerechnet.
- Transfers führen, soweit bekannt, `source_account_id` und `target_account_id`;
  ihr positiver Betrag ist die transferierte Größe, die Richtung folgt aus den
  Kontorollen.
- Tresor-Bestätigungen speichern Gesamtbestand, physisch gezählten Umschlaganteil,
  den zu diesem Zeitpunkt berechneten MoneyOS-Umschlagstand und einen möglichen
  Abgleichhinweis. Umschläge werden dem Vermögen nicht zusätzlich zugerechnet.
- Ereignistypen, Confidence/Probability und nichtnegative Ziel-/Bestandswerte
  werden durch Datenbank-Constraints geschützt.
