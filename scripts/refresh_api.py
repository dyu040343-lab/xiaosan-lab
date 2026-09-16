#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小散研究院 · 「立即刷新」触发接口

只做一件事：受控地跑一次 scripts/fetch_data.py，让用户点「刷新」就能拿到最新一版，
不用干等 cron 的下一个 10 分钟。

为什么需要一个服务：nginx 只能发静态文件，没法触发服务器动作。

三层保护（无节制触发会被东财按 IP 限流，反而弄坏正常的数据管道）：
  1. 仅交易时段开放（非交易时段数据本来就不会变）
  2. 单飞：同一时刻只允许一次抓取
  3. 限流：两次之间至少 MIN_INTERVAL 秒，每小时最多 MAX_PER_HOUR 次
  4. 来源校验：只接受本站 Origin/Referer（挡随手脚本，不是安全边界）

可选加固：把令牌写进 data/.refresh_token（chmod 600），
之后请求必须带 X-Refresh-Token 才放行（前端可在 localStorage 里存）。

部署：systemd 常驻，只监听 127.0.0.1:8081，由 nginx 反代 /api/refresh。
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FETCH = ROOT / "scripts" / "fetch_data.py"
DATA_JSON = ROOT / "data" / "radar_data.json"
TOKEN_FILE = ROOT / "data" / ".refresh_token"

PORT = int(os.environ.get("REFRESH_PORT", "8081"))
MIN_INTERVAL = int(os.environ.get("REFRESH_MIN_INTERVAL", "120"))
MAX_PER_HOUR = int(os.environ.get("REFRESH_MAX_PER_HOUR", "10"))
RUN_TIMEOUT = int(os.environ.get("REFRESH_TIMEOUT", "180"))
ALLOW_ORIGINS = [o.strip() for o in os.environ.get(
    "REFRESH_ALLOW_ORIGINS",
    "https://xiaosanlab.online,https://www.xiaosanlab.online").split(",") if o.strip()]

PATH = "/api/refresh"

LOCK = threading.Lock()
STATE = {"running": False, "history": [], "last": None}

# 复用抓数脚本里的同一套交易时段 / 交易日历判断，避免两处口径漂移
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import fetch_data as fd
    IMPORT_ERROR = None
except Exception as _e:                     # noqa: BLE001
    fd = None
    IMPORT_ERROR = repr(_e)


def in_window():
    """(是否可抓, 不可抓的原因) —— 读不到日历/模块时一律拒绝，宁可拒绝不乱跑"""
    if fd is None:
        return False, "服务端未就绪：" + str(IMPORT_ERROR)
    try:
        fd._CAL = fd.load_trading_calendar()          # 每次读盘：跨天、换日历都正确
        if not fd.is_trading_day():
            return False, "今天休市，数据不会变"
        if not fd.in_trading_window():
            return False, "非交易时段（交易日 9:15–11:35 / 12:55–15:35 之外），数据不会变"
        return True, ""
    except Exception as e:                            # noqa: BLE001
        return False, "交易日历读取失败：%r" % (e,)


def decide(now, history, running, window_ok, window_why, origin_ok, token_ok):
    """纯判定：返回 (HTTP 状态码, 给用户看的话)。抽出来是为了能离线单测。"""
    if not origin_ok:
        return 403, "请求来源不被允许"
    if not token_ok:
        return 401, "刷新口令不正确"
    if running:
        return 409, "服务器正在抓取中，请稍候几秒"
    if not window_ok:
        return 409, window_why or "非交易时段，数据不会变"
    recent = [t for t in history if now - t < 3600]
    if recent and now - recent[-1] < MIN_INTERVAL:
        left = int(MIN_INTERVAL - (now - recent[-1]) + 1)
        return 429, "刚刚刷新过，请 %d 秒后再试" % left
    if len(recent) >= MAX_PER_HOUR:
        return 429, "本小时触发次数已达上限（%d 次），请等下一次自动更新" % MAX_PER_HOUR
    return 200, ""


def run_fetch():
    t0 = time.time()
    p = subprocess.run([sys.executable, str(FETCH), "--force"], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=RUN_TIMEOUT)
    took = int((time.time() - t0) * 1000)
    return p.returncode, took, (p.stdout or "")[-1500:], (p.stderr or "")[-500:]


