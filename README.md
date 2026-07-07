# 入札・調達情報アグリゲータ（Bid Aggregator）

官公需情報ポータルサイト（KKJ）のAPIと調達ポータル（p-portal.go.jp）から
入札・調達情報を収集してSQLiteに蓄積し、検索・通知・エクスポートを行うCLIツールです。

## 概要

- **KKJ API**: 公式APIから定期取得し共通スキーマに正規化
- **調達ポータル**: HTMLスクレイピングで中央省庁の調達情報を取得
- SQLiteに保存し、キーワード・期間・機関で検索
- 保存検索・Slack/メール通知
- CSV/JSONエクスポート
- 日付分割による全件取得（1000件超対策）

## データソース比較

| ソース | 方式 | データ範囲 | 特徴 |
|--------|------|-----------|------|
| KKJ（官公需情報ポータル） | 公式API | 中央+地方 | 安定、XMLレスポンス |
| 調達ポータル | スクレイピング | 中央省庁のみ | HTML構造変更に注意 |

## 出典

- [官公需情報ポータルサイト（KKJ）](https://www.kkj.go.jp/s/)
- [調達ポータル（政府電子調達）](https://www.p-portal.go.jp/)

## 必要環境

- Python 3.11+
- SQLite 3

## インストール
```bash
# リポジトリをクローン
git clone <repository-url>
cd bid-aggregator

# 仮想環境を作成
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 依存パッケージをインストール
pip install -e ".[dev]"
```

## クイックスタート

### 1. データベースを初期化
```bash
python -c "from bid_aggregator.core.database import init_db; init_db()"
```

### 2. 入札情報を取得
```bash
# 調達ポータルから取得（推奨）
python -m bid_aggregator.cli.pportal_ingest -k "AI" --max-pages 5

# KKJ APIから取得
bid-cli ingest --source kkj --queries config/queries.yml
```

### 期間指定で一括取得（重複なし保存）
```bash
# 調達ポータル: 指定期間の全件を日別に取得してDBへ保存
bid-cli backfill --source pportal --from 2026-05-22 --to 2026-05-24 --days 1

# 調達ポータル: キーワード付き
bid-cli backfill --source pportal -k "生成AI" --from 2026-05-22 --to 2026-05-24 --days 1

# KKJ: API仕様上、キーワード・機関名・地域コードのいずれかが必要
bid-cli backfill --source kkj -k "AI" --from 2026-05-22 --to 2026-05-24 --days 1
```

### 3. 定期実行（両ソース）
```bash
./scripts/daily_run.sh --notify
```

## 調達ポータル取得

### 基本的な使い方
```bash
# ドライラン（DB保存なし）
python -m bid_aggregator.cli.pportal_ingest --dry-run

# キーワード検索
python -m bid_aggregator.cli.pportal_ingest -k "システム" --max-pages 5

# 公開開始日で絞り込み
python -m bid_aggregator.cli.pportal_ingest -k "生成AI" \
  --from 2026-05-22 --to 2026-05-24 --max-pages 1

# 全件取得（10ページ=500件）
python -m bid_aggregator.cli.pportal_ingest --max-pages 10
```

### 通知付き取得
```bash
# Slack通知
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/xxx/yyy/zzz"
python -m bid_aggregator.cli.pportal_ingest -k "AI" --max-pages 5

# メール通知
python -m bid_aggregator.cli.pportal_ingest -k "AI" --email "user@example.com"

# 通知を無効化
python -m bid_aggregator.cli.pportal_ingest -k "AI" --no-notify
```

### Pythonから直接使用
```python
from bid_aggregator.ingest.pportal_client import PPortalClient

with PPortalClient() as client:
    # 検索
    results, total = client.search(keyword="AI")
    print(f"総件数: {total}")
    
    for r in results[:5]:
        print(f"{r.title} - {r.organization}")
    
    # 詳細取得
    detail = client.get_detail_by_url(results[0].detail_url)
    print(f"調達種別: {detail.procurement_type}")
    print(f"品目分類: {detail.item_category}")
    print(f"資料URL: {detail.document_urls}")

# 全ページ取得（ジェネレータ）
with PPortalClient() as client:
    for result in client.search_all(keyword="", max_pages=10):
        print(result.title)
```

### 取得できる情報

**検索結果（PPortalSearchResult）**
- 調達案件番号
- 案件名称
- 調達機関
- 調達種別（入札公告、落札公示等）
- 公開開始日

**詳細情報（PPortalDetailResult）**
- 調達案件番号
- 調達種別
- 分類（物品・役務、簡易な公共事業）
- 調達案件名称
- 公開開始日
- 調達機関
- 調達機関所在地
- 調達品目分類
- 公告内容
- 調達資料URL

## KKJ API取得

### CLI コマンド
```bash
# 収集
bid-cli ingest --source kkj --queries config/queries.yml [--dry-run]

# 全件取得（1000件超対策）
bid-cli full-ingest --keyword "AI" --from 2025-01-01 --to 2025-01-31 \
                   [--days 7] [--org ORG] [--dry-run]

# 検索
bid-cli search --keyword TEXT [--from DATE] [--to DATE] [--org TEXT] \
               [--source kkj|pportal|all] [--order-by newest|deadline] \
               [--limit N]

# エクスポート
bid-cli export --format csv|json [--output FILE]

# 保存検索
bid-cli saved-search add --name NAME --keyword TEXT [--from DATE] [--to DATE]
bid-cli saved-search run --name NAME [--notify] [--channel slack] [--recipient URL]
bid-cli saved-search list [--enabled-only]

# データベース
bid-cli db init
bid-cli db stats
```

### queries.yml
```yaml
version: 1

queries:
  - name: ai_related
    source: kkj
    params:
      Query: "AI OR 機械学習 OR 画像検査"
    limit: 1000
    enabled: true
```

## 定期実行

### 手動実行
```bash
# 通知なし
./scripts/daily_run.sh

# 通知あり
./scripts/daily_run.sh --notify
```

### 環境変数でカスタマイズ
```bash
export PPORTAL_KEYWORD="システム"      # 調達ポータル検索キーワード
export PPORTAL_MAX_PAGES=5             # 最大ページ数
export SLACK_WEBHOOK_URL="https://..." # Slack通知先
./scripts/daily_run.sh --notify
```

### macOS launchd

`scripts/com.user.bid-aggregator.plist` を使用。詳細は `scripts/README.md` を参照。

## 設定

### 環境変数（.env）
```bash
# データベース
DATABASE_URL=sqlite:///data/bid_aggregator.db

# ログ
LOG_LEVEL=INFO

# 通知
NOTIFY_MAX_ITEMS=100

# Slack通知
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/xxx/yyy/zzz

# メール通知
NOTIFY_EMAIL=user@example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=user@example.com
SMTP_PASSWORD=password
SMTP_FROM=noreply@example.com

# 調達ポータル
PPORTAL_KEYWORD=
PPORTAL_MAX_PAGES=10
```

## ディレクトリ構成
```
bid-aggregator/
├── README.md
├── .env.example
├── pyproject.toml
├── config/
│   └── queries.example.yml
├── data/                    # SQLiteデータベース（gitignore）
├── logs/                    # ログファイル（gitignore）
├── scripts/
│   ├── daily_run.sh         # 定期実行スクリプト
│   ├── test_pportal.sh
│   └── com.user.bid-aggregator.plist
└── src/
    └── bid_aggregator/
        ├── cli/
        │   ├── main.py          # bid-cli
        │   └── pportal_ingest.py # 調達ポータルCLI
        ├── core/
        │   ├── config.py
        │   ├── database.py
        │   └── models.py
        ├── ingest/
        │   ├── kkj_client.py     # KKJ APIクライアント
        │   ├── pportal_client.py # 調達ポータルクライアント
        │   ├── normalizer.py
        │   ├── pipeline.py
        │   └── full_ingest.py
        └── notify/
            ├── sender.py
            └── runner.py
```


## 落札実績オープンデータ

調達ポータルで公開されている落札実績CSVを取り込みます。

### 使い方
```bash
# 利用可能なファイル一覧
python -m bid_aggregator.cli.pportal_award --list

# 過去7日分の差分を取得
python -m bid_aggregator.cli.pportal_award --days 7

# 特定日の差分を取得
python -m bid_aggregator.cli.pportal_award --date 20260131

# 年度全件を取得
python -m bid_aggregator.cli.pportal_award --year 2024

# CSV出力
python -m bid_aggregator.cli.pportal_award --days 7 --output awards.csv
```

### Pythonから直接使用
```python
from bid_aggregator.ingest.pportal_award import PPortalAwardClient

with PPortalAwardClient() as client:
    # 利用可能ファイル一覧
    files = client.list_available_files()
    print(f"全件: {len(files['yearly'])}件, 差分: {len(files['diff'])}件")
    
    # 差分ダウンロード
    records = client.download_diff("20260131")
    for r in records[:5]:
        print(f"{r.title} / {r.award_amount:,.0f}円 / {r.winner_name}")
    
    # 年度全件ダウンロード
    records = client.download_yearly(2024)
```

### 取得できる情報

| フィールド | 内容 |
|-----------|------|
| case_number | 調達案件番号 |
| title | 案件名称 |
| award_date | 落札日 |
| award_amount | 落札金額 |
| procurement_type | 調達種別コード |
| org_code | 機関コード |
| winner_name | 落札者名 |
| corporate_number | 法人番号 |

## 注意事項

- 調達ポータルはスクレイピングのため、サイトの構造変更で動作しなくなる可能性があります
- レート制限（デフォルト2秒間隔）を守ってください
- 大量取得時はサーバー負荷に配慮してください

## ライセンス

MIT License

## 関連リンク

- [官公需情報ポータルサイト](https://www.kkj.go.jp/s/)
- [KKJ API ガイド](https://www.kkj.go.jp/doc/ja/api_guide.pdf)
- [調達ポータル](https://www.p-portal.go.jp/)
