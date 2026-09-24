# MoneyOS

MoneyOS ist eine lokale persönliche Finanz-Web-App mit FastAPI, SQLAlchemy, SQLite,
Alembic und Jinja2; die Templates sind für lokale HTMX-Interaktionen vorbereitet.
Phase 2A ergänzt ein privates lokales Datei-Staging, Validierung und Quarantäne,
atomare Importgrenzen, Backups/Restore, Aufbewahrungsregeln und eine Diagnose.
Phase 2B enthält den produktiven Sparda-CSV-Adapter; Phase 2C ergänzt den
produktiven PayPal-CSV-Adapter mit konservativem Sparda-Funding-Matching. Amex-
und Amazon-Parser sind bewusst noch nicht enthalten.

## Voraussetzungen

- Windows 10/11
- Python 3.12 oder neuer (`py -3.12`)
- optional Docker Desktop

## Lokaler Start unter Windows

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --reload
```

Danach: <http://127.0.0.1:8000>. Beim Start werden Alembic-Migrationen automatisch
bis `head` ausgeführt. Der Seed ist idempotent und überschreibt keine
vorhandenen Daten. Für einen leeren privaten Start `MONEYOS_DEMO_MODE=false` setzen
und den Seed-Schritt auslassen.

Manuell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m app.seed.demo
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --reload
```

## Tests und Lint

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m alembic check
```

## Private Daten und Betrieb

Die Standardpfade sind relativ zum Projektverzeichnis:

- aktive SQLite-Datenbank: `data/moneyos.db`
- unveränderte, valide Staging-Dateien: `data/private/imports/staging/`
- abgewiesene Dateien: `data/private/imports/quarantine/`
- Backups: `backups/` (außerhalb des aktiven DB-Verzeichnisses `data/`)
- Logs: `logs/`

Alle Pfade sowie Größen- und Aufbewahrungsgrenzen sind über `.env` konfigurierbar.
Diese Verzeichnisse, Finanzexporte, Datenbanken, Logs und `.env` sind von Git
ausgeschlossen. Die Importansicht zeigt Status und maschinenlesbare Fehlercodes,
aber keine Rohinhalte oder ursprünglichen Dateinamen.

```powershell
.\.venv\Scripts\python.exe -m app.ops diagnostics
.\.venv\Scripts\python.exe -m app.ops backup

# App vorher stoppen; vor dem Restore entsteht automatisch ein Safety-Backup
.\.venv\Scripts\python.exe -m app.ops restore .\backups\moneyos-...-regular.zip --confirm

# optional auch die im Backup enthaltene .env wiederherstellen
.\.venv\Scripts\python.exe -m app.ops restore .\backups\moneyos-...-regular.zip --confirm --restore-config

# erst anzeigen, dann ausdrücklich anwenden
.\.venv\Scripts\python.exe -m app.ops retention
.\.venv\Scripts\python.exe -m app.ops retention --apply --confirm

