"""deadline_extractor の純粋関数（文書判定・テキスト抽出・妥当性チェック）に関する回帰テスト

LLM/DB/ネットワークに依存しない範囲のみをカバーする（task-20260718-009）。
"""

from bid_aggregator.enrich.deadline_extractor import (
    DeadlineExtraction,
    classify_content,
    extract_html_text,
    is_fetchable_url,
    is_plausible_deadline,
)

# =============================================================================
# is_fetchable_url
# =============================================================================


def test_is_fetchable_url_accepts_https() -> None:
    assert is_fetchable_url("https://example.go.jp/doc.pdf") is True


def test_is_fetchable_url_rejects_javascript_handler() -> None:
    # p-portal の detail_url に稀に混入する JavaScript ハンドラ文字列（pportal_client参照）
    url = "javascript:doSubmitParams(this, [{name:'procurementItemInfoId', value:'630833'}])"
    assert is_fetchable_url(url) is False


# =============================================================================
# classify_content
# =============================================================================


def test_classify_content_detects_pdf_by_magic_number() -> None:
    content = b"%PDF-1.4\n..."
    assert classify_content("https://example.go.jp/doc", "application/octet-stream", content) == "pdf"


def test_classify_content_detects_pdf_by_url_suffix() -> None:
    content = b"not really pdf bytes"
    assert classify_content("https://example.go.jp/doc.pdf?x=1", "", content) == "pdf"


def test_classify_content_detects_html_by_content_type() -> None:
    content = b"<html><body>test</body></html>"
    assert classify_content("https://example.go.jp/page", "text/html; charset=utf-8", content) == "html"


def test_classify_content_detects_plain_text_as_html_path() -> None:
    content = b"plain text body"
    assert classify_content("https://example.go.jp/page", "text/plain", content) == "html"


def test_classify_content_returns_unsupported_for_unknown_binary() -> None:
    content = b"\x89PNG\r\n\x1a\n"
    assert classify_content("https://example.go.jp/image", "image/png", content) == "unsupported"


# =============================================================================
# extract_html_text
# =============================================================================


def test_extract_html_text_strips_tags_and_scripts() -> None:
    html = (
        b"<html><head><style>.a{}</style><script>alert(1)</script></head>"
        b"<body><h1>\xe5\x85\xa5\xe6\x9c\xad\xe5\x85\xac\xe5\x91\x8a</h1>"
        b"<p>\xe7\xb7\xa0\xe5\x88\x87\xe6\x97\xa5: 2026\xe5\xb9\xb42\xe6\x9c\x881\xe6\x97\xa5</p></body></html>"
    )
    text = extract_html_text(html)
    assert "入札公告" in text
    assert "締切日" in text
    assert "alert" not in text
    assert "{}" not in text


def test_extract_html_text_truncates_long_document() -> None:
    html = ("<html><body>" + "あ" * 50_000 + "</body></html>").encode("utf-8")
    text = extract_html_text(html)
    assert len(text) <= 30_000


# =============================================================================
# is_plausible_deadline
# =============================================================================


def test_is_plausible_deadline_accepts_valid_future_date() -> None:
    assert is_plausible_deadline("2026-08-01", "2026-07-01T00:00:00+09:00") is True


def test_is_plausible_deadline_rejects_none() -> None:
    assert is_plausible_deadline(None, "2026-07-01") is False


def test_is_plausible_deadline_rejects_out_of_range_year() -> None:
    assert is_plausible_deadline("2045-01-01", None) is False


def test_is_plausible_deadline_rejects_before_published_date() -> None:
    assert is_plausible_deadline("2026-01-01", "2026-07-01T00:00:00+09:00") is False


def test_is_plausible_deadline_accepts_when_published_at_missing() -> None:
    assert is_plausible_deadline("2026-08-01", None) is True


# =============================================================================
# DeadlineExtraction スキーマ
# =============================================================================


def test_deadline_extraction_accepts_null_date_with_unknown_kind() -> None:
    extraction = DeadlineExtraction.model_validate(
        {
            "deadline_date": None,
            "deadline_kind": "不明",
            "confidence": "low",
            "evidence": "",
        }
    )
    assert extraction.deadline_date is None
    assert extraction.confidence == "low"
