from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.db.migrations import upgrade_database
from app.db.models import (
    Account,
    Category,
    EconomicEvent,
    Envelope,
    EnvelopeMovement,
    EnvelopeRulePeriod,
    EnvelopeSnapshot,
    ForecastEntry,
    Project,
    RecurringItem,
    ReviewItem,
)
from app.db.session import SessionLocal

D = Decimal

ENVELOPES = [
    ("Kleidung", "monthly_contribution", None, "80", "80"),
    ("Vergnügen", "monthly_contribution", None, "40", "40"),
    ("Haushalt", "monthly_contribution", None, "30", "30"),
    ("Mietrücklage", "monthly_contribution", None, "45", "30"),
    ("Urlaub", "monthly_contribution", None, "220", "250"),
    ("Geschenke", "monthly_contribution", None, "35", "35"),
    ("Dezembergeld", "monthly_contribution", None, "15", "15"),
    ("Zahnarztgeld", "target_balance", "500", "0", "0"),
    ("Unterstützung Brüder", "monthly_contribution", None, "25", "25"),
    ("Sparen", "monthly_contribution", None, "75", "0"),
    ("Noée", "monthly_contribution", None, "60", "60"),
    ("Fix", "monthly_contribution", None, "540", "500"),
]

BASELINES = {
    "Kleidung": "155",
    "Vergnügen": "25",
    "Haushalt": "45",
    "Mietrücklage": "310",
    "Urlaub": "1250",
    "Geschenke": "85",
    "Dezembergeld": "60",
    "Zahnarztgeld": "500",
    "Unterstützung Brüder": "140",
    "Sparen": "1325",
    "Noée": "950",
    "Fix": "35",
}


