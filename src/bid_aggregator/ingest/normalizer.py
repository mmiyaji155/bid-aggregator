"""
正規化モジュール

KKJ APIレスポンスを共通スキーマ（Item）に変換する。
"""

import logging
import re
from datetime import datetime

from bid_aggregator.core.database import generate_body_hash, generate_content_hash

#: body（案件説明文）の最大文字数。KKJ の project_description には稀に添付文書がまるごと
#: 埋め込まれた 100MB 超の「クジラレコード」が混入し（実測 157MB / 2026-07-19 特定）、
#: そのままだと単一 INSERT 文が巨大化して Supabase pooler 経由の書き込みが永久ハングする。
#: 表示・検索用途には十分な長さで切り詰める（body_hash は切り詰め後の値で計算し一貫させる）。
_MAX_BODY_CHARS = 100_000


def _truncate_body(text: str | None) -> str | None:
    if text and len(text) > _MAX_BODY_CHARS:
        return text[:_MAX_BODY_CHARS] + "\n…（本文が長大なため切り詰め）"
    return text
from bid_aggregator.core.models import Item, KKJSearchResult

logger = logging.getLogger(__name__)

INVALID_SOURCE_ITEM_IDS = {"", "-", "‐", "―", "ー"}


class NormalizationError(Exception):
    """正規化エラー"""

    def __init__(self, message: str, source_key: str | None = None):
        super().__init__(message)
        self.source_key = source_key


def parse_iso8601_date(date_str: str | None) -> datetime | None:
    """
    ISO8601形式の日付文字列をパース

    KKJ APIは ISO8601 形式（例: 2025-01-30T09:00:00+09:00）を返す
    """
    if not date_str:
        return None

    try:
        # Python 3.11+ では fromisoformat が拡張形式をサポート
        return datetime.fromisoformat(date_str)
    except ValueError:
        # 日付のみの場合
        try:
            return datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            logger.warning(f"日付パース失敗: {date_str}")
            return None


def normalize_kkj_result(result: KKJSearchResult, source: str = "kkj") -> Item:
    """
    KKJ検索結果を正規化してItemに変換

    Raises:
        NormalizationError: 必須フィールドが欠落している場合
    """
    # 必須フィールドのバリデーション
    if not result.project_name:
        raise NormalizationError(
            "project_name（件名）が空です",
            source_key=result.key,
        )

    # organization_name は空の場合がある（オプション）
    organization_name = result.organization_name or "不明"

    # 日付のパース
    published_at = parse_iso8601_date(result.cft_issue_date)
    # deadline_at: 応札締切（tender_submission_deadline）優先、
    # 無ければ入札公告の有効期限（period_end_time）にフォールバック
    deadline_source = result.tender_submission_deadline or result.period_end_time
    deadline_at = parse_iso8601_date(deadline_source)

    # 地域の構築
    region_parts = []
    if result.prefecture_name:
        region_parts.append(result.prefecture_name)
    if result.city_name:
        region_parts.append(result.city_name)
    region = " ".join(region_parts) if region_parts else None

    # ハッシュ生成
    content_hash = generate_content_hash(
        title=result.project_name,
        organization_name=organization_name,
        published_at=result.cft_issue_date,
        deadline_at=deadline_source,
        url=result.external_document_uri,
        source_item_id=result.key,
    )

    body_text = _truncate_body(result.project_description)
    body_hash = generate_body_hash(body_text)

    return Item(
        source=source,
        source_item_id=result.key,
        url=result.external_document_uri,
        title=result.project_name,
        organization_name=organization_name,
        published_at=published_at,
        deadline_at=deadline_at,
        category=result.category,
        region=region,
        body=body_text,
        body_hash=body_hash,
        content_hash=content_hash,
    )


def normalize_kkj_results(
    results: list[KKJSearchResult],
    source: str = "kkj",
) -> tuple[list[Item], list[tuple[KKJSearchResult, Exception]]]:
    """
    複数のKKJ検索結果を正規化

    Returns:
        (normalized_items, errors): 正規化成功したItemリストとエラーリスト
    """
    items = []
    errors = []

    for result in results:
        try:
            item = normalize_kkj_result(result, source)
            items.append(item)
        except Exception as e:
            logger.warning(f"正規化エラー: key={result.key}, error={e}")
            errors.append((result, e))

    return items, errors


# =============================================================================
# 調達ポータル正規化
# =============================================================================

def normalize_pportal_result(result, source: str = "pportal") -> Item:
    """
    調達ポータル検索結果を正規化してItemに変換

    Args:
        result: PPortalSearchResult
        source: ソース名

    Returns:
        Item: 正規化されたアイテム
    """
    from bid_aggregator.ingest.pportal_client import PPortalSearchResult

    if not isinstance(result, PPortalSearchResult):
        raise NormalizationError(f"無効な結果タイプ: {type(result)}")

    if not result.title:
        raise NormalizationError(
            "title（案件名称）が空です",
            source_key=result.case_number,
        )

    # 日付のパース
    published_at = parse_iso8601_date(result.publish_start) if result.publish_start else None
    deadline_at = parse_iso8601_date(result.publish_end) if result.publish_end else None

    source_item_id = result.case_number.strip() if result.case_number else ""
    if source_item_id in INVALID_SOURCE_ITEM_IDS:
        match = re.search(r"procurementItemInfoId'.*?value:'(\d+)'", result.detail_url or "")
        source_item_id = match.group(1) if match else ""

    # ハッシュ生成
    content_hash = generate_content_hash(
        title=result.title,
        organization_name=result.organization or "不明",
        published_at=result.publish_start,
        deadline_at=result.publish_end,
        url=result.detail_url,
        source_item_id=source_item_id,
    )

    body_hash = generate_body_hash("")  # 詳細本文は未取得

    return Item(
        source=source,
        source_item_id=source_item_id or None,
        url=result.detail_url or "",
        title=result.title,
        organization_name=result.organization or "不明",
        published_at=published_at,
        deadline_at=deadline_at,
        category=result.category,
        region=None,  # 調達ポータルは所在地を別途取得可能
        body="",  # 詳細は別途取得
        body_hash=body_hash,
        content_hash=content_hash,
    )


def normalize_pportal_results(
    results: list,
    source: str = "pportal",
) -> tuple[list[Item], list[tuple[any, Exception]]]:
    """
    複数の調達ポータル検索結果を正規化

    Returns:
        (normalized_items, errors): 正規化成功したItemリストとエラーリスト
    """
    items = []
    errors = []

    for result in results:
        try:
            item = normalize_pportal_result(result, source)
            items.append(item)
        except Exception as e:
            logger.warning(f"正規化エラー: case_number={getattr(result, 'case_number', 'unknown')}, error={e}")
            errors.append((result, e))

    return items, errors
