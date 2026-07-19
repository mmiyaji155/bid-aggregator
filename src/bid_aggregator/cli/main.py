"""
CLI メインモジュール

bid-cli コマンドのエントリーポイント。
"""

import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from bid_aggregator.core import init_db, get_db_stats, search_items, settings
from bid_aggregator.core.saved_search_db import (
    create_saved_search,
    delete_saved_search,
    list_saved_searches,
)
from bid_aggregator.ingest import load_queries_config, run_ingest
from bid_aggregator.notify import run_saved_search, send_notification

console = Console()


def setup_logging(level: str = "INFO") -> None:
    """ログ設定"""
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


@click.group()
@click.option("--debug", is_flag=True, help="デバッグモードを有効化")
@click.pass_context
def cli(ctx: click.Context, debug: bool) -> None:
    """入札・調達情報アグリゲータ CLI"""
    ctx.ensure_object(dict)
    log_level = "DEBUG" if debug else settings.log_level
    setup_logging(log_level)


# =============================================================================
# db コマンドグループ
# =============================================================================


@cli.group()
def db() -> None:
    """データベース管理"""
    pass


@db.command("init")
def db_init() -> None:
    """データベースを初期化"""
    init_db()
    console.print("[green]✓[/green] データベースを初期化しました")


@db.command("stats")
def db_stats() -> None:
    """データベースの統計情報を表示"""
    try:
        stats = get_db_stats()
        table = Table(title="データベース統計")
        table.add_column("テーブル", style="cyan")
        table.add_column("件数", justify="right", style="green")

        for table_name, count in stats.items():
            table.add_row(table_name, str(count))

        console.print(table)
    except Exception as e:
        console.print(f"[red]エラー:[/red] {e}")
        console.print("[yellow]ヒント:[/yellow] `bid-cli db init` を実行してください")
        sys.exit(1)


# =============================================================================
# ingest コマンド
# =============================================================================


@cli.command()
@click.option(
    "--source",
    type=click.Choice(["kkj"]),
    default="kkj",
    help="データソース",
)
@click.option(
    "--queries",
    type=click.Path(exists=True, path_type=Path),
    default="config/queries.yml",
    help="クエリ設定ファイル",
)
@click.option("--dry-run", is_flag=True, help="実際の保存をスキップ")
def ingest(source: str, queries: Path, dry_run: bool) -> None:
    """入札情報を取得してDBに保存（最大1000件）"""
    try:
        config = load_queries_config(queries)

        if dry_run:
            console.print("[yellow]ドライラン モード[/yellow]")

        result = run_ingest(config, source=source, dry_run=dry_run)

        # 結果表示
        table = Table(title="収集結果")
        table.add_column("クエリ名", style="cyan")
        table.add_column("取得", justify="right")
        table.add_column("新規", justify="right", style="green")
        table.add_column("更新", justify="right", style="yellow")
        table.add_column("エラー", justify="right", style="red")

        for qr in result.query_results:
            table.add_row(
                qr["query_name"],
                str(qr["fetched"]),
                str(qr["new"]),
                str(qr["updated"]),
                str(qr["errors"]),
            )

        table.add_section()
        table.add_row(
            "[bold]合計[/bold]",
            str(result.total_fetched),
            str(result.total_new),
            str(result.total_updated),
            str(result.total_errors),
        )

        console.print(table)

    except Exception as e:
        console.print(f"[red]エラー:[/red] {e}")
        sys.exit(1)


# =============================================================================
# full-ingest コマンド（全件取得）
# =============================================================================


