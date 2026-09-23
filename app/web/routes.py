from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.config import get_settings
from app.db.models import (
    Account,
    Category,
    EconomicEvent,
    ImportBatch,
    Project,
    ReviewItem,
    SourceTransaction,
    SourceTransactionAccount,
)
from app.db.session import get_db
from app.importers.sparda import SpardaFormatError, import_sparda_batch, preview_sparda_file
from app.services.balance_confirmations import (
    SOURCE_LABELS,
    STATUS_LABELS,
    account_balance_view,
    account_balance_views,
    calculate_envelope_cash_total,
    confirmation_history,
    create_balance_confirmation,
    parse_money,
    vault_free_cash,
)
from app.services.dashboard import build_dashboard
from app.services.diagnostics import run_diagnostics
from app.services.import_staging import batch_file_path, stage_upload
from app.services.reviews import build_review_view
from app.web.templating import templates

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


def render(request: Request, template: str, **context: object) -> HTMLResponse:
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        template,
        {
            "request": request,
            "profile_label": "Demo-Profil" if settings.demo_mode else "Privat-Profil",
            "private_import_enabled": not settings.demo_mode,
            **context,
        },
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DbSession) -> HTMLResponse:
    return render(
        request, "dashboard.html", active="dashboard", page_title="Übersicht", **build_dashboard(db)
    )


@router.get("/transactions", response_class=HTMLResponse)
def transactions(request: Request, db: DbSession) -> HTMLResponse:
    events = list(
        db.scalars(
            select(EconomicEvent)
            .options(
                selectinload(EconomicEvent.account),
                selectinload(EconomicEvent.source_account),
                selectinload(EconomicEvent.target_account),
                selectinload(EconomicEvent.category),
                selectinload(EconomicEvent.envelope),
                selectinload(EconomicEvent.project),
            )
            .order_by(EconomicEvent.occurred_at.desc())
        )
    )
    return render(
        request,
        "transactions.html",
        active="transactions",
        page_title="Transaktionen",
        events=events,
    )


@router.get("/envelopes", response_class=HTMLResponse)
def envelopes(request: Request, db: DbSession) -> HTMLResponse:
    data = build_dashboard(db)
    return render(request, "envelopes.html", active="envelopes", page_title="Umschläge", **data)


@router.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request, db: DbSession) -> HTMLResponse:
    return render(
        request,
        "accounts.html",
        active="accounts",
        page_title="Konten",
        accounts=account_balance_views(db),
        source_labels=SOURCE_LABELS,
        status_labels=STATUS_LABELS,
    )


def _confirmation_context(account: Account, db: Session) -> dict[str, object]:
    view = account_balance_view(db, account)
    return {
        "account": account,
        "account_view": view,
        "history": confirmation_history(db, account.id),
        "source_labels": SOURCE_LABELS,
        "status_labels": STATUS_LABELS,
        "calculated_envelope_total": (
            calculate_envelope_cash_total(db) if account.account_type == "cash_vault" else None
        ),
        "free_vault_cash": vault_free_cash(view.current),
    }


@router.get("/accounts/{account_id}/confirm", response_class=HTMLResponse)
def confirm_account_balance(request: Request, account_id: int, db: DbSession) -> HTMLResponse:
    if get_settings().demo_mode:
        raise HTTPException(status_code=409, detail="Saldo-Bestätigungen sind privatprofilgebunden")
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return render(
        request,
        "account_confirmation.html",
        active="accounts",
        page_title="Saldo bestätigen",
        **_confirmation_context(account, db),
    )


