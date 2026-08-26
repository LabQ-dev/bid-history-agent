"""나라장터 입찰참가 이력 검색 CLI.

회사명 또는 사업자등록번호(+대표자명)로, 주어진 기간의
입찰참가 사업과 평가순위(개찰순위)·평가점수를 검색한다.

사용 예:
  python main.py --company "주식회사 예신뷰" --from 20250101 --to 20250630
  python main.py --bizno 6438701544 --ceo 최인아 --from 20250601 --to 20250630 \
      --types 용역 --keyword 유지보수 --out result.xlsx
  python main.py --bid-no R25BK01027145            # 공고번호로 직접 조회
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from bid_history.api import NaraClient, OPENG_LIST_OPS
from bid_history.search import Query, DetailCache, search
from bid_history import report


def load_env():
    env = Path(__file__).parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def main():
    p = argparse.ArgumentParser(description="나라장터 입찰참가 이력·평가순위 검색")
    p.add_argument("--company", default="", help="회사명 (부분일치)")
    p.add_argument("--bizno", default="", help="사업자등록번호 (하이픈 무관)")
    p.add_argument("--ceo", default="", help="대표자명")
    p.add_argument("--from", dest="bgn", default="", help="조회 시작일 YYYYMMDD")
    p.add_argument("--to", dest="end", default="", help="조회 종료일 YYYYMMDD")
    p.add_argument("--types", nargs="*", default=list(OPENG_LIST_OPS),
                   choices=list(OPENG_LIST_OPS), help="업무구분 (기본: 전체)")
    p.add_argument("--keyword", default="", help="공고명 키워드(스윕량 축소용, 선택)")
    p.add_argument("--bid-no", default="", help="입찰공고번호로 직접 조회 (기간 불필요)")
    p.add_argument("--out", default="", help="결과 파일 경로 (.xlsx 또는 .csv)")
    p.add_argument("--max-calls", type=int, default=None,
                   help="공고 상세조회 API 최대 호출수 (일일 트래픽 보호)")
    p.add_argument("--cache", default=str(Path(__file__).parent / "cache.db"),
                   help="상세조회 캐시 DB 경로")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    load_env()
    key = os.environ.get("G2B_SERVICE_KEY", "")
    if not key:
        sys.exit("G2B_SERVICE_KEY가 없습니다. .env 파일에 공공데이터포털 인증키(Decoding)를 넣어주세요.")

    client = NaraClient(key)

    # 공고번호 직접 조회 모드
    if args.bid_no:
        rows = client.openg_participants(args.bid_no)
        q = Query(company=args.company, bizno=args.bizno, ceo=args.ceo)
        winner = next((r for r in rows if str(r.get("opengRank")) == "1"), None)
        results = [{**{"bidNtceNo": args.bid_no}, **r, "_winner": winner}
                   for r in rows if q.matches(r)]
        finish(results, args)
        return

    if not (args.company or args.bizno or args.ceo):
        sys.exit("검색조건이 없습니다. --company / --bizno / --ceo 중 하나 이상을 입력하세요.")
    if not (args.bgn and args.end):
        sys.exit("--from, --to (YYYYMMDD) 기간을 입력하세요.")

    q = Query(company=args.company, bizno=args.bizno, ceo=args.ceo,
              bgn=args.bgn, end=args.end, biz_types=args.types,
              keyword=args.keyword)
    cache = DetailCache(Path(args.cache))
    results = search(client, q, cache, max_detail_calls=args.max_calls)
    logging.info("총 API 호출수: %d", client.call_count)
    finish(results, args)


def finish(results, args):
    report.print_table(results)
    if args.out:
        out = Path(args.out)
        if out.suffix.lower() == ".csv":
            report.save_csv(results, out)
        else:
            report.save_xlsx(results, out.with_suffix(".xlsx"))
            out = out.with_suffix(".xlsx")
        print(f"\n결과 {len(results)}건 저장: {out}")


if __name__ == "__main__":
    main()
