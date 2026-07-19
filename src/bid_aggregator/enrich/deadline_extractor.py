"""
締切メタデータ抽出モジュール

対象: items のうち deadline_at IS NULL AND url IS NOT NULL の案件。
処理: url の文書（PDF/HTML）を取得しテキスト化 → LLM（tool use）で構造化抽出
      → confidence=high かつ日付が妥当な場合のみ items.deadline_at を更新。
      全試行を enrich_log に記録する（精度検証・再実行判断の基盤）。

使用方法:
    bid-cli enrich-deadlines --limit 20
"""

import io
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ValidationError

from bid_aggregator.core.database import get_connection

logger = logging.getLogger(__name__)


# =============================================================================
# 設定値
# =============================================================================

MODEL_NAME = "claude-haiku-4-5-20251001"
GEMINI_MODEL_NAME = "gemini-2.5-flash"
MAX_TOKENS = 1024

#: PDF: 先頭何ページまで読むか
_MAX_PDF_PAGES = 10
#: 文書テキストの最大文字数（PDF/HTML共通。LLMへ渡すコンテキストを抑える）
_MAX_DOC_CHARS = 30_000

#: 文書取得のタイムアウト（接続・読み取りを明示的に分離）
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

#: 妥当と見なす締切日の範囲（明らかな抽出ミスの足切り）
_PLAUSIBLE_DATE_MIN = date(2020, 1, 1)
_PLAUSIBLE_DATE_MAX = date(2030, 12, 31)

#: ANTHROPIC_API_KEY が環境変数に無い場合のフォールバック読み込み先。
#: mya-automate リポジトリの .env にキーだけを読み込む（値はログ・コードに出さない）。
_FALLBACK_ANTHROPIC_ENV_PATH = Path(
    os.environ.get(
        "BID_AGGREGATOR_ANTHROPIC_ENV_FILE",
        str(Path.home() / "projects" / "own" / "mya-automate" / ".env"),
    )
)


class DeadlineExtractionError(Exception):
    """締切抽出処理の致命的エラー（設定不備等）"""


# =============================================================================
# LLM 構造化出力スキーマ
# =============================================================================


class DeadlineExtraction(BaseModel):
    """LLM抽出結果"""

    deadline_date: str | None = None
    deadline_kind: Literal["応札締切", "資料提出", "質問期限", "不明"] = "不明"
    confidence: Literal["high", "low"] = "low"
    evidence: str = ""


_DEADLINE_TOOL = {
    "name": "extract_deadline",
    "description": "官公庁の入札公告文書から、応札・入札書提出の締切日を抽出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "deadline_date": {
                "type": ["string", "null"],
                "description": (
                    "応札・入札書提出の締切日（YYYY-MM-DD形式）。"
                    "文書中に明記されていない、または複数の候補があり特定できない場合は null。"
                ),
            },
            "deadline_kind": {
                "type": "string",
                "enum": ["応札締切", "資料提出", "質問期限", "不明"],
                "description": "抽出した日付が何の締切かの分類",
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "low"],
                "description": "deadline_date の確信度。明確に一つの応札締切が記載されていれば high、曖昧・推測を含むなら low",
            },
            "evidence": {
                "type": "string",
                "description": "根拠となる原文の抜粋（80字以内）",
            },
        },
        "required": ["deadline_date", "deadline_kind", "confidence", "evidence"],
    },
}


# =============================================================================
# 対象選定
# =============================================================================


