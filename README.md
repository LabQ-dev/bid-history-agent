# 입찰참가 이력 검색 에이전트 (bid-history-agent)

나라장터에서 **회사명 또는 사업자등록번호(+대표자명)** 로, 주어진 기간 내
**입찰참가 사업과 평가순위(개찰순위)·평가점수(기술/가격/종합)** 를 검색합니다.

## 동작 원리

조달청 나라장터 낙찰정보서비스 OpenAPI는 업체 기준 검색을 제공하지 않으므로
(공고번호·기간 기준만 제공), 3단계로 조회합니다.

```
1) 개찰결과 목록 (물품/공사/용역/외자 × 개찰일시 범위)
      getOpengResultListInfo{Thng,Cnstwk,Servc,Frgcpt}
              │  개찰완료 공고 목록
2) 공고별 참가업체 조회 → SQLite 캐시 (cache.db)
      getOpengResultListInfoOpengCompt
              │  참가업체별 개찰순위·투찰금액·기술/가격/종합 평가점수
3) 로컬 필터: (회사명 OR 사업자번호) AND 대표자명
              │
       콘솔 표 + XLSX/CSV
```

- **평가순위(opengRank)**: 협상에 의한 계약이면 기술+가격 합산 고득점순 협상순위,
  그 외에는 개찰순위입니다. 1위 = 낙찰(예정)자.
- 협상계약 건은 기술평가점수·입찰가격점수·종합평가점수도 함께 제공됩니다.

## 준비

