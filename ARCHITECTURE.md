# Architektur

MoneyOS ist local-first. `app/db` enthält Schema und Sessions, `app/domain` reine
Finanzfunktionen ohne Web- oder Datenbankabhängigkeit, `app/services` orchestriert
Abfragen und Berechnungen, `app/web` enthält FastAPI-Routen, Jinja-Templates und
statische Assets. Importer werden in Phase 2 als Adapter unter `app/importers`
ergänzt. Templates enthalten keine versteckte Finanzlogik.

Der Datenfluss lautet: unveränderlicher Rohimport → normalisierte Quelltransaktion
→ verknüpftes wirtschaftliches Ereignis → optionale Kategorie/Umschlag/Projekt-
Zuordnung → nachvollziehbares Review. Alle Geldwerte verwenden `Decimal` und
`NUMERIC(14,2)`.

SQLite ist der Standard. Der App-Start führt ausschließlich die eingefrorenen
Alembic-Migrationen bis `head` aus; `create_all` ist kein Produktionspfad.
SQLite-Fremdschlüssel sind pro Verbindung aktiviert. Es gibt keine
Cloud-Synchronisierung, Analytics oder externen Finanzdaten-Aufrufe.

`app/db/engine.py` ist die einzige Engine-Erzeugungsgrenze. Domain und Services
kennen keinen SQLite-Treiber. Dadurch kann später ein getesteter SQLCipher- oder
anderer kompatibler verschlüsselter SQLite-Backend ergänzt werden, ohne die
Finanzlogik umzuschreiben. Phase 2A fügt bewusst keine Verschlüsselungsabhängigkeit
hinzu.

Rohimporte, Source Transactions und historische Umschlagregeln sind append-only.
ORM-Hooks verhindern Änderungen im Anwendungscode; SQLite-Trigger schützen auch
gegen direkte SQL-Updates und -Löschungen. Ein partiell eindeutiger kanonischer
Source-Link verhindert, dass dieselbe Quelltransaktion zwei wirtschaftliche
Ereignisse begründet.

## Private Importgrenze

Demo- und Privat-Profil besitzen getrennte Datenbanken und getrennte operative
Verzeichnisse. `MONEYOS_DEMO_MODE=true` verwendet weiterhin `data/moneyos.db` und
`data/private/imports/`; `false` verwendet standardmäßig
`data/private/profiles/private/moneyos.db` und ausschließlich Unterverzeichnisse
von `data/private/profiles/private/`. Produktive Sparda-Imports sind im
Demo-Profil auf Service- und HTTP-Ebene gesperrt. Der Demo-Seed beendet sich im
Privat-Profil ohne Schreibzugriff.

`python -m app.ops init-private --confirm` initialisiert ausschließlich im
Privat-Profil die bestätigten Konto- und Umschlagstammdaten. Der Befehl erzeugt
vor jedem Lauf automatisch ein Safety-Backup und ist idempotent. Bestehende
Sparda-Source-Transactions werden nicht verändert: Eine additive
`source_transaction_accounts`-Verknüpfung ordnet sie dem Girokonto zu. Mutable
Economic Events erhalten das Primärkonto und Transfers zusätzlich explizite
Quell-/Zielkonten, soweit die bereits gespeicherte Importsemantik eindeutig ist.
Private Namen, Salden und Beitragsbeträge liest der Service aus der ignorierten
Datei `data/private/profiles/private/master_data.json`; sie sind keine
Quellcode-Konstanten und werden nicht mit Git versioniert.

`balance_confirmations` ist die revisionssichere Saldoquelle. Neue Bestätigungen
werden angehängt; Updates und Deletes verhindern ORM-Hooks und SQLite-Trigger.
Dashboard und Kontenseite verwenden ausschließlich die neueste bestätigte Zeile.
Vorläufige, nicht abgestimmte und fehlende Salden werden nicht als Nullwert in das
Vermögen gerechnet. `init-balance-history --confirm` übernimmt einmalig den
bereits importierten Sparda-Buchungssaldo samt Datum in diese Historie und erzeugt
vorher automatisch ein Safety-Backup.

`app/services/envelope_targets.py` erzeugt den Umschlag-Abgleich als reproduzierbare,
nicht persistierte Sicht. Er verwendet den bestätigten historischen Basis-Snapshot,
die zum jeweiligen Kalendermonat gültige Beitragsregel und ausschließlich bestätigte,
explizit zugeordnete Economic Events. Economic Events verändern den physischen
Ist-Bestand nicht; dafür zählen nur bestätigte Snapshots und physische
`envelope_movements`. Offene Reviews und unzugeordnete Ausgaben oder Refunds werden
nicht geschätzt, sondern markieren betroffene Sollwerte als vorläufig. Damit werden
weder historische Bestätigungen noch private Rohdaten beim Aufruf der Seite verändert.

