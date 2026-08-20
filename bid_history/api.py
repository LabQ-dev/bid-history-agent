"""조달청 나라장터 낙찰정보서비스(ScsbidInfoService) OpenAPI 클라이언트.

공공데이터포털: https://www.data.go.kr/data/15129397/openapi.do
Endpoint: http://apis.data.go.kr/1230000/as/ScsbidInfoService
"""

import time
import logging
from datetime import datetime, timedelta
from typing import Iterator, Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://apis.data.go.kr/1230000/as/ScsbidInfoService"

# 업무구분별 개찰결과 목록 오퍼레이션
OPENG_LIST_OPS = {
    "물품": "getOpengResultListInfoThng",
    "공사": "getOpengResultListInfoCnstwk",
    "용역": "getOpengResultListInfoServc",
    "외자": "getOpengResultListInfoFrgcpt",
}

# 개찰완료 건의 참가업체별 순위/평가점수 조회
OPENG_COMPT_OP = "getOpengResultListInfoOpengCompt"


class NaraApiError(Exception):
    pass


def date_chunks(bgn_dt: str, end_dt: str, days: int = 30):
    """YYYYMMDDHHMM 기간을 최대 `days`일 구간들로 분할."""
    bgn = datetime.strptime(bgn_dt, "%Y%m%d%H%M")
    end = datetime.strptime(end_dt, "%Y%m%d%H%M")
    cur = bgn
    while cur <= end:
        chunk_end = min(cur + timedelta(days=days) - timedelta(minutes=1), end)
        yield cur.strftime("%Y%m%d%H%M"), chunk_end.strftime("%Y%m%d%H%M")
        cur = chunk_end + timedelta(minutes=1)


class NaraClient:
    """페이징·재시도·호출간격을 처리하는 얇은 클라이언트."""

    def __init__(self, service_key: str, interval: float = 0.05,
                 timeout: int = 30, max_retries: int = 3):
        self.service_key = service_key
        self.interval = interval          # 초당 최대 30 TPS 제한 대응
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.call_count = 0

    def _get(self, operation: str, params: dict) -> dict:
        url = f"{BASE_URL}/{operation}"
        query = {"ServiceKey": self.service_key, "type": "json", **params}

        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            time.sleep(self.interval)
            self.call_count += 1
            try:
                res = self.session.get(url, params=query, timeout=self.timeout)

                # 게이트웨이 오류 (키 오류, 트래픽 초과 등) — HTTP 429로도 옴
                try:
                    data = res.json()
                except ValueError:
                    data = {}
                if "OpenAPI_ServiceResponse" in data:
                    header = data["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
                    code = header.get("returnReasonCode")
                    msg = header.get("errMsg") or header.get("returnAuthMsg")
                    if code == "22" or "LIMITED_NUMBER" in str(msg):
                        # 일일 한도 소진: 재시도 무의미 → 즉시 중단
                        raise NaraApiError(
                            "일일 트래픽 초과: 개발계정은 하루 1,000회까지만 호출할 수 있습니다. "
                            "내일 다시 실행하면 캐시된 부분 이후부터 이어서 조회합니다.")
                    raise NaraApiError(f"게이트웨이 오류({code}): {msg}")

                res.raise_for_status()

                # 나라장터 자체 오류 응답 (예: 조회기간 1개월 초과 시 resultCode 07)
                err = data.get("nkoneps.com.response.ResponseError")
                if err:
                    header = err.get("header", {})
                    raise NaraApiError(
                        f"{operation} 오류: {header.get('resultCode')} {header.get('resultMsg')}")

                header = data.get("response", {}).get("header", {})
                if header.get("resultCode") not in ("00", 0, "0"):
                    raise NaraApiError(
                        f"{operation} 오류: {header.get('resultCode')} {header.get('resultMsg')}")
                return data["response"].get("body", {}) or {}

            except (requests.RequestException, ValueError) as e:
                last_err = e
                wait = 2 ** attempt
                # 로그에 인증키가 노출되지 않도록 마스킹
                safe = str(e).replace(self.service_key, "***KEY***")
                logger.warning("%s 호출 실패(%d/%d): %s → %ds 후 재시도",
                               operation, attempt, self.max_retries, safe, wait)
                time.sleep(wait)
        raise NaraApiError(
            f"{operation} 호출 실패: {str(last_err).replace(self.service_key, '***KEY***')}")

    @staticmethod
    def _items(body: dict) -> list:
        items = body.get("items") or []
        if isinstance(items, dict):          # XML 변환 응답 호환
            items = items.get("item") or []
        if isinstance(items, dict):
            items = [items]
        return items

    def paged(self, operation: str, params: dict, num_of_rows: int = 999) -> Iterator[dict]:
        """모든 페이지를 순회하며 item을 반환."""
        page = 1
        while True:
            body = self._get(operation, {**params, "pageNo": page,
                                         "numOfRows": num_of_rows})
            items = self._items(body)
            yield from items
            total = int(body.get("totalCount") or 0)
            if page * num_of_rows >= total or not items:
                break
            page += 1

    # ── 오퍼레이션 래퍼 ──────────────────────────────

    def openg_result_list(self, biz_type: str, bgn_dt: str, end_dt: str,
                          inqry_div: str = "3") -> Iterator[dict]:
        """업무구분별 개찰결과 목록. 기본 조회구분 3(개찰일시), 일시형식 YYYYMMDDHHMM.

        API가 조회기간을 1개월 이내로 제한하므로(초과 시 '입력범위값 초과 에러'),
        긴 기간은 30일 단위로 나눠 순차 조회한다.
        """
        op = OPENG_LIST_OPS[biz_type]
        for chunk_bgn, chunk_end in date_chunks(bgn_dt, end_dt, days=30):
            logger.info("  기간 분할 조회: %s ~ %s", chunk_bgn[:8], chunk_end[:8])
            yield from self.paged(op, {
                "inqryDiv": inqry_div,
                "inqryBgnDt": chunk_bgn, "inqryEndDt": chunk_end,
            })

    def openg_participants(self, bid_ntce_no: str,
                           bid_ntce_ord: str = "", bid_clsfc_no: str = "",
                           rbid_no: str = "") -> list[dict]:
        """개찰완료 건의 참가업체별 개찰순위·평가점수 목록."""
        params = {"bidNtceNo": bid_ntce_no}
        if bid_ntce_ord:
            params["bidNtceOrd"] = bid_ntce_ord
        if bid_clsfc_no:
            params["bidClsfcNo"] = bid_clsfc_no
        if rbid_no:
            params["rbidNo"] = rbid_no
        return list(self.paged(OPENG_COMPT_OP, params))
