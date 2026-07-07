from bs4 import BeautifulSoup

from bid_aggregator.ingest.pportal_client import PPortalClient
from bid_aggregator.ingest.normalizer import normalize_pportal_result


def test_build_form_data_uses_current_pportal_field_names() -> None:
    client = PPortalClient()

    form_data = client._build_form_data(
        keyword="生成AI",
        procurement_types=None,
        organization_codes=None,
        publish_start_from="2026-05-22",
        publish_start_to="2026-05-24",
        classification="",
    )

    assert ("searchConditionBean.caseDivision", "0") in form_data
    assert ("searchConditionBean.articleNm", "生成AI") in form_data
    assert ("searchConditionBean.synonymClassification", "01") in form_data
    assert ("searchConditionBean.publicStartDateFrom", "2026/05/22") in form_data
    assert ("searchConditionBean.publicStartDateTo", "2026/05/24") in form_data
    assert all(name != "searchConditionBean.ankenMeisho" for name, _ in form_data)


def test_parse_search_results_reads_count_from_text_nodes() -> None:
    client = PPortalClient()
    html = """
    <html>
      <body>
        <div><span>1</span><span>件見つかりました。</span></div>
        <table class="search-result">
          <tbody>
            <tr>
              <td>0000000000000601419</td>
              <td>令和8年度生成AI利活用のための導入支援</td>
              <td>環境省</td>
              <td>東京都</td>
              <td></td>
              <td></td>
              <td>
                公示本文 令和8年5月22日 公開開始 入札
                <a href="javascript:doSubmitParams(
                    this,
                    [{name:'procurementItemInfoId', value:'621806'}]
                )">公示本文</a>
              </td>
            </tr>
          </tbody>
        </table>
      </body>
    </html>
    """

    results, total = client._parse_search_results(html)

    assert total == 1
    assert len(results) == 1
    assert results[0].case_number == "0000000000000601419"
    assert results[0].publish_start == "2026-05-22"
    assert results[0].category == "入札公告"


def test_normalize_pportal_result_uses_detail_id_when_case_number_is_placeholder() -> None:
    client = PPortalClient()
    html = """
    <table>
      <tbody>
        <tr>
          <td>‐</td>
          <td>採用活動に効果的なホームページの改修</td>
          <td>国土交通省</td>
          <td>東京都</td>
          <td></td>
          <td></td>
          <td>
            令和8年5月22日 公開開始
            <a href="javascript:doSubmitParams(
                this,
                [{name:'procurementItemInfoId', value:'622415'}]
            )">公示本文</a>
          </td>
        </tr>
      </tbody>
    </table>
    """

    result = client._parse_row(BeautifulSoup(html, "html.parser").select_one("tr"))
    item = normalize_pportal_result(result)

    assert item.source_item_id == "622415"
