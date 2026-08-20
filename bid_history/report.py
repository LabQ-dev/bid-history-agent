"""검색 결과를 콘솔 표 / CSV / XLSX로 출력."""

import csv
from pathlib import Path

COLUMNS = [
    ("opengDt",        "개찰일시"),
    ("_bizType",       "업무구분"),
    ("bidNtceNo",      "공고번호"),
    ("bidNtceNm",      "공고명"),
    ("dminsttNm",      "수요기관"),
    ("prtcptCnum",     "참가업체수"),
    ("opengRank",      "평가순위"),
    ("prcbdrNm",       "업체명"),
    ("prcbdrBizno",    "사업자번호"),
    ("prcbdrCeoNm",    "대표자"),
    ("bidprcAmt",      "투찰금액"),
    ("bidprcrt",       "투찰률(%)"),
    ("techEvlVal",     "기술평가점수"),
    ("bidPrceEvlVal",  "가격평가점수"),
    ("totalEvlAmtVal", "종합평가점수"),
    ("rmrk",           "비고"),
]


def to_rows(results: list[dict]) -> list[list]:
    rows = [[label for _, label in COLUMNS]]
    for r in results:
        rows.append([r.get(key, "") for key, _ in COLUMNS])
    return rows


def print_table(results: list[dict], limit: int = 50):
    if not results:
        print("조건에 맞는 입찰참가 이력이 없습니다.")
        return
    for r in results[:limit]:
        win = " ★낙찰(1위)" if str(r.get("opengRank")) == "1" else ""
        print(f"[{r.get('opengDt','')}] {r.get('_bizType','')} {r.get('bidNtceNo','')} "
              f"{str(r.get('bidNtceNm',''))[:40]}")
        print(f"    순위 {r.get('opengRank','-')}/{r.get('prtcptCnum','-')}{win} | "
              f"{r.get('prcbdrNm','')} ({r.get('prcbdrBizno','')}, 대표 {r.get('prcbdrCeoNm','')}) | "
              f"투찰 {r.get('bidprcAmt','')}원 ({r.get('bidprcrt','')}%) | "
              f"기술 {r.get('techEvlVal','') or '-'} / 가격 {r.get('bidPrceEvlVal','') or '-'} "
              f"/ 종합 {r.get('totalEvlAmtVal','') or '-'}")
    if len(results) > limit:
        print(f"... 외 {len(results) - limit}건 (파일 출력 참고)")


def save_csv(results: list[dict], path: Path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(to_rows(results))


def save_xlsx(results: list[dict], path: Path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "입찰참가이력"
    for row in to_rows(results):
        ws.append(row)

    header_fill = PatternFill("solid", fgColor="D9E1F2")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
    rank_col = [label for _, label in COLUMNS].index("평가순위") + 1
    win_fill = PatternFill("solid", fgColor="FFF2CC")
    for row in ws.iter_rows(min_row=2):
        if str(row[rank_col - 1].value) == "1":
            for cell in row:
                cell.fill = win_fill
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(path)
