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
