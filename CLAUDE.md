# bid-history-agent — Claude 작업 지침

나라장터 입찰참가 이력 검색 도구. 회사(사업자번호) 기준으로 기간 내 참가 입찰과
평가순위·점수를 검색한다. 대화와 문서는 한국어를 사용한다.

## 작업 규칙

- **코드 수정은 브랜치 → 커밋 → PR → 머지** 순서로 진행한다. main에 직접 커밋하지 않는다.
- 수정 후에는 반드시 검증한다: `uv run python -m py_compile <파일>` + 실데이터로 실제 검색/수집 동작 확인.
- 서버(web.py)를 수정했으면 재시작해야 반영된다 (아래 실행 명령).
- **document/ 폴더의 md 파일은 확정된 사항만 기록**하며, 기능·개발사항이 바뀌어 문서를
  고쳐야 할 때는 어떤 파일의 어떤 내용을 어떻게 바꿀지 먼저 사용자에게 제안하고
  **승인받은 후에만 수정**한다. 제안·향후 과제·추정 내용은 문서에 넣지 않는다.
- `git push`는 PR 워크플로우(브랜치 푸시) 외에는 사용자에게 먼저 확인받는다.
- `.env`(공공데이터포털 인증키)와 `cache.db`는 절대 커밋하지 않는다 (.gitignore 유지).
  로그·오류 메시지에 인증키가 노출되지 않게 마스킹을 유지한다.

## 실행

```bash
uv run python web.py                # 검색 웹서버 → http://localhost:8931
uv run python collector.py          # 어제치 개찰결과 수집
uv run python collector.py --from YYYYMMDD --to YYYYMMDD --types 용역 ...   # 백필
uv run python collector.py --coverage   # 수집 범위 확인 (공고수 = 명단보유 검산)
docker compose up -d --build        # web + collector(매일 06시) 컨테이너 실행
```

## 구조

- `web.py` — 로컬 웹서버 (검색 화면 서빙, /api/search·status·stop·bid·companies·download)
- `index.html` — 검색 화면 (LABQ 폼 디자인, 업체 검색 팝업, 개찰 순위표 팝업)
- `collector.py` — 개찰결과 일일 수집기/백필 (--daemon: 매일 06시)
- `bid_history/api.py` — 조달청 OpenAPI 클라이언트
- `bid_history/search.py` — DB 저장소(DetailCache)·검색 로직
- `bid_history/report.py` — 표/엑셀 출력 (1위 비교 컬럼 포함)

## DB (SQLite `cache.db`, WAL 모드)

- `bids` — 공고 메타 (key, 공고명, 개찰일시 opengDt, 수요기관, 업무구분 bizType)
- `participants` — 참가업체 1명 = 1행 (순위·투찰금액·평가점수, normNm/normBizno 인덱스)
- `fetched` — 참가업체 명단 수집 완료된 공고 key
- `swept` — 업무구분×일자별 공고 목록 스윕 완료 기록 (오늘/미래는 기록 금지)
- key = `공고번호|차수|분류번호|재입찰번호`. 조인은 항상 key로 한다 (공고번호 단독 금지 — 재공고/재입찰 구분).

## 조달청 API 제약 (ScsbidInfoService)

- **업체 기준 검색 API 없음** → 기간 스윕 + 공고별 상세 조회 + DB 적재 구조인 이유
- 목록 조회(getOpengResultListInfo*)는 **조회기간 최대 1개월** → 코드가 30일 단위 자동 분할
- 참가업체 명단(getOpengResultListInfoOpengCompt)은 **공고 1건당 1회 호출** → 호출량의 주범,
  그래서 수집기로 미리 쌓고 검색은 DB(SQL)에서 수행
- 일일 트래픽: 운영계정 (실측 12,000회+ 무제한 통과), 한도 초과 시 즉시 중단하고 안내
- 매칭 규칙: **사업자번호가 있으면 그것만으로 매칭** (이름 유사 타사 혼입 방지)

## 배포

- 사무실 리눅스: README의 systemd 가이드 또는 docker compose (데이터는 `data/` 볼륨,
  이전 시 `cache.db` 파일만 복사)
- GitHub Pages(labq-dev.github.io/bid-history-agent)는 **UI 미리보기 전용** — 검색 서버 없음