def select_targets(limit: int) -> list[dict[str, Any]]:
    """deadline_at が未設定かつ url がある案件を新しい順に取得する。"""
    with get_connection() as conn:
        cursor = conn.execute(
            """
            SELECT id, title, organization_name, url, published_at, created_at
            FROM items
            WHERE deadline_at IS NULL AND url IS NOT NULL AND url != ''
            ORDER BY COALESCE(published_at, created_at) DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]


# =============================================================================
# 文書取得・テキスト化
# =============================================================================


@dataclass
class FetchResult:
    ok: bool
    doc_kind: str  # "pdf" | "html" | "unsupported" | "error"
    text: str | None
    error: str | None


def classify_content(url: str, content_type: str, content: bytes) -> str:
    """Content-Type / URL拡張子 / マジックナンバーから文書種別を判定する。"""
    ct = (content_type or "").lower()
    lowered_url = url.lower().split("?")[0]

    if content[:5] == b"%PDF-":
        return "pdf"
    if "pdf" in ct or lowered_url.endswith(".pdf"):
        return "pdf"
    if "html" in ct or lowered_url.endswith((".html", ".htm")):
        return "html"
    if ct.startswith("text/"):
        # プレーンテキストもタグ除去経路（無害）で処理する
        return "html"
    return "unsupported"


def extract_html_text(content: bytes) -> str:
    """HTMLからタグを除去して本文テキスト化する（既存依存の BeautifulSoup を使用）。"""
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:_MAX_DOC_CHARS]


def _extract_pdf_text(content: bytes) -> tuple[str | None, str | None]:
    """PDFの先頭 _MAX_PDF_PAGES ページからテキストを抽出する。"""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - 依存導入済み前提
        return None, "pypdf がインストールされていません"

    try:
        reader = PdfReader(io.BytesIO(content))
    except Exception as e:
        return None, f"PDF読み込みエラー: {type(e).__name__}: {e}"

    pages_text: list[str] = []
    try:
        for page in reader.pages[:_MAX_PDF_PAGES]:
            try:
                pages_text.append(page.extract_text() or "")
            except Exception as e:  # 1ページの抽出失敗で全体を諦めない
                logger.debug(f"PDFページ抽出エラー: {type(e).__name__}: {e}")
    except Exception as e:
        return None, f"PDF本文抽出エラー: {type(e).__name__}: {e}"

    text = "\n".join(t for t in pages_text if t).strip()
    if not text:
        return None, "PDFからテキストを抽出できませんでした（スキャン画像等の可能性）"
    return text[:_MAX_DOC_CHARS], None


def is_fetchable_url(url: str) -> bool:
    """http(s) の絶対URLかどうかを判定する。

    調達ポータル（pportal）は detail_url に `javascript:doSubmitParams(...)` 形式の
    ハンドラ文字列をそのまま格納することがあり（pportal_client._parse_row 参照）、
    これは httpx で取得不能なため事前に弾く。
    """
    lowered = url.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _fetch_with_retry(
    http_client: httpx.Client, url: str
) -> tuple[httpx.Response | None, str | None]:
    """文書取得。API/ネットワークエラー時は1回だけリトライする。

    URL形式不正等（httpx.HTTPError に含まれない ValueError 等）も捕捉し、
    呼び出し元にエラー理由として返す（1件の異常URLで処理全体を止めない）。
    """
    last_error: str | None = None
    for attempt in range(2):
        try:
            response = http_client.get(url)
            return response, None
        except httpx.HTTPError as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"文書取得エラー（試行{attempt + 1}/2）: {url} -> {last_error}")
        except Exception as e:
            # URLパース不能など httpx.HTTPError に含まれない例外（想定外URL形式）
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"文書取得で想定外の例外（試行{attempt + 1}/2）: {url} -> {last_error}")
    return None, last_error


def fetch_and_extract_text(http_client: httpx.Client, url: str) -> FetchResult:
    """url を取得し、PDF/HTMLに応じてテキスト抽出する。未対応形式・失敗はスキップ理由を返す。"""
    if not is_fetchable_url(url):
        return FetchResult(
            ok=False,
            doc_kind="unsupported",
            text=None,
            error="無効なURL形式（http(s)絶対URLではない。JavaScriptハンドラ等の可能性）",
        )

    response, error = _fetch_with_retry(http_client, url)
    if response is None:
        return FetchResult(ok=False, doc_kind="error", text=None, error=error)

    if response.status_code >= 400:
        return FetchResult(
            ok=False, doc_kind="error", text=None, error=f"HTTPステータス {response.status_code}"
        )

    content = response.content
    content_type = response.headers.get("content-type", "")
    kind = classify_content(url, content_type, content)

    if kind == "pdf":
        text, err = _extract_pdf_text(content)
        if err:
            return FetchResult(ok=False, doc_kind="pdf", text=None, error=err)
        return FetchResult(ok=True, doc_kind="pdf", text=text, error=None)

    if kind == "html":
        text = extract_html_text(content)
        if not text.strip():
            return FetchResult(
                ok=False, doc_kind="html", text=None, error="本文抽出結果が空でした"
            )
        return FetchResult(ok=True, doc_kind="html", text=text, error=None)

    return FetchResult(
        ok=False,
        doc_kind="unsupported",
        text=None,
        error=f"未対応のContent-Type: {content_type or '不明'}",
    )


# =============================================================================
# 妥当性チェック
# =============================================================================


def is_plausible_deadline(deadline_date: str | None, published_at: str | None) -> bool:
    """締切日が 2020〜2030 の範囲内、かつ（分かれば）公開日以降であることを確認する。"""
    if not deadline_date:
        return False
    try:
        d = date.fromisoformat(deadline_date[:10])
    except ValueError:
        return False
    if not (_PLAUSIBLE_DATE_MIN <= d <= _PLAUSIBLE_DATE_MAX):
        return False
    if published_at:
        try:
            p = date.fromisoformat(str(published_at)[:10])
            if d < p:
                return False
        except ValueError:
            pass
    return True


# =============================================================================
# LLM 抽出
# =============================================================================


def _resolve_anthropic_api_key() -> str:
    """ANTHROPIC_API_KEY を環境変数、または mya-automate/.env から解決する（値はログしない）。"""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key

    if _FALLBACK_ANTHROPIC_ENV_PATH.exists():
        for line in _FALLBACK_ANTHROPIC_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    os.environ["ANTHROPIC_API_KEY"] = value
                    return value

    raise DeadlineExtractionError(
        "ANTHROPIC_API_KEY が見つかりません。環境変数、または "
        f"{_FALLBACK_ANTHROPIC_ENV_PATH} に設定してください。"
    )


def build_anthropic_client() -> Any:
    """anthropic クライアントを構築する（遅延importで未インストール環境でも他機能は使える）。"""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - 依存導入済み前提
        raise DeadlineExtractionError("anthropic パッケージが必要です") from exc

    api_key = _resolve_anthropic_api_key()
    return anthropic.Anthropic(api_key=api_key)


def _read_env_file_key(name: str) -> str:
    """mya-automate/.env から任意のキーを読む（値はログしない）。"""
    key = os.environ.get(name, "").strip()
    if key:
        return key
    if _FALLBACK_ANTHROPIC_ENV_PATH.exists():
        for line in _FALLBACK_ANTHROPIC_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    return ""


def build_llm_client() -> tuple[str, Any]:
    """利用可能な LLM プロバイダを選択する。

    ANTHROPIC_API_KEY があれば ("anthropic", client)、
    無ければ GEMINI_API_KEY で ("gemini", api_key)。どちらも無ければエラー。
    """
    try:
        return ("anthropic", build_anthropic_client())
    except DeadlineExtractionError:
        pass
    gemini_key = _read_env_file_key("GEMINI_API_KEY")
    if gemini_key:
        return ("gemini", gemini_key)
    raise DeadlineExtractionError(
        "ANTHROPIC_API_KEY / GEMINI_API_KEY のいずれも見つかりません。"
    )


_GEMINI_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "deadline_date": {"type": "STRING", "nullable": True},
        "deadline_kind": {
            "type": "STRING",
            "enum": ["応札締切", "資料提出", "質問期限", "不明"],
        },
        "confidence": {"type": "STRING", "enum": ["high", "low"]},
        "evidence": {"type": "STRING"},
    },
    "required": ["deadline_kind", "confidence", "evidence"],
}


def _extract_deadline_gemini(
    api_key: str, title: str, organization: str, doc_text: str
) -> DeadlineExtraction | None:
    """Gemini REST（response_schema による構造化出力）で締切を抽出する。"""
    import json as _json

    import httpx as _httpx

    prompt = _build_prompt(title, organization, doc_text)
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL_NAME}:generateContent"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _GEMINI_RESPONSE_SCHEMA,
            "maxOutputTokens": MAX_TOKENS,
            "temperature": 0,
        },
    }
    last_error: str | None = None
    for attempt in range(2):
        try:
            r = _httpx.post(
                url,
                params={"key": api_key},
                json=body,
                timeout=_httpx.Timeout(10.0, read=60.0),
            )
            r.raise_for_status()
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            payload = _json.loads(text)
            if payload.get("deadline_date") in ("", "null"):
                payload["deadline_date"] = None
            return DeadlineExtraction.model_validate(payload)
        except Exception as e:  # HTTPStatusError / KeyError / ValidationError 等
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Gemini抽出エラー（試行{attempt + 1}/2）: {last_error}")
    logger.error(f"Gemini抽出失敗（2回試行後）: {last_error}")
    return None


def _build_prompt(title: str, organization: str, doc_text: str) -> str:
    return (
        "以下は官公庁の入札・調達案件に関する公告文書の抜粋です。\n\n"
        f"案件名: {title}\n"
        f"発注機関: {organization}\n\n"
        "---文書本文---\n"
        f"{doc_text}\n"
        "---\n\n"
        "この文書から、応札・入札書提出の締切日を特定してください。"
        "資料提出締切・質問（意見招請）締切など他の締切と混同しないこと。"
        "応札締切が読み取れない場合は deadline_date を null にすること。"
        "日付が曖昧、または複数の解釈が可能な場合は confidence を low にすること。"
    )


def extract_deadline_llm(
    client: Any, title: str, organization: str, doc_text: str
) -> DeadlineExtraction | None:
    """LLM で締切情報を構造化抽出する。APIエラー時は1回だけリトライする。

    client には build_llm_client() の (provider, obj) タプル、
    または後方互換として anthropic クライアント単体を渡せる。
    """
    if isinstance(client, tuple):
        provider, obj = client
        if provider == "gemini":
            return _extract_deadline_gemini(obj, title, organization, doc_text)
        client = obj  # anthropic

    prompt = _build_prompt(title, organization, doc_text)

    last_error: str | None = None
    for attempt in range(2):
        try:
            response = client.messages.create(
                model=MODEL_NAME,
                max_tokens=MAX_TOKENS,
                tools=[_DEADLINE_TOOL],
                tool_choice={"type": "tool", "name": "extract_deadline"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:  # anthropic.APIError 等
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(f"LLM抽出エラー（試行{attempt + 1}/2）: {last_error}")
            continue

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "extract_deadline":
                try:
                    return DeadlineExtraction.model_validate(block.input)
                except ValidationError as e:
                    last_error = f"スキーマ検証エラー: {e}"
                    logger.warning(last_error)
                    break
        else:
            last_error = "tool_use ブロックが見つかりません"
            logger.warning(last_error)

    logger.error(f"LLM抽出失敗（2回試行後）: {last_error}")
    return None


# =============================================================================
# DB 書き込み・監査ログ
# =============================================================================


_WAREKI_RE = __import__("re").compile(
    r"令和\s*(\d{1,2}|元)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)


def normalize_deadline_date(raw: str | None) -> str | None:
    """LLM が返す日付表記を ISO (YYYY-MM-DD) に正規化する。和暦（令和N年M月D日）も変換する。"""
    if not raw:
        return None
    raw = raw.strip()
    m = _WAREKI_RE.search(raw)
    if m:
        year_part = m.group(1)
        year = 2018 + (1 if year_part == "元" else int(year_part))
        return f"{year:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return raw


def _now_utc() -> str:
    return datetime.now(UTC).isoformat()


def _insert_enrich_log(
    conn: Any,
    item_id: int,
    url: str | None,
    status: str,
    deadline_date: str | None,
    deadline_kind: str | None,
    confidence: str | None,
    evidence: str | None,
    error: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO enrich_log
        (item_id, url, status, deadline_date, deadline_kind, confidence, evidence, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (item_id, url, status, deadline_date, deadline_kind, confidence, evidence, error, _now_utc()),
    )


def _update_item_deadline(conn: Any, item_id: int, deadline_date: str) -> None:
    conn.execute(
        "UPDATE items SET deadline_at = ?, updated_at = ? WHERE id = ?",
        (f"{deadline_date}T00:00:00", _now_utc(), item_id),
    )


# =============================================================================
# メイン処理
# =============================================================================


class EnrichResult:
    """締切抽出処理の結果集計。"""

    def __init__(self) -> None:
        self.total: int = 0
        self.fetch_ok: int = 0
        self.fetch_skip: int = 0
        self.fetch_error: int = 0
        self.llm_high: int = 0
        self.llm_low: int = 0
        self.llm_error: int = 0
        self.updated: int = 0
        self.rows: list[dict[str, Any]] = []

    def summary(self) -> str:
        return (
            f"対象: {self.total}件, 文書取得成功: {self.fetch_ok}件, "
            f"スキップ: {self.fetch_skip}件, 取得エラー: {self.fetch_error}件, "
            f"LLM high: {self.llm_high}件, LLM low: {self.llm_low}件, "
            f"LLMエラー: {self.llm_error}件, deadline_at更新: {self.updated}件"
        )


def run_enrich_deadlines(
    limit: int = 20,
    dry_run: bool = False,
    request_interval: float = 1.0,
) -> EnrichResult:
    """締切メタデータ抽出パイプラインを実行する。

    Args:
        limit: 処理対象の上限件数（LLM呼び出し回数の上限にも一致する）
        dry_run: True の場合、DB書き込み（items更新・enrich_log記録）をスキップする
        request_interval: 文書取得の連続アクセス間隔（秒）
    """
    result = EnrichResult()
    targets = select_targets(limit)
    result.total = len(targets)
    if not targets:
        logger.info("締切抽出対象がありません（deadline_at IS NULL AND url IS NOT NULL の案件なし）")
        return result

    anthropic_client = build_llm_client()

    last_fetch_time = 0.0
    with httpx.Client(
        timeout=_HTTP_TIMEOUT, follow_redirects=True, headers=_HTTP_HEADERS
    ) as http_client:
        for idx, target in enumerate(targets):
            if idx > 0:
                elapsed = time.monotonic() - last_fetch_time
                if elapsed < request_interval:
                    time.sleep(request_interval - elapsed)
            last_fetch_time = time.monotonic()

            item_id = target["id"]
            url = target["url"]
            row: dict[str, Any] = {
                "item_id": item_id,
                "title": target["title"],
                "url": url,
            }

            try:
                row.update(
                    _process_one(
                        http_client=http_client,
                        anthropic_client=anthropic_client,
                        target=target,
                        result=result,
                        dry_run=dry_run,
                    )
                )
            except Exception as e:  # 1件の想定外エラーで全体を止めない
                logger.error(f"想定外のエラー: item_id={item_id}, error={type(e).__name__}: {e}")
                result.fetch_error += 1
                row.update(status="error", error=f"{type(e).__name__}: {e}")
                if not dry_run:
                    with get_connection() as conn:
                        _insert_enrich_log(
                            conn, item_id, url, "error", None, None, None, None, row["error"]
                        )
                        conn.commit()

            result.rows.append(row)

    logger.info(f"締切抽出完了: {result.summary()}")
    return result


def _process_one(
    http_client: httpx.Client,
    anthropic_client: Any,
    target: dict[str, Any],
    result: EnrichResult,
    dry_run: bool,
) -> dict[str, Any]:
    """1件分の処理（文書取得 → LLM抽出 → 書き込み）。呼び出し元で例外を捕捉する。"""
    item_id = target["id"]
    url = target["url"]

    doc = fetch_and_extract_text(http_client, url)
    if not doc.ok or not doc.text:
        status = "skip" if doc.doc_kind == "unsupported" else "error"
        if status == "skip":
            result.fetch_skip += 1
        else:
            result.fetch_error += 1
        if not dry_run:
            with get_connection() as conn:
                _insert_enrich_log(conn, item_id, url, status, None, None, None, None, doc.error)
                conn.commit()
        return {"status": status, "error": doc.error}

    result.fetch_ok += 1

    extraction = extract_deadline_llm(
        anthropic_client, target["title"], target["organization_name"], doc.text
    )
    if extraction is None:
        result.llm_error += 1
        error_msg = "LLM抽出失敗（2回試行後）"
        if not dry_run:
            with get_connection() as conn:
                _insert_enrich_log(conn, item_id, url, "error", None, None, None, None, error_msg)
                conn.commit()
        return {"status": "error", "error": error_msg}

    if extraction.confidence == "high":
        result.llm_high += 1
    else:
        result.llm_low += 1

    extraction.deadline_date = normalize_deadline_date(extraction.deadline_date)
    plausible = is_plausible_deadline(extraction.deadline_date, target.get("published_at"))
    should_write = (
        not dry_run
        and extraction.confidence == "high"
        and extraction.deadline_date is not None
        and plausible
    )

    if not dry_run:
        with get_connection() as conn:
            if should_write:
                _update_item_deadline(conn, item_id, extraction.deadline_date)  # type: ignore[arg-type]
                result.updated += 1
            _insert_enrich_log(
                conn,
                item_id,
                url,
                "ok",
                extraction.deadline_date,
                extraction.deadline_kind,
                extraction.confidence,
                extraction.evidence,
                None if plausible else "妥当性チェック不合格（範囲外/公開日より前）",
            )
            conn.commit()

    return {
        "status": "ok",
        "deadline_date": extraction.deadline_date,
        "deadline_kind": extraction.deadline_kind,
        "confidence": extraction.confidence,
        "evidence": extraction.evidence,
        "plausible": plausible,
        "written": should_write,
    }