@cli.command("full-ingest")
@click.option("--keyword", "-k", required=True, help="検索キーワード")
@click.option("--from", "from_date", required=True, help="開始日 (YYYY-MM-DD)")
@click.option("--to", "to_date", required=True, help="終了日 (YYYY-MM-DD)")
@click.option("--days", default=7, help="1チャンクの日数（デフォルト7日）")
@click.option("--org", default="", help="機関名で絞り込み")
@click.option("--region", default="", help="都道府県コード（カンマ区切り）")
@click.option("--dry-run", is_flag=True, help="実際の保存をスキップ")
def full_ingest(
    keyword: str,
    from_date: str,
    to_date: str,
    days: int,
    org: str,
    region: str,
    dry_run: bool,
) -> None:
    """日付範囲を分割して全件取得（1000件以上対応）"""
    from bid_aggregator.ingest import run_full_ingest, estimate_chunks
    from bid_aggregator.core.models import QueryConfig, QueryParams

    try:
        # チャンク数を見積もり
        chunks = estimate_chunks(from_date, to_date, days)
        console.print(f"[dim]期間: {from_date} 〜 {to_date}[/dim]")
        console.print(f"[dim]チャンク数（見積もり）: {chunks}個 ({days}日ごと)[/dim]")
        console.print()

        if dry_run:
            console.print("[yellow]ドライラン モード[/yellow]")

        # クエリ設定を作成
        params = QueryParams(
            Query=keyword,
            Organization_Name=org if org else "",
            LG_Code=region if region else "",
            Count=1000,
        )

        query = QueryConfig(
            name="full_ingest",
            source="kkj",
            params=params,
            limit=1000,
            enabled=True,
        )

        # 全件取得実行
        result = run_full_ingest(
            query=query,
            start_date=from_date,
            end_date=to_date,
            days_per_chunk=days,
            dry_run=dry_run,
        )

        # 結果表示
        console.print()
        table = Table(title="全件取得結果")
        table.add_column("期間", style="cyan")
        table.add_column("API件数", justify="right")
        table.add_column("取得", justify="right")
        table.add_column("新規", justify="right", style="green")
        table.add_column("更新", justify="right", style="yellow")
        table.add_column("エラー", justify="right", style="red")

        for cr in result.chunk_results:
            table.add_row(
                f"{cr['from']}〜{cr['to']}",
                str(cr["api_hits"]),
                str(cr["fetched"]),
                str(cr["new"]),
                str(cr["updated"]),
                str(cr["errors"]),
            )

        table.add_section()
        table.add_row(
            "[bold]合計[/bold]",
            "-",
            str(result.total_fetched),
            str(result.total_new),
            str(result.total_updated),
            str(result.total_errors),
        )

        console.print(table)

        # 警告
        if any(cr["fetched"] >= 1000 for cr in result.chunk_results):
            console.print()
            console.print(
                "[yellow]⚠ 一部のチャンクが1000件に達しました。[/yellow]\n"
                "[yellow]  --days を小さくして再実行することを検討してください。[/yellow]"
            )

    except Exception as e:
        console.print(f"[red]エラー:[/red] {e}")
        sys.exit(1)


# =============================================================================
# backfill コマンド（期間指定の重複なし保存）
# =============================================================================


