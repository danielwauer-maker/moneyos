# Sicherheit und Datenschutz

MoneyOS ist eine lokale Einzelbenutzer-Anwendung. Uvicorn bindet an `127.0.0.1`;
Docker veröffentlicht den Container-Port ausschließlich an `127.0.0.1` des Hosts.
Es gibt keine Cloud-Synchronisierung, Telemetrie, externes Key-Management oder
Uploads zu Drittdiensten.

## Demo-/Privat-Trennung

Das Demo-Profil (`MONEYOS_DEMO_MODE=true`) nutzt `data/moneyos.db`; produktive
Sparda-Imports sind dort hart gesperrt. Das Privat-Profil
(`MONEYOS_DEMO_MODE=false`) nutzt standardmäßig die eigene Datenbank
`data/private/profiles/private/moneyos.db`, eigene Staging-/Quarantäneverzeichnisse
unter `data/private/profiles/private/imports/`, Backups unter `backups/private/`
und Logs unter `logs/private/`. Der Demo-Seed schreibt niemals in das
Privat-Profil. Ein Profilwechsel erfordert einen Neustart der App; vor einem
privaten Import muss die Seitenleiste ausdrücklich `Privat-Profil` anzeigen.
Die private Stammdateninitialisierung ist bestätigungspflichtig und erzeugt vor
dem Backfill automatisch ein Safety-Backup. Sie ändert keine Raw Records oder
Source Transactions. Konto-, Salden- und Beitragswerte liegen in
`data/private/profiles/private/master_data.json`; das gesamte `data/`-Verzeichnis
ist ignoriert, sodass diese Werte nicht in Git gelangen.
Saldo-Bestätigungen werden ausschließlich im privaten SQLite-Profil gespeichert.
Formulare akzeptieren Decimal-Text, führen keine Rohdaten in Logs und sind im
Demo-Profil auf HTTP-Ebene gesperrt. Der Sparda-Historienbackfill liest nur den
bereits importierten Saldo und ändert keine Raw Records oder Source Transactions.
Kategorie-/Umschlagentscheidungen und private Vorschlagsregeln werden ebenfalls
ausschließlich in der lokalen Profildatenbank gespeichert. Regeln erzeugen nur
Vorschläge und führen keine stillen Zuordnungen aus. Tabellen und Logs zeigen keine IBANs,
Kartenkennungen oder Rohimporte; sichtbare Zahlungspartner werden vor der Ausgabe
mit der zentralen Redaction-Hilfe behandelt.
Kategoriepflege ändert ausschließlich Stammdaten. Sie schreibt weder Raw Records
noch Source Transactions, Salden, Umschlag-Snapshots oder physische Bewegungen.
Der Sparda-Reclassification-Apply ist privatprofilgebunden, bestätigungspflichtig,
atomar und erzeugt vorher ein Safety-Backup. Sein Bericht enthält nur aggregierte
Zähler und redigierte kanonische Händlernamen, keine Zahlungsidentifikatoren.

## Aktuelle Verschlüsselungsgrenze

SQLite verschlüsselt die Datenbankdatei nicht. Auch die ZIP-Backups sind nicht
verschlüsselt und können die lokale `.env` enthalten. Diese Dateien müssen daher
wie die zugrunde liegenden Kontoexporte geschützt werden. Empfohlen für Windows:

1. BitLocker oder Geräteverschlüsselung für alle Laufwerke mit Daten oder Backups.
2. Ein passwort-/Windows-Hello-geschütztes lokales Benutzerkonto ohne Freigabe des
   MoneyOS-Verzeichnisses an andere Konten.
3. Ein privates MoneyOS-Datenverzeichnis mit restriktiven NTFS-Rechten.
4. Ein getrenntes, ebenfalls verschlüsseltes Backupziel und regelmäßige
   Restore-Tests.

Die Engine-Erzeugung ist in `app/db/engine.py` gekapselt, sodass ein kompatibles,
getestetes SQLCipher-Backend später ergänzt werden kann. Bis dahin ist MoneyOS
nicht für einen unverschlüsselten gemeinsam genutzten Rechner geeignet.

## Protokollierung

Importservices protokollieren keine Dateinamen, IBANs, Kartennummern, E-Mails,
Transaktionsbeschreibungen oder Raw Records. `app/security/redaction.py` stellt
eine Redaktion für strukturierte Felder und bekannte Identifikatoren bereit; Tests
prüfen die Entfernung sensibler Werte. Neue Logs müssen technische Batch-IDs und
Fehlercodes statt Finanzinhalte verwenden.

Der Sparda-Adapter speichert vollständige IBAN-/BIC-, Gläubiger- und Mandatsfelder
nur im geschützten unveränderlichen Raw Record, weil sie für lokale Auditierbarkeit
und stabile Duplikaterkennung benötigt werden. Importvorschau, Zusammenfassung und
Logs geben diese Werte nicht aus. Fingerprints sind SHA-256-Werte und werden nicht
als Ersatz für Laufwerksverschlüsselung betrachtet.

## Backup und Restore

`python -m app.ops backup` erzeugt atomar ein zeitgestempeltes ZIP mit einer über
die SQLite-Backup-API konsistenten Datenbank, Manifest, SHA-256 und – falls
vorhanden – `.env`. Caches, Logs, Staging und temporäre Dateien werden nicht
gesichert. Das Backupverzeichnis darf nicht im aktiven Datenbankverzeichnis liegen.

Vor `restore` muss die App gestoppt sein. Der Restore verlangt `--confirm`, prüft
Archivpfade, Format, Hash und SQLite-Integrität und erzeugt **vor** dem Austausch
automatisch ein Safety-Backup der aktuellen Datenbank. `.env` wird nur mit dem
zusätzlichen Schalter `--restore-config` zurückgespielt.

## Aufbewahrung und Löschung

Die konservativen Defaults sind: Staging 90 Tage, Quarantäne 365 Tage, Logs 30
Tage und Backups 365 Tage; mindestens drei neueste Backups bleiben geschützt.
Retention ist explizit, zeigt standardmäßig nur Kandidaten und löscht ausschließlich
mit `--apply --confirm`. Sie löscht niemals das einzige Backup.
