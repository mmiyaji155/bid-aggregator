#!/bin/bash
# =============================================================================
# 入札情報アグリゲータ 定期実行スクリプト
# =============================================================================
#
# 使用方法:
#   ./scripts/daily_run.sh [--notify]
#
# オプション:
#   --notify  Slack/メール通知を有効化
#
# 環境変数:
#   BID_AGGREGATOR_DIR  プロジェクトディレクトリ（デフォルト: スクリプトの親ディレクトリ）
#   SLACK_WEBHOOK_URL   Slack通知先（--notify時に使用）
#   NOTIFY_EMAIL        メール通知先（--notify時に使用、Slack優先）
#   PPORTAL_KEYWORD     調達ポータル検索キーワード（デフォルト: 空=全件）
#   PPORTAL_MAX_PAGES   調達ポータル最大ページ数（デフォルト: 10）
#   PPORTAL_FROM        調達ポータル公開開始日の開始（YYYY-MM-DD、省略可）
#   PPORTAL_TO          調達ポータル公開開始日の終了（YYYY-MM-DD、省略可）
#
# =============================================================================

set -e

# スクリプトのディレクトリを取得
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="${BID_AGGREGATOR_DIR:-$(dirname "$SCRIPT_DIR")}"

# ログディレクトリ
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

# ログファイル（日付付き）
DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/daily_run_$DATE.log"

# 調達ポータル設定
PPORTAL_KEYWORD="${PPORTAL_KEYWORD:-}"
PPORTAL_MAX_PAGES="${PPORTAL_MAX_PAGES:-10}"
PPORTAL_FROM="${PPORTAL_FROM:-}"
PPORTAL_TO="${PPORTAL_TO:-}"

# 関数: ログ出力
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# 関数: エラーハンドリング
error_exit() {
    log "ERROR: $1"
    exit 1
}

# =============================================================================
# メイン処理
# =============================================================================

log "========== 定期実行開始 =========="
log "プロジェクトディレクトリ: $PROJECT_DIR"

# 仮想環境の確認と有効化
VENV_DIR="$PROJECT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    error_exit "仮想環境が見つかりません: $VENV_DIR"
fi

log "仮想環境を有効化: $VENV_DIR"
source "$VENV_DIR/bin/activate"

# プロジェクトディレクトリに移動
cd "$PROJECT_DIR"

# bid-cliの確認（オプショナル）
BID_CLI_AVAILABLE=false
if command -v bid-cli &> /dev/null; then
    BID_CLI_AVAILABLE=true
fi

# 設定ファイルの確認
CONFIG_FILE="$PROJECT_DIR/config/queries.yml"

# =============================================================================
# 1. KKJ データ取得
# =============================================================================

if [ -f "$CONFIG_FILE" ] && [ "$BID_CLI_AVAILABLE" = true ]; then
    log "--- KKJ データ取得開始 ---"
    
    if bid-cli ingest --queries "$CONFIG_FILE" >> "$LOG_FILE" 2>&1; then
        log "KKJ データ取得完了"
    else
        log "WARNING: KKJ データ取得でエラーが発生しました（処理は継続）"
    fi
else
    log "--- KKJ データ取得スキップ（bid-cli未設定または設定ファイルなし）---"
fi

# =============================================================================
# 2. 調達ポータル データ取得
# =============================================================================

log "--- 調達ポータル データ取得開始 ---"
log "キーワード: '${PPORTAL_KEYWORD:-（全件）}', 最大ページ: $PPORTAL_MAX_PAGES"
if [ -n "$PPORTAL_FROM" ] || [ -n "$PPORTAL_TO" ]; then
    log "公開開始日: ${PPORTAL_FROM:-} ～ ${PPORTAL_TO:-}"
fi

# 通知オプションの構築
PPORTAL_OPTS=()
PPORTAL_DATE_OPTS=()
if [ -n "$PPORTAL_FROM" ]; then
    PPORTAL_DATE_OPTS+=(--from "$PPORTAL_FROM")
fi
if [ -n "$PPORTAL_TO" ]; then
    PPORTAL_DATE_OPTS+=(--to "$PPORTAL_TO")