@cli.command("backfill")
@click.option(
    "--source",
    type=click.Choice(["pportal", "kkj", "all"]),
    default="pportal",
    help="データソース",
)
@click.option("--from", "from_date", required=True, help="開始日 (YYYY-MM-DD)")
@click.option("--to", "to_date", required=True, help="終了日 (YYYY-MM-DD)")
@click.option("--keyword", "-k", default="", help="検索キーワード（pportalは空で全件）")
@click.option("--days", default=1, help="1チャンクの日数（デフォルト1日）")
@click.option("--max-pages", default=100, help="pportalの1チャンクあたり最大ページ数")
@click.option("--org", default="", help="KKJの機関名絞り込み")
@click.option("--region", default="", help="KKJの都道府県コード（カンマ区切り）")
@click.option("--dry-run", is_flag=True, help="実際の保存をスキップ")
def backfill(
    source: str,
    from_date: str,
    to_date: str,
    keyword: str,
    days: int,
    max_pages: int,
    org: str,
    region: str,
    dry_run: bool,
) -> None:
    """期間を指定して重複を避けながらDBへ保存"""
    from bid_aggregator.core.models import QueryConfig, QueryParams
    from bid_aggregator.ingest import estimate_chunks, run_full_ingest, run_pportal_backfill

    if days < 1:
        raise click.ClickException("--days は1以上を指定してください")
    if max_pages < 1:
        raise click.ClickException("--max-pages は1以上を指定してください")

    includes_kkj = source in {"kkj", "all"}
    includes_pportal = source in {"pportal", "all"}

    if includes_kkj and not any([keyword, org, region]):
        raise click.ClickException(
            "KKJ APIは検索条件が必須です。--keyword、--org、--region のいずれかを指定してください。"
        )

    try:
        chunks = estimate_chunks(from_date, to_date, days)
        console.print(f"[dim]期間: {from_date} 〜 {to_date}[/dim]")
        console.print(f"[dim]ソース: {source}, チャンク数: {chunks}個 ({days}日ごと)[/dim]")
        if keyword:
            console.print(f"[dim]キーワード: {keyword}[/dim]")
        if dry_run:
            console.print("[yellow]ドライラン モード[/yellow]")
        console.print()

        results = {}

        if includes_pportal:
            results["pportal"] = run_pportal_backfill(
                start_date=from_date,
                end_date=to_date,
                keyword=keyword,
                days_per_chunk=days,
                max_pages=max_pages,
                dry_run=dry_run,
            )

        if includes_kkj:
            params = QueryParams(
                Query=keyword,
                Organization_Name=org if org else "",
                LG_Code=region if region else "",
                Count=1000,
            )
            query = QueryConfig(
                name="backfill_kkj",
                source="kkj",
                params=params,
                limit=1000,
                enabled=True,
            )
            results["kkj"] = run_full_ingest(
                query=query,
                start_date=from_date,
                end_date=to_date,
                days_per_chunk=days,
                dry_run=dry_run,
            )

        total_errors = 0
        for source_name, result in results.items():
            total_errors += result.total_errors
            console.print()
            table = Table(title=f"backfill結果: {source_name}")
            table.add_column("期間", style="cyan")
            table.add_column("API件数", justify="right")
            table.add_column("取得", justify="right")
            table.add_column("新規", justify="right", style="green")
            table.add_column("更新", justify="right", style="yellow")
            table.add_column("エラー", justify="right", style="red")

            for cr in result.chunk_results:
                table.add_row(
                    f"{cr['from']}〜{cr['to']}",
                    str(cr["api_hits"]),
                    str(cr["fetched"]),
                    str(cr["new"]),
                    str(cr["updated"]),
                    str(cr["errors"]),
                )

            table.add_section()
            table.add_row(
                "[bold]合計[/bold]",
                "-",
                str(result.total_fetched),
                str(result.total_new),
                str(result.total_updated),
                str(result.total_errors),
            )
            console.print(table)

        if total_errors:
            console.print(
                "[yellow]一部チャンクにエラーがあります。[/yellow] "
                "[yellow]同じコマンドを再実行すると未取得分を補完できます。[/yellow]"
            )
            sys.exit(1)

    except Exception as e:
        console.print(f"[red]エラー:[/red] {e}")
        sys.exit(1)


@cli.command("pportal-daily")
@click.option("--lookback-days", default=3, help="今日を含めて何日分を取得するか")
@click.option("--keyword", "-k", default="", help="検索キーワード（空で全件）")
@click.option("--max-pages", default=100, help="1日あたり最大ページ数")
@click.option("--dry-run", is_flag=True, help="実際の保存をスキップ")
def pportal_daily(
    lookback_days: int,
    keyword: str,
    max_pages: int,
    dry_run: bool,
) -> None:
    """調達ポータルの日次取得。Cloud Scheduler向け。"""
    from bid_aggregator.ingest import run_pportal_backfill

    if lookback_days < 1:
        raise click.ClickException("--lookback-days は1以上を指定してください")
    if max_pages < 1:
        raise click.ClickException("--max-pages は1以上を指定してください")

    today = datetime.now(ZoneInfo(settings.timezone)).date()
    start_date = today - timedelta(days=lookback_days - 1)

    try:
        result = run_pportal_backfill(
            start_date=start_date.isoformat(),
            end_date=today.isoformat(),
            keyword=keyword,
            days_per_chunk=1,
            max_pages=max_pages,
            dry_run=dry_run,
        )

        table = Table(title="pportal-daily結果")
        table.add_column("期間", style="cyan")
        table.add_column("API件数", justify="right")
        table.add_column("取得", justify="right")
        table.add_column("新規", justify="right", style="green")
        table.add_column("更新", justify="right", style="yellow")
        table.add_column("エラー", justify="right", style="red")

        for cr in result.chunk_results:
            table.add_row(
                f"{cr['from']}〜{cr['to']}",
                str(cr["api_hits"]),
                str(cr["fetched"]),
                str(cr["new"]),
                str(cr["updated"]),
                str(cr["errors"]),
            )

        table.add_section()
        table.add_row(
            "[bold]合計[/bold]",
            "-",
            str(result.total_fetched),
            str(result.total_new),
            str(result.total_updated),
            str(result.total_errors),
        )
        console.print(table)

        if result.total_errors:
            sys.exit(1)

    except Exception as e:
        console.print(f"[red]エラー:[/red] {e}")
        sys.exit(1)


# =============================================================================
# enrich-deadlines コマンド（締切メタデータ抽出）
# =============================================================================


