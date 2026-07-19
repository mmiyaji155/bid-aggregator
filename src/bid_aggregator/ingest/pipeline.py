"""
収集パイプライン

queries.ymlを読み込み、KKJ APIからデータを取得し、DBに保存する。
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from bid_aggregator.core.database import (
    generate_raw_hash,
    generate_request_fingerprint,
    save_raw_fetch,
    upsert_items_batch,
)
from bid_aggregator.core.models import QueriesConfig, QueryConfig, RawFetch
from bid_aggregator.ingest.kkj_client import KKJClient
from bid_aggregator.ingest.normalizer import normalize_kkj_results

logger = logging.getLogger(__name__)


class IngestError(Exception):
    """収集エラー"""
    pass


def load_queries_config(path: Path | str) -> QueriesConfig:
    """
    queries.ymlを読み込む
    """
    path = Path(path)
    if not path.exists():
        raise IngestError(f"設定ファイルが見つかりません: {path}")
    
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    
    return QueriesConfig.model_validate(data)


class IngestResult:
    """収集結果"""
    
    def __init__(self):
        self.total_fetched: int = 0
        self.total_new: int = 0
        self.total_updated: int = 0
        self.total_errors: int = 0
        self.query_results: list[dict] = []
    
    def add_query_result(
        self,
        query_name: str,
        fetched: int,
        new: int,
        updated: int,
        errors: int,
    ) -> None:
        self.total_fetched += fetched
        self.total_new += new
        self.total_updated += updated
        self.total_errors += errors
        self.query_results.append({
            "query_name": query_name,
            "fetched": fetched,
            "new": new,
            "updated": updated,
            "errors": errors,
        })
    
    def summary(self) -> str:
        return (
            f"取得: {self.total_fetched}件, "
            f"新規: {self.total_new}件, "
            f"更新: {self.total_updated}件, "
            f"エラー: {self.total_errors}件"
        )


def run_ingest(
    config: QueriesConfig,
    source: str = "kkj",
    dry_run: bool = False,
) -> IngestResult:
    """
    収集パイプラインを実行
    
    Args:
        config: クエリ設定
        source: データソース（kkj）
        dry_run: Trueの場合、DBへの保存をスキップ
    
    Returns:
        IngestResult: 収集結果
    """
    result = IngestResult()
    
    # 有効なクエリのみ抽出
    enabled_queries = [q for q in config.queries if q.enabled and q.source == source]
    
    if not enabled_queries:
        logger.warning(f"有効なクエリがありません (source={source})")
        return result
    
    logger.info(f"収集開始: {len(enabled_queries)}件のクエリを実行")
    
    with KKJClient() as client:
        for query in enabled_queries:
            try:
                query_result = _process_query(client, query, dry_run)
                result.add_query_result(
                    query_name=query.name,
                    fetched=query_result["fetched"],
                    new=query_result["new"],
                    updated=query_result["updated"],
                    errors=query_result["errors"],
                )
            except Exception as e:
                import traceback
                logger.error(f"クエリ実行エラー: {query.name}")
                logger.error(f"エラー詳細: {type(e).__name__}: {e}")
                logger.error(f"トレースバック:\n{traceback.format_exc()}")
                result.add_query_result(
                    query_name=query.name,
                    fetched=0,
                    new=0,
                    updated=0,
                    errors=1,
                )
    
    logger.info(f"収集完了: {result.summary()}")
    return result


def _process_query(
    client: KKJClient,
    query: QueryConfig,
    dry_run: bool,
) -> dict:
    """
    単一クエリを処理
    """
    logger.info(f"クエリ実行: {query.name}")
    
    # パラメータをコピーしてlimitを反映
    params = query.params.model_copy()
    params.Count = min(query.limit, params.Count, 1000)
    
    # 日付範囲の取得
    from_date = None
    to_date = None
    if query.date_range:
        from_date = query.date_range.from_
        to_date = query.date_range.to
    
    # API呼び出し
    if from_date or to_date:
        response, raw_body, status_code, content_type = client.search_with_date_range(
            params, from_date, to_date
        )
    else:
        response, raw_body, status_code, content_type = client.search(params)
    
    logger.info(f"API応答: search_hits={response.search_hits}, results={len(response.results)}")
    
    # エラーチェック
    if response.error:
        logger.error(f"APIエラー: {response.error}")
        return {"fetched": 0, "new": 0, "updated": 0, "errors": 1}
    
    # raw保存
    if not dry_run:
        raw_fetch = RawFetch(
            source=query.source,
            fetched_at=datetime.now(timezone.utc),
            request_fingerprint=generate_request_fingerprint(
                query.source,
                params.model_dump(exclude_none=True),
            ),
            http_status=status_code,
            content_type=content_type,
            raw_hash=generate_raw_hash(raw_body),
            raw_payload=raw_body,
        )
        save_raw_fetch(raw_fetch)
        logger.debug("raw_fetch保存完了")
    
    # 正規化
    logger.info(f"正規化開始: {len(response.results)}件")
    items, normalize_errors = normalize_kkj_results(response.results, query.source)
    logger.info(f"正規化完了: 成功={len(items)}件, エラー={len(normalize_errors)}件")
    
    if normalize_errors:
        for result, err in normalize_errors[:5]:  # 最初の5件のエラーをログ
            logger.warning(f"正規化エラー詳細: key={result.key}, error={err}")
    
    # DB保存（バッチ upsert。1件ずつの往復を避け、100件単位でまとめて書き込む）
    new_count = 0
    updated_count = 0
    db_error_count = 0

    if not dry_run:
        logger.info(f"DB保存開始: {len(items)}件")
        batch_result = upsert_items_batch(items, batch_size=100)
        new_count = batch_result.new_count
        updated_count = batch_result.updated_count
        db_error_count = batch_result.error_count
        logger.info(
            f"DB保存完了: 新規={new_count}件, 更新={updated_count}件, "
            f"DBエラー={db_error_count}件"
        )
    else:
        # dry_runの場合は全て新規とみなす
        new_count = len(items)

    return {
        "fetched": len(response.results),
        "new": new_count,
        "updated": updated_count,
        "errors": len(normalize_errors) + db_error_count,
    }