@router.post("/accounts/{account_id}/confirm")
def save_account_balance(
    request: Request,
    account_id: int,
    db: DbSession,
    confirmed_at: Annotated[str, Form()],
    balance: Annotated[str, Form()],
    source_type: Annotated[str, Form()],
    status: Annotated[str, Form()],
    source_reference: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None,
    envelope_cash_total: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    if get_settings().demo_mode:
        raise HTTPException(status_code=409, detail="Saldo-Bestätigungen sind privatprofilgebunden")
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        timestamp = datetime.fromisoformat(confirmed_at)
        entered = parse_money(balance)
        envelope_total = parse_money(envelope_cash_total) if envelope_cash_total else None
        if account.account_type in {"cash_wallet", "cash_vault"}:
            source_type = "manual_count"
        create_balance_confirmation(
            db,
            account=account,
            confirmed_at=timestamp,
            entered_balance=entered,
            source_type=source_type,
            status=status,
            source_reference=source_reference,
            notes=notes,
            envelope_cash_total=envelope_total,
        )
        db.commit()
    except (ValueError, OverflowError) as exc:
        db.rollback()
        response = render(
            request,
            "account_confirmation.html",
            active="accounts",
            page_title="Saldo bestätigen",
            error=str(exc),
            **_confirmation_context(account, db),
        )
        response.status_code = 422
        return response
    return RedirectResponse(f"/accounts/{account_id}/confirm", status_code=303)


@router.get("/planning", response_class=HTMLResponse)
def planning(request: Request) -> HTMLResponse:
    return render(
        request,
        "placeholder.html",
        active="planning",
        page_title="Planung",
        section="Liquiditätsplanung",
        description="6–8-Wochen-Prognose, wiederkehrende Kosten und geplante Umschlagzuführungen.",
    )


@router.get("/projects", response_class=HTMLResponse)
def projects(request: Request, db: DbSession) -> HTMLResponse:
    rows = list(db.scalars(select(Project).order_by(Project.name)))
    return render(request, "projects.html", active="projects", page_title="Projekte", projects=rows)


@router.get("/categories", response_class=HTMLResponse)
def categories(request: Request, db: DbSession) -> HTMLResponse:
    rows = list(
        db.scalars(
            select(Category).where(Category.parent_id.is_(None)).order_by(Category.sort_order)
        )
    )
    return render(
        request, "categories.html", active="categories", page_title="Kategorien", categories=rows
    )


@router.get("/review", response_class=HTMLResponse)
def review(request: Request, db: DbSession) -> HTMLResponse:
    rows = list(
        db.scalars(
            select(ReviewItem)
            .options(
                selectinload(ReviewItem.economic_event),
                selectinload(ReviewItem.proposed_category),
                selectinload(ReviewItem.proposed_envelope),
                selectinload(ReviewItem.source_transaction)
                .selectinload(SourceTransaction.account_links)
                .selectinload(SourceTransactionAccount.account),
            )
            .order_by(ReviewItem.status, ReviewItem.id)
        )
    )
    return render(
        request,
        "review.html",
        active="review",
        page_title="Prüfen",
        reviews=[build_review_view(row) for row in rows],
    )


@router.get("/import", response_class=HTMLResponse)
def imports(
    request: Request,
    db: DbSession,
    duplicate: bool = False,
    batch_id: int | None = None,
) -> HTMLResponse:
    batches = list(db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(50)))
    selected_batch = db.get(ImportBatch, batch_id) if batch_id is not None else None
    return render(
        request,
        "import.html",
        active="import",
        page_title="Import",
        batches=batches,
        duplicate=duplicate,
        selected_batch=selected_batch,
    )


@router.post("/import/stage")
def upload_import(
    db: DbSession,
    source_type: Annotated[str, Form()],
    upload: Annotated[UploadFile, File()],
) -> RedirectResponse:
    result = stage_upload(
        db,
        source_type=source_type,
        original_filename=upload.filename or "upload",
        stream=upload.file,
        settings=get_settings(),
    )
    if result.batch.source_type == "sparda" and result.batch.status == "valid":
        if not result.duplicate:
            return RedirectResponse(f"/import/{result.batch.id}/preview", status_code=303)
        return RedirectResponse(
            f"/import?duplicate=true&batch_id={result.batch.id}", status_code=303
        )
    return RedirectResponse(
        f"/import?duplicate={str(result.duplicate).lower()}&batch_id={result.batch.id}",
        status_code=303,
    )


@router.get("/import/{batch_id}/preview", response_class=HTMLResponse)
def preview_sparda(request: Request, batch_id: int, db: DbSession) -> HTMLResponse:
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.source_type != "sparda" or batch.status != "valid":
        raise HTTPException(status_code=404, detail="Valid Sparda import batch not found")
    try:
        rows, summary = preview_sparda_file(batch_file_path(batch, get_settings()))
    except SpardaFormatError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc
    return render(
        request,
        "sparda_preview.html",
        active="import",
        page_title="Sparda-Vorschau",
        batch=batch,
        rows=rows[:100],
        summary=summary.as_dict(),
        truncated=len(rows) > 100,
    )


@router.post("/import/{batch_id}/execute")
def execute_sparda(batch_id: int, db: DbSession) -> RedirectResponse:
    if get_settings().demo_mode:
        raise HTTPException(
            status_code=409,
            detail="Produktive Sparda-Importe sind im Demo-Profil gesperrt.",
        )
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.source_type != "sparda" or batch.status != "valid":
        raise HTTPException(status_code=404, detail="Valid Sparda import batch not found")
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    import_sparda_batch(factory, batch_id, get_settings())
    return RedirectResponse(f"/import?batch_id={batch_id}", status_code=303)


@router.get("/diagnostics", response_class=HTMLResponse)
def diagnostics(request: Request, db: DbSession) -> HTMLResponse:
    return render(
        request,
        "diagnostics.html",
        active="settings",
        page_title="Diagnose",
        checks=run_diagnostics(db, get_settings()),
    )


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request) -> HTMLResponse:
    return render(
        request,
        "placeholder.html",
        active="settings",
        page_title="Einstellungen",
        section="Lokale Einstellungen",
        description=(
            "Datenbank, Importprofile, Regeln und Darstellungsoptionen – "
            "standardmäßig vollständig lokal."
        ),
    )