@cli.command("enrich-deadlines")
@click.option("--limit", "-n", default=20, help="処理対象の上限件数（LLM呼び出し回数の上限と一致）")
@click.option("--dry-run", is_flag=True, help="文書取得・LLM抽出は実行するがDB書き込みをスキップ")
@click.option("--interval", default=1.0, help="文書取得の連続アクセス間隔（秒）")
def enrich_deadlines(limit: int, dry_run: bool, interval: float) -> None:
    """締切未設定の案件の公告文書を取得し、LLMで締切日を抽出してDBに反映する"""
    from bid_aggregator.enrich import DeadlineExtractionError, run_enrich_deadlines

    if limit < 1:
        raise click.ClickException("--limit は1以上を指定してください")

    try:
        if dry_run:
            console.print("[yellow]ドライラン モード（DB書き込みなし）[/yellow]")

        result = run_enrich_deadlines(limit=limit, dry_run=dry_run, request_interval=interval)

        table = Table(title="締切抽出結果")
        table.add_column("項目", style="cyan")
        table.add_column("件数", justify="right")
        table.add_row("対象", str(result.total))
        table.add_row("文書取得成功", str(result.fetch_ok))
        table.add_row("文書取得スキップ", str(result.fetch_skip))
        table.add_row("文書取得エラー", str(result.fetch_error))
        table.add_row("LLM抽出 high", str(result.llm_high), style="green")
        table.add_row("LLM抽出 low", str(result.llm_low), style="yellow")
        table.add_row("LLM抽出エラー", str(result.llm_error), style="red")
        table.add_section()
        table.add_row("[bold]deadline_at 更新[/bold]", str(result.updated))
        console.print(table)

        if result.rows:
            console.print()
            detail = Table(title="抽出詳細")
            detail.add_column("ID", justify="right")
            detail.add_column("状態")
            detail.add_column("タイトル", overflow="fold", max_width=40)
            detail.add_column("締切日")
            detail.add_column("確信度")
            detail.add_column("反映")
            for row in result.rows:
                detail.add_row(
                    str(row.get("item_id")),
                    row.get("status", "-"),
                    (row.get("title") or "")[:40],
                    row.get("deadline_date") or "-",
                    row.get("confidence") or "-",
                    "✓" if row.get("written") else "-",
                )
            console.print(detail)

    except DeadlineExtractionError as e:
        console.print(f"[red]エラー:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]エラー:[/red] {e}")
        sys.exit(1)


# =============================================================================
# search コマンド
# =============================================================================


@cli.command()
@click.option("--keyword", "-k", default="", help="検索キーワード")
@click.option("--from", "from_date", default=None, help="公開日の開始（YYYY-MM-DD）")
@click.option("--to", "to_date", default=None, help="公開日の終了（YYYY-MM-DD）")
@click.option("--org", default="", help="機関名")
@click.option(
    "--source",
    type=click.Choice(["kkj", "pportal", "all"]),
    default="all",
    help="データソース",
)
@click.option(
    "--order-by",
    type=click.Choice(["newest", "deadline"]),
    default="newest",
    help="並び順",
)
@click.option("--limit", "-n", default=20, help="取得件数")
@click.option("--offset", default=0, help="開始位置")
@click.option("--json", "output_json", is_flag=True, help="JSON形式で出力")
def search(
    keyword: str,
    from_date: str | None,
    to_date: str | None,
    org: str,
    source: str,
    order_by: str,
    limit: int,
    offset: int,
    output_json: bool,
) -> None:
    """入札情報を検索"""
    try:
        items, total = search_items(
            keyword=keyword,
            from_date=from_date,
            to_date=to_date,
            org=org,
            source=source if source != "all" else "",
            order_by=order_by,
            limit=limit,
            offset=offset,
        )

        if output_json:
            # JSON出力
            data = {
                "total": total,
                "offset": offset,
                "limit": limit,
                "items": [item.model_dump(mode="json") for item in items],
            }
            console.print_json(json.dumps(data, ensure_ascii=False, default=str))
        else:
            # テーブル出力
            console.print(f"[dim]検索結果: {total}件中 {offset+1}〜{offset+len(items)}件[/dim]\n")

            for item in items:
                console.print(f"[bold cyan]{item.title}[/bold cyan]")
                console.print(f"  機関: {item.organization_name}")
                if item.published_at:
                    console.print(f"  公開日: {item.published_at.strftime('%Y-%m-%d')}")
                if item.deadline_at:
                    console.print(f"  締切: {item.deadline_at.strftime('%Y-%m-%d')}")
                if item.url:
                    console.print(f"  URL: {item.url}")
                console.print()

    except Exception as e:
        console.print(f"[red]エラー:[/red] {e}")
        sys.exit(1)


