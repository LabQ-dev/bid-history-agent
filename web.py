"""나라장터 입찰참가 이력 검색 — 로컬 웹 UI.

실행:  uv run python web.py   →  http://localhost:8931 접속

정적 HTML만으로는 불가능한 구조라 로컬 서버를 사용한다:
공공데이터포털 API는 브라우저 직접 호출(CORS)이 차단되고,
인증키를 HTML에 넣으면 노출되기 때문. 검색 로직은 CLI와 동일한
bid_history 패키지를 그대로 사용한다.
"""

import base64
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from main import load_env
import os

from bid_history.api import NaraClient, OPENG_LIST_OPS
from bid_history.search import Query, DetailCache, search
from bid_history import report

BASE = Path(__file__).parent

# .env 설정 (기본: 이 컴퓨터에서만 접속)
#   WEB_HOST=0.0.0.0      → 사무실 등 다른 컴퓨터에서도 접속 허용
#   WEB_PORT=8931         → 접속 포트
#   WEB_PASSWORD=비밀번호  → 접속 시 비밀번호 요구 (외부 접속 허용 시 필수 권장)
load_env()
HOST = os.environ.get("WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEB_PORT", "8931"))
PASSWORD = os.environ.get("WEB_PASSWORD", "")
DATA_DIR = Path(os.environ.get("G2B_DATA_DIR", BASE))  # 도커에서는 볼륨 경로

# ── 검색 작업 상태 (동시 1건) ────────────────────────
job = {"state": "idle", "logs": [], "results": [], "error": ""}
job_lock = threading.Lock()
stop_event = threading.Event()


class JobLogHandler(logging.Handler):
    def emit(self, record):
        with job_lock:
            job["logs"].append(self.format(record))
            del job["logs"][:-200]


def run_search(params: dict):
    global job
    handler = JobLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    root = logging.getLogger("bid_history")
    root.addHandler(handler)
    try:
        key = os.environ.get("G2B_SERVICE_KEY", "")
        if not key:
            raise RuntimeError("G2B_SERVICE_KEY가 없습니다. .env에 인증키를 설정하세요.")
        client = NaraClient(key)
        q = Query(
            company=params.get("company", "").strip(),
            bizno=params.get("bizno", "").strip(),
            ceo=params.get("ceo", "").strip(),
            bgn=params.get("bgn", "").strip(),
            end=params.get("end", "").strip(),
            biz_types=params.get("types") or list(OPENG_LIST_OPS),
            keyword=params.get("keyword", "").strip(),
        )
        if not (q.company or q.bizno or q.ceo):
            raise RuntimeError("회사명 / 사업자등록번호 / 대표자명 중 하나 이상을 입력하세요.")
        if not (q.bgn and q.end):
            raise RuntimeError("조회 기간을 입력하세요.")
        # 일일한도 보호: 기본 1,000회, 최대 5,000회 (운영계정 기준)
        max_calls = min(int(params.get("maxCalls") or 1000), 5000)
        cache = DetailCache(DATA_DIR / "cache.db")
        results = search(client, q, cache, max_detail_calls=max_calls,
                         should_stop=stop_event.is_set)
        with job_lock:
            job["results"] = results
            job["state"] = "done"
            job["logs"].append(f"완료 — 매칭 {len(results)}건, API 호출 {client.call_count}회")
    except Exception as e:
        with job_lock:
            job["state"] = "error"
            job["error"] = str(e)
    finally:
        root.removeHandler(handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 콘솔 소음 제거
        pass

    def _authorized(self) -> bool:
        """WEB_PASSWORD 설정 시 브라우저 기본 로그인창(Basic Auth)으로 확인."""
        if not PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode()
                _, _, pw = decoded.partition(":")
                if hmac.compare_digest(pw, PASSWORD):
                    return True
            except Exception:
                pass
        body = "인증이 필요합니다.".encode()
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="bid-history", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self):
        if not self._authorized():
            return
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, (BASE / "index.html").read_bytes(),
                       "text/html; charset=utf-8")
        elif path == "/api/bid":
            # 공고번호로 개찰결과(참가업체 전체 순위표) 조회 — DB(캐시)에서만 읽음
            from urllib.parse import parse_qs
            no = parse_qs(urlparse(self.path).query).get("no", [""])[0].strip()
            rows = []
            if no:
                rows = DetailCache(DATA_DIR / "cache.db").get_rows(no)

                def rank(r):
                    try:
                        return int(r.get("opengRank"))
                    except (TypeError, ValueError):
                        return 9999
                rows.sort(key=rank)
            self._json({"found": bool(rows), "no": no, "rows": rows})
        elif path == "/api/status":
            with job_lock:
                rows = report.to_rows(job["results"]) if job["state"] == "done" else []
                self._json({"state": job["state"], "logs": job["logs"][-30:],
                            "error": job["error"], "rows": rows})
        elif path == "/api/download":
            with job_lock:
                results = list(job["results"])
            if not results:
                # 서버 재시작 등으로 결과가 없는 상태 — 빈 파일 대신 안내
                body = ("<meta charset='utf-8'><p style='font-family:sans-serif'>"
                        "다운로드할 검색 결과가 없습니다. 검색을 다시 실행한 뒤 "
                        "엑셀 다운로드를 눌러주세요.</p>").encode()
                self._send(200, body, "text/html; charset=utf-8")
                return
            out = DATA_DIR / "검색결과.xlsx"
            report.save_xlsx(results, out)
            body = out.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''%EA%B2%80%EC%83%89%EA%B2%B0%EA%B3%BC.xlsx")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self._authorized():
            return
        path = urlparse(self.path).path
        if path == "/api/stop":
            stop_event.set()
            self._json({"ok": True})
            return
        if path != "/api/search":
            self._send(404, b"not found", "text/plain")
            return
        with job_lock:
            if job["state"] == "running":
                self._json({"ok": False, "error": "이미 검색이 진행 중입니다."}, 409)
                return
            job.update({"state": "running", "logs": [], "results": [], "error": ""})
        stop_event.clear()
        length = int(self.headers.get("Content-Length", 0))
        params = json.loads(self.rfile.read(length) or b"{}")
        threading.Thread(target=run_search, args=(params,), daemon=True).start()
        self._json({"ok": True})


def main():
    logging.basicConfig(level=logging.INFO)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"나라장터 입찰참가 이력 검색 UI: http://localhost:{PORT}  (Ctrl+C로 종료)")
    if HOST != "127.0.0.1":
        print(f"외부 접속 허용됨 — 다른 컴퓨터에서는 http://<이 컴퓨터 IP>:{PORT} 로 접속")
        if not PASSWORD:
            print("⚠️  WEB_PASSWORD가 설정되지 않았습니다. 같은 네트워크의 누구나 접속해"
                  " API 한도를 소진할 수 있으니 .env에 WEB_PASSWORD 설정을 권장합니다.")
    server.serve_forever()


if __name__ == "__main__":
    main()
