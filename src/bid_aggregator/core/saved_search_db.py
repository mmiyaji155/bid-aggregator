"""
保存検索データベース操作

保存検索、実行履歴、ヒット結果、通知履歴のCRUD操作を提供する。
"""

import json
from datetime import datetime, timezone

from bid_aggregator.core.database import get_connection, insert_and_get_id


def _now_utc() -> str:
    """現在時刻をUTC ISO8601形式で取得"""
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# saved_searches CRUD
# =============================================================================


def create_saved_search(
    name: str,
    filters: dict,
    query_ref: str | None = None,
    order_by: str = "newest",
    schedule: str | None = None,
    only_new: bool = True,
    enabled: bool = True,
) -> int:
    """保存検索を作成"""
    with get_connection() as conn:
        now = _now_utc()
        saved_search_id = insert_and_get_id(
            conn,
            """
            INSERT INTO saved_searches 
            (name, filters_json, query_ref, order_by, schedule, only_new, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                json.dumps(filters, ensure_ascii=False),
                query_ref,
                order_by,
                schedule,
                1 if only_new else 0,
                1 if enabled else 0,
                now,
                now,
            ),
        )
        conn.commit()
        return saved_search_id


def get_saved_search(name: str) -> dict | None:
    """保存検索を名前で取得"""
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM saved_searches WHERE name = ?",
            (name,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)


def get_saved_search_by_id(saved_search_id: int) -> dict | None:
    """保存検索をIDで取得"""
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT * FROM saved_searches WHERE id = ?",
            (saved_search_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)


def list_saved_searches(enabled_only: bool = False) -> list[dict]:
    """保存検索一覧を取得"""
    with get_connection() as conn:
        if enabled_only:
            cursor = conn.execute(
                "SELECT * FROM saved_searches WHERE enabled = 1 ORDER BY name"
            )
        else:
            cursor = conn.execute("SELECT * FROM saved_searches ORDER BY name")
        return [dict(row) for row in cursor.fetchall()]


def delete_saved_search(name: str) -> bool:
    """保存検索を削除"""
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM saved_searches WHERE name = ?",
            (name,),
        )
        conn.commit()
        return cursor.rowcount > 0


def update_saved_search_last_run(saved_search_id: int, last_run_at: str) -> None:
    """保存検索の最終実行日時を更新"""
    with get_connection() as conn:
        conn.execute(
            "UPDATE saved_searches SET last_run_at = ?, updated_at = ? WHERE id = ?",
            (last_run_at, _now_utc(), saved_search_id),
        )
        conn.commit()


# =============================================================================
# saved_search_runs CRUD
# =============================================================================


def create_saved_search_run(
    saved_search_id: int,
    query_ref: str | None = None,
    filters_snapshot: dict | None = None,
) -> int:
    """保存検索の実行履歴を作成"""
    with get_connection() as conn:
        now = _now_utc()
        run_id = insert_and_get_id(
            conn,
            """
            INSERT INTO saved_search_runs 
            (saved_search_id, query_ref, filters_snapshot, run_at, hit_count, status)
            VALUES (?, ?, ?, ?, 0, 'running')
            """,
            (
                saved_search_id,
                query_ref,
                json.dumps(filters_snapshot, ensure_ascii=False) if filters_snapshot else None,
                now,
            ),
        )
        conn.commit()
        return run_id


def update_saved_search_run(
    run_id: int,
    hit_count: int,
    status: str,
    error_message: str | None = None,
    notified_channels: list[str] | None = None,
    notify_status: str | None = None,
    notify_error: str | None = None,
) -> None:
    """保存検索の実行履歴を更新"""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE saved_search_runs SET
                hit_count = ?,
                status = ?,
                error_message = ?,
                notified_channels = ?,
                notify_status = ?,
                notify_error = ?
            WHERE id = ?
            """,
            (
                hit_count,
                status,
                error_message,
                json.dumps(notified_channels) if notified_channels else None,
                notify_status,
                notify_error,
                run_id,
            ),
        )
        conn.commit()


def get_last_run(saved_search_id: int) -> dict | None:
    """保存検索の最終実行履歴を取得"""
    with get_connection() as conn:
        cursor = conn.execute(
            """
            SELECT * FROM saved_search_runs 
            WHERE saved_search_id = ? AND status = 'ok'
            ORDER BY run_at DESC LIMIT 1
            """,
            (saved_search_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(row)


# =============================================================================
# saved_search_hits CRUD
# =============================================================================


def create_saved_search_hit(
    run_id: int,
    item_id: int,
    content_hash: str,
) -> int:
    """保存検索のヒット結果を作成"""
    with get_connection() as conn:
        now = _now_utc()
        hit_id = insert_and_get_id(
            conn,
            """
            INSERT INTO saved_search_hits 
            (saved_search_run_id, item_id, content_hash, matched_at)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, item_id, content_hash, now),
        )
        conn.commit()
        return hit_id


def get_previous_hit_item_ids(saved_search_id: int) -> set[int]:
    """過去にヒットしたitem_idの集合を取得"""
    with get_connection() as conn:
        cursor = conn.execute(
            """
            SELECT DISTINCT h.item_id 
            FROM saved_search_hits h
            JOIN saved_search_runs r ON h.saved_search_run_id = r.id
            WHERE r.saved_search_id = ? AND h.item_id IS NOT NULL
            """,
            (saved_search_id,),
        )
        return {row["item_id"] for row in cursor.fetchall()}


def mark_hits_notified(run_id: int) -> None:
    """ヒット結果を通知済みにマーク"""
    with get_connection() as conn:
        now = _now_utc()
        conn.execute(
            "UPDATE saved_search_hits SET notified_at = ? WHERE saved_search_run_id = ?",
            (now, run_id),
        )
        conn.commit()


# =============================================================================
# saved_search_notifications CRUD
# =============================================================================


def create_notification(
    run_id: int,
    channel: str,
    recipient: str,
    status: str,
    dedupe_key: str,
    error_message: str | None = None,
) -> int:
    """通知履歴を作成"""
    with get_connection() as conn:
        now = _now_utc()
        notification_id = insert_and_get_id(
            conn,
            """
            INSERT INTO saved_search_notifications 
            (saved_search_run_id, channel, recipient, status, attempt_count, last_attempt_at, error_message, dedupe_key)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (run_id, channel, recipient, status, now, error_message, dedupe_key),
        )
        conn.commit()
        return notification_id


def update_notification_status(
    notification_id: int,
    status: str,
    error_message: str | None = None,
) -> None:
    """通知ステータスを更新"""
    with get_connection() as conn:
        now = _now_utc()
        conn.execute(
            """
            UPDATE saved_search_notifications SET
                status = ?,
                attempt_count = attempt_count + 1,
                last_attempt_at = ?,
                error_message = ?
            WHERE id = ?
            """,
            (status, now, error_message, notification_id),
        )
        conn.commit()


def get_failed_notifications(max_attempts: int = 3) -> list[dict]:
    """再試行対象の失敗した通知を取得"""
    with get_connection() as conn:
        cursor = conn.execute(
            """
            SELECT * FROM saved_search_notifications
            WHERE status = 'failed' AND attempt_count < ?
            ORDER BY last_attempt_at ASC
            """,
            (max_attempts,),
        )
        return [dict(row) for row in cursor.fetchall()]
