"""webapp 用データアクセス層。

既存 core.database.get_connection() を再利用し、案件（items）・保存検索（saved_searches）は
読み取り流用、応札プロジェクト管理まわり（bid_projects / documents / watches / schedule_events）
は本モジュールが新規 DDL を追加してオーナーシップを持つ。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from typing import Any

from bid_aggregator.core.database import get_connection, insert_and_get_id

WEBAPP_DDL = """
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'watching' CHECK (status IN ('watching', 'declined')),
    decline_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS bid_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER,
    manual_title TEXT,
    manual_org TEXT,
    status TEXT NOT NULL DEFAULT '準備中'
        CHECK (status IN ('準備中', '提出済み', '結果待ち', '落札', '失注')),
    assignee TEXT,
    notes TEXT,
    price INTEGER,
    retrospective TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bid_project_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '未着手' CHECK (status IN ('未着手', '作成中', '完了')),
    assignee TEXT,
    due_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (bid_project_id) REFERENCES bid_projects(id)
);

CREATE TABLE IF NOT EXISTS schedule_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bid_project_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_date TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (bid_project_id) REFERENCES bid_projects(id)
);

CREATE INDEX IF NOT EXISTS idx_watches_item ON watches(item_id);
CREATE INDEX IF NOT EXISTS idx_bid_projects_status ON bid_projects(status);
CREATE INDEX IF NOT EXISTS idx_bid_projects_item ON bid_projects(item_id);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(bid_project_id);
CREATE INDEX IF NOT EXISTS idx_schedule_events_project ON schedule_events(bid_project_id);
"""

STATUS_ORDER = ["準備中", "提出済み", "結果待ち", "落札", "失注"]
BOARD_COLUMNS = ["準備中", "提出済み", "結果待ち", "落札失注"]  # 落札/失注は1列にまとめて表示


def init_webapp_db() -> None:
    with get_connection() as conn:
        conn.executescript(WEBAPP_DDL)
        conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    return dict(row)


def _rows_to_dicts(rows: list) -> list[dict]:
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------- items (read + filter)


def list_categories() -> list[str]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT DISTINCT category FROM items WHERE category IS NOT NULL AND category != '' ORDER BY category"
        )
        return [r[0] if not isinstance(r, dict) else r["category"] for r in cur.fetchall()]


def list_regions() -> list[str]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT DISTINCT region FROM items WHERE region IS NOT NULL AND region != '' ORDER BY region"
        )
        return [r[0] if not isinstance(r, dict) else r["region"] for r in cur.fetchall()]


def search_items_filtered(
    keyword: str = "",
    category: str = "",
    region: str = "",
    org: str = "",
    deadline: str = "",  # "" | "has" | "none"
    order_by: str = "newest",
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[dict], int]:
    with get_connection() as conn:
        conditions: list[str] = []
        params: list[Any] = []
        if keyword:
            conditions.append("(i.title LIKE ? OR i.body LIKE ? OR i.organization_name LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
        if category:
            conditions.append("i.category = ?")
            params.append(category)
        if region:
            conditions.append("i.region LIKE ?")
            params.append(f"%{region}%")
        if org:
            conditions.append("i.organization_name LIKE ?")
            params.append(f"%{org}%")
        if deadline == "has":
            conditions.append("i.deadline_at IS NOT NULL")
        elif deadline == "none":
            conditions.append("i.deadline_at IS NULL")

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        order_clause = (
            "CASE WHEN i.deadline_at IS NULL THEN 1 ELSE 0 END, i.deadline_at ASC"
            if order_by == "deadline"
            else "COALESCE(i.published_at, i.created_at) DESC"
        )

        count_sql = f"SELECT COUNT(*) AS c FROM items i WHERE {where_clause}"  # noqa: S608
        cur = conn.execute(count_sql, params)
        row = cur.fetchone()
        total = row[0] if not isinstance(row, dict) else row["c"]

        query_sql = f"""
            SELECT i.*, w.status AS watch_status
            FROM items i
            LEFT JOIN watches w ON w.item_id = i.id
            WHERE {where_clause}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
        """  # noqa: S608
        cur = conn.execute(query_sql, [*params, limit, offset])
        return _rows_to_dicts(cur.fetchall()), total


def get_item(item_id: int) -> dict | None:
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT i.*, w.status AS watch_status, w.decline_reason
            FROM items i
            LEFT JOIN watches w ON w.item_id = i.id
            WHERE i.id = ?
            """,
            (item_id,),
        )
        return _row_to_dict(cur.fetchone())


