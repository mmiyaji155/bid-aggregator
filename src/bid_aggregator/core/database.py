"""
データベース管理モジュール

SQLite/PostgreSQLデータベースの初期化、マイグレーション、CRUD操作を提供する。
"""

import hashlib
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Literal

from bid_aggregator.core.config import settings
from bid_aggregator.core.models import Item, RawFetch


# =============================================================================
# DDL（テーブル定義）
# =============================================================================

SQLITE_DDL_STATEMENTS = """
-- raw_fetch: 生データ保存
CREATE TABLE IF NOT EXISTS raw_fetch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    raw_hash TEXT NOT NULL,
    raw_payload BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_fetch_source ON raw_fetch(source);
CREATE INDEX IF NOT EXISTS idx_raw_fetch_fetched_at ON raw_fetch(fetched_at);
CREATE INDEX IF NOT EXISTS idx_raw_fetch_raw_hash ON raw_fetch(raw_hash);

-- items: 正規化案件データ
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_item_id TEXT,
    url TEXT,
    title TEXT NOT NULL,
    organization_name TEXT NOT NULL,
    published_at TEXT,
    deadline_at TEXT,
    category TEXT,
    region TEXT,
    body TEXT,
    body_hash TEXT,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_item 
    ON items(source, source_item_id) WHERE source_item_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_url 
    ON items(url) WHERE url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_items_content_hash ON items(content_hash);
CREATE INDEX IF NOT EXISTS idx_items_published_at ON items(published_at);
CREATE INDEX IF NOT EXISTS idx_items_deadline_at ON items(deadline_at);
CREATE INDEX IF NOT EXISTS idx_items_organization_name ON items(organization_name);

-- saved_searches: 保存検索
CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    filters_json TEXT NOT NULL,
    query_ref TEXT,
    order_by TEXT DEFAULT 'newest',
    schedule TEXT,
    only_new INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    last_hit_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- saved_search_runs: 保存検索実行履歴
CREATE TABLE IF NOT EXISTS saved_search_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_search_id INTEGER NOT NULL,
    query_ref TEXT,
    filters_snapshot TEXT,
    run_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error_message TEXT,
    notified_channels TEXT,
    notify_status TEXT,
    notify_error TEXT,
    FOREIGN KEY (saved_search_id) REFERENCES saved_searches(id)
);

CREATE INDEX IF NOT EXISTS idx_ssr_saved_search_id ON saved_search_runs(saved_search_id);
CREATE INDEX IF NOT EXISTS idx_ssr_run_at ON saved_search_runs(run_at);

-- saved_search_hits: 保存検索ヒット結果
CREATE TABLE IF NOT EXISTS saved_search_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_search_run_id INTEGER NOT NULL,
    item_id INTEGER,
    content_hash TEXT,
    matched_at TEXT NOT NULL,
    notified_at TEXT,
    FOREIGN KEY (saved_search_run_id) REFERENCES saved_search_runs(id),
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE INDEX IF NOT EXISTS idx_ssh_run_id ON saved_search_hits(saved_search_run_id);
CREATE INDEX IF NOT EXISTS idx_ssh_item_id ON saved_search_hits(item_id);
CREATE INDEX IF NOT EXISTS idx_ssh_notified_at ON saved_search_hits(notified_at);

-- saved_search_notifications: 通知送信履歴
CREATE TABLE IF NOT EXISTS saved_search_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_search_run_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT NOT NULL,
    error_message TEXT,
    dedupe_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY (saved_search_run_id) REFERENCES saved_search_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_ssn_run_id ON saved_search_notifications(saved_search_run_id);
CREATE INDEX IF NOT EXISTS idx_ssn_channel ON saved_search_notifications(channel);
CREATE INDEX IF NOT EXISTS idx_ssn_status ON saved_search_notifications(status);
"""

