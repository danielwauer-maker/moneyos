from pathlib import Path
import sqlite3
import datetime
import hashlib

SOURCE = Path("/opt/moneyos/data/private/profiles/private/moneyos.db")
BACKUP_DIR = Path("/opt/moneyos/backups")
KEEP_DAYS = 30

BACKUP_DIR.mkdir(parents=True, exist_ok=True)

timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
target = BACKUP_DIR / f"moneyos-{timestamp}.db"

if not SOURCE.exists():
    raise SystemExit(f"Source database not found: {SOURCE}")

source = sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True)
destination = sqlite3.connect(target)

try:
    source.backup(destination)
finally:
    destination.close()
    source.close()

with sqlite3.connect(target) as conn:
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]

if result != "ok":
    target.unlink(missing_ok=True)
    raise SystemExit(f"Backup integrity check failed: {result}")

sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
(target.with_suffix(".db.sha256")).write_text(
    f"{sha256}  {target.name}\n",
    encoding="utf-8",
)

cutoff = datetime.datetime.now() - datetime.timedelta(days=KEEP_DAYS)

for old in BACKUP_DIR.glob("moneyos-*.db"):
    modified = datetime.datetime.fromtimestamp(old.stat().st_mtime)
    if modified < cutoff:
        old.unlink(missing_ok=True)
        old.with_suffix(".db.sha256").unlink(missing_ok=True)

print(f"Backup OK: {target}")
print(f"SHA256: {sha256}")
