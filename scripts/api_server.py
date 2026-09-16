#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小散研究院 · 轻量 API 服务（只监听 127.0.0.1:8081，由 nginx 反代 /api/*）

提供两件事：

1. `POST /api/refresh` —— 受控地跑一次 scripts/fetch_data.py，让页面「刷新」能拿到最新一版
   `GET  /api/refresh` —— 只报状态，不触发
   保护：仅交易时段 + 单飞 + 冷却/频次 + 来源校验（+ 可选口令）

2. 自选股账号（可选功能，用户不登录就完全用不到）
   `POST /api/auth/register` {username, password} → {token, user}
   `POST /api/auth/login`    {username, password} → {token, user}
   `POST /api/auth/logout`   (Bearer)            → {ok}
   `GET  /api/watchlist`     (Bearer)            → {ok, codes, updated}
   `PUT  /api/watchlist`     (Bearer) {codes}    → {ok, codes, updated}
   说明：**没有邮箱找回**（个人工具，不接邮件），所以密码丢了只能重注册；若忘记密码可用
   `scripts/reset_user.py` 在本机重置（见 HANDOFF）。
   密码用 PBKDF2-HMAC-SHA256（20 万次 + 随机盐）存 `data/users.json`（0600），不存明文。

设计原则：纯标准库、无框架、状态只落两个 JSON 文件；所有写盘都是「临时文件 + 原子替换」。
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FETCH = ROOT / "scripts" / "fetch_data.py"
DATA_JSON = ROOT / "data" / "radar_data.json"
TOKEN_FILE = ROOT / "data" / ".refresh_token"        # 可选：刷新接口口令
USERS_FILE = ROOT / "data" / "users.json"            # 账号 + 各自的自选

PORT = int(os.environ.get("API_PORT", os.environ.get("REFRESH_PORT", "8081")))
MIN_INTERVAL = int(os.environ.get("REFRESH_MIN_INTERVAL", "120"))
MAX_PER_HOUR = int(os.environ.get("REFRESH_MAX_PER_HOUR", "10"))
RUN_TIMEOUT = int(os.environ.get("REFRESH_TIMEOUT", "180"))
ALLOW_ORIGINS = [o.strip() for o in os.environ.get(
    "REFRESH_ALLOW_ORIGINS",
    "https://xiaosanlab.online,https://www.xiaosanlab.online").split(",") if o.strip()]

WL_MAX_CODES = 300          # 单个账号最多存多少只
LOGIN_MAX_PER_HOUR = 30     # 单 IP 每小时登录/注册尝试上限（挡暴力猜密码）

LOCK = threading.RLock()
STATE = {"running": False, "history": [], "last": None}
LOGIN_HITS = {}             # ip -> [ts, ...]

# 复用抓数脚本里的同一套交易时段 / 交易日历判断，避免两处口径漂移
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import fetch_data as fd
    IMPORT_ERROR = None
except Exception as _e:                               # noqa: BLE001
    fd = None
    IMPORT_ERROR = repr(_e)


# ── 刷新：交易时段判断 ────────────────────────────────────────────
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


# ── 账号 / 自选：存储层 ──────────────────────────────────────────
def _load_users():
    try:
        with open(USERS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                                 # noqa: BLE001
        return {}


def _save_users(users):
    """临时文件 + 原子替换，避免写到一半断电把账号表写坏"""
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_name(USERS_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, USERS_FILE)


def _hash_pw(pw, salt_hex, iters):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                               bytes.fromhex(salt_hex), iters).hex()


def _clean_codes(codes):
    """只留 6 位数字、去重保序、限长"""
    out, seen = [], set()
    for c in (codes or []):
        c = str(c).strip()
        if re.fullmatch(r"\d{6}", c) and c not in seen:
            seen.add(c); out.append(c)
        if len(out) >= WL_MAX_CODES:
            break
    return out


def create_user(name, pw):
    """返回 (token, None) 或 (None, 错误信息)"""
    with LOCK:
        users = _load_users()
        if name in users:
            return None, "这个用户名已被占用"
        salt = secrets.token_hex(16)
        users[name] = {
            "salt": salt,
            "hash": _hash_pw(pw, salt, 200000),
            "iter": 200000,
            "created": int(time.time()),
            "tokens": [{"t": secrets.token_urlsafe(24), "ts": int(time.time())}],
            "codes": [],
            "updated": None,
        }
        _save_users(users)
        return users[name]["tokens"][0]["t"], None


