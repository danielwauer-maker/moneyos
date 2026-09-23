# MoneyOS – Project Context

MoneyOS ist ein local-first Finanzbetriebssystem für private Nutzung. Technische
Quelle der Wahrheit sind `BUSINESS_RULES.md`, `DATA_MODEL.md`, `UI_REFERENCE.md`
und `CODEX_WORKFLOW.md`. Die veröffentlichte Sites-Oberfläche ist die eingefrorene
visuelle Referenz. Keine Cloud-Synchronisierung, Telemetrie, echten Finanzdaten,
Geheimnisse oder privaten Exporte im Repository.

Phase 1 umfasst Struktur, Schema/Migration, fiktiven Seed, getestete Domainregeln,
Dashboard, Konten, Transaktionen, Umschläge/Abgleich, Review und alle Hauptseiten.

Phase 2A stellt private lokale Ablage, Quarantäne, Backup/Restore, Retention und
Diagnose bereit. Phase 2B ergänzt ausschließlich den produktiven Sparda-CSV-
Adapter mit Vorschau, atomarem Import, Idempotenz und konservativem Review.
PayPal-, Amex- und Amazon-Parser sind weiterhin nicht produktiv.
