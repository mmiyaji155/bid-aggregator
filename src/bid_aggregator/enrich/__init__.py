"""
締切メタデータ抽出（Enrich）モジュール

KKJ検索APIは応札締切を返さない（TenderSubmissionDeadline / PeriodEndTime タグは
実測で出現しない）。締切情報は各案件の公告文書（items.url が指すPDF/HTML）にのみ
存在するため、文書を取得しLLMで締切日を抽出して items.deadline_at を補完する。
"""

from bid_aggregator.enrich.deadline_extractor import (
    DeadlineExtraction,
    DeadlineExtractionError,
    EnrichResult,
    run_enrich_deadlines,
)

__all__ = [
    "DeadlineExtraction",
    "DeadlineExtractionError",
    "EnrichResult",
    "run_enrich_deadlines",
]
