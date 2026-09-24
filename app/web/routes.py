from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.config import get_settings
from app.db.models import (
    Account,
    Category,
    EconomicEvent,
    Envelope,
    ImportBatch,
    Project,
    RawImportRecord,
    ReviewItem,
    SourceTransaction,
    SourceTransactionAccount,
)
from app.db.session import get_db
from app.importers.paypal import (
    PayPalFormatError,
    import_paypal_batch,
    preview_paypal_file,
)
from app.importers.sparda import SpardaFormatError, import_sparda_batch, preview_sparda_file
from app.services.backup import create_backup
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
from app.services.categories import (
    category_groups,
    create_category,
    rename_category,
    set_category_active,
)
from app.services.dashboard import build_dashboard
from app.services.diagnostics import run_diagnostics
from app.services.envelope_assignments import (
    apply_assignment_decisions,
    build_assignment_workspace,
    rule_condition_label,
    update_suggestion_rule,
)
from app.services.import_staging import batch_file_path, stage_upload
from app.services.reviews import build_review_view
from app.services.transaction_details import event_transaction_views
from app.services.transaction_review import (
    apply_transaction_decision,
    build_transaction_review,
    create_project,
)
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
        transaction_views=event_transaction_views(db, events),
    )


@router.get("/envelopes", response_class=HTMLResponse)
def envelopes(request: Request, db: DbSession) -> HTMLResponse:
    data = build_dashboard(db)
    data["assignment_progress"] = build_assignment_workspace(db).progress
    return render(request, "envelopes.html", active="envelopes", page_title="Umschläge", **data)


@router.get("/envelope-assignments", response_class=HTMLResponse)
def envelope_assignments(request: Request, db: DbSession) -> HTMLResponse:
    params = request.query_params
    try:
        category_id = int(params["category"]) if params.get("category") else None
        account_id = int(params["account"]) if params.get("account") else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid assignment filter") from exc
    workspace = build_assignment_workspace(
        db,
        month=params.get("month"),
        merchant=params.get("merchant"),
        category_id=category_id,
        account_id=account_id,
        state=params.get("state", "unresolved"),
        group_by=params.get("group_by", "priority"),
    )
    query = str(request.url.query)
    return_to = request.url.path + (f"?{query}" if query else "") + "#workspace"
    rule_rows = [
        {
            "rule": rule,
            "condition": rule_condition_label(rule, db),
            "category_id": (rule.action_json or {}).get("category_id"),
            "envelope_id": (rule.action_json or {}).get("envelope_id"),
            "envelope_decision": (rule.action_json or {}).get("envelope_decision"),
        }
        for rule in workspace.rules
    ]
    return render(
        request,
        "envelope_assignments.html",
        active="envelope_assignment",
        page_title="Umschlag-Zuordnung",
        workspace=workspace,
        filters=params,
        return_to=return_to,
        rule_rows=rule_rows,
        error=params.get("error"),
    )


def _assignment_redirect(
    return_to: str | None,
    error: str | None = None,
    selected_category_id: int | None = None,
) -> str:
    from urllib.parse import parse_qsl, quote, urlencode

    target = (
        return_to
        if return_to and return_to.startswith("/envelope-assignments")
        else "/envelope-assignments"
    )
    if error:
        separator = "&" if "?" in target else "?"
        fragment = ""
        if "#" in target:
            target, fragment = target.split("#", 1)
            fragment = f"#{fragment}"
        target = f"{target}{separator}error={quote(error)}{fragment}"
    elif selected_category_id is not None:
        fragment = ""
        if "#" in target:
            target, fragment = target.split("#", 1)
            fragment = f"#{fragment}"
        path, separator, query = target.partition("?")
        params = dict(parse_qsl(query, keep_blank_values=True)) if separator else {}
        params["new_category"] = str(selected_category_id)
        target = f"{path}?{urlencode(params)}{fragment}"
    return target