POSTGRES_DDL_STATEMENTS = """
-- raw_fetch: 生データ保存
CREATE TABLE IF NOT EXISTS raw_fetch (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    raw_hash TEXT NOT NULL,
    raw_payload BYTEA NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_fetch_source ON raw_fetch(source);
CREATE INDEX IF NOT EXISTS idx_raw_fetch_fetched_at ON raw_fetch(fetched_at);
CREATE INDEX IF NOT EXISTS idx_raw_fetch_raw_hash ON raw_fetch(raw_hash);

-- items: 正規化案件データ
CREATE TABLE IF NOT EXISTS items (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    source_item_id TEXT,
    url TEXT,
    title TEXT NOT NULL,
    organization_name TEXT NOT NULL,
    published_at TEXT,
    deadline_at TEXT,
    category TEXT,
    region TEXT,
    body TEXT,
    body_hash TEXT,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_items_source_item
    ON items(source, source_item_id) WHERE source_item_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_url
    ON items(url) WHERE url IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_items_content_hash ON items(content_hash);
CREATE INDEX IF NOT EXISTS idx_items_published_at ON items(published_at);
CREATE INDEX IF NOT EXISTS idx_items_deadline_at ON items(deadline_at);
CREATE INDEX IF NOT EXISTS idx_items_organization_name ON items(organization_name);

-- saved_searches: 保存検索
CREATE TABLE IF NOT EXISTS saved_searches (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    filters_json TEXT NOT NULL,
    query_ref TEXT,
    order_by TEXT DEFAULT 'newest',
    schedule TEXT,
    only_new INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    last_hit_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- saved_search_runs: 保存検索実行履歴
CREATE TABLE IF NOT EXISTS saved_search_runs (
    id BIGSERIAL PRIMARY KEY,
    saved_search_id BIGINT NOT NULL REFERENCES saved_searches(id),
    query_ref TEXT,
    filters_snapshot TEXT,
    run_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error_message TEXT,
    notified_channels TEXT,
    notify_status TEXT,
    notify_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_ssr_saved_search_id ON saved_search_runs(saved_search_id);
CREATE INDEX IF NOT EXISTS idx_ssr_run_at ON saved_search_runs(run_at);

-- saved_search_hits: 保存検索ヒット結果
CREATE TABLE IF NOT EXISTS saved_search_hits (
    id BIGSERIAL PRIMARY KEY,
    saved_search_run_id BIGINT NOT NULL REFERENCES saved_search_runs(id),
    item_id BIGINT REFERENCES items(id),
    content_hash TEXT,
    matched_at TEXT NOT NULL,
    notified_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_ssh_run_id ON saved_search_hits(saved_search_run_id);
CREATE INDEX IF NOT EXISTS idx_ssh_item_id ON saved_search_hits(item_id);
CREATE INDEX IF NOT EXISTS idx_ssh_notified_at ON saved_search_hits(notified_at);

-- saved_search_notifications: 通知送信履歴
CREATE TABLE IF NOT EXISTS saved_search_notifications (
    id BIGSERIAL PRIMARY KEY,
    saved_search_run_id BIGINT NOT NULL REFERENCES saved_search_runs(id),
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT NOT NULL,
    error_message TEXT,
    dedupe_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_ssn_run_id ON saved_search_notifications(saved_search_run_id);
CREATE INDEX IF NOT EXISTS idx_ssn_channel ON saved_search_notifications(channel);
CREATE INDEX IF NOT EXISTS idx_ssn_status ON saved_search_notifications(status);
"""


# =============================================================================
# データベース接続
# =============================================================================


def get_database_backend() -> Literal["sqlite", "postgresql"]:
    """現在の設定からDBバックエンドを判定する。"""
    url = settings.database_url
    if settings.db_host or settings.db_name or settings.db_user:
        return "postgresql"
    if url.startswith(("postgresql://", "postgres://")):
        return "postgresql"
    if url.startswith("sqlite:///"):
        return "sqlite"
    raise ValueError(f"Unsupported database URL: {url}")