def seed_demo() -> None:
    upgrade_database()
    with SessionLocal.begin() as session:
        if session.scalar(select(Account.id).limit(1)):
            print("Demo-Daten sind bereits vorhanden. Kein Überschreiben ausgeführt.")
            return

        accounts = {
            row.name: row
            for row in [
                Account(name="Demo-Girokonto", account_type="checking", balance=D("5480.25")),
                Account(
                    name="Demo-Kreditkarte",
                    account_type="credit_card",
                    balance=D("-715.40"),
                    is_liability=True,
                ),
                Account(name="Demo-Zahlungskonto", account_type="paypal", balance=D("132.80")),
                Account(name="Demo-Portemonnaie", account_type="cash_wallet", balance=D("95.00")),
                Account(
                    name="Demo-Tresor",
                    account_type="cash_vault",
                    balance=D("5320.00"),
                    notes="Enthält physische Umschläge.",
                ),
                Account(
                    name="Historisches Demokonto",
                    account_type="checking",
                    balance=D("0"),
                    is_active=False,
                    notes="Historisch/inaktiv",
                ),
            ]
        }
        session.add_all(accounts.values())

        roots: dict[str, Category] = {}
        category_spec = {
            "Lebensmittel": ["Supermarkt", "Bioladen", "Bäckerei", "Metzgerei"],
            "Gastronomie": [],
            "Auto & Mobilität": ["Tanken", "Parken", "KFZ-Steuer"],
            "Wohnen & Haushalt": ["Baumarkt", "Rundfunk", "Strom"],
            "Gesundheit": ["Apotheke"],
            "Drogerie & Pflege": [],
            "Kleidung": [],
            "Kinder": [],
            "Freizeit & Vergnügen": [],
            "Geschenke": [],
            "Urlaub & Reisen": [],
            "Kommunikation": ["Internet", "Mobilfunk"],
            "Abos & Digitales": [],
            "Versicherungen": [],
            "Spenden & Unterstützung": [],
            "Bank & Gebühren": [],
        }
        for order, (name, children) in enumerate(category_spec.items()):
            root = Category(name=name, sort_order=order)
            session.add(root)
            session.flush()
            roots[name] = root
            session.add_all(
                [
                    Category(name=child, parent_id=root.id, sort_order=i)
                    for i, child in enumerate(children)
                ]
            )

        envelopes: dict[str, Envelope] = {}
        for order, (name, rule_type, target, old_amount, new_amount) in enumerate(ENVELOPES):
            envelope = Envelope(
                name=name,
                target_rule_type=rule_type,
                target_amount=D(target) if target else None,
                sort_order=order,
            )
            session.add(envelope)
            session.flush()
            envelopes[name] = envelope
            session.add(
                EnvelopeSnapshot(
                    envelope_id=envelope.id,
                    snapshot_date=date(2026, 4, 30),
                    physical_balance=D(BASELINES[name]),
                    source="confirmed_baseline",
                    is_confirmed=True,
                )
            )
            session.add(
                EnvelopeRulePeriod(
                    envelope_id=envelope.id,
                    valid_from=date(2026, 1, 1),
                    valid_to=date(2026, 5, 31),
                    monthly_amount=D(old_amount),
                    rule_type=rule_type,
                    target_amount=D(target) if target else None,
                )
            )
            session.add(
                EnvelopeRulePeriod(
                    envelope_id=envelope.id,
                    valid_from=date(2026, 6, 1),
                    valid_to=None,
                    monthly_amount=D(new_amount),
                    rule_type=rule_type,
                    target_amount=D(target) if target else None,
                )
            )

        project = Project(
            name="Sommerreise 2027",
            starts_at=date(2027, 7, 5),
            ends_at=date(2027, 7, 16),
            status="active",
            notes="Fiktives Demo-Projekt",
        )
        session.add(project)
        session.flush()

        today = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        events = [
            EconomicEvent(
                event_type="income",
                occurred_at=today - timedelta(days=8),
                description="Gehalt Muster GmbH",
                amount=D("3850"),
                account=accounts["Demo-Girokonto"],
                status="booked",
                confidence=D("1"),
            ),
            EconomicEvent(
                event_type="expense",
                occurred_at=today - timedelta(days=2),
                description="Markt & Mehr",
                amount=D("86.42"),
                account=accounts["Demo-Kreditkarte"],
                category=roots["Lebensmittel"],
                status="booked",
                confidence=D("0.98"),
            ),
            EconomicEvent(
                event_type="expense",
                occurred_at=today - timedelta(days=4),
                description="Nordlicht Hotel",
                amount=D("420"),
                account=accounts["Demo-Kreditkarte"],
                category=roots["Urlaub & Reisen"],
                project=project,
                status="booked",
                confidence=D("0.94"),
            ),
            EconomicEvent(
                event_type="transfer",
                occurred_at=today - timedelta(days=5),
                description="Kreditkartenabrechnung",
                amount=D("715.40"),
                account=accounts["Demo-Girokonto"],
                status="booked",
                confidence=D("1"),
            ),
            EconomicEvent(
                event_type="refund",
                occurred_at=today - timedelta(days=6),
                description="Erstattung Musterladen",
                amount=D("39.90"),
                account=accounts["Demo-Kreditkarte"],
                category=roots["Kleidung"],
                status="booked",
                confidence=D("1"),
            ),
            EconomicEvent(
                event_type="expense",
                occurred_at=today - timedelta(days=1),
                description="Café Morgenrot",
                amount=D("18.60"),
                account=accounts["Demo-Zahlungskonto"],
                category=roots["Gastronomie"],
                status="review",
                confidence=D("0.71"),
            ),
        ]
        session.add_all(events)
        session.flush()

        session.add_all(
            [
                EnvelopeMovement(
                    envelope_id=envelopes["Kleidung"].id,
                    occurred_at=datetime(2026, 5, 10),
                    movement_type="expense",
                    amount=D("125"),
                    notes="Fiktiver Demo-Kauf",
                ),
                EnvelopeMovement(
                    envelope_id=envelopes["Vergnügen"].id,
                    occurred_at=datetime(2026, 5, 1),
                    movement_type="contribution",
                    amount=D("50"),
                ),
                EnvelopeMovement(
                    envelope_id=envelopes["Haushalt"].id,
                    occurred_at=datetime(2026, 5, 1),
                    movement_type="contribution",
                    amount=D("25"),
                ),
                EnvelopeMovement(
                    envelope_id=envelopes["Zahnarztgeld"].id,
                    occurred_at=datetime(2026, 5, 12),
                    movement_type="expense",
                    amount=D("120"),
                    notes="Ziel wird wieder auf 500 € aufgefüllt",
                ),
                EnvelopeMovement(
                    envelope_id=envelopes["Urlaub"].id,
                    occurred_at=datetime(2026, 5, 1),
                    movement_type="contribution",
                    amount=D("300"),
                ),
            ]
        )
        session.add(
            ReviewItem(
                economic_event_id=events[-1].id,
                review_type="category_assignment",
                proposed_category_id=roots["Gastronomie"].id,
                proposed_event_type="expense",
                confidence=D("0.71"),
                explanation="Händlername ähnelt bekannten Gastronomie-Buchungen.",
                status="open",
            )
        )
        session.add_all(
            [
                RecurringItem(
                    name="Miete",
                    direction="expense",
                    amount=D("1050"),
                    frequency="monthly",
                    next_due_at=date.today() + timedelta(days=5),
                    account_id=accounts["Demo-Girokonto"].id,
                    category_id=roots["Wohnen & Haushalt"].id,
                ),
                RecurringItem(
                    name="Internet",
                    direction="expense",
                    amount=D("44.99"),
                    frequency="monthly",
                    next_due_at=date.today() + timedelta(days=9),
                    account_id=accounts["Demo-Girokonto"].id,
                    category_id=roots["Kommunikation"].id,
                ),
            ]
        )
        for week, amount in enumerate(
            ["5850", "4670", "5010", "4420", "5150", "4870", "5520", "6200"]
        ):
            session.add(
                ForecastEntry(
                    forecast_date=date.today() + timedelta(weeks=week),
                    source_type="demo_projection",
                    direction="balance",
                    amount=D(amount),
                    account_id=accounts["Demo-Girokonto"].id,
                    probability=D("0.85"),
                    notes="Fiktive Demo-Prognose",
                )
            )
    print("Fiktive MoneyOS-Demo-Daten wurden geladen.")


if __name__ == "__main__":
    seed_demo()