def list_watched_items(limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT i.*, w.status AS watch_status
            FROM items i
            JOIN watches w ON w.item_id = i.id AND w.status = 'watching'
            ORDER BY COALESCE(i.deadline_at, '9999-12-31') ASC
            LIMIT ?
            """,
            (limit,),
        )
        return _rows_to_dicts(cur.fetchall())


def count_new_items(days: int = 3) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS c FROM items WHERE created_at >= datetime('now', ?)",
            (f"-{days} days",),
        )
        row = cur.fetchone()
        return row[0] if not isinstance(row, dict) else row["c"]


# ---------------------------------------------------------------- watches


def set_watch(item_id: int, status: str = "watching", decline_reason: str | None = None) -> None:
    now = _now()
    with get_connection() as conn:
        existing = conn.execute("SELECT id FROM watches WHERE item_id = ?", (item_id,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE watches SET status = ?, decline_reason = ?, updated_at = ? WHERE item_id = ?",
                (status, decline_reason, now, item_id),
            )
        else:
            conn.execute(
                "INSERT INTO watches (item_id, status, decline_reason, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (item_id, status, decline_reason, now, now),
            )
        conn.commit()


def remove_watch(item_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM watches WHERE item_id = ?", (item_id,))
        conn.commit()


# ---------------------------------------------------------------- bid_projects


def create_bid_project(
    item_id: int | None,
    manual_title: str | None,
    manual_org: str | None,
    assignee: str,
    notes: str,
    deadline_event_date: str | None,
) -> int:
    now = _now()
    with get_connection() as conn:
        project_id = insert_and_get_id(
            conn,
            """
            INSERT INTO bid_projects
                (item_id, manual_title, manual_org, status, assignee, notes, created_at, updated_at)
            VALUES (?, ?, ?, '準備中', ?, ?, ?, ?)
            """,
            (item_id, manual_title, manual_org, assignee, notes, now, now),
        )
        if deadline_event_date:
            conn.execute(
                "INSERT INTO schedule_events (bid_project_id, event_type, event_date, note, created_at) "
                "VALUES (?, '応札締切', ?, NULL, ?)",
                (project_id, deadline_event_date, now),
            )
        conn.commit()
        return project_id


def get_bid_project(project_id: int) -> dict | None:
    with get_connection() as conn:
        cur = conn.execute(
            """
            SELECT bp.*, i.title AS item_title, i.organization_name AS item_org,
                   i.url AS item_url, i.category AS item_category, i.region AS item_region
            FROM bid_projects bp
            LEFT JOIN items i ON i.id = bp.item_id
            WHERE bp.id = ?
            """,
            (project_id,),
        )
        return _row_to_dict(cur.fetchone())


def list_bid_projects(status: str | None = None, assignee: str | None = None) -> list[dict]:
    with get_connection() as conn:
        conditions = []
        params: list[Any] = []
        if status:
            conditions.append("bp.status = ?")
            params.append(status)
        if assignee:
            conditions.append("bp.assignee = ?")
            params.append(assignee)
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        cur = conn.execute(
            f"""
            SELECT bp.*, i.title AS item_title, i.organization_name AS item_org
            FROM bid_projects bp
            LEFT JOIN items i ON i.id = bp.item_id
            WHERE {where_clause}
            ORDER BY bp.updated_at DESC
            """,  # noqa: S608
            params,
        )
        projects = _rows_to_dicts(cur.fetchall())

    for project in projects:
        project["deadline"] = get_project_deadline(project["id"])
        docs = list_documents(project["id"])
        project["doc_total"] = len(docs)
        project["doc_done"] = sum(1 for d in docs if d["status"] == "完了")
        project["progress_pct"] = (
            round(100 * project["doc_done"] / project["doc_total"]) if project["doc_total"] else 0
        )
    return projects


def update_bid_project_status(project_id: int, status: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE bid_projects SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), project_id),
        )
        conn.commit()


def update_bid_project_retrospective(project_id: int, retrospective: str, price: int | None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE bid_projects SET retrospective = ?, price = ?, updated_at = ? WHERE id = ?",
            (retrospective, price, _now(), project_id),
        )
        conn.commit()


def get_project_deadline(project_id: int) -> str | None:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT event_date FROM schedule_events WHERE bid_project_id = ? AND event_type = '応札締切' "
            "ORDER BY event_date ASC LIMIT 1",
            (project_id,),
        )
        row = cur.fetchone()
        if row:
            return row[0] if not isinstance(row, dict) else row["event_date"]
        return None


# ---------------------------------------------------------------- documents


def add_document(project_id: int, name: str, assignee: str = "", due_date: str = "") -> int:
    now = _now()
    with get_connection() as conn:
        doc_id = insert_and_get_id(
            conn,
            """
            INSERT INTO documents (bid_project_id, name, status, assignee, due_date, created_at, updated_at)
            VALUES (?, ?, '未着手', ?, ?, ?, ?)
            """,
            (project_id, name, assignee or None, due_date or None, now, now),
        )
        conn.commit()
        return doc_id


def list_documents(project_id: int) -> list[dict]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM documents WHERE bid_project_id = ? ORDER BY due_date IS NULL, due_date ASC, id ASC",
            (project_id,),
        )
        return _rows_to_dicts(cur.fetchall())


def update_document_status(doc_id: int, status: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE documents SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), doc_id),
        )
        conn.commit()


# ---------------------------------------------------------------- schedule_events


def add_schedule_event(project_id: int, event_type: str, event_date: str, note: str = "") -> int:
    with get_connection() as conn:
        event_id = insert_and_get_id(
            conn,
            "INSERT INTO schedule_events (bid_project_id, event_type, event_date, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, event_type, event_date, note or None, _now()),
        )
        conn.commit()
        return event_id


def list_schedule_events(project_id: int) -> list[dict]:
    with get_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM schedule_events WHERE bid_project_id = ? ORDER BY event_date ASC",
            (project_id,),
        )
        return _rows_to_dicts(cur.fetchall())


# ---------------------------------------------------------------- saved_searches (read + CRUD)


def list_saved_searches() -> list[dict]:
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM saved_searches ORDER BY created_at DESC")
        return _rows_to_dicts(cur.fetchall())


def create_saved_search(name: str, filters_json: str, order_by: str, only_new: bool) -> int:
    now = _now()
    with get_connection() as conn:
        search_id = insert_and_get_id(
            conn,
            """
            INSERT INTO saved_searches
                (name, filters_json, order_by, only_new, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (name, filters_json, order_by, 1 if only_new else 0, now, now),
        )
        conn.commit()
        return search_id


def delete_saved_search(search_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
        conn.commit()


def toggle_saved_search(search_id: int, enabled: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE saved_searches SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _now(), search_id),
        )
        conn.commit()


# ---------------------------------------------------------------- deadline helpers


def days_until(date_str: str | None) -> int | None:
    """ISO日付文字列から本日までの残日数を返す（負値=超過）。"""
    if not date_str:
        return None
    try:
        d = date_str[:10]
        target = date.fromisoformat(d)
    except ValueError:
        return None
    return (target - date.today()).days
