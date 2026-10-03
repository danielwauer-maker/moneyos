from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Category, CategoryAssignmentDecision, EconomicEvent

OPTIMIZED_CATEGORY_HIERARCHY: dict[str, tuple[str, ...]] = {
    "Einnahmen": (
        "Gehalt / ALG",
        "Kindergeld",
        "Erstattung / Rückzahlung",
        "Private Zahlung / Kostenerstattung",
        "Sonstige Einnahmen",
    ),
    "Lebensmittel": (
        "Supermarkt",
        "Bäckerei",
        "Metzgerei",
        "Bioladen",
        "Sonstige Lebensmittel",
    ),
    "Gastronomie": ("Restaurant", "Imbiss / Fast Food", "Café / Eis"),
    "Wohnen & Haushalt": (
        "Miete",
        "Strom",
        "Möbel & Einrichtung",
        "Baumarkt / Renovierung",
        "Haushaltswaren",
        "Reparatur / Ersatzteile Haushalt",
    ),
    "Drogerie & Körperpflege": ("Drogerie", "Körperpflege", "Friseur"),
    "Gesundheit": (
        "Apotheke / Medikamente",
        "Arzt",
        "Zahnarzt",
        "Brille",
        "Kontaktlinsen",
    ),
    "Auto & Mobilität": (
        "Tanken",
        "Parken",
        "Maut",
        "Fähre",
        "ÖPNV",
        "Werkstatt",
        "Reifen",
        "TÜV",
        "Kfz-Steuer",
        "Kfz-Versicherung",
        "Kfz-Teile",
    ),
    "Kommunikation": ("Mobilfunk", "Internet"),
    "Abos & Digitales": (
        "Streaming",
        "Musik",
        "Cloud",
        "Amazon Prime",
        "Software / Online-Dienste",
    ),
    "Versicherungen": ("Privathaftpflicht", "Hausrat", "Sonstige Versicherungen"),
    "Kleidung": ("Kleidung", "Schuhe"),
    "Freizeit": ("Ausflug / Eintritt", "Sport", "Veranstaltung", "Hobby"),
    "Geschenke": (),
    "Spenden & Unterstützung": ("Spenden", "Unterstützung Familie / Freunde"),
    "Reisen": ("Unterkunft", "Reisegebühren", "Sonstige Reisekosten"),
    "Bank & Gebühren": ("Bankgebühren", "Zinsen / Gebühren", "Bußgeld / Gebühren"),
    "Sonstiges": (),
}

PARENT_ALIASES: dict[str, tuple[str, ...]] = {}
CHILD_ALIASES: dict[tuple[str, str], tuple[str, ...]] = {}


@dataclass(frozen=True)
class ManagedCategory:
    category: Category
    event_count: int
    pending_decision_count: int


@dataclass(frozen=True)
class CategoryGroup:
    parent: ManagedCategory
    children: tuple[ManagedCategory, ...]


@dataclass(frozen=True)
class CategorySelectorGroup:
    parent: Category
    children: tuple[Category, ...]


def clean_category_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).split()).strip()


def normalized_category_name(name: str) -> str:
    return clean_category_name(name).casefold()


def _validate_unique_name(
    db: Session,
    name: str,
    *,
    parent_id: int | None,
    exclude_id: int | None = None,
) -> str:
    clean_name = clean_category_name(name)
    if not clean_name:
        raise ValueError("Kategoriename fehlt")
    if len(clean_name) > 100:
        raise ValueError("Kategoriename darf höchstens 100 Zeichen enthalten")
    normalized = normalized_category_name(clean_name)
    for category in db.scalars(select(Category)):
        if (
            category.id != exclude_id
            and category.parent_id == parent_id
            and normalized_category_name(category.name) == normalized
        ):
            raise ValueError("Eine Kategorie mit diesem Namen existiert bereits")
    return clean_name


def create_category(db: Session, *, name: str, parent_id: int | None = None) -> Category:
    parent: Category | None = None
    if parent_id is not None:
        parent = db.get(Category, parent_id)
        if parent is None or not parent.is_active:
            raise ValueError("Aktive Hauptkategorie erforderlich")
        if parent.parent_id is not None:
            raise ValueError("Unterkategorien können keine weiteren Unterkategorien enthalten")
    clean_name = _validate_unique_name(db, name, parent_id=parent.id if parent else None)
    category = Category(name=clean_name, parent_id=parent.id if parent else None, is_active=True)
    db.add(category)
    db.flush()
    return category


def rename_category(db: Session, *, category_id: int, name: str) -> Category:
    category = db.get(Category, category_id)
    if category is None:
        raise ValueError("Kategorie nicht gefunden")
    category.name = _validate_unique_name(
        db, name, parent_id=category.parent_id, exclude_id=category.id
    )
    category.updated_at = datetime.now(UTC).replace(tzinfo=None)
    return category