def login_user(name, pw):
    with LOCK:
        users = _load_users()
        u = users.get(name)
        if not u:
            return None, "用户名或密码不对"
        if _hash_pw(pw, u["salt"], u.get("iter", 200000)) != u["hash"]:
            return None, "用户名或密码不对"
        tok = secrets.token_urlsafe(24)
        u["tokens"] = ([{"t": tok, "ts": int(time.time())}] + u.get("tokens", []))[:5]
        _save_users(users)
        return tok, None


def user_by_token(tok):
    """返回 (用户名, 用户字典) 或 (None, None)"""
    if not tok:
        return None, None
    with LOCK:
        users = _load_users()
        for name, u in users.items():
            for t in u.get("tokens", []):
                if t.get("t") == tok:
                    return name, u
    return None, None


def logout_user(tok):
    with LOCK:
        users = _load_users()
        hit = False
        for name, u in users.items():
            keep = [t for t in u.get("tokens", []) if t.get("t") != tok]
            if len(keep) != len(u.get("tokens", [])):
                u["tokens"] = keep
                hit = True
        if hit:
            _save_users(users)
    return hit


def get_codes(name):
    with LOCK:
        u = _load_users().get(name) or {}
        return _clean_codes(u.get("codes")), u.get("updated")


def put_codes(name, codes):
    """返回 (codes, updated)；若内容没变则不写盘"""
    clean = _clean_codes(codes)
    with LOCK:
        users = _load_users()
        u = users.get(name)
        if not u:
            return None, None
        if u.get("codes") != clean:
            u["codes"] = clean
            u["updated"] = int(time.time() * 1000)
            _save_users(users)
        return u.get("codes") or [], u.get("updated")


def login_allowed(ip, now=None):
    """很粗的登录频率限制：同一 IP 每小时最多 LOGIN_MAX_PER_HOUR 次尝试"""
    now = now or time.time()
    with LOCK:
        hits = [t for t in LOGIN_HITS.get(ip, []) if now - t < 3600]
        if len(hits) >= LOGIN_MAX_PER_HOUR:
            LOGIN_HITS[ip] = hits
            return False
        hits.append(now)
        LOGIN_HITS[ip] = hits
    return True