def data_time():
    """只扫文件头 —— 整份 JSON 有 6.9MB，没必要为了一个时间戳全读进来"""
    try:
        with open(DATA_JSON, encoding="utf-8") as f:
            head = f.read(2048)
        m = re.search(r'"update_time"\s*:\s*"([^"]+)"', head)
        return m.group(1) if m else None
    except Exception:                                 # noqa: BLE001
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "xiaosan-refresh/1.0"

    def log_message(self, fmt, *args):                # 交给 journald 统一收
        sys.stderr.write("[refresh] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _origin_ok(self):
        vals = [self.headers.get("Origin") or "", self.headers.get("Referer") or ""]
        if not any(vals):
            return False                              # 浏览器同源 POST 一定会带
        return any(v.startswith(a) for v in vals for a in ALLOW_ORIGINS)

    def _token_ok(self):
        try:
            want = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except Exception:                             # noqa: BLE001
            want = ""
        if not want:
            return True                               # 未配置口令 → 只靠限流 + 来源
        return (self.headers.get("X-Refresh-Token") or "").strip() == want

    def do_GET(self):
        """只报状态，不触发抓取（给健康检查/前端探活用）"""
        if self.path.split("?")[0] != PATH:
            return self._send(404, {"ok": False, "message": "not found"})
        ok, why = in_window()
        now = time.time()
        with LOCK:
            hist = list(STATE["history"])
            running, last = STATE["running"], STATE["last"]
        return self._send(200, {
            "ok": True, "running": running, "in_window": ok, "window_hint": why,
            "hour_count": len([t for t in hist if now - t < 3600]),
            "min_interval": MIN_INTERVAL, "max_per_hour": MAX_PER_HOUR,
            "data_time": data_time(), "last": last,
        })

    def do_POST(self):
        if self.path.split("?")[0] != PATH:
            return self._send(404, {"ok": False, "message": "not found"})

        origin_ok, token_ok = self._origin_ok(), self._token_ok()
        # 来源/口令没过就不去读日历（省掉无意义的 IO）
        window_ok, why = in_window() if (origin_ok and token_ok) else (False, "")

        with LOCK:
            code, msg = decide(time.time(), list(STATE["history"]), STATE["running"],
                               window_ok, why, origin_ok, token_ok)
            if code == 200:
                STATE["running"] = True
        if code != 200:
            return self._send(code, {"ok": False, "message": msg, "data_time": data_time()})

        try:
            rc, took, out, err = run_fetch()
            ok = (rc == 0)
            if ok:
                msg = "服务器已重新抓取（%.1f 秒）" % (took / 1000.0)
            else:
                msg = "服务器抓取失败，请稍后再试"
            with LOCK:
                if ok:
                    STATE["history"].append(time.time())
                STATE["last"] = {"ts": int(time.time()), "ok": ok, "took_ms": took}
            return self._send(200 if ok else 500, {
                "ok": ok, "message": msg, "took_ms": took, "data_time": data_time(),
                "rc": rc, "stderr": err if not ok else "",
                "tail": out[-400:] if not ok else "",
            })
        except subprocess.TimeoutExpired:
            with LOCK:
                STATE["last"] = {"ts": int(time.time()), "ok": False, "took_ms": RUN_TIMEOUT * 1000}
            return self._send(504, {"ok": False, "message": "抓取超时（>%d 秒），请稍后再试" % RUN_TIMEOUT})
        except Exception as e:                        # noqa: BLE001
            return self._send(500, {"ok": False, "message": "触发失败：%r" % (e,)})
        finally:
            with LOCK:
                STATE["running"] = False


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    ok, why = in_window()
    print("refresh-api 监听 127.0.0.1:%d ｜ 交易时段: %s%s ｜ 限流 %ds / %d 次每小时 ｜ 口令: %s"
          % (PORT, ok, ("（" + why + "）") if not ok else "",
             MIN_INTERVAL, MAX_PER_HOUR, "已配置" if TOKEN_FILE.exists() else "未配置"), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