def set_category_active(db: Session, *, category_id: int, active: bool) -> Category:
    category = db.get(Category, category_id)
    if category is None:
        raise ValueError("Kategorie nicht gefunden")
    if not active and category.parent_id is None:
        active_children = db.scalar(
            select(func.count())
            .select_from(Category)
            .where(Category.parent_id == category.id, Category.is_active)
        )
        if active_children:
            raise ValueError("Aktive Unterkategorien zuerst deaktivieren")
    if active and category.parent_id is not None:
        parent = db.get(Category, category.parent_id)
        if parent is None or not parent.is_active:
            raise ValueError("Hauptkategorie zuerst reaktivieren")
    category.is_active = active
    category.updated_at = datetime.now(UTC).replace(tzinfo=None)
    return category


def hard_delete_category(db: Session, *, category_id: int) -> None:
    category = db.get(Category, category_id)
    if category is None:
        raise ValueError("Kategorie nicht gefunden")
    raise ValueError("Kategorien werden nicht gelöscht; bitte deaktivieren")


def _find_category(db: Session, *, name: str, parent_id: int | None) -> Category | None:
    normalized = normalized_category_name(name)
    return next(
        (
            category
            for category in db.scalars(select(Category).where(Category.parent_id == parent_id))
            if normalized_category_name(category.name) == normalized
        ),
        None,
    )


def _ensure_named_category(
    db: Session,
    *,
    name: str,
    parent_id: int | None,
    aliases: tuple[str, ...] = (),
) -> tuple[Category, bool]:
    category = _find_category(db, name=name, parent_id=parent_id)
    changed = False
    if category is None:
        for alias in aliases:
            category = _find_category(db, name=alias, parent_id=parent_id)
            if category is not None:
                category.name = name
                category.updated_at = datetime.now(UTC).replace(tzinfo=None)
                changed = True
                break
    if category is not None and category.name != name:
        category.name = name
        category.updated_at = datetime.now(UTC).replace(tzinfo=None)
        changed = True
    if category is None:
        category = create_category(db, name=name, parent_id=parent_id)
        changed = True
    if not category.is_active:
        category.is_active = True
        category.updated_at = datetime.now(UTC).replace(tzinfo=None)
        changed = True
    return category, changed


def ensure_optimized_category_hierarchy(db: Session) -> int:
    changes = 0
    for parent_name, child_names in OPTIMIZED_CATEGORY_HIERARCHY.items():
        parent, changed = _ensure_named_category(
            db,
            name=parent_name,
            parent_id=None,
            aliases=PARENT_ALIASES.get(parent_name, ()),
        )
        changes += int(changed)
        for child_name in child_names:
            _, child_changed = _ensure_named_category(
                db,
                name=child_name,
                parent_id=parent.id,
                aliases=CHILD_ALIASES.get((parent_name, child_name), ()),
            )
            changes += int(child_changed)
    return changes


def category_by_path(db: Session, parent_name: str, child_name: str | None) -> Category:
    parent = _find_category(db, name=parent_name, parent_id=None)
    if parent is None:
        raise ValueError(f"Kategoriepfad fehlt: {parent_name}")
    if child_name is None:
        return parent
    child = _find_category(db, name=child_name, parent_id=parent.id)
    if child is None:
        raise ValueError(f"Kategoriepfad fehlt: {parent_name} / {child_name}")
    return child


def category_selector_groups(
    db: Session, *, include_inactive: bool = False
) -> tuple[CategorySelectorGroup, ...]:
    query = select(Category).options(selectinload(Category.parent))
    if not include_inactive:
        query = query.where(Category.is_active)
    categories = list(db.scalars(query))
    parents = sorted(
        (category for category in categories if category.parent_id is None),
        key=lambda category: normalized_category_name(category.name),
    )
    return tuple(
        CategorySelectorGroup(
            parent=parent,
            children=tuple(
                sorted(
                    (category for category in categories if category.parent_id == parent.id),
                    key=lambda category: normalized_category_name(category.name),
                )
            ),
        )
        for parent in parents
    )


def category_groups(db: Session) -> tuple[CategoryGroup, ...]:
    categories = list(
        db.scalars(
            select(Category)
            .options(selectinload(Category.parent))
            .order_by(Category.sort_order, Category.name)
        )
    )
    event_counts = dict(
        db.execute(
            select(EconomicEvent.category_id, func.count(EconomicEvent.id))
            .where(
                EconomicEvent.category_id.is_not(None),
                EconomicEvent.status.in_(("booked", "confirmed")),
            )
            .group_by(EconomicEvent.category_id)
        ).all()
    )
    pending_counts = dict(
        db.execute(
            select(
                CategoryAssignmentDecision.category_id,
                func.count(CategoryAssignmentDecision.id),
            )
            .where(CategoryAssignmentDecision.economic_event_id.is_(None))
            .group_by(CategoryAssignmentDecision.category_id)
        ).all()
    )
    managed = {
        category.id: ManagedCategory(
            category=category,
            event_count=event_counts.get(category.id, 0),
            pending_decision_count=pending_counts.get(category.id, 0),
        )
        for category in categories
    }
    parents = sorted(
        (category for category in categories if category.parent_id is None),
        key=lambda category: normalized_category_name(category.name),
    )
    return tuple(
        CategoryGroup(
            parent=managed[parent.id],
            children=tuple(
                managed[child.id]
                for child in sorted(
                    (item for item in categories if item.parent_id == parent.id),
                    key=lambda item: normalized_category_name(item.name),
                )
            ),
        )
        for parent in parents
    )
