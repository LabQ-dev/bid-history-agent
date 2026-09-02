"""개찰결과 일일 수집기.

매일 전날 개찰완료된 공고의 참가업체·순위 데이터를 미리 받아
검색 캐시(cache.db)에 쌓는다. 수집된 기간을 검색하면 공고별
상세조회가 전부 캐시에서 나와 API를 거의 쓰지 않고 즉시 끝난다.

사용:
  python collector.py                          # 어제 하루치 수집 (기본)
  python collector.py --date 20260810         # 특정일 수집
  python collector.py --from 20260701 --to 20260731   # 과거 소급(백필)
  python collector.py --daemon                 # 매일 06:00 자동 수집 (도커용)

일일 API 한도 보호: --max-calls (기본 800회) 도달 시 중단.
이미 수집된 공고는 건너뛰므로 다음 실행 시 이어서 수집된다.
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from main import load_env
from bid_history.api import NaraClient, OPENG_LIST_OPS, NaraApiError
from bid_history.search import Query, DetailCache, sweep_bids

logger = logging.getLogger("bid_history.collector")

DATA_DIR = Path(os.environ.get("G2B_DATA_DIR", Path(__file__).parent))


def collect(client: NaraClient, cache: DetailCache, bgn: str, end: str,
            biz_types: list, max_calls: int) -> dict:
    """기간 내 개찰완료 공고의 참가업체 데이터를 캐시에 적재."""
    q = Query(bgn=bgn, end=end, biz_types=biz_types)
    bids = list(sweep_bids(client, q, cache))   # 공고 메타데이터도 bids 테이블에 적재
    stats = {"bids": len(bids), "fetched": 0, "skipped": 0, "calls": 0}
    logger.info("개찰완료 공고 %d건 (%s~%s) — 수집 시작", len(bids), bgn, end)

    for idx, bid in enumerate(bids, 1):
        key = "|".join(str(bid.get(k, "")) for k in
                       ("bidNtceNo", "bidNtceOrd", "bidClsfcNo", "rbidNo"))
        if cache.get(key) is not None:
            stats["skipped"] += 1
            continue
        if stats["calls"] >= max_calls:
            logger.warning("호출 한도(%d) 도달 — 남은 %d건은 다음 실행 때 이어서 수집",
                           max_calls, len(bids) - idx + 1)
            break
        try:
            rows = client.openg_participants(
                str(bid.get("bidNtceNo", "")),
                str(bid.get("bidNtceOrd", "")),
                str(bid.get("bidClsfcNo", "")),
                str(bid.get("rbidNo", "")))
        except NaraApiError as e:
            if "트래픽 초과" in str(e):
                logger.error("일일 트래픽 초과 — 수집 중단 (%d/%d건). 내일 이어서 수집됩니다.",
                             idx - 1, len(bids))
                break
            logger.warning("공고 %s 수집 실패: %s", bid.get("bidNtceNo"), e)
            continue
        stats["calls"] += 1
        stats["fetched"] += 1
        cache.put(key, rows)
        if idx % 200 == 0:
            logger.info("진행 %d/%d (신규 %d, 캐시존재 %d)",
                        idx, len(bids), stats["fetched"], stats["skipped"])

    logger.info("수집 완료 — 공고 %(bids)d건 중 신규 %(fetched)d건 적재, "
                "기존 %(skipped)d건, API 호출 %(calls)d회(목록조회 제외)", stats)
    return stats


def run_once(args):
    key = os.environ.get("G2B_SERVICE_KEY", "")
    if not key:
        sys.exit("G2B_SERVICE_KEY가 없습니다. .env에 인증키를 설정하세요.")
    client = NaraClient(key)
    cache = DetailCache(DATA_DIR / "cache.db")

    if args.bgn and args.end:
        bgn, end = args.bgn, args.end
    else:
        day = args.date or (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        bgn = end = day
    collect(client, cache, bgn, end, args.types, args.max_calls)
    logger.info("총 API 호출수(목록조회 포함): %d", client.call_count)


def next_run_time(hour: int) -> datetime:
    now = datetime.now()
    run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if run <= now:
        run += timedelta(days=1)
    return run


def main():
    p = argparse.ArgumentParser(description="나라장터 개찰결과 일일 수집기")
    p.add_argument("--date", default="", help="수집할 날짜 YYYYMMDD (기본: 어제)")
    p.add_argument("--from", dest="bgn", default="", help="백필 시작일 YYYYMMDD")
    p.add_argument("--to", dest="end", default="", help="백필 종료일 YYYYMMDD")
    p.add_argument("--types", nargs="*", default=list(OPENG_LIST_OPS),
                   choices=list(OPENG_LIST_OPS), help="업무구분 (기본: 전체)")
    p.add_argument("--max-calls", type=int,
                   default=int(os.environ.get("COLLECT_MAX_CALLS", "8000")),
                   help="상세조회 최대 호출수 (기본 8000, 일일한도 보호)")
    p.add_argument("--daemon", action="store_true",
                   help="매일 지정 시각에 최근 N일치 자동 수집 (도커용)")
    p.add_argument("--lookback", type=int,
                   default=int(os.environ.get("COLLECT_LOOKBACK_DAYS", "7")),
                   help="--daemon이 매일 재확인할 최근 일수 (기본 7 — 조달청이 "
                        "뒤늦게 등록하는 개찰결과를 따라잡기 위함)")
    p.add_argument("--coverage", action="store_true",
                   help="수집 범위 요약(개찰일별 공고수/명단 보유수) 출력 후 종료")
    p.add_argument("--hour", type=int,
                   default=int(os.environ.get("COLLECT_HOUR", "6")),
                   help="--daemon 수집 시각 (기본 06시)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%m-%d %H:%M:%S")
    load_env()

    if args.coverage:
        cache = DetailCache(DATA_DIR / "cache.db")
        rows = cache.coverage()
        print(f"{'개찰일':12} {'공고수':>6} {'명단보유':>6}")
        for d, n, have in rows:
            mark = "" if n == have else "  ← 미완"
            print(f"{d or '(없음)':12} {n:>6} {have:>6}{mark}")
        print(f"합계: 공고 {sum(r[1] for r in rows)}건 / 명단 보유 {sum(r[2] for r in rows)}건")
        return

    if not args.daemon:
        run_once(args)
        return

    logger.info("데몬 모드 — 매일 %02d:00에 최근 %d일치 개찰결과를 수집합니다.",
                args.hour, args.lookback)
    while True:
        run_at = next_run_time(args.hour)
        wait = (run_at - datetime.now()).total_seconds()
        logger.info("다음 수집: %s (%.0f분 후)", run_at, wait / 60)
        time.sleep(max(wait, 1))
        try:
            # 어제까지 최근 N일을 재확인 — 이미 받은 공고는 건너뛰므로
            # 실제 비용은 신규(뒤늦게 등록된) 건들뿐
            args.date = ""
            args.bgn = (datetime.now() - timedelta(days=args.lookback)).strftime("%Y%m%d")
            args.end = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            run_once(args)
        except Exception as e:
            logger.error("수집 실패: %s — 내일 재시도", e)


if __name__ == "__main__":
    main()
