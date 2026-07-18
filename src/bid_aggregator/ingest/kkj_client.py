"""
KKJ APIクライアント

官公需情報ポータルサイトのAPIからデータを取得する。
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from bid_aggregator.core.config import settings
from bid_aggregator.core.models import KKJAPIResponse, KKJAttachment, KKJSearchResult, QueryParams

logger = logging.getLogger(__name__)

# KKJ APIレスポンスに稀に混入する不正な UTF-8（サロゲートペアの誤直列化。
# 例: \xed\xa0\xb5\xed\xb2\x8f のような CESU-8 風のバイト列）を検出・修復するための正規表現。
# 標準の "utf-8" strict decode ではこのバイト列は invalid continuation byte として
# UnicodeDecodeError になる。"surrogatepass" で寛容にデコードすると、対応する
# サロゲートペア（\ud800-\udbff の上位 + \udc00-\udfff の下位）が復元できるので、
# 正しいペアは実際の文字（結合済みの Unicode コードポイント）へ復元し、
# 対応が取れない孤立サロゲートは置換文字 U+FFFD に変換して再エンコードする。
_SURROGATE_PAIR_RE = re.compile("[\ud800-\udbff][\udc00-\udfff]")
_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _combine_surrogate_pair(match: re.Match) -> str:
    """サロゲートペア（高位+低位）を実際の1文字（結合済みコードポイント）へ変換する"""
    hi = ord(match.group(0)[0])
    lo = ord(match.group(0)[1])
    code_point = 0x10000 + (hi - 0xD800) * 0x400 + (lo - 0xDC00)
    return chr(code_point)


def sanitize_kkj_xml_bytes(content: bytes) -> bytes:
    """
    KKJ APIレスポンスのバイト列から、不正な UTF-8 サロゲートペア誤直列化を除去・修復する。

    KKJ 公式APIはHTTP 200で返すが、一部レコードのタイトル等に
    誤って CESU-8 的にエンコードされたサロゲートペア（本来 UTF-8 では現れてはいけない
    バイト列）が混入することがあり、標準の xml.etree.ElementTree（内部で strict utf-8
    decode を行う）がこれを ParseError として全体を落としてしまう既知の不具合がある
    （kn-20260711-013）。

    ここでは lxml 等の追加依存を増やさず、標準ライブラリのみで対処する:
      1. まず strict utf-8 でデコードを試み、成功すればそのまま返す（無傷ならノーコスト）
      2. 失敗した場合のみ "surrogatepass" で寛容にデコードし、
         正しく対になっているサロゲートペアは実際の文字へ復元し、
         孤立したサロゲートは U+FFFD に置換したうえで utf-8 に再エンコードする
    """
    try:
        content.decode("utf-8")
        return content
    except UnicodeDecodeError:
        logger.warning("KKJ APIレスポンスに不正なUTF-8バイト列を検出。サニタイズを実行します")

    text = content.decode("utf-8", errors="surrogatepass")
    text = _SURROGATE_PAIR_RE.sub(_combine_surrogate_pair, text)
    text = _LONE_SURROGATE_RE.sub("�", text)
    return text.encode("utf-8")


class KKJAPIError(Exception):
    """KKJ API エラー"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class KKJClient:
    """KKJ APIクライアント"""

    def __init__(
        self,
        base_url: str = settings.kkj_api_url,
        timeout: float = settings.kkj_request_timeout,
        request_interval: float = settings.kkj_request_interval,
    ):
        self.base_url = base_url
        self.timeout = timeout
        self.request_interval = request_interval
        self._last_request_time: float = 0
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": "BidAggregator/1.0 (https://github.com/yourname/bid-aggregator)",
            },
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._client.close()

    def _wait_for_rate_limit(self) -> None:
        """レート制限のための待機"""
        if self._last_request_time > 0:
            elapsed = time.time() - self._last_request_time
            if elapsed < self.request_interval:
                time.sleep(self.request_interval - elapsed)

    def _build_params(self, params: QueryParams) -> dict[str, str]:
        """APIパラメータを構築"""
        result = {}
        
        # 必須パラメータ（いずれか1つ以上）
        if params.Query:
            result["Query"] = params.Query
        if params.Project_Name:
            result["Project_Name"] = params.Project_Name
        if params.Organization_Name:
            result["Organization_Name"] = params.Organization_Name
        if params.LG_Code:
            result["LG_Code"] = params.LG_Code
        
        # 必須パラメータが1つもない場合はエラー
        if not any([params.Query, params.Project_Name, params.Organization_Name, params.LG_Code]):
            raise KKJAPIError(
                "Query, Project_Name, Organization_Name, LG_Code のいずれか1つ以上が必要です"
            )
        
        # 任意パラメータ
        result["Count"] = str(min(params.Count, 1000))
        
        if params.Category:
            result["Category"] = str(params.Category)
        if params.Procedure_Type:
            result["Procedure_Type"] = str(params.Procedure_Type)
        if params.Certification:
            result["Certification"] = params.Certification
        if params.CFT_Issue_Date:
            result["CFT_Issue_Date"] = params.CFT_Issue_Date
        if params.Tender_Submission_Deadline:
            result["Tender_Submission_Deadline"] = params.Tender_Submission_Deadline
        if params.Opening_Tenders_Event:
            result["Opening_Tenders_Event"] = params.Opening_Tenders_Event
        if params.Period_End_Time:
            result["Period_End_Time"] = params.Period_End_Time
        
        return result

    def _parse_xml_response(self, xml_content: bytes) -> KKJAPIResponse:
        """XMLレスポンスをパース"""
        xml_content = sanitize_kkj_xml_bytes(xml_content)

        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            raise KKJAPIError(f"XMLパースエラー: {e}") from e

        # エラーチェック
        error_elem = root.find("e")
        if error_elem is not None:
            raise KKJAPIError(f"API エラー: {error_elem.text}")

        # バージョン
        version_elem = root.find("Version")
        version = version_elem.text if version_elem is not None else "unknown"

        # 検索結果
        search_results = root.find("SearchResults")
        if search_results is None:
            return KKJAPIResponse(version=version, search_hits=0, results=[])

        # ヒット件数
        search_hits_elem = search_results.find("SearchHits")
        search_hits = int(search_hits_elem.text) if search_hits_elem is not None else 0

        # 各検索結果をパース（個別レコードが壊れていても全体を落とさずスキップする）
        results = []
        for sr in search_results.findall("SearchResult"):
            try:
                result = self._parse_search_result(sr)
            except Exception as e:
                key_elem = sr.find("Key")
                key = key_elem.text if key_elem is not None else "unknown"
                logger.warning(f"SearchResultのパースに失敗、スキップします: key={key}, error={e}")
                continue
            results.append(result)

        return KKJAPIResponse(version=version, search_hits=search_hits, results=results)

    def _parse_search_result(self, elem: ET.Element) -> KKJSearchResult:
        """SearchResult要素をパース"""
        def get_text(tag: str) -> str | None:
            child = elem.find(tag)
            return child.text if child is not None else None

        def get_int(tag: str) -> int | None:
            text = get_text(tag)
            return int(text) if text else None

        # 添付ファイル
        attachments = []
        attachments_elem = elem.find("Attachments")
        if attachments_elem is not None:
            for att in attachments_elem.findall("Attachment"):
                name_elem = att.find("Name")
                uri_elem = att.find("Uri")
                attachments.append(KKJAttachment(
                    name=name_elem.text if name_elem is not None else None,
                    uri=uri_elem.text if uri_elem is not None else None,
                ))

        return KKJSearchResult(
            result_id=get_int("ResultId") or 0,
            key=get_text("Key") or "",
            external_document_uri=get_text("ExternalDocumentURI"),
            project_name=get_text("ProjectName") or "",
            date=get_text("Date"),
            file_type=get_text("FileType"),
            file_size=get_int("FileSize"),
            lg_code=get_text("LgCode"),
            prefecture_name=get_text("PrefectureName"),
            city_code=get_text("CityCode"),
            city_name=get_text("CityName"),
            organization_name=get_text("OrganizationName"),
            certification=get_text("Certification"),
            cft_issue_date=get_text("CftIssueDate"),
            period_end_time=get_text("PeriodEndTime"),
            category=get_text("Category"),
            procedure_type=get_text("ProcedureType"),
            location=get_text("Location"),
            tender_submission_deadline=get_text("TenderSubmissionDeadline"),
            opening_tenders_event=get_text("OpeningTendersEvent"),
            item_code=get_text("ItemCode"),
            project_description=get_text("ProjectDescription"),
            attachments=attachments,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    def _fetch(self, params: dict[str, str]) -> tuple[bytes, int, str]:
        """
        APIリクエストを実行（リトライ付き）
        
        Returns:
            (response_body, status_code, content_type)
        """
        self._wait_for_rate_limit()
        
        logger.debug(f"KKJ API request: {params}")
        
        response = self._client.get(self.base_url, params=params)
        self._last_request_time = time.time()
        
        logger.debug(f"KKJ API response: status={response.status_code}")
        
        # 4xx/5xx エラー
        if response.status_code >= 400:
            raise KKJAPIError(
                f"HTTP {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
            )
        
        content_type = response.headers.get("content-type", "application/xml")
        return response.content, response.status_code, content_type

    def search(self, params: QueryParams) -> tuple[KKJAPIResponse, bytes, int, str]:
        """
        検索を実行
        
        Returns:
            (parsed_response, raw_body, status_code, content_type)
        """
        api_params = self._build_params(params)
        raw_body, status_code, content_type = self._fetch(api_params)
        parsed = self._parse_xml_response(raw_body)
        return parsed, raw_body, status_code, content_type

    def search_with_date_range(
        self,
        params: QueryParams,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> tuple[KKJAPIResponse, bytes, int, str]:
        """
        日付範囲を指定して検索
        """
        # CFT_Issue_Date パラメータを構築
        if from_date and to_date:
            params.CFT_Issue_Date = f"{from_date}/{to_date}"
        elif from_date:
            params.CFT_Issue_Date = f"{from_date}/"
        elif to_date:
            params.CFT_Issue_Date = f"/{to_date}"
        
        return self.search(params)
