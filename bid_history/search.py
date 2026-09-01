"""나라장터 개찰결과 저장·검색.

API가 업체 기준 검색을 제공하지 않으므로(공고번호·기간 기준만 제공),
개찰결과를 정규화된 SQLite DB에 쌓고 검색은 DB에서 SQL로 수행한다.

  bids          공고 메타데이터 (목록 API ①) — 공고명·개찰일시·수요기관 등
  participants  참가업체 1명 = 1행 (개찰완료 API ②) — 순위·투찰금액·평가점수
  fetched       참가업체 명단을 이미 받아온 공고 key (0명 공고 구분용)
  swept         업무구분×일자 단위로 공고 목록 스윕을 마친 기록

검색 흐름:
  1) 요청 기간 중 스윕 안 된 (업무구분, 일자) 구간만 목록 API로 스윕
  2) 기간 내 명단 미수집 공고만 개찰완료 API로 수집 (호출 한도 보호)
  3) SQL 조인으로 회사명/사업자번호/대표자명 매칭 → 즉시 반환
"""

import json
import re
import sqlite3
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .api import NaraClient, OPENG_LIST_OPS, NaraApiError

logger = logging.getLogger(__name__)

_CORP_SUFFIX = re.compile(r"주식회사|\(주\)|\(유\)|유한회사|유한책임회사|합자회사|합명회사|\s+")

# participants 테이블 컬럼 (API ② 응답 필드명 그대로 유지 — 재조립 시 그대로 dict가 됨)
P_FIELDS = ["opengRank", "prcbdrBizno", "prcbdrNm", "prcbdrCeoNm",
            "bidprcAmt", "bidprcrt", "bidPrceEvlVal", "techEvlVal",
            "totalEvlAmtVal", "rmrk", "bidprcDt", "drwtNo1", "drwtNo2"]
B_FIELDS = ["bidNtceNo", "_bizType", "bidNtceNm", "opengDt", "dminsttNm",
            "ntceInsttNm", "prtcptCnum", "progrsDivCdNm"]


def norm_name(s: str) -> str:
    """회사명 비교용 정규화: 법인 접두어·공백 제거."""
    return _CORP_SUFFIX.sub("", str(s or "")).lower()


def norm_bizno(s: str) -> str:
    return re.sub(r"[^0-9]", "", str(s or ""))


def _days(bgn: str, end: str) -> list:
    """YYYYMMDD 범위의 날짜 목록."""
    d0 = datetime.strptime(bgn, "%Y%m%d")
    d1 = datetime.strptime(end, "%Y%m%d")
    return [(d0 + timedelta(days=i)).strftime("%Y%m%d")
            for i in range((d1 - d0).days + 1)]


def _chunks(days: list) -> list:
    """정렬된 날짜 목록을 연속 구간 [(bgn, end), ...] 으로 묶는다."""
    out = []
    for d in sorted(days):
        prev = out[-1][1] if out else None
        if prev and (datetime.strptime(d, "%Y%m%d")
                     - datetime.strptime(prev, "%Y%m%d")).days == 1:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [(a, b) for a, b in out]


@dataclass
class Query:
    company: str = ""          # 회사명 (부분일치, 법인격 무시)
    bizno: str = ""            # 사업자등록번호 (숫자만 비교)
    ceo: str = ""              # 대표자명 (부분일치)
    bgn: str = ""              # YYYYMMDD
    end: str = ""              # YYYYMMDD
    biz_types: list = field(default_factory=lambda: list(OPENG_LIST_OPS))
    keyword: str = ""          # 공고명 필터(선택)

    def matches(self, row: dict) -> bool:
        """참가업체 row가 검색조건에 부합하는가 (공고번호 직접 조회 등 API 경로용).

        회사명/사업자번호는 하나만 맞아도 매칭(OR),
        대표자명은 주어진 경우 추가로 만족해야 함(AND).
        """
        ok_corp = True
        if self.bizno:
            ok_corp = norm_bizno(self.bizno) == norm_bizno(row.get("prcbdrBizno"))
        elif self.company:
            ok_corp = norm_name(self.company) in norm_name(row.get("prcbdrNm"))
        ok_ceo = True
        if self.ceo:
            ok_ceo = self.ceo.replace(" ", "") in str(row.get("prcbdrCeoNm") or "").replace(" ", "")
        return ok_corp and ok_ceo