# =============================================================================
# export コマンド
# =============================================================================


@cli.command()
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["csv", "json"]),
    default="csv",
    help="出力形式",
)
@click.option("--output", "-o", type=click.Path(path_type=Path), help="出力ファイル")
@click.option("--keyword", "-k", default="", help="検索キーワード")
@click.option("--from", "from_date", default=None, help="公開日の開始")
@click.option("--to", "to_date", default=None, help="公開日の終了")
@click.option("--org", default="", help="機関名")
@click.option("--limit", "-n", default=1000, help="取得件数")
def export(
    output_format: str,
    output: Path | None,
    keyword: str,
    from_date: str | None,
    to_date: str | None,
    org: str,
    limit: int,
) -> None:
    """検索結果をエクスポート"""
    import csv
    import io

    try:
        items, total = search_items(
            keyword=keyword,
            from_date=from_date,
            to_date=to_date,
            org=org,
            limit=limit,
        )

        if output_format == "json":
            data = [item.model_dump(mode="json") for item in items]
            content = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        else:
            # CSV
            buffer = io.StringIO()
            writer = csv.writer(buffer)

            # ヘッダー
            headers = [
                "source", "source_item_id", "url", "title", "organization_name",
                "published_at", "deadline_at", "category", "region",
            ]
            writer.writerow(headers)

            # データ
            for item in items:
                writer.writerow([
                    item.source,
                    item.source_item_id or "",
                    item.url or "",
                    item.title,
                    item.organization_name,
                    item.published_at.isoformat() if item.published_at else "",
                    item.deadline_at.isoformat() if item.deadline_at else "",
                    item.category or "",
                    item.region or "",
                ])

            content = buffer.getvalue()

        if output:
            output.write_text(content, encoding="utf-8")
            console.print(f"[green]✓[/green] {output} に保存しました ({len(items)}件)")
        else:
            console.print(content)

    except Exception as e:
        console.print(f"[red]エラー:[/red] {e}")
        sys.exit(1)


# =============================================================================
# saved-search コマンドグループ
# =============================================================================


@cli.group("saved-search")
def saved_search_group() -> None:
    """保存検索の管理"""
    pass


@saved_search_group.command("add")
@click.option("--name", "-n", required=True, help="保存検索の名前")
@click.option("--keyword", "-k", default="", help="検索キーワード")
@click.option("--from", "from_date", default=None, help="公開日の開始")
@click.option("--to", "to_date", default=None, help="公開日の終了")
@click.option("--org", default="", help="機関名")
@click.option("--source", default="", help="データソース")
@click.option("--order-by", type=click.Choice(["newest", "deadline"]), default="newest")
@click.option("--schedule", type=click.Choice(["daily", "hourly"]), default=None)
@click.option("--only-new/--all", default=True, help="新規のみ通知")
def saved_search_add(
    name: str,
    keyword: str,
    from_date: str | None,
    to_date: str | None,
    org: str,
    source: str,
    order_by: str,
    schedule: str | None,
    only_new: bool,
) -> None:
    """保存検索を追加"""
    try:
        filters = {
            "keyword": keyword,
            "from": from_date,
            "to": to_date,
            "org": org,
            "source": source,
        }

        saved_search_id = create_saved_search(
            name=name,
            filters=filters,
            order_by=order_by,
            schedule=schedule,
            only_new=only_new,
            enabled=True,
        )

        console.print(f"[green]✓[/green] 保存検索を作成しました: {name} (ID: {saved_search_id})")

    except Exception as e:
        console.print(f"[red]エラー:[/red] {e}")
        sys.exit(1)


@saved_search_group.command("list")
@click.option("--enabled-only", is_flag=True, help="有効なもののみ表示")
def saved_search_list(enabled_only: bool) -> None:
    """保存検索一覧を表示"""
    try:
        searches = list_saved_searches(enabled_only=enabled_only)

        if not searches:
            console.print("[dim]保存検索がありません[/dim]")
            return

        table = Table(title="保存検索一覧")
        table.add_column("ID", justify="right")
        table.add_column("名前", style="cyan")
        table.add_column("キーワード")
        table.add_column("スケジュール")
        table.add_column("新規のみ")
        table.add_column("有効")
        table.add_column("最終実行")

        for ss in searches:
            filters = json.loads(ss["filters_json"])
            table.add_row(
                str(ss["id"]),
                ss["name"],
                filters.get("keyword", "")[:30] or "-",
                ss.get("schedule") or "-",
                "✓" if ss["only_new"] else "-",
                "✓" if ss["enabled"] else "-",
                ss.get("last_run_at", "-") or "-",
            )

        console.print(table)

    except Exception as e:
        console.print(f"[red]エラー:[/red] {e}")
        sys.exit(1)