# Originaldatei eines Batches löschen; Metadaten bleiben erhalten
.\.venv\Scripts\python.exe -m app.ops delete-staged 123 --confirm
```

SQLite und Backup-ZIPs sind derzeit **nicht verschlüsselt**. Auf Windows werden
BitLocker/Geräteverschlüsselung, ein geschütztes lokales Benutzerkonto und ein
privates MoneyOS-Datenverzeichnis vorausgesetzt. Details: [SECURITY.md](SECURITY.md).

## Docker

```powershell
docker compose up --build
```

SQLite-Daten, typische Finanzexportformate, Import-/Exportverzeichnisse, Logs,
`.env` und private Quelldaten sind per `.gitignore` ausgeschlossen. Demo-Daten
sind vollständig fiktiv. Docker veröffentlicht Port 8000 ausschließlich auf
`127.0.0.1`.

## Architektur und Regeln

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [DATA_MODEL.md](DATA_MODEL.md)
- [BUSINESS_RULES.md](BUSINESS_RULES.md)
- [IMPORTS.md](IMPORTS.md)
- [SECURITY.md](SECURITY.md)
- [UI_REFERENCE.md](UI_REFERENCE.md)
- [CODEX_WORKFLOW.md](CODEX_WORKFLOW.md)

## Nächster Schritt

Vor einem realen Import in `.env` `MONEYOS_DEMO_MODE=false` setzen und die App neu
starten. Das Privat-Profil verwendet standardmäßig
`data/private/profiles/private/moneyos.db`; die Seitenleiste muss danach
`Privat-Profil` anzeigen. Im Demo-Profil ist jeder produktive Import hart
gesperrt und Demo-Daten werden nie in die private Datenbank übernommen.

Danach `python -m app.ops diagnostics` ausführen und den realen Sparda-Export auf
der Seite `Import` neu auswählen. MoneyOS validiert und zeigt zunächst eine
redigierte Vorschau; importiert wird erst nach „Atomaren Import starten“. Eine
zuvor im Demo-Profil bereitgestellte Datei wird absichtlich nicht profilübergreifend
übernommen. PayPal besitzt eine eigene redigierte Vorschau und wird erst nach
ausdrücklicher Bestätigung und automatischem Safety-Backup atomar importiert.
Amex und Amazon bleiben spätere, getrennte Adapter.

Nach dem ersten Sparda-Import werden die bestätigten privaten Konto- und
Umschlagstammdaten samt additivem Kontobackfill einmalig beziehungsweise beliebig
oft idempotent angelegt. Die privaten Werte liegen ausschließlich in der von Git
ignorierten Datei `data/private/profiles/private/master_data.json`:

```powershell
$env:MONEYOS_DEMO_MODE = "false"
$env:MONEYOS_PRIVATE_DATABASE_URL = "sqlite:///./data/private/profiles/private/moneyos.db"
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m app.ops init-private --confirm
```

Der letzte Befehl legt vor Änderungen automatisch ein privates Safety-Backup an.
Unbestätigte Salden von Amex, PayPal, Portemonnaie, Tresor und C24 bleiben sichtbar
als „Nicht abgestimmt“ und werden nicht als 0 Euro erfunden.

Nach Migration `0006` wird der bestehende importierte Sparda-Saldo einmalig in
die append-only Bestätigungshistorie übernommen:

```powershell
$env:MONEYOS_DEMO_MODE = "false"
$env:MONEYOS_PRIVATE_DATABASE_URL = "sqlite:///./data/private/profiles/private/moneyos.db"
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m app.ops init-balance-history --confirm
```

Jeder Lauf legt zuerst ein Safety-Backup an; der Backfill selbst ist idempotent.
Weitere Salden werden auf `Konten` über `Saldo bestätigen` datiert erfasst.
Portemonnaie und Tresor verwenden manuelle Zählungen. Beim Tresor werden
Gesamtbestand und Umschlaganteil getrennt festgehalten; Abweichungen zum
berechneten Umschlagstand erzeugen eine Warnung statt einer stillen Korrektur.

Die Seite **Umschläge** berechnet das aktuelle Soll aus dem bestätigten Basisstand,
den historischen Beitragszeiträumen und explizit zugeordneten Economic Events.
Unzugeordnete Ausgaben und offene Reviews werden nicht geraten: Der Abgleich bleibt
sichtbar „vorläufig“, bis die Zuordnung bestätigt ist. Das rechnerische Soll wird
erst danach auf volle 5 Euro abgerundet; Rundungsrest und ein möglicher Fehlbetrag
werden separat ausgewiesen. Diese Ansicht schreibt weder Ist-Snapshots noch
Umschlagbewegungen in die Datenbank.

Der Arbeitsbereich **Umschlag-Zuordnung** bündelt die relevanten Vorgänge seit dem
bestätigten Basisstand. Kategorie/Unterkategorie und physischer Umschlag werden in
derselben kompakten Tabelle, aber als unabhängige Entscheidungen geführt. Einzel-
und Mehrfachauswahl können nur eine Kategorie, nur einen Umschlag oder beides
erhalten; „Kein Umschlag“ ist ebenfalls eine finale, unabhängige Entscheidung. Filter
für Monat, Zahlungspartner, Kategorie, Konto und Entscheidungsstatus sowie
Gruppierungen nach Zahlungspartner, Kategorie, Monat und Betrag unterstützen den
schnellen Abgleich. Aus konsistenten Mehrfachauswahlen können priorisierte Regeln
entstehen; diese können Kategorie und Umschlag gemeinsam vorschlagen, ordnen aber
niemals still zu. Review-only-Quellen behalten eine Kategorieentscheidung für eine
spätere Ereigniserzeugung, ohne dadurch vorzeitig ein Economic Event anzulegen.

Die Seite **Kategorien** verwaltet Haupt- und Unterkategorien im aktiven Profil.
Sie unterstützt Anlegen, Umbenennen, Deaktivieren und Reaktivieren und zeigt je
Kategorie die Zahl zugeordneter Economic Events sowie noch nicht materialisierter
Review-Entscheidungen. Kategorien werden nicht hart gelöscht. Inaktive Kategorien
bleiben an historischen Vorgängen sichtbar, erscheinen aber nicht in normalen
Neuzuordnungslisten. Im Arbeitsbereich **Umschlag-Zuordnung** legt
`+ Neue Kategorie` Haupt- oder Unterkategorien an und kehrt mit erhaltenen Filtern
zur Auswahl zurück.

Sparda-Händler werden bei strukturierten Kartenumsätzen aus dem Zahlungsdetail
extrahiert. Dadurch bleibt der rohe Gegenpart sichtbar, während beispielsweise
`DZ BANK AG → tatsächlicher Händler` nachvollziehbar kategorisiert wird. Eine sichere
Neubewertung vorhandener privater Sparda-Daten läuft zweistufig:

```powershell
$env:MONEYOS_DEMO_MODE = "false"
.\.venv\Scripts\python.exe -m app.ops reclassify-sparda
.\.venv\Scripts\python.exe -m app.ops reclassify-sparda --apply --confirm
```

Der erste Befehl ist read-only. Der Apply-Befehl erzeugt zuerst ein Safety-Backup,
schützt manuelle Kategorieentscheidungen und schreibt ausschließlich Kategorien
an Economic Events beziehungsweise Vorschläge an offene Reviews.

Transaktionslisten zeigen bei Sparda-Vorgängen die ursprüngliche Bank-Gegenpartei
in der ersten Zeile und ein abgeleitetes Buchungsdetail darunter. Der kanonische
Händler bleibt als dezenter Hinweis sowie für sichere Gruppierung und Vorschläge
verfügbar. Kategorieauswahl und -filter sind alphabetisch nach Hauptkategorie
gruppiert, Unterkategorien stehen direkt darunter und können ohne Frontend-Framework
durchsucht werden.

Klarna und PayPal werden als Zahlungsintermediäre getrennt vom kanonischen Händler
geführt. Nur explizite Detailangaben wie `Purchase at H+M` werden als Händler-
Vorschlag verwendet; EREF-/Transaktions-IDs und `INSTANT TRANSFER` bleiben außen
vor. Reine Provider-Funding- und Settlement-Zeilen bleiben Transfers.

Der ausschließlich lesende Detail-Audit für das Privatprofil lautet:

```powershell
$env:MONEYOS_DEMO_MODE = "false"
.\.venv\Scripts\python.exe -m app.ops audit-sparda-details
```

### Chronologische Prüfung und Projekte

Für systematische private Zuordnung steht `/transaction-review` zur Verfügung.
Die Ansicht sortiert importierte Vorgänge nach Datum und erlaubt unabhängige
Entscheidungen für wirtschaftlichen Typ, Kategorie, Umschlag und Projekt.
`/review` bleibt die Ausnahme-Inbox. Projekte können unter `/projects` angelegt,
umbenannt, archiviert und reaktiviert werden; sie besitzen bewusst noch kein
eigenes Budget. Explizite Entscheidungen sind idempotent und verändern keine
Raw Records oder Source Transactions.
