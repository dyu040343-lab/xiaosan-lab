#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A 股交易日历生成器
──────────────────────────────────────────────────────────────
数据来源（两个独立来源交叉校验）：
  1. 深圳证券交易所交易日历接口
     GET https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=YYYY-MM
     → 该月全部交易日（含未来已公布月份）★ 主来源
  2. 上证指数日 K 线（东方财富 push2his）
     → 只覆盖已经过去的日期，用来反向校验主来源（缺 K 线的工作日 = 休市）

产出：data/trading_calendar.json
  {
    "version": 1,
    "updated": "2026-09-15",
    "source": "...",
    "rule": "周六周日一律休市（含调休补班的周末）；closed_days 只列额外休市的工作日",
    "years_covered": [2026],
    "closed_days": ["2026-01-01", ...]
  }

用法：
  python3 scripts/update_calendar.py                # 刷新「今年 + 明年」，明年未公布时自动跳过
  python3 scripts/update_calendar.py --years 2026 2027
  python3 scripts/update_calendar.py --verify-only  # 不写文件，只打印与上证指数 K 线的差异

⚠️ 每年 12 月交易所公布次年休市安排后，跑一次这个脚本即可（也可以顺手跑在 CI 里）。
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import date, timedelta

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "trading_calendar.json")

SZSE_URL = ("https://www.szse.cn/api/report/exchange/onepersistenthour/monthList"
            "?month={month}&random=0.{rand}")
SZSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://www.szse.cn/marketServices/deal/period/index.html",
    "Accept": "application/json, text/plain, */*",
}
KLINE_URL = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000001"
             "&fields1=f1,f2,f3&fields2=f51,f52&klt=101&fqt=1&beg={beg}&end={end}&lmt=700")
# ⚠️ lmt 别调大：实测 lmt=1000 会被东财直接掐断连接（RemoteDisconnected），700 稳定
KLINE_HEADERS = {
    "User-Agent": SZSE_HEADERS["User-Agent"],
    "Referer": "https://quote.eastmoney.com/",
}


def http_json(url, headers, tries=4, timeout=25):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:                       # 深交所偶发 SSL EOF，重试即可
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def szse_month(month_str, rand=98765):
    """返回该月交易日列表 ['2026-10-08', ...]；空列表 = 该月未公布/无数据"""
    d = http_json(SZSE_URL.format(month=month_str, rand=rand), SZSE_HEADERS)
    rows = d.get("data") or []
    return [r.get("jyrq") for r in rows if str(r.get("jybz", "1")) == "1" and r.get("jyrq")]


def szse_year(year):
    days, missing = [], []
    for m in range(1, 13):
        mm = f"{year}-{m:02d}"
        got = szse_month(mm, rand=98765 + m)
        if not got:
            missing.append(mm)
        days += got
    return sorted(set(days)), missing


def index_days(beg, end):
    """上证指数日 K 线日期 = 实际交易日（只覆盖已过去的日期）"""
    d = http_json(KLINE_URL.format(beg=beg, end=end), KLINE_HEADERS)
    kl = (d.get("data") or {}).get("klines") or []
    return sorted(x.split(",")[0] for x in kl)


def weekdays_between(d0, d1):
    cur, out = d0, []
    while cur <= d1:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def main():
    ap = argparse.ArgumentParser()
    this_year = date.today().year
    ap.add_argument("--years", nargs="*", type=int, default=[this_year, this_year + 1])
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    all_closed, covered, report = [], [], []
    for y in sorted(set(args.years)):
        days, missing = szse_year(y)
        print(f"[深交所] {y} 年交易日 {len(days)} 天（{len(missing)} 个月未公布）")
        if len(missing) >= 6:            # 一个都没公布（如次年年中前）：整年跳过
            print(f"  ↳ {y} 年尚未公布，跳过")
            continue
        closed = [d for d in weekdays_between(date(y, 1, 1), date(y, 12, 31)) if d not in set(days)]
        print(f"  ↳ 额外休市的工作日 {len(closed)} 天: {' '.join(closed)}")
        all_closed += closed
        covered.append(y)

    if not covered:
        print("❌ 没有拿到任何已公布年份，放弃写入")
        return 1

    # ── 与上证指数 K 线交叉校验（仅已过去的日期）──
    past_end = date.today().isoformat()
    past_beg = f"{min(covered)}-01-01"
    try:
        real = set(index_days(past_beg.replace("-", ""), past_end.replace("-", "")))
        real = set() if not real else {d for d in real if len(d) == 10}
        if real:
            print(f"[上证指数] 实际交易日 {len(real)} 天（{past_beg} ~ {past_end}）")
            mismatch = []
            for d in all_closed:
                if d > past_end:
                    continue
                if str(d) in real:                       # 日历说休市，K 线却有 → 日历错
                    mismatch.append(("日历称休市但实际有交易", d))
            for d in weekdays_between(date.fromisoformat(past_beg), date.fromisoformat(past_end)):
                if d not in real and d not in set(all_closed):   # K 线没有但日历称交易 → 日历漏了
                    mismatch.append(("日历漏了休市日", d))
            if mismatch:
                print("⚠️ 与上证指数 K 线不一致：")
                for why, d in mismatch:
                    print(f"   - {why}: {d}")
            else:
                print("✅ 与上证指数 K 线完全一致（已过去的日期）")
        else:
            mismatch = []
            print("ℹ️ 拿不到 K 线，跳过交叉校验")
    except Exception as e:
        print(f"ℹ️ 交叉校验失败（{type(e).__name__}），跳过：{e}")

    out = {
        "version": 1,
        "updated": date.today().isoformat(),
        "source": "深圳证券交易所交易日历接口 + 上证指数日K交叉校验",
        "rule": "周六周日一律休市（含调休补班的周末）；closed_days 只列额外休市的【工作日】",
        "years_covered": sorted(covered),
        "closed_days": sorted(set(all_closed)),
    }
    if args.verify_only:
        print("--verify-only：不写文件")
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"✅ 已写入 {OUT}")
    print(f"   覆盖年份 {out['years_covered']}，休市工作日 {len(out['closed_days'])} 天")
    return 0


if __name__ == "__main__":
    sys.exit(main())