@router.post("/envelope-assignments/decide")
def decide_envelope_assignments(
    request: Request,
    db: DbSession,
    candidate_keys: Annotated[list[str], Form()],
    decision: Annotated[str | None, Form()] = None,
    operation: Annotated[str | None, Form()] = None,
    envelope_id: Annotated[int | None, Form()] = None,
    envelope_mode: Annotated[str | None, Form()] = None,
    category_id: Annotated[int | None, Form()] = None,
    create_rule: Annotated[bool, Form()] = False,
    rule_basis: Annotated[str, Form()] = "auto",
    return_to: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    if get_settings().demo_mode:
        raise HTTPException(
            status_code=409, detail="Umschlag-Zuordnungen sind privatprofilgebunden"
        )
    try:
        apply_category = operation in {"category", "combined", "category_no_envelope"} or (
            operation == "rule" and category_id is not None
        )
        if operation in {"envelope", "combined"}:
            decision = "assigned"
        elif operation in {"no_envelope", "category_no_envelope"}:
            decision = "no_envelope"
        elif operation == "later":
            decision = "later"
        elif operation == "category":
            decision = None
        elif operation == "rule":
            decision = envelope_mode if envelope_mode in {"assigned", "no_envelope"} else None
            create_rule = True
        apply_assignment_decisions(
            db,
            keys=candidate_keys,
            decision=decision,
            envelope_id=envelope_id,
            category_id=category_id,
            apply_category=apply_category,
            create_rule=create_rule or request.query_params.get("create_rule") == "true",
            rule_basis=rule_basis,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(_assignment_redirect(return_to, str(exc)), status_code=303)
    return RedirectResponse(_assignment_redirect(return_to), status_code=303)


@router.post("/envelope-assignments/rules/{rule_id}")
def edit_envelope_rule(
    rule_id: int,
    db: DbSession,
    priority: Annotated[int, Form()],
    envelope_id: Annotated[int | None, Form()] = None,
    category_id: Annotated[int | None, Form()] = None,
    envelope_mode: Annotated[str | None, Form()] = None,
    enabled: Annotated[bool, Form()] = False,
    return_to: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    if get_settings().demo_mode:
        raise HTTPException(status_code=409, detail="Umschlag-Regeln sind privatprofilgebunden")
    try:
        update_suggestion_rule(
            db,
            rule_id=rule_id,
            priority=priority,
            enabled=enabled,
            category_id=category_id,
            envelope_decision=envelope_mode,
            envelope_id=envelope_id,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(_assignment_redirect(return_to, str(exc)), status_code=303)
    return RedirectResponse(_assignment_redirect(return_to), status_code=303)


@router.post("/envelope-assignments/categories")
def add_review_subcategory(
    db: DbSession,
    name: Annotated[str, Form()],
    parent_id: Annotated[int | None, Form()] = None,
    return_to: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    try:
        category = create_category(db, parent_id=parent_id, name=name)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(_assignment_redirect(return_to, str(exc)), status_code=303)
    return RedirectResponse(
        _assignment_redirect(return_to, selected_category_id=category.id), status_code=303
    )


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
    project_stats = {
        project.id: {
            "count": db.scalar(
                select(func.count(EconomicEvent.id)).where(EconomicEvent.project_id == project.id)
            )
            or 0,
            "expenses": db.scalar(
                select(func.coalesce(func.sum(EconomicEvent.amount), 0)).where(
                    EconomicEvent.project_id == project.id, EconomicEvent.event_type == "expense"
                )
            )
            or 0,
            "refunds": db.scalar(
                select(func.coalesce(func.sum(EconomicEvent.amount), 0)).where(
                    EconomicEvent.project_id == project.id, EconomicEvent.event_type == "refund"
                )
            )
            or 0,
            "first": db.scalar(
                select(func.min(EconomicEvent.occurred_at)).where(
                    EconomicEvent.project_id == project.id
                )
            ),
            "last": db.scalar(
                select(func.max(EconomicEvent.occurred_at)).where(
                    EconomicEvent.project_id == project.id
                )
            ),
        }
        for project in rows
    }
    return render(
        request,
        "projects.html",
        active="projects",
        page_title="Projekte",
        projects=rows,
        project_stats=project_stats,
    )


@router.post("/projects")
def add_project(
    db: DbSession,
    name: Annotated[str, Form()],
    starts_at: Annotated[str | None, Form()] = None,
    ends_at: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    try:
        create_project(
            db,
            name=name,
            starts_at=date.fromisoformat(starts_at) if starts_at else None,
            ends_at=date.fromisoformat(ends_at) if ends_at else None,
            notes=notes,
        )
        db.commit()
    except (ValueError, OverflowError) as exc:
        db.rollback()
        return RedirectResponse(f"/projects?error={str(exc)}", status_code=303)
    return RedirectResponse("/projects", status_code=303)


@router.post("/projects/{project_id}/status")
def project_status(
    project_id: int, db: DbSession, active: Annotated[bool, Form()] = True
) -> RedirectResponse:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    project.status = "active" if active else "archived"
    db.commit()
    return RedirectResponse("/projects", status_code=303)


@router.post("/projects/{project_id}/rename")
def project_rename(
    project_id: int, db: DbSession, name: Annotated[str, Form()]
) -> RedirectResponse:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    clean = " ".join(name.split())
    if not clean:
        return RedirectResponse("/projects?error=Projektname+leer", status_code=303)
    duplicate = db.scalar(
        select(Project).where(Project.id != project_id, Project.name.ilike(clean))
    )
    if duplicate is not None:
        return RedirectResponse("/projects?error=Projekt+existiert+bereits", status_code=303)
    project.name = clean
    db.commit()
    return RedirectResponse("/projects", status_code=303)


@router.get("/transaction-review", response_class=HTMLResponse)
def transaction_review(request: Request, db: DbSession) -> HTMLResponse:
    params = request.query_params

    def optional_int(name: str) -> int | None:
        value = params.get(name)
        return int(value) if value else None

    rows, progress = build_transaction_review(
        db,
        sort=params.get("sort", "newest"),
        month=params.get("month"),
        date_from=params.get("date_from"),
        date_to=params.get("date_to"),
        merchant=params.get("merchant"),
        economic_type=params.get("economic_type"),
        account_id=optional_int("account"),
        category_id=optional_int("category"),
        envelope_id=optional_int("envelope"),
        project_id=optional_int("project"),
        unresolved_only=params.get("unresolved") == "true",
    )
    return render(
        request,
        "transaction_review.html",
        active="transaction_review",
        page_title="Transaktionsprüfung",
        rows=rows,
        progress=progress,
        projects=list(
            db.scalars(select(Project).where(Project.status == "active").order_by(Project.name))
        ),
        accounts=list(db.scalars(select(Account).where(Account.is_active).order_by(Account.name))),
        categories=list(
            db.scalars(select(Category).where(Category.is_active).order_by(Category.name))
        ),
        envelopes=list(
            db.scalars(select(Envelope).where(Envelope.is_active).order_by(Envelope.sort_order))
        ),
        filters=params,
    )


@router.post("/transaction-review/decide")
def decide_transaction_review(
    db: DbSession,
    candidate_keys: Annotated[list[str], Form()],
    economic_type: Annotated[str | None, Form()] = None,
    category_id: Annotated[int | None, Form()] = None,
    envelope_decision: Annotated[str | None, Form()] = None,
    envelope_id: Annotated[int | None, Form()] = None,
    project_decision: Annotated[str | None, Form()] = None,
    project_id: Annotated[int | None, Form()] = None,
    create_rule: Annotated[bool, Form()] = False,
    return_to: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    try:
        if envelope_id is not None and envelope_decision is None:
            envelope_decision = "assigned"
        if project_id is not None and project_decision is None:
            project_decision = "assigned"
        apply_transaction_decision(
            db,
            candidate_keys=candidate_keys,
            economic_type=economic_type,
            category_id=category_id,
            envelope_decision=envelope_decision,
            envelope_id=envelope_id,
            project_decision=project_decision,
            project_id=project_id,
            create_rule=create_rule,
        )
        db.commit()
    except ValueError:
        db.rollback()
    return RedirectResponse(
        return_to
        if return_to and (return_to.startswith("/transaction-review") or return_to == "/review")
        else "/transaction-review",
        status_code=303,
    )


@router.get("/categories", response_class=HTMLResponse)
def categories(request: Request, db: DbSession) -> HTMLResponse:
    return render(
        request,
        "categories.html",
        active="categories",
        page_title="Kategorien",
        category_groups=category_groups(db),
        error=request.query_params.get("error"),
    )


def _category_redirect(error: str | None = None) -> str:
    from urllib.parse import quote

    return f"/categories?error={quote(error)}" if error else "/categories"


@router.post("/categories")
def add_category(
    db: DbSession,
    name: Annotated[str, Form()],
    parent_id: Annotated[int | None, Form()] = None,
) -> RedirectResponse:
    try:
        create_category(db, name=name, parent_id=parent_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(_category_redirect(str(exc)), status_code=303)
    return RedirectResponse(_category_redirect(), status_code=303)


@router.post("/categories/{category_id}/rename")
def rename_category_route(
    category_id: int,
    db: DbSession,
    name: Annotated[str, Form()],
) -> RedirectResponse:
    try:
        rename_category(db, category_id=category_id, name=name)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(_category_redirect(str(exc)), status_code=303)
    return RedirectResponse(_category_redirect(), status_code=303)


@router.post("/categories/{category_id}/status")
def category_status_route(
    category_id: int,
    db: DbSession,
    active: Annotated[bool, Form()],
) -> RedirectResponse:
    try:
        set_category_active(db, category_id=category_id, active=active)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(_category_redirect(str(exc)), status_code=303)
    return RedirectResponse(_category_redirect(), status_code=303)


@router.get("/review", response_class=HTMLResponse)
def review(request: Request, db: DbSession) -> HTMLResponse:
    rows = list(
        db.scalars(
            select(ReviewItem)
            .options(
                selectinload(ReviewItem.economic_event),
                selectinload(ReviewItem.proposed_category),
                selectinload(ReviewItem.proposed_envelope),
                selectinload(ReviewItem.proposed_project),
                selectinload(ReviewItem.source_transaction)
                .selectinload(SourceTransaction.account_links)
                .selectinload(SourceTransactionAccount.account),
            )
            .order_by(ReviewItem.status, ReviewItem.id)
        )
    )
    raw_ids = {
        row.source_transaction.raw_record_id
        for row in rows
        if row.source_transaction and row.source_transaction.raw_record_id is not None
    }
    raw_by_id = {
        raw.id: raw
        for raw in db.scalars(select(RawImportRecord).where(RawImportRecord.id.in_(raw_ids)))
    }
    return render(
        request,
        "review.html",
        active="review",
        page_title="Prüfen",
        reviews=[
            build_review_view(
                row,
                raw_by_id.get(row.source_transaction.raw_record_id)
                if row.source_transaction
                else None,
            )
            for row in rows
        ],
        categories=list(
            db.scalars(select(Category).where(Category.is_active).order_by(Category.name))
        ),
        envelopes=list(
            db.scalars(select(Envelope).where(Envelope.is_active).order_by(Envelope.sort_order))
        ),
        projects=list(
            db.scalars(select(Project).where(Project.status == "active").order_by(Project.name))
        ),
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
    if result.batch.source_type in {"sparda", "paypal"} and result.batch.status == "valid":
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
def preview_import(request: Request, batch_id: int, db: DbSession) -> HTMLResponse:
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.source_type not in {"sparda", "paypal"} or batch.status != "valid":
        raise HTTPException(status_code=404, detail="Valid import batch not found")
    path = batch_file_path(batch, get_settings())
    if batch.source_type == "paypal":
        try:
            rows, summary, _matches = preview_paypal_file(path, db)
        except PayPalFormatError as exc:
            raise HTTPException(status_code=422, detail=exc.code) from exc
        return render(
            request,
            "paypal_preview.html",
            active="import",
            page_title="PayPal-Vorschau",
            batch=batch,
            rows=rows[:100],
            summary=summary.as_dict(),
            truncated=len(rows) > 100,
        )
    try:
        rows, summary = preview_sparda_file(path)
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
def execute_import(batch_id: int, db: DbSession) -> RedirectResponse:
    if get_settings().demo_mode:
        raise HTTPException(
            status_code=409,
            detail="Produktive Importe sind im Demo-Profil gesperrt.",
        )
    batch = db.get(ImportBatch, batch_id)
    if batch is None or batch.source_type not in {"sparda", "paypal"} or batch.status != "valid":
        raise HTTPException(status_code=404, detail="Valid import batch not found")
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    if batch.source_type == "paypal":
        settings = get_settings()
        create_backup(
            settings.active_database_url,
            settings.active_backup_dir,
            backup_type="pre-paypal-import-safety",
        )
        import_paypal_batch(factory, batch_id, settings)
    else:
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