@saved_search_group.command("run")
@click.option("--name", "-n", required=True, help="保存検索の名前")
@click.option("--notify/--no-notify", default=False, help="通知を送信")
@click.option("--channel", type=click.Choice(["slack", "email"]), default="slack")
@click.option("--recipient", "-r", multiple=True, help="通知先（複数指定可）")
@click.option("--dry-run", is_flag=True, help="実際の保存・通知をスキップ")
def saved_search_run_cmd(
    name: str,
    notify: bool,
    channel: str,
    recipient: tuple[str, ...],
    dry_run: bool,
) -> None:
    """保存検索を実行"""
    try:
        # 通知設定
        notify_config = None
        if notify and recipient:
            notify_config = {
                "channel": channel,
                "recipients": list(recipient),
                "enabled": True,
                "max_items": settings.notify_max_items,
            }

        if dry_run:
            console.print("[yellow]ドライラン モード[/yellow]")

        result = run_saved_search(
            name=name,
            notify=notify,
            notify_config=notify_config,
            dry_run=dry_run,
        )

        # 結果表示
        if result["status"] == "ok":
            console.print(f"[green]✓[/green] 保存検索を実行しました: {name}")
            console.print(f"  検索結果: {result['total']}件")
            console.print(f"  新規: {result['new']}件")
            if notify:
                if result.get("notified"):
                    console.print("  通知: [green]送信成功[/green]")
                elif result.get("notify_status") == "partial":
                    console.print("  通知: [yellow]一部失敗[/yellow]")
                elif result["new"] == 0:
                    console.print("  通知: [dim]新規なし（スキップ）[/dim]")
                else:
                    console.print("  通知: [dim]設定なし[/dim]")
        else:
            console.print(f"[red]✗[/red] 保存検索が失敗しました: {name}")
            console.print(f"  エラー: {result.get('error', '不明')}")
            sys.exit(1)

    except Exception as e:
        console.print(f"[red]エラー:[/red] {e}")
        sys.exit(1)


@saved_search_group.command("delete")
@click.option("--name", "-n", required=True, help="保存検索の名前")
@click.confirmation_option(prompt="本当に削除しますか?")
def saved_search_delete(name: str) -> None:
    """保存検索を削除"""
    try:
        if delete_saved_search(name):
            console.print(f"[green]✓[/green] 保存検索を削除しました: {name}")
        else:
            console.print(f"[yellow]保存検索が見つかりません:[/yellow] {name}")

    except Exception as e:
        console.print(f"[red]エラー:[/red] {e}")
        sys.exit(1)


# =============================================================================
# notify コマンドグループ
# =============================================================================


@cli.group()
def notify() -> None:
    """通知の管理"""
    pass


@notify.command("test")
@click.option("--channel", type=click.Choice(["slack", "email"]), required=True)
@click.option("--recipient", "-r", required=True, help="通知先")
def notify_test(channel: str, recipient: str) -> None:
    """通知のテスト送信"""
    from bid_aggregator.core.models import Item
    from datetime import datetime

    try:
        # テスト用のダミーアイテム
        test_items = [
            Item(
                source="test",
                title="【テスト】入札情報アラートのテスト通知",
                organization_name="テスト機関",
                published_at=datetime.now(),
                url="https://www.kkj.go.jp/s/",
                content_hash="test",
            ),
        ]

        console.print("[dim]テスト通知を送信中...[/dim]")

        send_notification(
            channel=channel,
            recipient=recipient,
            items=test_items,
            saved_search_name="テスト通知",
        )

        console.print(f"[green]✓[/green] テスト通知を送信しました: {channel} -> {recipient}")

    except Exception as e:
        console.print(f"[red]エラー:[/red] {e}")
        sys.exit(1)


# =============================================================================
# エントリーポイント
# =============================================================================


def main() -> None:
    """CLIエントリーポイント"""
    cli()


if __name__ == "__main__":
    main()
