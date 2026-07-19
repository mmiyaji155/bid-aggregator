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

-- enrich_log: 締切メタデータ抽出（deadline_extractor）の監査ログ
-- 公告文書の取得・LLM抽出の全試行を記録し、精度検証・再実行判断の基盤とする
CREATE TABLE IF NOT EXISTS enrich_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    url TEXT,
    status TEXT NOT NULL,
    deadline_date TEXT,
    deadline_kind TEXT,
    confidence TEXT,
    evidence TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE INDEX IF NOT EXISTS idx_enrich_log_item_id ON enrich_log(item_id);
CREATE INDEX IF NOT EXISTS idx_enrich_log_created_at ON enrich_log(created_at);
CREATE INDEX IF NOT EXISTS idx_enrich_log_status ON enrich_log(status);
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

-- enrich_log: 締切メタデータ抽出（deadline_extractor）の監査ログ
-- 公告文書の取得・LLM抽出の全試行を記録し、精度検証・再実行判断の基盤とする
CREATE TABLE IF NOT EXISTS enrich_log (
    id BIGSERIAL PRIMARY KEY,
    item_id BIGINT NOT NULL REFERENCES items(id),
    url TEXT,
    status TEXT NOT NULL,
    deadline_date TEXT,
    deadline_kind TEXT,
    confidence TEXT,
    evidence TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_enrich_log_item_id ON enrich_log(item_id);
CREATE INDEX IF NOT EXISTS idx_enrich_log_created_at ON enrich_log(created_at);
CREATE INDEX IF NOT EXISTS idx_enrich_log_status ON enrich_log(status);
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


#: 接続確立時に付与する libpq レベルの堅牢化パラメータ。
#: - connect_timeout: TCP/TLS ハンドシェイクが詰まった場合に永久待ちにしない
#: - keepalives*: アイドル中の pooler 側切断（silent drop）を検知してハングを防ぐ
_PG_ROBUSTNESS_KWARGS: dict[str, int] = {
    "connect_timeout": 10,
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}

#: サーバー側クエリタイムアウト（ミリ秒）。1文が詰まった場合に接続を無限占有しない。
_PG_STATEMENT_TIMEOUT_MS = 60_000


def _merge_statement_timeout_options(existing_options: str | None) -> str:
    """既存の `options`（例: `-c search_path=govbid`）に statement_timeout を追記する。

    既存の options 値を破壊せず末尾に `-c statement_timeout=...` を追加する。
    """
    extra = f"-c statement_timeout={_PG_STATEMENT_TIMEOUT_MS}"
    if existing_options:
        return f"{existing_options} {extra}"
    return extra


def _connect_postgres() -> PostgresConnection:
    try:
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - PostgreSQL利用時のみ
        raise RuntimeError("PostgreSQLを使うには psycopg[binary] が必要です") from exc

    # 接続先は Supabase の PgBouncer/Supavisor（transaction pooling mode、既定 port 6543）を
    # 経由することが多い。psycopg3 は既定で prepare_threshold=5 回目以降のクエリをサーバーサイド
    # PREPARE するが、transaction pooling では毎トランザクションごとに物理接続が入れ替わり得るため、
    # クライアント側にキャッシュした prepared statement が別の物理接続では存在せず
    # "prepared statement ... does not exist" エラーになる（同一SQLを大量反復する upsert_item 等で顕在化）。
    # prepare_threshold=None でサーバーサイド prepare を無効化し、pooler越しでも安全に動作させる。
    if settings.db_host or settings.db_name or settings.db_user:
        kwargs: dict[str, Any] = {
            "host": settings.db_host,
            "dbname": settings.db_name,
            "user": settings.db_user,
            "password": settings.db_password,
            "row_factory": dict_row,
            "options": _merge_statement_timeout_options(None),
            **_PG_ROBUSTNESS_KWARGS,
        }
        if settings.db_port is not None:
            kwargs["port"] = settings.db_port
        conn = psycopg.connect(**kwargs, autocommit=True, prepare_threshold=None)
    else:
        # DATABASE_URL（DSN文字列）を分解し、既存の options（例: search_path 指定）を
        # 保持したまま statement_timeout と keepalive 系パラメータを追加する。
        parsed = conninfo_to_dict(settings.database_url)
        parsed["options"] = _merge_statement_timeout_options(parsed.get("options"))
        parsed.update(_PG_ROBUSTNESS_KWARGS)
        conn = psycopg.connect(
            **parsed, row_factory=dict_row, autocommit=True, prepare_threshold=None
        )
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


#: raw_payload をこのサイズ（バイト）以上なら zlib 圧縮して保存する。
#: KKJ の広域クエリは 1 レスポンス 70MB 級になり、巨大 BYTEA の単発 INSERT は
#: リモート Postgres（Supabase pooler）経由でハングする実績があるため（2026-07-19 特定）、
#: 大きいペイロードは圧縮して転送量を 1/10 程度に抑える。XML/JSON はよく縮む。
#: 圧縮した場合は content_type に "; codec=zlib" を付記する（raw_hash は元データのハッシュのまま）。
_RAW_PAYLOAD_COMPRESS_THRESHOLD = 1 * 1024 * 1024  # 1MB


def save_raw_fetch(raw: RawFetch) -> int:
    """生データを保存（大きいペイロードは zlib 圧縮）"""
    import zlib

    payload = raw.raw_payload
    content_type = raw.content_type
    if len(payload) >= _RAW_PAYLOAD_COMPRESS_THRESHOLD:
        compressed = zlib.compress(payload, level=6)
        logger_ = __import__("logging").getLogger(__name__)
        logger_.info(
            "raw_payload を圧縮して保存: %d bytes -> %d bytes", len(payload), len(compressed)
        )
        payload = compressed
        content_type = f"{content_type}; codec=zlib"

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
                content_type,
                raw.raw_hash,
                payload,
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


# =============================================================================
# CRUD操作: items（バッチ upsert）
# =============================================================================

_ITEM_COLUMNS = (
    "source_item_id",
    "url",
    "title",
    "organization_name",
    "published_at",
    "deadline_at",
    "category",
    "region",
    "body",
    "body_hash",
    "content_hash",
)


class BatchUpsertResult:
    """バッチ upsert の結果集計。"""

    def __init__(self) -> None:
        self.new_count: int = 0
        self.updated_count: int = 0
        self.error_count: int = 0

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return (
            f"BatchUpsertResult(new={self.new_count}, "
            f"updated={self.updated_count}, errors={self.error_count})"
        )


def _item_field_values(item: Item, now: str) -> tuple:
    return (
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
    )


def upsert_items_batch(items: list[Item], batch_size: int = 100) -> BatchUpsertResult:
    """
    複数の案件を一括 upsert する。

    PostgreSQL では既存レコードの探索（source_item_id → url → content_hash の優先順位、
    upsert_item と同一のセマンティクス）と書き込みを `batch_size` 件単位でまとめて実行し、
    1件ごとに発生していた往復（ラウンドトリップ）を大幅に削減する。
    接続系エラーが起きた場合はバッチ単位で1回だけ再接続・再試行し、それでも失敗するバッチは
    warning を出して読み飛ばし、全体を止めない。

    SQLite では従来どおり `upsert_item` を1件ずつ呼び出す（ローカルファイルのため
    ラウンドトリップコストが無視できるほど小さく、バッチ化の恩恵が薄いため）。
    """
    import logging

    logger = logging.getLogger(__name__)

    result = BatchUpsertResult()
    if not items:
        return result

    if get_database_backend() != "postgresql":
        for item in items:
            try:
                _, is_new = upsert_item(item)
                if is_new:
                    result.new_count += 1
                else:
                    result.updated_count += 1
            except Exception as e:
                logger.error(f"DB保存エラー: {item.title[:30]}..., error={e}")
                result.error_count += 1
        return result

    for start in range(0, len(items), batch_size):
        chunk = items[start : start + batch_size]
        _upsert_postgres_chunk_with_retry(chunk, result)

    return result


def _upsert_postgres_chunk_with_retry(chunk: list[Item], result: BatchUpsertResult) -> None:
    import logging

    logger = logging.getLogger(__name__)

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - PostgreSQL利用時のみ
        raise RuntimeError("PostgreSQLを使うには psycopg[binary] が必要です") from exc

    for attempt in (1, 2):
        try:
            with get_connection() as conn:
                new_c, updated_c = _upsert_postgres_chunk(conn, chunk)
                result.new_count += new_c
                result.updated_count += updated_c
            return
        except psycopg.OperationalError as e:
            # get_connection() は例外発生時に自前でキャッシュ接続を close & 破棄済みのため、
            # 次の試行では自動的に新しい接続が張られる
            logger.warning(
                f"DBバッチ書き込みで接続エラー（試行{attempt}/2、{len(chunk)}件）: {e}"
            )
            if attempt == 2:
                logger.warning(
                    f"DBバッチ書き込みを断念しスキップします（{len(chunk)}件が未反映）"
                )
                result.error_count += len(chunk)
                return
        except Exception as e:
            # 接続エラー以外（データ不整合等）はバッチ全体を失わせず、
            # 1件ずつのフォールバック処理にダウングレードして被害を局所化する
            logger.warning(
                f"DBバッチ書き込みで想定外のエラー、1件ずつにフォールバックします: {e}"
            )
            for item in chunk:
                try:
                    _, is_new = upsert_item(item)
                    if is_new:
                        result.new_count += 1
                    else:
                        result.updated_count += 1
                except Exception as item_exc:
                    logger.error(f"DB保存エラー: {item.title[:30]}..., error={item_exc}")
                    result.error_count += 1
            return


def _upsert_postgres_chunk(
    conn: "PostgresConnection", chunk: list[Item]
) -> tuple[int, int]:
    """PostgreSQL向け: 1チャンク分の既存ID探索＋一括update/insertを行う。"""
    now = _now_utc()

    # --- 既存ID探索（優先順位: source_item_id → url → content_hash） ---
    existing_by_sid: dict[tuple[str, str], int] = {}
    sid_pairs = [(it.source, it.source_item_id) for it in chunk if it.source_item_id]
    if sid_pairs:
        placeholders = ",".join(["(?,?)"] * len(sid_pairs))
        flat_params = [v for pair in sid_pairs for v in pair]
        cursor = conn.execute(
            f"SELECT id, source, source_item_id FROM items WHERE (source, source_item_id) IN ({placeholders})",  # noqa: S608
            flat_params,
        )
        for row in cursor.fetchall():
            existing_by_sid[(row["source"], row["source_item_id"])] = row["id"]

    existing_by_url: dict[str, int] = {}
    urls = list({it.url for it in chunk if it.url})
    if urls:
        placeholders = ",".join(["?"] * len(urls))
        cursor = conn.execute(
            f"SELECT id, url FROM items WHERE url IN ({placeholders})",  # noqa: S608
            urls,
        )
        for row in cursor.fetchall():
            existing_by_url[row["url"]] = row["id"]

    existing_by_hash: dict[str, int] = {}
    hashes = list({it.content_hash for it in chunk if it.content_hash})
    if hashes:
        placeholders = ",".join(["?"] * len(hashes))
        cursor = conn.execute(
            f"SELECT id, content_hash FROM items WHERE content_hash IN ({placeholders})",  # noqa: S608
            hashes,
        )
        for row in cursor.fetchall():
            existing_by_hash[row["content_hash"]] = row["id"]

    to_update: list[tuple[int, Item]] = []
    to_insert: list[Item] = []
    for item in chunk:
        existing_id = None
        if item.source_item_id:
            existing_id = existing_by_sid.get((item.source, item.source_item_id))
        if existing_id is None and item.url:
            existing_id = existing_by_url.get(item.url)
        if existing_id is None:
            existing_id = existing_by_hash.get(item.content_hash)

        if existing_id is not None:
            to_update.append((existing_id, item))
        else:
            to_insert.append(item)

    updated_count = 0
    if to_update:
        # UPDATE ... FROM (VALUES ...) による一括更新。id 列のみ bigint へキャストすれば
        # 他列は全て TEXT のため型解決の問題は生じない。
        value_rows = []
        flat_params = []
        for existing_id, item in to_update:
            value_rows.append("(?::bigint,?,?,?,?,?,?,?,?,?,?,?,?)")
            flat_params.append(existing_id)
            flat_params.extend(_item_field_values(item, now))
        values_sql = ",".join(value_rows)
        sql = f"""
            UPDATE items SET
                source_item_id = v.source_item_id,
                url = v.url,
                title = v.title,
                organization_name = v.organization_name,
                published_at = v.published_at,
                deadline_at = v.deadline_at,
                category = v.category,
                region = v.region,
                body = v.body,
                body_hash = v.body_hash,
                content_hash = v.content_hash,
                updated_at = v.updated_at
            FROM (VALUES {values_sql}) AS v(
                id, source_item_id, url, title, organization_name,
                published_at, deadline_at, category, region, body, body_hash,
                content_hash, updated_at
            )
            WHERE items.id = v.id
        """  # noqa: S608
        conn.execute(sql, flat_params)
        updated_count = len(to_update)

    new_count = 0
    if to_insert:
        # 同一チャンク内に重複キー（source_item_id/url）を持つ行が万一含まれても
        # 全体を失敗させないよう ON CONFLICT DO NOTHING を安全弁として付与する。
        value_rows = []
        flat_params = []
        for item in to_insert:
            value_rows.append("(?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
            flat_params.append(item.source)
            flat_params.extend(_item_field_values(item, now))
            flat_params.append(now)  # created_at
        values_sql = ",".join(value_rows)
        sql = f"""
            INSERT INTO items (
                source, source_item_id, url, title, organization_name,
                published_at, deadline_at, category, region, body, body_hash,
                content_hash, updated_at, created_at
            )
            VALUES {values_sql}
            ON CONFLICT DO NOTHING
        """  # noqa: S608
        conn.execute(sql, flat_params)
        new_count = len(to_insert)

    conn.commit()
    return new_count, updated_count


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