# ── HTTP 层 ─────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "xiaosan-api/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[api] %s - %s\n" % (self.address_string(), fmt % args))

    # ---- 工具 ----
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

    @property
    def route(self):
        return self.path.split("?")[0].rstrip("/") or "/"

    def _origin_ok(self):
        vals = [self.headers.get("Origin") or "", self.headers.get("Referer") or ""]
        if not any(vals):
            return False                              # 浏览器同源 POST 一定会带
        return any(v.startswith(a) for v in vals for a in ALLOW_ORIGINS)

    def _json_body(self, limit=65536):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:                             # noqa: BLE001
            return None
        if n <= 0 or n > limit:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:                             # noqa: BLE001
            return None

    def _bearer(self):
        h = self.headers.get("Authorization") or ""
        return h[7:].strip() if h.lower().startswith("bearer ") else ""

    def _refresh_token_ok(self):
        try:
            want = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except Exception:                             # noqa: BLE001
            want = ""
        if not want:
            return True                               # 未配置口令 → 只靠限流 + 来源
        return (self.headers.get("X-Refresh-Token") or "").strip() == want

    # ---- 路由 ----
    def do_GET(self):
        r = self.route
        if r == "/api/refresh":
            return self._refresh_status()
        if r == "/api/watchlist":
            return self._watchlist_get()
        return self._send(404, {"ok": False, "message": "not found"})

    def do_POST(self):
        r = self.route
        if r == "/api/refresh":
            return self._refresh_trigger()
        if r == "/api/auth/register":
            return self._auth_register()
        if r == "/api/auth/login":
            return self._auth_login()
        if r == "/api/auth/logout":
            return self._auth_logout()
        return self._send(404, {"ok": False, "message": "not found"})

    def do_PUT(self):
        if self.route == "/api/watchlist":
            return self._watchlist_put()
        return self._send(404, {"ok": False, "message": "not found"})

    # ---- 刷新 ----
    def _refresh_status(self):
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

    def _refresh_trigger(self):
        origin_ok, token_ok = self._origin_ok(), self._refresh_token_ok()
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
            msg = "服务器已重新抓取（%.1f 秒）" % (took / 1000.0) if ok else "服务器抓取失败，请稍后再试"
            with LOCK:
                if ok:
                    STATE["history"].append(time.time())
                STATE["last"] = {"ts": int(time.time()), "ok": ok, "took_ms": took}
            return self._send(200 if ok else 500, {
                "ok": ok, "message": msg, "took_ms": took, "data_time": data_time(),
                "rc": rc, "stderr": err if not ok else "", "tail": out[-400:] if not ok else "",
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

    # ---- 账号 ----
    def _check_auth_preconditions(self):
        if not self._origin_ok():
            self._send(403, {"ok": False, "message": "请求来源不被允许"})
            return False
        if not login_allowed(self.client_address[0]):
            self._send(429, {"ok": False, "message": "尝试过于频繁，请稍后再试"})
            return False
        return True

    def _auth_register(self):
        if not self._check_auth_preconditions():
            return
        b = self._json_body() or {}
        name = str(b.get("username") or "").strip()
        pw = str(b.get("password") or "")
        if not re.fullmatch(r"[A-Za-z0-9_\-]{3,20}", name):
            return self._send(400, {"ok": False, "message": "用户名 3–20 位，只能用字母/数字/下划线/短横线"})
        if len(pw) < 6:
            return self._send(400, {"ok": False, "message": "密码至少 6 位"})
        if len(pw) > 72:
            return self._send(400, {"ok": False, "message": "密码太长了（≤72 位）"})
        tok, err = create_user(name, pw)
        if err:
            return self._send(409, {"ok": False, "message": err})
        return self._send(200, {"ok": True, "user": name, "token": tok,
                                "message": "账号已创建，自选会跟着这个账号走"})

    def _auth_login(self):
        if not self._check_auth_preconditions():
            return
        b = self._json_body() or {}
        name = str(b.get("username") or "").strip()
        pw = str(b.get("password") or "")
        tok, err = login_user(name, pw)
        if err:
            return self._send(401, {"ok": False, "message": err})
        return self._send(200, {"ok": True, "user": name, "token": tok, "message": "已登录"})

    def _auth_logout(self):
        if not self._origin_ok():
            return self._send(403, {"ok": False, "message": "请求来源不被允许"})
        logout_user(self._bearer())
        return self._send(200, {"ok": True, "message": "已退出登录（本机自选保留）"})

    # ---- 自选 ----
    def _require_user(self):
        name, _u = user_by_token(self._bearer())
        if not name:
            self._send(401, {"ok": False, "message": "登录已失效，请重新登录"})
            return None
        return name

    def _watchlist_get(self):
        name = self._require_user()
        if not name:
            return
        codes, updated = get_codes(name)
        return self._send(200, {"ok": True, "user": name, "codes": codes, "updated": updated})

    def _watchlist_put(self):
        name = self._require_user()
        if not name:
            return
        b = self._json_body() or {}
        if not isinstance(b.get("codes"), list):
            return self._send(400, {"ok": False, "message": "codes 必须是数组"})
        codes, updated = put_codes(name, b["codes"])
        if codes is None:
            return self._send(401, {"ok": False, "message": "登录已失效，请重新登录"})
        return self._send(200, {"ok": True, "user": name, "codes": codes, "updated": updated})


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    ok, why = in_window()
    print("api 监听 127.0.0.1:%d ｜ 交易时段: %s%s ｜ 刷新限流 %ds / %d 次每小时 ｜ 自选账号: %s"
          % (PORT, ok, ("（" + why + "）") if not ok else "",
             MIN_INTERVAL, MAX_PER_HOUR,
             ("%d 个" % len(_load_users())) if USERS_FILE.exists() else "尚未创建"), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
