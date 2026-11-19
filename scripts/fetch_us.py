#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股 · AI 版图 数据抓取（腾讯行情源）

为什么用腾讯而不是东财：
- 东财 push2 的 ulist 一次只能拼几十个 secid，且连续打会被整站 502；
  腾讯 qt.gtimg.cn 一次 GET 能拿 70 只，字段里还带 52 周高低（判断「是不是低位」要用）。
- 东财 push2his 对美股 K 线不返回数据，拿不到 52 周区间。

字段（`~` 分隔，索引从 0 开始）：
  1 中文名 / 3 现价 / 4 昨收 / 30 时间 / 31 涨跌额 / 32 涨跌幅 / 33 当日高 / 34 当日低
  39 PE / 44 流通市值(亿) / 45 总市值(亿) / 46 英文名 / 48 52周最高 / 49 52周最低

用法：
  python3 scripts/fetch_us.py
  python3 scripts/fetch_us.py --dry-run
"""
import json
import os
import sys
import time
import tempfile
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("需要 requests")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(DATA, "us_ai.json")

QT = "https://qt.gtimg.cn/q="
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}

# 上游 → 下游，8 个环节。hard=True 计入「AI 硬件层」。
# 一只股票只归一组，避免同一份市值被算两遍。
POOL = [
    ("chip",   "算力芯片",     True,
     ["NVDA", "AVGO", "AMD", "MU", "INTC", "TXN", "ARM", "ADI", "QCOM", "MRVL",
      "SNDK", "STX", "WDC", "MPWR", "ALAB", "CRDO"]),
    ("equip",  "晶圆制造与设备", True,
     ["TSM", "ASML", "AMAT", "LRCX", "KLAC", "CDNS", "SNPS", "UMC", "GFS", "AMKR"]),
    ("net",    "网络与光模块",  True,
     ["ANET", "LITE", "COHR", "CIEN", "FN", "AAOI"]),
    ("hw",     "服务器与存储",  True,
     ["DELL", "HPE", "NTAP", "PSTG", "SMCI"]),
    ("infra",  "数据中心与电力", True,
     ["GEV", "NEE", "ETN", "EQIX", "CEG", "PWR", "VRT", "DLR", "VST", "TLN", "MOD", "SMR"]),
    ("cloud",  "云与算力平台",  False,
     ["MSFT", "GOOGL", "AMZN", "META", "ORCL", "NBIS", "CRWV", "IREN", "APLD"]),
    ("soft",   "AI 软件",      False,
     ["PLTR", "CRM", "SNOW", "NOW", "ADBE", "DDOG", "MDB"]),
    ("device", "终端与应用",    False,
     ["AAPL", "TSLA", "ISRG", "APP", "PATH"]),
]

# 腾讯部分美股只给英文全名，压成短名（页面一行放不下）
NAME_FIX = {
    "NVDA": "英伟达", "AAPL": "苹果", "MSFT": "微软", "GOOGL": "谷歌-A", "AMZN": "亚马逊",
    "META": "Meta", "TSLA": "特斯拉", "AVGO": "博通", "AMD": "超威半导体", "MU": "美光",
    "SNDK": "闪迪", "WDC": "西部数据", "STX": "希捷", "QCOM": "高通", "INTC": "英特尔",
    "TXN": "德州仪器", "ADI": "亚德诺", "ARM": "安谋", "MRVL": "迈威尔", "MPWR": "美国芯源",
    "ALAB": "Astera Labs", "CRDO": "Credo", "TSM": "台积电", "UMC": "联电", "GFS": "格芯",
    "ASML": "阿斯麦", "AMAT": "应用材料", "LRCX": "泛林", "KLAC": "科磊",
    "SNPS": "新思科技", "CDNS": "铿腾电子", "AMKR": "艾马克",
    "ANET": "Arista", "CIEN": "Ciena", "COHR": "Coherent", "LITE": "Lumentum", "FN": "Fabrinet",
    "AAOI": "应用光电", "SMCI": "超微电脑", "DELL": "戴尔", "HPE": "慧与",
    "PSTG": "Pure Storage", "NTAP": "美国网存", "VRT": "维谛", "ETN": "伊顿", "PWR": "广达服务",
    "CEG": "星座能源", "VST": "瑞致达", "TLN": "Talen", "GEV": "GE Vernova",
    "NEE": "新纪元能源", "DLR": "数字房地产", "EQIX": "易昆尼克斯", "MOD": "摩丁制造",
    "SMR": "NuScale", "ORCL": "甲骨文", "NBIS": "Nebius", "CRWV": "CoreWeave",
    "IREN": "IREN", "APLD": "Applied Digital", "PLTR": "Palantir", "CRM": "赛富时",
    "SNOW": "Snowflake", "NOW": "ServiceNow", "ADBE": "Adobe", "DDOG": "Datadog",
    "MDB": "MongoDB", "ISRG": "直觉外科", "APP": "AppLovin", "PATH": "UiPath",
}


def clean_name(code, raw):
    """优先用固定短名；纯英文超长就截断，中文名保留。"""
    if code in NAME_FIX:
        return NAME_FIX[code]
    s = (raw or code).strip()
    if len(s) > 18 and all(ord(c) < 128 for c in s):
        return s[:16] + "…"
    return s

BJ = timezone(timedelta(hours=8))
CHUNK = 35          # 单次 URL 里放多少只（70 只分两次，URL 不至于过长）
MIN_OK = 55         # 少于这个数视为整次失败，不覆盖旧文件


def us_session(now_bj):
    """(最近收盘日, 是否盘中)。美东 9:30–16:00。"""
    offset = -4 if 3 <= now_bj.month <= 10 else -5
    et = now_bj.astimezone(timezone(timedelta(hours=offset)))
    live = et.weekday() < 5 and (9, 30) <= (et.hour, et.minute) < (16, 0)
    d = et.date()
    if (et.hour, et.minute) < (16, 0):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat(), live


def parse_qt(raw, out):
    """解析 v_usXXX="..." 行，写入 out。"""
    for line in raw.strip().split("\n"):
        if '="' not in line:
            continue
        code = line.split("=")[0].replace("v_us", "").strip()
        body = line.split('="', 1)[1].rstrip('";')
        f = body.split("~")
        if len(f) < 50 or not f[3]:
            continue
        try:
            price = float(f[3])
            mcap = float(f[45] or 0) * 1e8
            if price <= 0 or mcap <= 0:
                continue
            hi52 = float(f[48] or 0)
            lo52 = float(f[49] or 0)
            out[code] = {
                "code": code,
                "name": clean_name(code, f[1] or f[46]),
                "price": price,
                "chg": round(float(f[32] or 0), 2),
                "pe": round(float(f[39] or 0), 2),
                "mcap": mcap,
                "hi52": hi52 or None,
                "lo52": lo52 or None,
                "dd": round((price - hi52) / hi52 * 100, 1) if hi52 else None,
            }
        except (ValueError, IndexError):
            continue


def fetch_chunk(codes, tries=4):
    q = ",".join("us" + c for c in codes)
    for k in range(tries):
        try:
            # 腾讯国内直连，显式绕开 socks 代理（代理是给东财用的）
            s = requests.Session()
            s.trust_env = False
            r = s.get(QT + q, headers=HEADERS, timeout=25)
            if r.status_code == 200:
                r.encoding = "gbk"
                return r.text
            print(f"    HTTP {r.status_code}，重试 {k + 1}/{tries}")
        except Exception as e:
            print(f"    {type(e).__name__}: {e}，重试 {k + 1}/{tries}")
        time.sleep(2 + 2 * k)
    return None


def atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.chmod(tmp, 0o644)        # mkstemp 默认 600，nginx 读不到
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def main():
    dry = "--dry-run" in sys.argv
    all_codes = [c for _, _, _, cs in POOL for c in cs]
    print(f"[pool] {len(all_codes)} 只 / {len(POOL)} 个环节")

    got = {}
    for i in range(0, len(all_codes), CHUNK):
        chunk = all_codes[i:i + CHUNK]
        raw = fetch_chunk(chunk)
        if not raw:
            print(f"[chunk {i // CHUNK + 1}] 失败")
            continue
        before = len(got)
        parse_qt(raw, got)
        print(f"[chunk {i // CHUNK + 1}] {len(got) - before}/{len(chunk)}")
        time.sleep(1)

    miss = [c for c in all_codes if c not in got]
    print(f"[fetch] 命中 {len(got)} / {len(all_codes)}，缺 {miss}")

    prev = load_json(OUT, {})
    prev_items = {x["code"]: x for x in prev.get("items", [])}
    stale = []
    for c in all_codes:
        if c in got:
            continue
        if c in prev_items:
            got[c] = dict(prev_items[c])
            stale.append(c)

    if len(got) - len(stale) < MIN_OK:
        print(f"[abort] 只抓到 {len(got) - len(stale)} 只，低于阈值 {MIN_OK}，保留旧文件")
        return 2

    now = datetime.now(BJ)
    us_date, live = us_session(now)
    out = {
        "fetched_at": now.isoformat(timespec="seconds"),
        "us_date": us_date,
        "live": live,
        "source": "tencent",
        "stale": stale,
        "groups": [{"key": k, "name": n, "hard": h, "codes": cs} for k, n, h, cs in POOL],
        "items": [got[c] for c in all_codes if c in got],
    }

    if dry:
        print(json.dumps(out, ensure_ascii=False, indent=1)[:900])
        return 0

    atomic_write(OUT, out)
    tot = sum(x["mcap"] for x in out["items"] if x["code"] not in stale)
    nv = got.get("NVDA", {}).get("mcap")
    print(f"[done] {len(out['items'])} 只，合计 ${tot / 1e12:.2f}T，"
          f"NVDA {nv / tot * 100:.1f}%，NVDA 距 52 周高 {got.get('NVDA', {}).get('dd')}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
