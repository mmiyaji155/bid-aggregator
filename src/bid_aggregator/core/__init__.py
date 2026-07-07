"""
コアモジュール

設定、データベース、モデル定義を提供する。
"""

from bid_aggregator.core.config import settings
from bid_aggregator.core.database import (
    generate_body_hash,
    generate_content_hash,
    generate_raw_hash,
    generate_request_fingerprint,
    get_connection,
    get_database_backend,
    get_db_stats,
    insert_and_get_id,
    init_db,
    save_raw_fetch,
    search_items,
    upsert_item,
)
from bid_aggregator.core.models import (
    Item,
    KKJAPIResponse,
    KKJSearchResult,
    QueriesConfig,
    QueryConfig,
    RawFetch,
    SearchResult,
)

__all__ = [
    "settings",
    "get_connection",
    "get_database_backend",
    "init_db",
    "get_db_stats",
    "insert_and_get_id",
    "save_raw_fetch",
    "upsert_item",
    "search_items",
    "generate_content_hash",
    "generate_body_hash",
    "generate_raw_hash",
    "generate_request_fingerprint",
    "Item",
    "RawFetch",
    "SearchResult",
    "QueriesConfig",
    "QueryConfig",
    "KKJAPIResponse",
    "KKJSearchResult",
]