def get_db_path() -> Path:
    """データベースファイルのパスを取得"""
    url = settings.database_url
    if url.startswith("sqlite:///"):
        path = Path(url.replace("sqlite:///", ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    raise ValueError(f"Unsupported database URL: {url}")


class PostgresConnection:
    """sqlite互換に近い最小DB接続ラッパー。"""

    backend = "postgresql"

    def __init__(self, conn: Any):
        self._conn = conn

    def execute(self, sql: str, params: list | tuple | None = None) -> Any:
        return self._conn.execute(_convert_placeholders(sql), tuple(params or ()))

    def executescript(self, script: str) -> None:
        with self._conn.cursor() as cursor:
            for statement in _split_sql_script(script):
                cursor.execute(statement)

    def commit(self) -> None:
        if not getattr(self._conn, "autocommit", False):
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def _convert_placeholders(sql: str) -> str:
    """sqlite形式の `?` placeholder をpsycopg形式へ変換する。"""
    return sql.replace("?", "%s")


def _split_sql_script(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";") if statement.strip()]


def _connect_postgres() -> PostgresConnection:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - PostgreSQL利用時のみ
        raise RuntimeError("PostgreSQLを使うには psycopg[binary] が必要です") from exc

    if settings.db_host or settings.db_name or settings.db_user:
        kwargs = {
            "host": settings.db_host,
            "dbname": settings.db_name,
            "user": settings.db_user,
            "password": settings.db_password,
            "row_factory": dict_row,
        }
        if settings.db_port is not None:
            kwargs["port"] = settings.db_port
        conn = psycopg.connect(**kwargs, autocommit=True)
    else:
        conn = psycopg.connect(settings.database_url, row_factory=dict_row, autocommit=True)
    return PostgresConnection(conn)


_pg_local = threading.local()
_PG_PING_IDLE_SECONDS = 60.0


def _get_cached_postgres() -> PostgresConnection:
    """スレッドローカルに PostgreSQL 接続をキャッシュして再利用する。

    リモート DB（Supabase 等）では接続確立（TCP+TLS ハンドシェイク）が支配的コストのため、
    利用のたびの張り直しを避ける。psycopg 接続はスレッド非安全なので threading.local で
    スレッドごとに分離する。生存確認の ping はアイドルが 60 秒を超えた時だけ行い
    （pooler のアイドル切断対策）、直近利用の接続は往復なしで返す。
    """
    import time as _time

    conn: PostgresConnection | None = getattr(_pg_local, "conn", None)
    last_used: float = getattr(_pg_local, "last_used", 0.0)
    now = _time.monotonic()
    if conn is not None:
        if getattr(conn._conn, "closed", False):
            _pg_local.conn = None
            conn = None
        elif now - last_used > _PG_PING_IDLE_SECONDS:
            try:
                conn.execute("SELECT 1")
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                _pg_local.conn = None
                conn = None
    if conn is None:
        conn = _connect_postgres()
        _pg_local.conn = conn
    _pg_local.last_used = now
    return conn


@contextmanager
def get_connection() -> Generator[sqlite3.Connection | PostgresConnection, None, None]:
    """データベース接続を取得（コンテキストマネージャ）

    PostgreSQL はスレッドローカルにキャッシュした接続を再利用する（close しない）。
    返却時に rollback して未コミットのトランザクション状態をリセットする
    （書き込み側は従来どおり明示 commit を呼ぶ。commit 済みなら rollback は no-op）。
    SQLite は従来どおり毎回開いて閉じる（ローカルファイルのため接続コストが無視できる）。
    """
    if get_database_backend() == "postgresql":
        pg_conn = _get_cached_postgres()
        try:
            yield pg_conn
        except Exception:
            # 失敗した接続は破棄して次回再接続（半端な状態の温存を避ける）
            try:
                pg_conn.close()
            except Exception:
                pass
            _pg_local.conn = None
            raise
    else:
        db_path = get_db_path()
        sqlite_conn = sqlite3.connect(db_path)
        sqlite_conn.row_factory = sqlite3.Row
        try:
            yield sqlite_conn
        finally:
            sqlite_conn.close()


def init_db() -> None:
    """データベースを初期化（テーブル作成）"""
    with get_connection() as conn:
        ddl = POSTGRES_DDL_STATEMENTS if _is_postgres_conn(conn) else SQLITE_DDL_STATEMENTS
        conn.executescript(ddl)
        conn.commit()


def get_db_stats() -> dict:
    """データベースの統計情報を取得"""
    with get_connection() as conn:
        stats = {}
        for table in ["raw_fetch", "items", "saved_searches", "saved_search_runs"]:
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            stats[table] = _first_value(cursor.fetchone())
        return stats


def _is_postgres_conn(conn: sqlite3.Connection | PostgresConnection) -> bool:
    return isinstance(conn, PostgresConnection)


def _first_value(row: Any) -> Any:
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def insert_and_get_id(
    conn: sqlite3.Connection | PostgresConnection,
    sql: str,
    params: tuple,
) -> int:
    """INSERTを実行して生成されたidを返す。"""
    if _is_postgres_conn(conn):
        cursor = conn.execute(f"{sql.rstrip()} RETURNING id", params)
        return int(_first_value(cursor.fetchone()))
    cursor = conn.execute(sql, params)
    return int(cursor.lastrowid or 0)


# =============================================================================
# ハッシュ生成
# =============================================================================


def normalize_string(s: str | None) -> str:
    """文字列を正規化（NFKC、空白正規化、トリム）"""
    if s is None:
        return ""
    # NFKC正規化
    s = unicodedata.normalize("NFKC", s)
    # 連続する空白・改行を1つに
    s = " ".join(s.split())
    # 前後トリム
    return s.strip()


def escape_pipe(s: str) -> str:
    """パイプ文字をエスケープ"""
    return s.replace("\\", "\\\\").replace("|", "\\|")


def generate_content_hash(
    title: str,
    organization_name: str,
    published_at: str | None,
    deadline_at: str | None,
    url: str | None,
    source_item_id: str | None = None,
) -> str:
    """content_hashを生成"""
    parts = []
    if source_item_id:
        parts.append(escape_pipe(normalize_string(source_item_id)))
    parts.extend([
        escape_pipe(normalize_string(title)),
        escape_pipe(normalize_string(organization_name)),
        escape_pipe(normalize_string(published_at)),
        escape_pipe(normalize_string(deadline_at)),
        escape_pipe(normalize_string(url)),
    ])
    content = "|".join(parts)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def generate_body_hash(body: str | None) -> str | None:
    """body_hashを生成"""
    if not body:
        return None
    normalized = normalize_string(body)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def generate_raw_hash(payload: bytes) -> str:
    """raw_hashを生成"""
    return hashlib.sha256(payload).hexdigest()


def generate_request_fingerprint(source: str, params: dict) -> str:
    """request_fingerprintを生成"""
    # パラメータをキー昇順でソート、空値除外
    sorted_params = sorted(
        ((k, v) for k, v in params.items() if v),
        key=lambda x: x[0],
    )
    param_str = "&".join(f"{k}={v}" for k, v in sorted_params)
    content = f"{source}:{param_str}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# =============================================================================
# CRUD操作: raw_fetch
# =============================================================================


def save_raw_fetch(raw: RawFetch) -> int:
    """生データを保存"""
    with get_connection() as conn:
        row_id = insert_and_get_id(
            conn,
            """
            INSERT INTO raw_fetch 
            (source, fetched_at, request_fingerprint, http_status, content_type, raw_hash, raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw.source,
                raw.fetched_at.isoformat(),
                raw.request_fingerprint,
                raw.http_status,
                raw.content_type,
                raw.raw_hash,
                raw.raw_payload,
            ),
        )
        conn.commit()
        return row_id


# =============================================================================
# CRUD操作: items
# =============================================================================


def _now_utc() -> str:
    """現在時刻をUTC ISO8601形式で取得"""
    return datetime.now(timezone.utc).isoformat()


def upsert_item(item: Item) -> tuple[int, bool]:
    """
    案件をupsert（挿入または更新）
    
    Returns:
        (item_id, is_new): IDと新規挿入かどうか
    """
    with get_connection() as conn:
        now = _now_utc()
        
        # 既存レコードを検索（優先順: source_item_id → url → content_hash）
        existing_id = None
        
        if item.source_item_id:
            cursor = conn.execute(
                "SELECT id FROM items WHERE source = ? AND source_item_id = ?",
                (item.source, item.source_item_id),
            )
            row = cursor.fetchone()
            if row:
                existing_id = row["id"]
        
        if existing_id is None and item.url:
            cursor = conn.execute(
                "SELECT id FROM items WHERE url = ?",
                (item.url,),
            )
            row = cursor.fetchone()
            if row:
                existing_id = row["id"]
        
        if existing_id is None:
            cursor = conn.execute(
                "SELECT id FROM items WHERE content_hash = ?",
                (item.content_hash,),
            )
            row = cursor.fetchone()
            if row:
                existing_id = row["id"]
        
        if existing_id:
            # 更新
            conn.execute(
                """
                UPDATE items SET
                    source_item_id = ?,
                    url = ?,
                    title = ?,
                    organization_name = ?,
                    published_at = ?,
                    deadline_at = ?,
                    category = ?,
                    region = ?,
                    body = ?,
                    body_hash = ?,
                    content_hash = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    item.source_item_id,
                    item.url,
                    item.title,
                    item.organization_name,
                    item.published_at.isoformat() if item.published_at else None,
                    item.deadline_at.isoformat() if item.deadline_at else None,
                    item.category,
                    item.region,
                    item.body,
                    item.body_hash,
                    item.content_hash,
                    now,
                    existing_id,
                ),
            )
            conn.commit()
            return existing_id, False
        else:
            # 新規挿入
            item_id = insert_and_get_id(
                conn,
                """
                INSERT INTO items 
                (source, source_item_id, url, title, organization_name, 
                 published_at, deadline_at, category, region, body, body_hash, 
                 content_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.source,
                    item.source_item_id,
                    item.url,
                    item.title,
                    item.organization_name,
                    item.published_at.isoformat() if item.published_at else None,
                    item.deadline_at.isoformat() if item.deadline_at else None,
                    item.category,
                    item.region,
                    item.body,
                    item.body_hash,
                    item.content_hash,
                    now,
                    now,
                ),
            )
            conn.commit()
            return item_id, True


def search_items(
    keyword: str = "",
    from_date: str | None = None,
    to_date: str | None = None,
    org: str = "",
    source: str = "",
    order_by: str = "newest",
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Item], int]:
    """
    案件を検索
    
    Returns:
        (items, total_count): 検索結果とヒット総数
    """
    with get_connection() as conn:
        # WHERE句の構築
        conditions = []
        params: list = []
        
        if keyword:
            conditions.append("(title LIKE ? OR body LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        
        if from_date:
            conditions.append("published_at >= ?")
            params.append(from_date)
        
        if to_date:
            conditions.append("published_at <= ?")
            params.append(to_date)
        
        if org:
            conditions.append("organization_name LIKE ?")
            params.append(f"%{org}%")
        
        if source and source != "all":
            conditions.append("source = ?")
            params.append(source)
        
        where_clause = " AND ".join(conditions) if conditions else "1=1"
        
        # ORDER BY句
        if order_by == "deadline":
            order_clause = "CASE WHEN deadline_at IS NULL THEN 1 ELSE 0 END, deadline_at ASC"
        else:  # newest
            order_clause = "COALESCE(published_at, created_at) DESC"
        
        # 総件数を取得
        count_sql = f"SELECT COUNT(*) FROM items WHERE {where_clause}"  # noqa: S608
        cursor = conn.execute(count_sql, params)
        total_count = _first_value(cursor.fetchone())
        
        # 結果を取得
        query_sql = f"""
            SELECT * FROM items 
            WHERE {where_clause} 
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
        """  # noqa: S608
        cursor = conn.execute(query_sql, params + [limit, offset])
        rows = cursor.fetchall()
        
        items = []
        for row in rows:
            items.append(Item(
                id=row["id"],
                source=row["source"],
                source_item_id=row["source_item_id"],
                url=row["url"],
                title=row["title"],
                organization_name=row["organization_name"],
                published_at=datetime.fromisoformat(row["published_at"]) if row["published_at"] else None,
                deadline_at=datetime.fromisoformat(row["deadline_at"]) if row["deadline_at"] else None,
                category=row["category"],
                region=row["region"],
                body=row["body"],
                body_hash=row["body_hash"],
                content_hash=row["content_hash"],
                created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
                updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
            ))
        
        return items, total_count
