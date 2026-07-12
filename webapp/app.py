"""gov-bid 入札管理アプリ MVP — FastAPI エントリポイント。

起動:
    cd bid-aggregator
    .venv/bin/uvicorn webapp.app:app --reload --port 8000

APP-DESIGN.md / tokens.json: ops/mya--general/output/gov-bid-app/brand/（正本。将来 mya--gov-bid へ移管）
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from webapp import db

BASE_DIR = Path(__file__).parent

app = FastAPI(title="gov-bid 入札管理アプリ（MVP）")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------------------------------------------------------------- basic auth


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    """BASIC_AUTH_USERNAME / BASIC_AUTH_PASSWORD が両方設定されている時だけ Basic 認証を要求する。

    未設定（ローカル開発時など）は認証を素通しする。
    """
    username = os.environ.get("BASIC_AUTH_USERNAME")
    password = os.environ.get("BASIC_AUTH_PASSWORD")
    if not username or not password:
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        import base64

        try:
            decoded = base64.b64decode(auth_header[len("Basic ") :]).decode("utf-8")
            supplied_user, _, supplied_pass = decoded.partition(":")
        except Exception:  # noqa: BLE001
            supplied_user, supplied_pass = "", ""
        if secrets.compare_digest(supplied_user, username) and secrets.compare_digest(
            supplied_pass, password
        ):
            return await call_next(request)

    return Response(
        content="Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="gov-bid"'},
    )


@app.on_event("startup")
def _startup() -> None:
    db.init_webapp_db()


# ---------------------------------------------------------------- template filters


def deadline_badge(date_str: str | None) -> dict[str, str]:
    """締切残日数から表示バッジ情報を返す（機能色: warning/overdue/neutral）。"""
    if not date_str:
        return {"label": "締切未設定", "css": "badge-neutral"}
    days = db.days_until(date_str)
    if days is None:
        return {"label": "締切未設定", "css": "badge-neutral"}
    if days < 0:
        return {"label": f"超過 {abs(days)}日", "css": "badge-overdue"}
    if days == 0:
        return {"label": "本日締切", "css": "badge-overdue"}
    if days <= 5:
        return {"label": f"あと{days}日", "css": "badge-warning"}
    return {"label": f"あと{days}日", "css": "badge-neutral"}


def status_badge_css(status: str) -> str:
    return {
        "準備中": "badge-progress",
        "提出済み": "badge-progress",
        "結果待ち": "badge-progress",
        "落札": "badge-complete",
        "失注": "badge-overdue",
    }.get(status, "badge-neutral")


def fromjson(value: str | None) -> dict:
    if not value:
        return {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


templates.env.filters["deadline_badge"] = deadline_badge
templates.env.filters["status_badge_css"] = status_badge_css
templates.env.filters["days_until"] = db.days_until
templates.env.filters["fromjson"] = fromjson

PAGE_SIZE = 20


def _demo_state(request: Request) -> str | None:
    """QA/レビュー用: ?_demo_state=loading|empty|error で状態を強制表示する（実データは変更しない）。"""
    state = request.query_params.get("_demo_state")
    if state in ("loading", "empty", "error"):
        return state
    return None


def _with_query(url: str, **params: str | None) -> str:
    """URL の既存クエリを保持したまま追加パラメータを付与する。"""
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value is not None:
            query[key] = value
    new_query = urlencode(query, quote_via=quote)
    return urlunparse(parsed._replace(query=new_query))


def _redirect_with_toast(
    url: str, message: str, *, moved: int | None = None, status_code: int = 303
) -> RedirectResponse:
    """トースト表示用の ?toast= (と、必要なら &moved=) を付与してリダイレクトする。"""
    target = _with_query(url, toast=message, moved=str(moved) if moved is not None else None)
    return RedirectResponse(url=target, status_code=status_code)


# ---------------------------------------------------------------- dashboard


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard")
def dashboard(request: Request):
    demo = _demo_state(request)
    ctx: dict[str, Any] = {"request": request, "active_nav": "dashboard", "demo_state": demo}
    if demo == "error":
        return templates.TemplateResponse(request, "dashboard.html", ctx)
    try:
        new_items_count = 0 if demo == "empty" else db.count_new_items(days=3)
        projects = [] if demo == "empty" else db.list_bid_projects()
        deadline_soon = sorted(
            (p for p in projects if p.get("deadline") and db.days_until(p["deadline"]) is not None),
            key=lambda p: db.days_until(p["deadline"]),
        )[:5]
        watched = [] if demo == "empty" else db.list_watched_items(limit=5)
        saved_searches = [] if demo == "empty" else db.list_saved_searches()
        ctx.update(
            new_items_count=new_items_count,
            deadline_soon=deadline_soon,
            project_count=len(projects),
            watched=watched,
            saved_searches=saved_searches,
        )
    except Exception as exc:  # noqa: BLE001
        ctx["demo_state"] = "error"
        ctx["error_message"] = str(exc)
    return templates.TemplateResponse(request, "dashboard.html", ctx)


# ---------------------------------------------------------------- items list


@app.get("/items")
def items_list(
    request: Request,
    q: str = "",
    category: str = "",
    region: str = "",
    deadline: str = "",
    order_by: str = "newest",
    view: str = "all",  # all | watching
    page: int = 1,
):
    demo = _demo_state(request)
    ctx: dict[str, Any] = {
        "request": request,
        "active_nav": "items",
        "demo_state": demo,
        "q": q,
        "category": category,
        "region": region,
        "deadline": deadline,
        "order_by": order_by,
        "view": view,
        "categories": db.list_categories(),
        "regions": db.list_regions(),
    }
    if demo == "error":
        return templates.TemplateResponse(request, "items_list.html", ctx)
    try:
        if view == "watching":
            all_watched = db.list_watched_items(limit=500)
            total = len(all_watched)
            items = all_watched[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
        elif demo == "empty":
            items, total = [], 0
        else:
            items, total = db.search_items_filtered(
                keyword=q,
                category=category,
                region=region,
                org="",
                deadline=deadline,
                order_by=order_by,
                limit=PAGE_SIZE,
                offset=(page - 1) * PAGE_SIZE,
            )
        has_filters = bool(q or category or region or deadline)
        ctx.update(
            items=items,
            total=total,
            page=page,
            page_count=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            has_filters=has_filters,
        )
    except Exception as exc:  # noqa: BLE001
        ctx["demo_state"] = "error"
        ctx["error_message"] = str(exc)
    return templates.TemplateResponse(request, "items_list.html", ctx)


@app.post("/items/{item_id}/watch")
def toggle_watch(item_id: int, action: str = Form(...), next: str = Form("/items")):
    if action == "watch":
        db.set_watch(item_id, status="watching")
        message = "ウォッチに登録しました"
    elif action == "unwatch":
        db.remove_watch(item_id)
        message = "ウォッチを解除しました"
    else:
        message = "更新しました"
    return _redirect_with_toast(next, message)


@app.post("/items/bulk-watch")
def bulk_watch(item_ids: list[int] = Form(...), next: str = Form("/items")):
    for item_id in item_ids:
        db.set_watch(item_id, status="watching")
    return _redirect_with_toast(next, f"{len(item_ids)}件をウォッチしました")


@app.post("/items/bulk-decline")
def bulk_decline(item_ids: list[int] = Form(...), reason: str = Form(""), next: str = Form("/items")):
    for item_id in item_ids:
        db.set_watch(item_id, status="declined", decline_reason=reason)
    return _redirect_with_toast(next, f"{len(item_ids)}件を見送りにしました")


# ---------------------------------------------------------------- item detail


@app.get("/items/{item_id}")
def item_detail(request: Request, item_id: int):
    demo = _demo_state(request)
    ctx: dict[str, Any] = {"request": request, "active_nav": "items", "demo_state": demo, "item_id": item_id}
    if demo == "error":
        return templates.TemplateResponse(request, "item_detail.html", ctx)
    try:
        item = db.get_item(item_id)
        ctx["item"] = item
        ctx["deadline_days"] = db.days_until(item["deadline_at"]) if item else None
    except Exception as exc:  # noqa: BLE001
        ctx["demo_state"] = "error"
        ctx["error_message"] = str(exc)
    return templates.TemplateResponse(request, "item_detail.html", ctx)


@app.post("/items/{item_id}/bid")
def bid_on_item(
    item_id: int,
    assignee: str = Form(...),
    notes: str = Form(""),
    deadline_date: str = Form(""),
):
    project_id = db.create_bid_project(
        item_id=item_id,
        manual_title=None,
        manual_org=None,
        assignee=assignee,
        notes=notes,
        deadline_event_date=deadline_date or None,
    )
    return _redirect_with_toast(f"/board/projects/{project_id}", "応札プロジェクトを作成しました")


@app.post("/items/{item_id}/decline")
def decline_item(item_id: int, reason: str = Form("")):
    db.set_watch(item_id, status="declined", decline_reason=reason)
    return _redirect_with_toast("/items", "案件を見送りにしました")


# ---------------------------------------------------------------- board (kanban)


@app.get("/board")
def board(request: Request):
    demo = _demo_state(request)
    moved_raw = request.query_params.get("moved")
    moved_id = int(moved_raw) if moved_raw and moved_raw.isdigit() else None
    ctx: dict[str, Any] = {"request": request, "active_nav": "board", "demo_state": demo, "moved_id": moved_id}
    if demo == "error":
        return templates.TemplateResponse(request, "board.html", ctx)
    try:
        projects = [] if demo == "empty" else db.list_bid_projects()
        columns: dict[str, list[dict]] = {"準備中": [], "提出済み": [], "結果待ち": [], "落札失注": []}
        for p in projects:
            key = p["status"] if p["status"] in ("準備中", "提出済み", "結果待ち") else "落札失注"
            columns[key].append(p)
        ctx["columns"] = columns
    except Exception as exc:  # noqa: BLE001
        ctx["demo_state"] = "error"
        ctx["error_message"] = str(exc)
    return templates.TemplateResponse(request, "board.html", ctx)


@app.post("/board/quick-add")
def quick_add_project(title: str = Form(...), org: str = Form(""), assignee: str = Form("")):
    project_id = db.create_bid_project(
        item_id=None,
        manual_title=title,
        manual_org=org,
        assignee=assignee,
        notes="",
        deadline_event_date=None,
    )
    return _redirect_with_toast(f"/board/projects/{project_id}", "応札プロジェクトを作成しました")


@app.post("/board/projects/{project_id}/status")
def update_project_status(project_id: int, status: str = Form(...), next: str = Form("/board")):
    db.update_bid_project_status(project_id, status)
    return _redirect_with_toast(next, f"ステータスを「{status}」に変更しました", moved=project_id)


# ---------------------------------------------------------------- project detail


@app.get("/board/projects/{project_id}")
def project_detail(request: Request, project_id: int):
    demo = _demo_state(request)
    ctx: dict[str, Any] = {
        "request": request,
        "active_nav": "board",
        "demo_state": demo,
        "project_id": project_id,
        "statuses": db.STATUS_ORDER,
    }
    if demo == "error":
        return templates.TemplateResponse(request, "project_detail.html", ctx)
    try:
        project = db.get_bid_project(project_id)
        ctx["project"] = project
        ctx["deadline"] = db.get_project_deadline(project_id)
        ctx["deadline_days"] = db.days_until(ctx["deadline"])
        ctx["documents"] = [] if demo == "empty" else db.list_documents(project_id)
        ctx["events"] = [] if demo == "empty" else db.list_schedule_events(project_id)
    except Exception as exc:  # noqa: BLE001
        ctx["demo_state"] = "error"
        ctx["error_message"] = str(exc)
    return templates.TemplateResponse(request, "project_detail.html", ctx)


@app.post("/board/projects/{project_id}/documents")
def add_document(project_id: int, name: str = Form(...), assignee: str = Form(""), due_date: str = Form("")):
    db.add_document(project_id, name=name, assignee=assignee, due_date=due_date)
    return _redirect_with_toast(f"/board/projects/{project_id}", "書類を更新しました")


@app.post("/board/projects/{project_id}/documents/{doc_id}/status")
def update_document_status(project_id: int, doc_id: int, status: str = Form(...)):
    db.update_document_status(doc_id, status)
    return _redirect_with_toast(f"/board/projects/{project_id}", "書類を更新しました")


@app.post("/board/projects/{project_id}/schedule")
def add_schedule(project_id: int, event_type: str = Form(...), event_date: str = Form(...), note: str = Form("")):
    db.add_schedule_event(project_id, event_type=event_type, event_date=event_date, note=note)
    return _redirect_with_toast(f"/board/projects/{project_id}", "予定を追加しました")


@app.post("/board/projects/{project_id}/retrospective")
def update_retrospective(project_id: int, retrospective: str = Form(""), price: str = Form("")):
    price_val = int(price) if price.strip().isdigit() else None
    db.update_bid_project_retrospective(project_id, retrospective=retrospective, price=price_val)
    return _redirect_with_toast(f"/board/projects/{project_id}", "振り返りメモを保存しました")


# ---------------------------------------------------------------- saved searches


@app.get("/saved-searches")
def saved_searches_page(request: Request):
    demo = _demo_state(request)
    ctx: dict[str, Any] = {"request": request, "active_nav": "saved_searches", "demo_state": demo}
    if demo == "error":
        return templates.TemplateResponse(request, "saved_searches.html", ctx)
    try:
        ctx["searches"] = [] if demo == "empty" else db.list_saved_searches()
    except Exception as exc:  # noqa: BLE001
        ctx["demo_state"] = "error"
        ctx["error_message"] = str(exc)
    return templates.TemplateResponse(request, "saved_searches.html", ctx)


@app.post("/saved-searches")
def create_saved_search(
    name: str = Form(...),
    keyword: str = Form(""),
    category: str = Form(""),
    region: str = Form(""),
    order_by: str = Form("newest"),
    only_new: bool = Form(True),
):
    filters = {"keyword": keyword, "category": category, "region": region}
    db.create_saved_search(name=name, filters_json=json.dumps(filters, ensure_ascii=False), order_by=order_by, only_new=only_new)
    return _redirect_with_toast("/saved-searches", "保存検索を作成しました")


@app.post("/saved-searches/{search_id}/delete")
def delete_saved_search(search_id: int):
    db.delete_saved_search(search_id)
    return _redirect_with_toast("/saved-searches", "保存検索を削除しました")


@app.post("/saved-searches/{search_id}/toggle")
def toggle_saved_search(search_id: int, enabled: bool = Form(...)):
    db.toggle_saved_search(search_id, enabled)
    return _redirect_with_toast("/saved-searches", f"保存検索を{'有効' if enabled else '無効'}にしました")