class DetailCache:
    """개찰결과 정규화 저장소 (SQLite). key = 공고번호|차수|분류번호|재입찰번호"""

    def __init__(self, path: Path):
        # timeout: 잠금 시 30초 대기 / WAL: 조회(DB Browser 등)와 쓰기 동시 허용
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        c = self.conn
        c.execute("CREATE TABLE IF NOT EXISTS bids ("
                  " key TEXT PRIMARY KEY, bidNtceNo TEXT NOT NULL, bizType TEXT,"
                  " bidNtceNm TEXT, opengDt TEXT, dminsttNm TEXT, ntceInsttNm TEXT,"
                  " prtcptCnum TEXT, progrsDivCdNm TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bids_opengDt ON bids(opengDt)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bids_no ON bids(bidNtceNo)")
        c.execute("CREATE TABLE IF NOT EXISTS fetched (key TEXT PRIMARY KEY)")
        c.execute("CREATE TABLE IF NOT EXISTS swept ("
                  " bizType TEXT, day TEXT, PRIMARY KEY (bizType, day))")
        self._migrate_json_table()
        c.execute("CREATE TABLE IF NOT EXISTS participants ("
                  " key TEXT NOT NULL,"
                  + ",".join(f" {f} TEXT" for f in P_FIELDS) + ","
                  " normNm TEXT, normBizno TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_p_key ON participants(key)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_p_normNm ON participants(normNm)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_p_bizno ON participants(normBizno)")
        c.commit()

    # ── 구버전(JSON 1테이블) 마이그레이션 ─────────────

    def _migrate_json_table(self):
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(participants)")]
        if "data" not in cols:
            return  # 이미 신 스키마
        logger.info("구버전 스키마 감지 — participants(JSON)를 행 단위로 마이그레이션")
        self.conn.execute("ALTER TABLE participants RENAME TO participants_json")
        self.conn.execute("CREATE TABLE participants ("
                          " key TEXT NOT NULL,"
                          + ",".join(f" {f} TEXT" for f in P_FIELDS) + ","
                          " normNm TEXT, normBizno TEXT)")
        n_bids, n_rows = 0, 0
        for key, data in self.conn.execute(
                "SELECT key, data FROM participants_json"):
            self.conn.execute("INSERT OR IGNORE INTO fetched VALUES (?)", (key,))
            for r in json.loads(data):
                self._insert_participant(key, r)
                n_rows += 1
            n_bids += 1
        self.conn.execute("DROP TABLE participants_json")
        self.conn.commit()
        logger.info("마이그레이션 완료 — 공고 %d건, 참가업체 %d행", n_bids, n_rows)

    def _insert_participant(self, key: str, r: dict):
        self.conn.execute(
            f"INSERT INTO participants VALUES (?{',?' * (len(P_FIELDS) + 2)})",
            [key] + [str(r.get(f, "") or "") for f in P_FIELDS]
            + [norm_name(r.get("prcbdrNm")), norm_bizno(r.get("prcbdrBizno"))])

    # ── 적재 ─────────────────────────────────────────

    def put_bid(self, key: str, bid: dict, commit: bool = True):
        self.conn.execute(
            "INSERT OR REPLACE INTO bids VALUES (?,?,?,?,?,?,?,?,?)",
            (key, str(bid.get("bidNtceNo", "")), bid.get("_bizType", ""),
             bid.get("bidNtceNm", ""), bid.get("opengDt", ""),
             bid.get("dminsttNm", ""), bid.get("ntceInsttNm", ""),
             str(bid.get("prtcptCnum", "")), bid.get("progrsDivCdNm", "")))
        if commit:
            self.conn.commit()

    def put(self, key: str, rows: list):
        """공고 하나의 참가업체 명단 저장 (재수집 시 교체)."""
        self.conn.execute("DELETE FROM participants WHERE key=?", (key,))
        for r in rows:
            self._insert_participant(key, r)
        self.conn.execute("INSERT OR REPLACE INTO fetched VALUES (?)", (key,))
        self.conn.commit()

    def mark_swept(self, biz_type: str, days: Iterable[str]):
        self.conn.executemany(
            "INSERT OR IGNORE INTO swept VALUES (?,?)",
            [(biz_type, d) for d in days])
        self.conn.commit()

    # ── 조회 ─────────────────────────────────────────

    def get(self, key: str) -> Optional[list]:
        """공고 key의 참가업체 명단. 미수집이면 None (수집됐지만 0명이면 [])."""
        if not self.conn.execute(
                "SELECT 1 FROM fetched WHERE key=?", (key,)).fetchone():
            return None
        return self.get_rows(key)

    def get_rows(self, key_or_no: str) -> list:
        """key 또는 공고번호로 참가업체 행 조회 (dict 재조립)."""
        like = key_or_no if "|" in key_or_no else key_or_no + "|%"
        cur = self.conn.execute(
            f"SELECT {','.join(P_FIELDS)} FROM participants WHERE key LIKE ?",
            (like,))
        return [dict(zip(P_FIELDS, row)) for row in cur.fetchall()]

    def missing_days(self, q: Query) -> dict:
        """업무구분별로 아직 스윕 안 된 날짜 목록."""
        want = _days(q.bgn, q.end)
        out = {}
        for biz in q.biz_types:
            have = {r[0] for r in self.conn.execute(
                "SELECT day FROM swept WHERE bizType=?", (biz,))}
            miss = [d for d in want if d not in have]
            if miss:
                out[biz] = miss
        return out

    def _range_where(self, q: Query):
        d0 = f"{q.bgn[:4]}-{q.bgn[4:6]}-{q.bgn[6:]} 00:00:00"
        d1 = f"{q.end[:4]}-{q.end[4:6]}-{q.end[6:]} 23:59:59"
        where = ["b.opengDt >= ?", "b.opengDt <= ?",
                 f"b.bizType IN ({','.join('?' * len(q.biz_types))})"]
        params = [d0, d1, *q.biz_types]
        if q.keyword:
            where.append("REPLACE(b.bidNtceNm, ' ', '') LIKE ?")
            params.append(f"%{q.keyword.replace(' ', '')}%")
        return " AND ".join(where), params

    def unfetched_bids(self, q: Query) -> list:
        """기간·조건 내에서 참가업체 명단이 아직 없는 공고 목록."""
        where, params = self._range_where(q)
        cur = self.conn.execute(
            "SELECT b.key, b.bidNtceNo, b.bizType, b.bidNtceNm, b.opengDt,"
            "       b.dminsttNm, b.ntceInsttNm, b.prtcptCnum, b.progrsDivCdNm"
            " FROM bids b LEFT JOIN fetched f ON f.key = b.key"
            f" WHERE f.key IS NULL AND {where}", params)
        return [dict(zip(["key"] + B_FIELDS, row)) for row in cur.fetchall()]

    def db_search(self, q: Query) -> list:
        """정규화 테이블에서 SQL로 참가 이력 검색 (즉시)."""
        where, params = self._range_where(q)
        # 사업자번호가 있으면 그것만으로 매칭 (이름이 비슷한 타사 혼입 방지),
        # 없을 때만 회사명 부분일치
        if q.bizno:
            where += " AND p.normBizno = ?"
            params.append(norm_bizno(q.bizno))
        elif q.company:
            where += " AND p.normNm LIKE ?"
            params.append(f"%{norm_name(q.company)}%")
        if q.ceo:
            where += " AND REPLACE(p.prcbdrCeoNm, ' ', '') LIKE ?"
            params.append(f"%{q.ceo.replace(' ', '')}%")

        cur = self.conn.execute(
            f"SELECT p.key, {','.join('p.' + f for f in P_FIELDS)},"
            f"       {','.join('b.' + f.lstrip('_') if f != '_bizType' else 'b.bizType' for f in B_FIELDS)}"
            " FROM participants p JOIN bids b ON b.key = p.key"
            f" WHERE {where}", params)
        results = []
        for row in cur.fetchall():
            d = dict(zip(["key"] + P_FIELDS + B_FIELDS, row))
            results.append(d)
        # 공고별 1위(낙찰자) 붙이기
        for r in results:
            w = self.conn.execute(
                f"SELECT {','.join(P_FIELDS)} FROM participants"
                " WHERE key=? AND opengRank='1' LIMIT 1", (r["key"],)).fetchone()
            r["_winner"] = dict(zip(P_FIELDS, w)) if w else None
        return results

    def coverage(self) -> list:
        """일자별 (공고수, 명단 보유수) — 수집 범위 확인용."""
        return self.conn.execute(
            "SELECT substr(b.opengDt,1,10) d, COUNT(*),"
            "       SUM(CASE WHEN f.key IS NOT NULL THEN 1 ELSE 0 END)"
            " FROM bids b LEFT JOIN fetched f ON f.key = b.key"
            " GROUP BY d ORDER BY d").fetchall()


