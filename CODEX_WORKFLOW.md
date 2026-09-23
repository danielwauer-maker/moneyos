# MoneyOS – Codex Workflow

Vor Änderungen in dieser Reihenfolge lesen: `PROJECT_CONTEXT.md`,
`BUSINESS_RULES.md`, `DATA_MODEL.md`, `UI_REFERENCE.md`. Dokumentierte
Geschäftsregeln haben Vorrang vor widersprechendem Code.

Fokussierte Änderungen; keine eigenmächtige Neugestaltung; Finanzlogik außerhalb
von Templates/Routen; modulare Parser; explizite Domain-Services; Decimal für Geld;
Tests bei Regeländerungen; Migrationshistorie bewahren; Annahmen dokumentieren;
keine sensiblen Daten committen.

Pflichttests: 5-Euro-Rundung inklusive Defizit; Umschlag-Abgleich mit und ohne
Giro-Abhebung; Zahnarzt-Zielregel; keine Tresor-Doppelzählung; eine Ausgabe bei
Kartenkauf plus Abrechnung; technische PayPal-Zeilen und Refund-Klassifizierung.

Demo-Daten sind fiktiv und zeigen Kontotypen, Abgleich, Zahnarztregel, Review,
Projekt, Refund, Fixkosten und Forecast. Fertig bedeutet: Verhalten funktioniert,
Tests und Lint laufen, keine sensiblen Daten, Regeln/Dokumentation konsistent und
kein unabhängiges Redesign.

Importer dürfen Raw Import Records, Source Transactions und historische
Umschlagregeln nur anhängen. Sie müssen die zentralen Domain-Klassifizierer nutzen;
unbekannte Quelltypen gehen in Review. Direkte Schemaerzeugung mit `create_all`
ist außerhalb isolierter Tests nicht zulässig.
