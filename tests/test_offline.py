"""API 없이 도는 오프라인 테스트 — CI에서 매 PR마다 실행.

검색 매칭·캐시·한도 처리·1위 비교 계산이 깨지지 않았는지 확인한다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bid_history.search import (  # noqa: E402
    Query, DetailCache, search, norm_name, norm_bizno, _days, _chunks)
from bid_history import report  # noqa: E402


class FakeClient:
    """조달청 API 흉내 — 공고 4건, 각 2개사 참가."""
    call_count = 0

    def openg_result_list(self, biz_type, bgn, end, inqry_div="3"):
        if biz_type != "용역":
            return
        for i in range(4):
            yield {"bidNtceNo": f"B{i}", "bidNtceOrd": "0", "bidClsfcNo": "0",
                   "rbidNo": "0", "bidNtceNm": f"테스트 공고 {i}",
                   "opengDt": f"2026-08-2{i} 11:00:00", "prtcptCnum": "2",
                   "dminsttNm": "테스트기관", "progrsDivCdNm": "개찰완료"}

    def openg_participants(self, no, *a):
        self.call_count += 1
        return [
            {"opengRank": "1", "prcbdrNm": "주식회사 랩큐", "prcbdrBizno": "1234567890",
             "prcbdrCeoNm": "김랩큐", "bidprcAmt": "100000000", "bidprcrt": "95",
             "totalEvlAmtVal": "98", "rmrk": "정상"},
            {"opengRank": "2", "prcbdrNm": "(주)경쟁사", "prcbdrBizno": "9876543210",
             "prcbdrCeoNm": "이경쟁", "bidprcAmt": "90000000", "bidprcrt": "90",
             "totalEvlAmtVal": "91", "rmrk": "정상"},
        ]


def test_norms():
    assert norm_name("주식회사 랩 큐") == norm_name("(주)랩큐") == "랩큐"
    assert norm_bizno("123-45-67890") == "1234567890"


def test_date_chunks():
    days = _days("20260101", "20260103")
    assert days == ["20260101", "20260102", "20260103"]
    assert _chunks(["20260101", "20260102", "20260105"]) == [
        ("20260101", "20260102"), ("20260105", "20260105")]


def test_search_flow(tmp_path):
    cache = DetailCache(tmp_path / "t.db")
    fc = FakeClient()
    q = Query(bizno="123-45-67890", bgn="20260820", end="20260823",
              biz_types=["용역"])
    res = search(fc, q, cache)
    assert len(res) == 4 and fc.call_count == 4
    # 재검색: 전부 캐시에서 (API 0회 추가)
    res2 = search(fc, q, cache, max_detail_calls=0)
    assert len(res2) == 4 and fc.call_count == 4
    # 1위 비교 필드
    e = report.enrich(res[0])
    assert e["_winnerNm"] == "주식회사 랩큐" and e["_amtDiff"] == "-"
    # 회사명 검색 (사업자번호 없이)
    q2 = Query(company="경쟁사", bgn="20260820", end="20260823", biz_types=["용역"])
    res3 = search(fc, q2, cache, max_detail_calls=0)
    assert len(res3) == 4 and all(r["opengRank"] == "2" for r in res3)
    # 표/엑셀 변환이 깨지지 않는지
    rows = report.to_rows(res)
    assert rows[0][0] == "개찰일시" and len(rows) == 5


def test_limit_then_continue(tmp_path):
    cache = DetailCache(tmp_path / "t2.db")
    fc = FakeClient()
    q = Query(company="랩큐", bgn="20260820", end="20260823", biz_types=["용역"])
    r1 = search(fc, q, cache, max_detail_calls=2)
    assert fc.call_count == 2 and len(r1) == 2
    r2 = search(fc, q, cache, max_detail_calls=2)
    assert fc.call_count == 4 and len(r2) == 4