fi
if [ "$1" = "--notify" ]; then
    if [ -n "$SLACK_WEBHOOK_URL" ]; then
        PPORTAL_OPTS=(--slack-webhook "$SLACK_WEBHOOK_URL")
        log "調達ポータル通知: Slack"
    elif [ -n "$NOTIFY_EMAIL" ]; then
        PPORTAL_OPTS=(--email "$NOTIFY_EMAIL")
        log "調達ポータル通知: Email ($NOTIFY_EMAIL)"
    fi
fi

# 調達ポータル取得実行
if python -m bid_aggregator.cli.pportal_ingest \
    -k "$PPORTAL_KEYWORD" \
    --max-pages "$PPORTAL_MAX_PAGES" \
    "${PPORTAL_DATE_OPTS[@]}" \
    "${PPORTAL_OPTS[@]}" >> "$LOG_FILE" 2>&1; then
    log "調達ポータル データ取得完了"
else
    log "WARNING: 調達ポータル取得でエラーが発生しました（処理は継続）"
fi

# =============================================================================
# 3. 保存検索の実行（--notify オプション時、bid-cli使用可能時）
# =============================================================================

if [ "$1" = "--notify" ] && [ "$BID_CLI_AVAILABLE" = true ]; then
    log "--- 保存検索・通知開始 ---"
    
    # 通知先の決定
    NOTIFY_CHANNEL=""
    NOTIFY_RECIPIENT=""
    
    if [ -n "$SLACK_WEBHOOK_URL" ]; then
        NOTIFY_CHANNEL="slack"
        NOTIFY_RECIPIENT="$SLACK_WEBHOOK_URL"
        log "通知先: Slack"
    elif [ -n "$NOTIFY_EMAIL" ]; then
        NOTIFY_CHANNEL="email"
        NOTIFY_RECIPIENT="$NOTIFY_EMAIL"
        log "通知先: Email ($NOTIFY_EMAIL)"
    else
        log "WARNING: 通知先が設定されていません（SLACK_WEBHOOK_URL または NOTIFY_EMAIL）"
    fi
    
    # 有効な保存検索を実行
    if [ -n "$NOTIFY_CHANNEL" ]; then
        # 保存検索一覧を取得して実行
        SAVED_SEARCHES=$(bid-cli saved-search list --enabled-only 2>/dev/null | grep -E "^\│" | awk -F'│' '{print $3}' | tr -d ' ' | grep -v "^$" | grep -v "名前" || true)
        
        if [ -n "$SAVED_SEARCHES" ]; then
            for name in $SAVED_SEARCHES; do
                log "保存検索実行: $name"
                if bid-cli saved-search run \
                    --name "$name" \
                    --notify \
                    --channel "$NOTIFY_CHANNEL" \
                    --recipient "$NOTIFY_RECIPIENT" >> "$LOG_FILE" 2>&1; then
                    log "保存検索完了: $name"
                else
                    log "WARNING: 保存検索でエラー: $name"
                fi
            done
        else
            log "有効な保存検索がありません"
        fi
    fi
else
    log "保存検索・通知はスキップ"
fi

# =============================================================================
# 4. 統計情報の出力
# =============================================================================

log "--- 統計情報 ---"
if [ "$BID_CLI_AVAILABLE" = true ]; then
    bid-cli db stats >> "$LOG_FILE" 2>&1 || true
else
    # bid-cliがない場合はPythonで統計を取得
python -c "
from bid_aggregator.core.database import get_connection
with get_connection() as conn:
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM items')
    total = cursor.fetchone()[0]
    cursor.execute('SELECT source, COUNT(*) FROM items GROUP BY source')
    by_source = cursor.fetchall()
    print(f'総アイテム数: {total}')
    for source, count in by_source:
        print(f'  {source}: {count}')
" >> "$LOG_FILE" 2>&1 || true
fi

# =============================================================================
# 5. 古いログの削除（30日以上前）
# =============================================================================

log "--- 古いログの削除 ---"
find "$LOG_DIR" -name "daily_run_*.log" -mtime +30 -delete 2>/dev/null || true
log "30日以上前のログを削除しました"

# =============================================================================
# 完了
# =============================================================================

log "========== 定期実行完了 =========="

# 仮想環境を無効化
deactivate 2>/dev/null || true

exit 0
