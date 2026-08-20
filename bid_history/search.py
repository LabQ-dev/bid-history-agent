"""기간 내 개찰완료 공고를 스윕하고, 공고별 참가업체를 조회해
회사명/사업자등록번호/대표자명으로 필터링한다.

API가 업체 기준 검색을 제공하지 않으므로(공고번호·기간 기준만 제공),
  1) 개찰결과 목록(업무구분별, 개찰일시 범위)          → 공고 목록
  2) 공고별 개찰완료 참가업체 목록(OpengCompt)          → 순위·평가점수
  3) 로컬 필터(회사명 or 사업자번호, 대표자명)          → 결과
순서로 조회한다. 2)의 호출량이 크므로 SQLite 캐시를 사용한다.
"""

import json
import re
import sqlite3
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .api import NaraClient, OPENG_LIST_OPS, NaraApiError

logger = logging.getLogger(__name__)

_CORP_SUFFIX = re.compile(r"주식회사|\(주\)|\(유\)|유한회사|유한책임회사|합자회사|합명회사|\s+")


def norm_name(s: str) -> str:
    """회사명 비교용 정규화: 법인 접두어·공백 제거."""
    return _CORP_SUFFIX.sub("", str(s or "")).lower()


def norm_bizno(s: str) -> str:
    return re.sub(r"[^0-9]", "", str(s or ""))


@dataclass
class Query:
    company: str = ""          # 회사명 (부분일치, 법인격 무시)
    bizno: str = ""            # 사업자등록번호 (숫자만 비교)
    ceo: str = ""              # 대표자명 (부분일치)
    bgn: str = ""              # YYYYMMDD
    end: str = ""              # YYYYMMDD
    biz_types: list = field(default_factory=lambda: list(OPENG_LIST_OPS))
    keyword: str = ""          # 공고명 필터(선택, 스윕량 축소용)

    def matches(self, row: dict) -> bool:
        """참가업체 row가 검색조건에 부합하는가.

        회사명/사업자번호는 하나만 맞아도 매칭(OR),
        대표자명은 주어진 경우 추가로 만족해야 함(AND).
        """
        ok_corp = True
        if self.company or self.bizno:
            ok_corp = False
            if self.bizno and norm_bizno(self.bizno) == norm_bizno(row.get("prcbdrBizno")):
                ok_corp = True
            if self.company and norm_name(self.company) in norm_name(row.get("prcbdrNm")):
                ok_corp = True
        ok_ceo = True
        if self.ceo:
            ok_ceo = self.ceo.replace(" ", "") in str(row.get("prcbdrCeoNm") or "").replace(" ", "")
        return ok_corp and ok_ceo


class DetailCache:
    """공고별 참가업체 조회 결과 캐시 (SQLite)."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS participants ("
            " key TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def get(self, key: str) -> Optional[list]:
        row = self.conn.execute(
            "SELECT data FROM participants WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, data: list):
        self.conn.execute(
            "INSERT OR REPLACE INTO participants VALUES (?,?)",
            (key, json.dumps(data, ensure_ascii=False)))
        self.conn.commit()


def sweep_bids(client: NaraClient, q: Query) -> Iterable[dict]:
    """기간 내 개찰완료 공고 목록(업무구분 포함)을 생성."""
    bgn, end = q.bgn + "0000", q.end + "2359"
    kw = q.keyword.replace(" ", "").lower()
    seen = set()
    for biz_type in q.biz_types:
        logger.info("[%s] 개찰결과 목록 조회 %s~%s", biz_type, q.bgn, q.end)
        for item in client.openg_result_list(biz_type, bgn, end):
            if str(item.get("progrsDivCdNm", "")) != "개찰완료":
                continue
            if kw and kw not in str(item.get("bidNtceNm", "")).replace(" ", "").lower():
                continue
            key = (item.get("bidNtceNo"), item.get("bidNtceOrd"),
                   item.get("bidClsfcNo"), item.get("rbidNo"))
            if key in seen:
                continue
            seen.add(key)
            item["_bizType"] = biz_type
            yield item


def search(client: NaraClient, q: Query, cache: DetailCache,
           max_detail_calls: Optional[int] = None,
           progress_every: int = 200) -> list[dict]:
    """검색 실행. 결과: 매칭된 참가 레코드 목록(공고정보 + 업체·순위·점수)."""
    bids = list(sweep_bids(client, q))
    logger.info("개찰완료 공고 %d건 → 공고별 참가업체 조회 시작", len(bids))

    results, detail_calls = [], 0
    for idx, bid in enumerate(bids, 1):
        ntce_no = str(bid.get("bidNtceNo", ""))
        key = "|".join(str(bid.get(k, "")) for k in
                       ("bidNtceNo", "bidNtceOrd", "bidClsfcNo", "rbidNo"))
        rows = cache.get(key)
        if rows is None:
            if max_detail_calls is not None and detail_calls >= max_detail_calls:
                logger.warning("상세조회 호출 한도(%d) 도달 — 이후 %d건 미조회. "
                               "같은 명령을 다시 실행하면 캐시 이후부터 이어서 조회합니다.",
                               max_detail_calls, len(bids) - idx + 1)
                break
            try:
                rows = client.openg_participants(
                    ntce_no,
                    str(bid.get("bidNtceOrd", "")),
                    str(bid.get("bidClsfcNo", "")),
                    str(bid.get("rbidNo", "")))
            except NaraApiError as e:
                if "트래픽 초과" in str(e):
                    logger.error("일일 트래픽 초과로 중단 (%d/%d건 처리). "
                                 "내일 재실행 시 캐시 이후부터 이어집니다.", idx - 1, len(bids))
                    break
                logger.warning("공고 %s 상세조회 실패: %s", ntce_no, e)
                rows = []
            detail_calls += 1
            cache.put(key, rows)

        for row in rows:
            if q.matches(row):
                results.append({**bid, **row})

        if idx % progress_every == 0:
            logger.info("진행 %d/%d (API 상세호출 %d, 매칭 %d)",
                        idx, len(bids), detail_calls, len(results))

    # 개찰일시 → 순위 순 정렬
    results.sort(key=lambda r: (str(r.get("opengDt", "")),
                                int(r.get("opengRank") or 999)))
    return results
