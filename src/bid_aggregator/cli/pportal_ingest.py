#!/usr/bin/env python3
"""
調達ポータル取得CLI

使用例:
    # ドライラン
    python -m bid_aggregator.cli.pportal_ingest --dry-run
    
    # キーワード検索
    python -m bid_aggregator.cli.pportal_ingest -k "AI" --max-pages 5
    
    # Slack通知付き
    python -m bid_aggregator.cli.pportal_ingest -k "AI" --slack-webhook "$SLACK_WEBHOOK_URL"
    
    # メール通知付き
    python -m bid_aggregator.cli.pportal_ingest -k "AI" --email "user@example.com"
"""

import argparse
import logging
import os
import sys

from bid_aggregator.ingest.full_ingest import run_pportal_ingest, run_pportal_ingest_with_notify


def main():
    parser = argparse.ArgumentParser(description="調達ポータルから入札情報を取得")
    parser.add_argument("-k", "--keyword", default="", help="検索キーワード")
    parser.add_argument("--max-pages", type=int, default=10, help="最大取得ページ数")
    parser.add_argument("--from", dest="publish_start_from", help="公開開始日の開始（YYYY-MM-DD）")
    parser.add_argument("--to", dest="publish_start_to", help="公開開始日の終了（YYYY-MM-DD）")
    parser.add_argument("--dry-run", action="store_true", help="DB保存・通知をスキップ")
    parser.add_argument("-v", "--verbose", action="store_true", help="詳細ログ")
    
    # 通知オプション
    parser.add_argument("--slack-webhook", 
                        default=os.environ.get("SLACK_WEBHOOK_URL"),
                        help="Slack Webhook URL（環境変数 SLACK_WEBHOOK_URL も可）")
    parser.add_argument("--email", 
                        default=os.environ.get("NOTIFY_EMAIL"),
                        help="通知先メールアドレス（環境変数 NOTIFY_EMAIL も可）")
    parser.add_argument("--no-notify", action="store_true", help="通知を無効化")
    
    args = parser.parse_args()
    
    # ログ設定
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    print("=" * 60)
    print("調達ポータル取得")
    print("=" * 60)
    print(f"キーワード: '{args.keyword}'")
    if args.publish_start_from or args.publish_start_to:
        print(f"公開開始日: {args.publish_start_from or ''} ～ {args.publish_start_to or ''}")
    print(f"最大ページ: {args.max_pages}")
    print(f"ドライラン: {args.dry_run}")
    print(f"Slack通知: {'有効' if args.slack_webhook and not args.no_notify else '無効'}")
    print(f"メール通知: {'有効' if args.email and not args.no_notify else '無効'}")
    print()
    
    try:
        # 通知の有無で分岐
        if (args.slack_webhook or args.email) and not args.no_notify:
            result = run_pportal_ingest_with_notify(
                keyword=args.keyword,
                max_pages=args.max_pages,
                publish_start_from=args.publish_start_from,
                publish_start_to=args.publish_start_to,
                slack_webhook_url=args.slack_webhook if not args.no_notify else None,
                email_to=args.email if not args.no_notify else None,
                dry_run=args.dry_run,
            )
        else:
            result = run_pportal_ingest(
                keyword=args.keyword,
                max_pages=args.max_pages,
                publish_start_from=args.publish_start_from,
                publish_start_to=args.publish_start_to,
                dry_run=args.dry_run,
            )
        
        print()
        print("=" * 60)
        print("結果")
        print("=" * 60)
        print(result.summary())
        
        return 0
        
    except Exception as e:
        logging.error(f"エラー: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