def sweep_bids(client: NaraClient, q: Query,
               cache: Optional[DetailCache] = None) -> Iterable[dict]:
    """기간 내 개찰완료 공고 목록을 생성하고 bids 테이블에 적재.

    키워드 필터는 여기서 적용하지 않는다(테이블이 편향되지 않도록) —
    키워드는 이후 SQL 단계에서 거른다.
    """
    bgn, end = q.bgn + "0000", q.end + "2359"
    seen = set()
    for biz_type in q.biz_types:
        logger.info("[%s] 개찰결과 목록 조회 %s~%s", biz_type, q.bgn, q.end)
        for item in client.openg_result_list(biz_type, bgn, end):
            if str(item.get("progrsDivCdNm", "")) != "개찰완료":
                continue
            key = "|".join(str(item.get(k, "")) for k in
                           ("bidNtceNo", "bidNtceOrd", "bidClsfcNo", "rbidNo"))
            if key in seen:
                continue
            seen.add(key)
            item["_bizType"] = biz_type
            if cache is not None:
                cache.put_bid(key, item, commit=False)
            yield item
        if cache is not None:
            # 오늘(및 미래)은 개찰이 계속 추가되므로 '스윕 완료'로 기록하지 않음
            today = datetime.now().strftime("%Y%m%d")
            cache.mark_swept(biz_type, [d for d in _days(q.bgn, q.end) if d < today])
            cache.conn.commit()


