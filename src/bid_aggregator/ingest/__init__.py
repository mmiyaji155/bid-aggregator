"""
収集（Ingest）モジュール

KKJ API等からデータを取得し、正規化してDBに保存する。
"""

from bid_aggregator.ingest.full_ingest import (
    FullIngestResult,
    date_range_generator,
    estimate_chunks,
    run_full_ingest,
    run_pportal_backfill,
)
from bid_aggregator.ingest.kkj_client import KKJAPIError, KKJClient
from bid_aggregator.ingest.normalizer import (
    NormalizationError,
    normalize_kkj_result,
    normalize_kkj_results,
)
from bid_aggregator.ingest.pipeline import (
    IngestError,
    IngestResult,
    load_queries_config,
    run_ingest,
)

__all__ = [
    "KKJClient",
    "KKJAPIError",
    "normalize_kkj_result",
    "normalize_kkj_results",
    "NormalizationError",
    "load_queries_config",
    "run_ingest",
    "IngestResult",
    "IngestError",
    "run_full_ingest",
    "run_pportal_backfill",
    "FullIngestResult",
    "date_range_generator",
    "estimate_chunks",
]