1. [공공데이터포털](https://www.data.go.kr/data/15129397/openapi.do)에서
   **조달청_나라장터 낙찰정보서비스** 활용신청 → 인증키(Decoding) 발급
2. `.env` 생성:
   ```
   cp .env.example .env   # G2B_SERVICE_KEY 입력
   ```
3. 의존성 설치:
   ```
   uv sync   # 또는 pip install requests openpyxl
   ```

## 사용법 1 — 웹 화면 (권장)

```bash
uv run python web.py
```

실행 후 브라우저에서 **http://localhost:8931** 접속.
회사명/사업자번호/대표자명 + 기간을 입력하고 검색하면 진행 로그가 실시간으로
표시되고, 결과 표(1위 낙찰 행 하이라이트)와 엑셀 다운로드를 제공합니다.

> 정적 HTML만으로는 만들 수 없습니다 — 공공데이터포털 API가 브라우저
> 직접 호출(CORS)을 차단하고, 인증키를 HTML에 넣으면 노출되기 때문입니다.
> 그래서 로컬 서버(web.py)가 API 호출을 대신 수행합니다 (외부 의존성 없음,
> 파이썬 표준 라이브러리 http.server 사용).

## 사용법 2 — 터미널 (CLI)

```bash
# 회사명으로 상반기 참가이력 검색 (용역만, 결과 엑셀 저장)
python main.py --company "주식회사 예신뷰" --from 20250101 --to 20250630 \
    --types 용역 --out result.xlsx

# 사업자번호 + 대표자명으로 검색
python main.py --bizno 643-87-01544 --ceo 최인아 --from 20250601 --to 20250630

# 공고명 키워드로 스윕량 축소 (조회할 공고 수를 줄여 API 호출 절약)
python main.py --company 예신뷰 --from 20250101 --to 20250630 --keyword 청소

# 특정 공고번호의 참가업체·순위 직접 조회
python main.py --bid-no R25BK01027145
```

| 옵션 | 설명 |
|---|---|
| `--company` | 회사명 (부분일치, `(주)`/`주식회사`/공백 무시) |
| `--bizno` | 사업자등록번호 (하이픈 무관) |
| `--ceo` | 대표자명 (회사명/사업자번호와 AND 조건) |
| `--from` `--to` | 개찰일 기준 조회기간 (YYYYMMDD) |
| `--types` | 업무구분: 물품 공사 용역 외자 (기본 전체) |
| `--keyword` | 공고명 키워드 필터 (API 호출량 축소용) |
| `--out` | 결과 파일 (.xlsx / .csv). 1위(낙찰)는 하이라이트 |
| `--max-calls` | 공고 상세조회 최대 호출수 (아래 트래픽 한도 참고) |
| `--bid-no` | 공고번호 직접 조회 (기간 불필요) |

## 사무실 공용 컴퓨터에 올리기 (팀원 공동 사용)

항상 켜져 있는 컴퓨터에서 실행해두면 같은 네트워크의 팀원들이 브라우저로 사용할 수 있습니다.

1. 그 컴퓨터에 이 레포를 클론하고 `uv sync`, `.env` 생성 (인증키 입력)
2. `.env`에 외부 접속·비밀번호 설정 추가:
   ```
   WEB_HOST=0.0.0.0
   WEB_PASSWORD="팀공용비밀번호"
   ```
3. `uv run python web.py` 실행 (터미널을 켜둔 동안 동작)
4. 팀원들은 `http://<그 컴퓨터 IP>:8931` 접속 → 브라우저 로그인창에
   아이디는 아무거나, 비밀번호만 입력

- 컴퓨터 IP 확인: Linux `hostname -I` / Mac `ipconfig getifaddr en0` / Windows `ipconfig`
- `WEB_PASSWORD`를 설정하지 않으면 같은 네트워크의 누구나 접속해 API 일일한도를
  소진할 수 있으므로 외부 접속 허용 시 반드시 설정하세요.

### 리눅스 자동 실행 (systemd)

재부팅해도 자동으로 켜지고, 꺼지면 5초 후 자동 재시작됩니다.

```bash
# 1) uv 설치 (없다면) 및 프로젝트 준비
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/LabQ-dev/bid-history-agent.git ~/bid-history-agent
cd ~/bid-history-agent && uv sync
# .env 생성: G2B_SERVICE_KEY, WEB_HOST=0.0.0.0, WEB_PASSWORD 입력

# 2) 서비스 파일의 USERNAME(2곳)·경로를 실제 계정명으로 수정 후 등록
sed "s/USERNAME/$(whoami)/g" deploy/bid-history.service | sudo tee /etc/systemd/system/bid-history.service
sudo systemctl daemon-reload
sudo systemctl enable --now bid-history

# 3) 확인
systemctl status bid-history          # 상태
journalctl -u bid-history -f          # 로그
```

- 방화벽(ufw) 사용 시 포트 개방: `sudo ufw allow 8931`
- 코드 업데이트 반영: `cd ~/bid-history-agent && git pull && sudo systemctl restart bid-history`

## API 트래픽 한도 주의

공고 1건당 상세조회 1회가 필요합니다. 기간 내 개찰완료 공고가 수천 건이면
호출도 수천 회입니다.

- **개발계정: 1,000건/일** → 긴 기간·전체 업무구분 검색은 하루에 못 끝날 수 있음
- 공공데이터포털에서 **활용사례 등록(운영계정 전환)** 시 트래픽 증가
- 조회 결과는 `cache.db`에 캐시되므로, 한도 초과로 중단돼도
  **다음 날 같은 명령을 재실행하면 이어서** 조회합니다
- `--types 용역`, `--keyword`, 짧은 기간으로 호출량을 줄이는 것을 권장

## 파일

| 파일 | 역할 |
|---|---|
| `web.py` | 로컬 웹서버 (검색 API + 진행상황 + 엑셀 다운로드) |
| `index.html` | 검색 웹 화면 (web.py가 서빙) |
| `main.py` | CLI 진입점 |
| `bid_history/api.py` | OpenAPI 클라이언트 (페이징·재시도·TPS 간격) |
| `bid_history/search.py` | 스윕 → 상세조회 → 필터, SQLite 캐시 |
| `bid_history/report.py` | 콘솔 표 / CSV / XLSX 출력 |

## 문의

labq@labq.kr