def search(client: NaraClient, q: Query, cache: DetailCache,
           max_detail_calls: Optional[int] = None,
           progress_every: int = 200,
           should_stop=None) -> list[dict]:
    """검색 실행: 부족한 부분만 API로 채우고, 매칭은 DB에서 SQL로.

    should_stop: 호출 시 True를 반환하면 API 수집을 멈추고
    그때까지 DB에 있는 범위로 결과를 반환한다.
    결과: 매칭된 참가 레코드 목록 (공고정보 + 업체·순위·점수 + _winner)
    """
    stopped = False

    def _stop() -> bool:
        nonlocal stopped
        if should_stop and should_stop():
            stopped = True
        return stopped

    # 1) 스윕 안 된 (업무구분, 일자) 구간만 목록 API로 채움
    missing = cache.missing_days(q)
    if missing:
        for biz, days in missing.items():
            for c_bgn, c_end in _chunks(days):
                if _stop():
                    break
                sub = Query(bgn=c_bgn, end=c_end, biz_types=[biz])
                for _ in sweep_bids(client, sub, cache):
                    pass
            if stopped:
                break
    else:
        logger.info("공고 목록: 요청 기간 전체가 이미 DB에 있음 (API 스윕 생략)")

    # 2) 명단 미수집 공고만 개찰완료 API로 수집
    todo = [] if stopped else cache.unfetched_bids(q)
    detail_calls, uncached = 0, 0
    if todo:
        logger.info("참가업체 명단 미수집 공고 %d건 — 수집 시작", len(todo))
    for idx, bid in enumerate(todo, 1):
        if _stop():
            uncached = len(todo) - idx + 1
            break
        if max_detail_calls is not None and detail_calls >= max_detail_calls:
            uncached = len(todo) - idx + 1
            logger.warning("상세조회 호출 한도(%d) 도달 — %d건 미수집. "
                           "수집된 범위 안에서 검색합니다.", max_detail_calls, uncached)
            break
        try:
            rows = client.openg_participants(
                str(bid.get("bidNtceNo", "")), *bid["key"].split("|")[1:])
        except NaraApiError as e:
            if "트래픽 초과" in str(e):
                uncached = len(todo) - idx + 1
                logger.error("일일 트래픽 초과 — %d건 미수집. 수집된 범위 안에서 검색합니다.",
                             uncached)
                break
            logger.warning("공고 %s 상세조회 실패: %s", bid.get("bidNtceNo"), e)
            continue
        detail_calls += 1
        cache.put(bid["key"], rows)
        if idx % progress_every == 0:
            logger.info("진행 %d/%d (API 상세호출 %d)", idx, len(todo), detail_calls)
    if stopped:
        logger.warning("사용자 요청으로 수집을 중지했습니다 — 지금까지 DB에 있는 "
                       "범위에서 결과를 표시합니다.")
    if uncached:
        logger.warning("미수집 공고 %d건은 이번 결과에서 빠질 수 있습니다 — "
                       "재검색 또는 수집기 실행 시 채워집니다.", uncached)

    # 3) SQL 매칭
    results = cache.db_search(q)
    results.sort(key=lambda r: (str(r.get("opengDt", "")),
                                int(r.get("opengRank") or 999)))
    logger.info("DB 검색 완료 — 매칭 %d건 (API 상세호출 %d회)", len(results), detail_calls)
    return results