`app/services/envelope_assignments.py` stellt den gemeinsamen Arbeitsbereich für
Kategorie- und Umschlagentscheidungen bereit. `category_assignment_decisions`
bewahrt die bestätigte Kategorie unabhängig von der Umschlagdimension; bei einem
vorhandenen Economic Event wird dessen Kategorie aktualisiert, bei Review-only-
Quellen bleibt die Entscheidung separat und erzeugt kein geratenes Ereignis.
`envelope_assignment_decisions` unterscheidet eine
bestätigte Zuordnung, die finale Entscheidung „Kein Umschlag“ und „Später prüfen“.
Nur bei bereits bestätigten Ausgaben oder Refunds wird die Zuordnung auf das
Economic Event übertragen; ein Review-only-Vorgang erzeugt dadurch kein Ereignis.
Priorisierte `review_suggestion`-Regeln können Kategorie, Umschlag oder beides als
Vorschlag liefern. Sie werden niemals automatisch ausgeführt. Physische Snapshots,
Bewegungen und Kontosalden liegen vollständig außerhalb dieses Workflows.

`app/services/categories.py` ist die gemeinsame Schreibgrenze für Kategorie-
Stammdaten. Namen werden Unicode-normalisiert, von Rand-/Mehrfachleerzeichen
bereinigt und ohne Beachtung der Groß-/Kleinschreibung auf Duplikate geprüft.
Umbenennen aktualisiert dieselbe Zeile und bewahrt Fremdschlüssel. Die Hierarchie
ist auf Haupt- und Unterkategorie begrenzt; es gibt keinen Reparent- oder
Hard-Delete-Endpunkt. Deaktivierung blendet Kategorien nur für neue Zuordnungen
aus und verändert weder Ereignisse noch Entscheidungen oder Finanzwerte.

`app/domain/sparda.py` trennt Händlerextraktion von Kategorieklassifikation. Bei
strukturierten Kartenumsätzen wird der erste Händlerabschnitt des Zahlungsdetails
verwendet; der generische Gegenpart bleibt für Erklärbarkeit erhalten. Regeln
arbeiten auf dem extrahierten Händler und konkreten Detailmerkmalen, nicht auf
`DZ BANK AG` als solchem. `app/services/sparda_reclassification.py` plant die
Neubewertung read-only und wendet sie anschließend atomar an. Bestätigte
`category_assignment_decisions` sperren jede automatische Überschreibung.
Review-only-Quellen erzeugen ausschließlich `ReviewItem`-Vorschläge.

`app/services/transaction_details.py` erzeugt aus unveränderten Sparda-Quellen eine
reine Anzeigesicht aus ursprünglicher Gegenpartei, informativer zweiter Detailzeile
und kanonischem Händler. Tabellen zeigen die Gegenpartei zuerst; der kanonische
Händler dient nur zuverlässiger Gruppierung, Regeln und Kategorie-Vorschlägen. Die
Ableitung schreibt keine Werte in Raw Records oder Source Transactions.
Die gleiche, konservative Ableitung erkennt Klarna-/PayPal-Provider nur aus
explizitem Detailtext; Referenz- und Funding-Token werden nicht kanonisiert.

`app/services/import_staging.py` übernimmt Streaming, SHA-256, unveränderte lokale
Ablage, Dateityp-/Inhaltsprüfung und Quarantäne. Erst ein Batch im Zustand `valid`
darf an `app/services/import_execution.py` übergeben werden. Der spätere Parser-
Callback läuft mit allen Raw Records, Source Transactions, Economic Events und
Links in genau einer DB-Transaktion. Bei einem Fehler bleibt nur der Batch mit
einem neutralen Diagnosecode bestehen; alle finanziellen Teilzeilen werden
zurückgerollt. Dateihash und eindeutige Transaktions-Fingerprints bilden zwei
Idempotenzebenen.

Importzustände: `uploaded` → `validating` → `valid` → `imported`; verdächtige
Dateien gehen nach `quarantined`, Parserfehler nach `failed`. Legacy-`pending`
wird nur für bestehende Phase-1-Daten toleriert.

Der produktive Sparda-Adapter liegt in `app/importers/sparda.py`. Dekodierung,
CSV-Struktur, Datums-/Decimal-Normalisierung und Fingerprints sind dort von der
fachlichen Klassifikation in `app/domain/sparda.py` getrennt. Der Domainteil ruft
für jeden sicheren Typ den zentralen Account-Movement-Klassifizierer auf. Routen
und Templates orchestrieren nur Vorschau bzw. Start und enthalten keine
Finanzklassifikation.

## Schutzmodell

Die Web-App bleibt auf `127.0.0.1` gebunden. SQLite und Backup-ZIPs sind aktuell
unverschlüsselt. Das vorgesehene Windows-Modell ist daher: BitLocker bzw.
Geräteverschlüsselung für das Laufwerk, ein geschütztes lokales OS-Konto und ein
privates, nur für dieses Konto lesbares MoneyOS-Datenverzeichnis. Backups gehören
auf ein getrenntes, ebenfalls verschlüsseltes lokales Ziel. Es gibt weder Cloud-
Key-Management noch externe Datendienste.

## Phase 2B.8 review dimensions

The `/review` inbox remains the exception queue. `/transaction-review` is the
chronological private bookkeeping workspace. It reads immutable source rows and
existing events, while explicit type, category, envelope and project decisions
are stored independently. A type decision on a review-only source creates one
canonical Economic Event through the domain service; repeating the decision is
idempotent. Project decisions never affect balances or envelope actuals.
