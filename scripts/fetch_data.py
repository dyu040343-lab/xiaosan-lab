#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小散研究院 - 数据抓取脚本 v3
数据源：东方财富 push2 行情接口（四档资金流：超大单/大单/中单/小单，均为净额）
散户 = 小单净额，主力 = 超大单 + 大单（东财 f62 口径）
"""

import json
import os
import re
import sys
import time
import requests
from datetime import datetime, timedelta, timezone

OUTPUT_DIR = "data"
CACHE_FILE = f"{OUTPUT_DIR}/radar_data_cache.json"
# 完整快照的轮转备份。只在**完整**抓取成功时轮转，所以备份里永远是完整快照。
CACHE_BAKS = [f"{OUTPUT_DIR}/radar_data_cache.bak1.json",
              f"{OUTPUT_DIR}/radar_data_cache.bak2.json"]

# ── 完成度 / 缓存健康度闸门 ────────────────────────────────────────
# 🩸 2026-09-23 事故：原判定是 `len(stocks) > 50` 就返回 "live"，而 live 会 save_cache()。
#    于是"抓到 500 条"被当成"全市场"，把 5918 条的好缓存逐次覆盖成 500 → 200 条，
#    全站数据**永久损毁**且无备份可回滚。
#    铁律：**只有完整抓取才允许写缓存；不完整的数据绝不进缓存。**
FULL_MIN_RATIO = 0.98        # 唯一代码数 ≥ 接口 total 的 98% 才算「完整」
PARTIAL_MIN_CODES = 500      # 抓到 ≥500 只才算「部分数据」，低于此视同抓取失败
CACHE_HEALTHY_MIN = 3000     # 快照行数 ≥ 此值才算「健康快照」，可顶替本次不完整抓取

# 最近一次抓取的诊断信息（供 data/health.json 与结尾汇总打印，不参与业务逻辑）
LAST_FETCH_INFO = {}

# push2 行情主机候选，按优先级排列。
# ⚠️ 老实说：push2.eastmoney.com 与 push2delay.eastmoney.com 现在解析到**同一批 IP**
#    （119.3.232.150 / 120.79.191.232 / 120.76.218.228），所谓"延迟镜像"**并没有真正冗余** ——
#    2026-09-23 两个域名一起 502 就是证据。留多候选只是为了容忍单域名/单节点故障，
#    真正的冗余靠 fetch_from_ulist() 这条**不同接口**的通道（见 P2）。
PUSH2_HOSTS = [
    "https://push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
]

# 东方财富第 1 页请求用的筛选条件与字段（clist / ulist 共用同一套字段语义）
EM_FS = "m:0 t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
EM_FIELDS = ("f12,f14,f2,f3,f6,f8,f10,f24,f25,f26,f62,f184,f66,f69,f72,f75,"
             "f78,f81,f84,f87,f100,f124,f38,f39")

# ── 备用通道：ulist.np（**不同接口**，见 fetch_from_ulist 的说明）──────────
ULIST_PATH = "/api/qt/ulist.np/get"
ULIST_BATCH = 200      # 每次请求带多少个证券代码（实测 200 可用；400 开始不稳）
ULIST_MAX_REQ = 70     # 单次抓取最多打多少个请求（200×70 ≈14000，兜住全市场）

# ── 限流容错：跨运行累积 ────────────────────────────────────────────
# 🩸 2026-09-24 实测：上游被限流后会「能用约 5 次 → 断约 22 秒 → 又能用」，
#    按 100 只/页要打 60 页，**一次运行根本打不完**（原来只重试 3×2=6 次就放弃，
#    所以每次只拿到 1500~2200 只、永远判不完整）。
#    现在：① 页级无限重试（带退避）② 进度**跨多次 cron 运行累积**——
#    每成功一页就落盘，下一次接着补，拼齐后才算 complete。
FETCH_BUDGET_S = 300      # 单次抓取的时间预算（秒）。cron 10 分钟一跑，留足余量
PAGE_GAP_S = 0.6          # 成功页之间的间隔，别把上游打急
RETRY_GAP_S = 3.0         # 失败后的退避（上游窗口约 22 秒断一次，3 秒足够探到开口）
STALL_LIMIT = 200         # 连续失败上限（兜底防死循环；正常会被时间预算先打断）
# 🩸 2026-11-06：香港出口 IP 被东财 push2 网关拦（三个 IP 全 502），cron 仍每 10 分钟
#    打 60 页 ×3 重试 = 一天几千次无效请求，只会把「限流」升级成「封 IP」。
#    5xx 是服务端故障（不是限流），连续这么多次就说明整站拒绝本机，立刻停手。
SERVER_DOWN_LIMIT = 8     # 连续 5xx 次数上限，超过本轮直接收工
EMPTY_STREAK_LIMIT = 3    # 连续空页上限：说明已翻过末页
ACC_FILE = f"{OUTPUT_DIR}/fetch_accum.json"   # 当日抓取累积器
_CLI_BUDGET = None        # `--budget N` 覆盖本次抓取的时间预算

# ── 交易时段闸门 ────────────────────────────────────────────────
# 北京时间（固定 UTC+8，不依赖服务器时区）：
#   上午 9:15–11:35（含集合竞价 9:15 与午间收尾）
#   下午 12:55–15:35（含 13:00 开盘与 15:20 收盘后定格当天最终数据）
# 开市日由 data/trading_calendar.json 决定（周末 + 法定节假日），
# 非开市日 / 非时段直接跳过、不写文件（加 --force 可强制抓取）。
# 日历用 scripts/update_calendar.py 生成，每年 12 月交易所公布次年安排后跑一次。
BJ_TZ = timezone(timedelta(hours=8))
TRADING_WINDOWS = ((9 * 60 + 15, 11 * 60 + 35), (12 * 60 + 55, 15 * 60 + 35))
CALENDAR_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "trading_calendar.json")

_CAL = None


def load_trading_calendar():
    """读取交易日历，返回 {'closed': set, 'years': set}；失败返回 None"""
    try:
        with open(CALENDAR_FILE, encoding="utf-8") as f:
            cal = json.load(f)
        return {"closed": set(cal.get("closed_days") or []),
                "years": set(cal.get("years_covered") or [])}
    except Exception as e:
        print(f"  ⚠️ 交易日历读取失败（{type(e).__name__}）：{CALENDAR_FILE}")
        print("     ↳ 退回「只看工作日」，请跑 python3 scripts/update_calendar.py")
        return None


def is_trading_day(d=None, cal=None):
    """今天是否开市：周末一律休市（含调休补班的周末）+ 日历中的法定节假日。
    日历缺失或未覆盖该年份时**保守返回 True**（宁可真跑一次，数据不变也无害）"""
    d = d or datetime.now(BJ_TZ).date()
    if d.weekday() >= 5:                      # 周六 / 周日
        return False
    cal = _CAL if cal is None else cal
    if cal and d.year in cal["years"]:
        return d.isoformat() not in cal["closed"]
    return True


def in_trading_window(now=None):
    """是否处于 A 股交易时段（开市日 + 时间窗）"""
    now = now or datetime.now(BJ_TZ)
    if not is_trading_day(now.date()):
        return False
    minutes = now.hour * 60 + now.minute
    return any(start <= minutes <= end for start, end in TRADING_WINDOWS)


EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}

# 东方财富 push2 请求头（模拟浏览器）
EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/bkzj/hy.html",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _snapshot_rows(d):
    """快照里的个股行数；不是合法快照则返回 0"""
    return len(d.get("retail_flow") or []) if isinstance(d, dict) else 0


def load_best_cache():
    """在主缓存 + 备份里挑**行数最多**的一份，返回 (行数, 数据, 路径)；都没有返回 None。
    为什么不是直接读主缓存：主缓存可能被历史 bug 写成很小的一份（2026-09-23 就是这样），
    从备份里捞回完整快照才是救命的。"""
    best = None
    for p in [CACHE_FILE] + CACHE_BAKS:
        d = _read_json(p)
        n = _snapshot_rows(d)
        if n and (best is None or n > best[0]):
            best = (n, d, p)
    return best


def load_cache():
    """读缓存：优先主缓存，其次备份；都没有返回 None"""
    best = load_best_cache()
    if not best:
        return None
    n, d, p = best
    if p != CACHE_FILE:
        print(f"  ⚠️ 主缓存不可用，改用备份快照 {os.path.basename(p)}（{n} 条）")
    return d


def save_cache(data):
    """保存缓存。**只允许在完整抓取成功时调用**（见 fetch_retail_money_flow）。

    先写临时文件、再轮转旧快照、最后原子替换：
    任何一步失败都不会让"唯一的好快照"消失。"""
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:                                   # 轮转：bak1 → bak2，旧 cache → bak1
            if os.path.exists(CACHE_BAKS[1]):
                os.remove(CACHE_BAKS[1])
            for src, dst in ((CACHE_BAKS[0], CACHE_BAKS[1]),
                             (CACHE_FILE, CACHE_BAKS[0])):
                if os.path.exists(src):
                    os.replace(src, dst)
        except Exception:
            pass                               # 轮转失败不影响本次缓存写入
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def guess_sector(name, code):
    sectors = {
        '半导体': ['半导体', '芯片', '封测', '集成', '微电'],
        'PCB/算力': ['PCB', '电路', '算力', '服务', '计算机'],
        '面板/显示': ['面板', '显示', '光电', '玻璃'],
        '光通信': ['光纤', '光模块', '通信', '光缆'],
        '新能源': ['新能', '锂电', '光伏', '电池', '充电'],
        '医药': ['药', '医疗', '生物', '医药'],
        '金融': ['银行', '证券', '保险', '金融'],
        '化工': ['化工', '化学', '氟', '材料'],
        '消费': ['酒', '食品', '乳', '家电', '百货'],
        '地产': ['地产', '万科', '保利', '发展'],
    }
    for sector, keywords in sectors.items():
        if any(kw in name for kw in keywords):
            return sector
    if code.startswith('6013') or code.startswith('6000'):
        return '金融'
    if code.startswith('300'):
        return '科技'
    return '综合'


def to_float(val):
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _fetch_page(session, base_url, params, path="/api/qt/clist/get"):
    """请求单页数据，失败时输出详细诊断信息"""
    resp = session.get(base_url + path, params=params, headers=EM_HEADERS, timeout=15)
    resp.encoding = "utf-8"
    if resp.status_code != 200:
        print(f"  ⚠️ HTTP {resp.status_code} 来自 {base_url}，响应头200字节: {resp.text[:200]}")
        resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or data.get("data") is None:
        print(f"  ⚠️ {base_url} 返回非数据内容: {str(data)[:200]}")
        raise ValueError("API返回无数据（可能被反爬拦截）")
    return data


# 备用通道的诊断信息（fetch_from_eastmoney 填，fetch_retail_money_flow 并入 LAST_FETCH_INFO）
FETCH_DIAG = {}


def _secid(code):
    """证券代码 → 东财 secid（1 = 沪市，0 = 深市 / 北交所）。

    ⚠️ 不能只看首位数字：**900xxx 是沪市 B 股（1.）而 920xxx 是北交所（0.）**，
       都是 9 开头。北交所另有 43xxxx / 83xxxx / 87xxxx，也是 0.。
    """
    if code[:3] == "920" or code[:2] in ("43", "83", "87"):
        return "0." + code
    return ("1." if code[:1] in ("6", "9") else "0.") + code


def _load_accum():
    """当日抓取累积器：跨多次运行把全市场拼齐（上游被限流时一次跑不完）。

    结构：{"date","pages":[已成功页],"items":{code:item},"expected_total","first_ts","last_ts"}
    跨日自动作废 —— 昨天的进度对今天没用。
    """
    today = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    d = _read_json(ACC_FILE)
    if isinstance(d, dict) and d.get("date") == today:
        return {"date": today,
                "pages": set(d.get("pages") or []),
                "items": d.get("items") or {},
                "expected_total": d.get("expected_total") or 0,
                "first_ts": d.get("first_ts"),
                "last_ts": d.get("last_ts")}
    return {"date": today, "pages": set(), "items": {}, "expected_total": 0,
            "first_ts": None, "last_ts": None}


def _save_accum(acc):
    """先写临时文件再原子替换 —— 抓取中途被打断也不能把进度写坏"""
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        tmp = ACC_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": acc["date"], "pages": sorted(acc["pages"]),
                       "items": acc["items"],
                       "expected_total": acc["expected_total"],
                       "first_ts": acc["first_ts"], "last_ts": acc["last_ts"]},
                      f, ensure_ascii=False)
        os.replace(tmp, ACC_FILE)
    except Exception:
        pass


def _clear_accum():
    try:
        if os.path.exists(ACC_FILE):
            os.remove(ACC_FILE)
    except Exception:
        pass


def _clist_params(page):
    """clist 分页参数（fid=f12 与盘中变化无关，见下方注释）"""
    return {
        "pn": page,
        # ⚠️ 实测服务端会把 pz 压到 100（传 5000 也只回 100）→ 全市场要打 60 页
        "pz": 5000,
        "po": 0,          # 0 = 升序
        "np": 1,
        "fltt": 2,
        "invt": 2,
        # 🩸 fid 必须用**与盘中变化无关**的字段排序（f12 = 证券代码）。
        #    原来用 f62（主力净额）：盘中每次请求之间排序都在变，
        #    60 页打下来的边界漂移 = 有的票重复抓、有的票整只漏掉。
        #    2026-09-18 实测：5917 行里 183 个代码重复、180 只整只漏掉，
        #    用户报的「凯盛科技 600552 没数据」就是被漏掉的其中之一。
        "fid": "f12",
        "fs": EM_FS,
        "fields": EM_FIELDS,
    }


def known_codes():
    """已知的全市场代码表：取最健康的那份快照。备用通道拿它当输入。"""
    best = load_best_cache()
    if not best:
        return []
    seen, out = set(), []
    for r in best[1].get("retail_flow") or []:
        c = str(r.get("code") or "").strip()
        if len(c) == 6 and c.isdigit() and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def fetch_from_ulist(session, codes, deadline=None):
    """备用通道：用 `ulist.np/get` 分批查已知代码。

    🩸 为什么值得存在：`clist/get` 与 `ulist.np/get` 是**两个不同的接口**。
       2026-09-23 实测 `clist/get` 全线 502 时，同一台服务器上的
       `ulist.np/get`、`stock/get` 仍然返回 200 —— 所以这是同源之内唯一有意义的降级通道。
       （反之，push2 与 push2delay 已解析到同一批 IP，"延迟镜像"其实**没有**冗余。）
       另：`ulist.np` 一次能带 **200 只**，比 clist 的 100 只/页少一半请求数。

    ⚠️ 局限（必须让调用方知道）：
       · 只能刷新**已知代码**，发现不了当天新上市的票 → 结果一律算**不完整**、不写缓存
       · 代码表来自快照；没有任何快照时无法工作

    deadline：绝对时间戳；到点就收工（配合跨运行累积，下次接着补）。
    """
    items, ok_batches, fail, stall = [], 0, 0, 0
    limit = min(len(codes), ULIST_BATCH * ULIST_MAX_REQ)
    for i in range(0, limit, ULIST_BATCH):
        if deadline and time.time() >= deadline:
            print(f"  ⏱️ [ulist] 时间预算用尽，停在 {ok_batches} 批")
            break
        chunk = codes[i:i + ULIST_BATCH]
        params = {"fltt": 2, "invt": 2, "fields": EM_FIELDS,
                  "secids": ",".join(_secid(c) for c in chunk)}
        try:
            data = _fetch_page(session, PUSH2_HOSTS[0], params, path=ULIST_PATH)
            diff = data["data"].get("diff") or []
            items.extend(diff)
            ok_batches += 1
            stall = 0
            print(f"  🔁 [ulist] 第{i // ULIST_BATCH + 1}批: {len(diff)} 只（累计 {len(items)}）")
            time.sleep(PAGE_GAP_S)
        except Exception as e:
            fail += 1
            stall += 1
            if stall <= 2:
                print(f"  ⚠️ [ulist] 第{i // ULIST_BATCH + 1}批失败: {str(e)[:80]}")
            if stall >= 20 and ok_batches == 0:
                print("  ⚠️ [ulist] 连续 20 批全失败，放弃备用通道")
                break
            if stall >= STALL_LIMIT:
                print("  ⚠️ [ulist] 连续失败过多，收工（进度已存）")
                break
            time.sleep(RETRY_GAP_S)
    print(f"  🔁 [ulist] 结束：{len(items)} 只（成功 {ok_batches} 批 / 失败 {fail} 批）")
    return items


def fetch_from_eastmoney(budget_s=None):
    """东方财富 push2 API - 四档完整数据

    返回 **(stocks, complete, expected_total)**：
      - stocks         解析后的个股列表（可能不完整）
      - complete       **是否取到全市场**（唯一代码数 ≥ total×FULL_MIN_RATIO）
      - expected_total 接口声明的全市场只数（拿不到时为 0）

    🩸 2026-09-23 前这个函数把 `complete` 只用来打印警告、**没有 return 出来**，
       调用方只能靠 `len(stocks) > 50` 猜完成度 → 部分数据被当成全市场并覆盖了缓存。
       调用方**必须**看 complete，不许再看行数。

    🩸 2026-09-24：上游被限流后一次运行打不完 60 页（原来重试 6 次就放弃，永远不完整）。
       现改为**跨多次运行累积**（`data/fetch_accum.json`）+ 页级重试，
       用 `budget_s` 控制单次时间预算（cron 用默认，手动刷新传小一点）。
    """
    print("🔍 [数据源] 东方财富 push2 API（四档资金流）...")

    global FETCH_DIAG
    FETCH_DIAG = {}

    t_end = time.time() + (budget_s or FETCH_BUDGET_S)
    session = requests.Session()

    acc = _load_accum()
    expected_total = acc["expected_total"] or 0
    have = len(acc["items"])

    # 上次累积已经拼齐 → 丢掉它，本次从头抓一遍新鲜的。
    # （否则一整天都在用早上抓的旧页；万一这次抓不成，还有 cache 兜底）
    if expected_total and have >= expected_total * FULL_MIN_RATIO:
        print(f"  ♻️ 上次累积已完整（{have} 只），本次重新抓一遍新鲜的")
        _clear_accum()
        acc = _load_accum()
        expected_total = 0
        have = 0
    elif have:
        print(f"  ♻️ 复用当日累积：{have} 只 / 已完成 {len(acc['pages'])} 页"
              f"［{acc['first_ts']} ~ {acc['last_ts']}］")

    host_i = 0
    total_pages = 0
    page = 1
    stall = 0
    srv_fail = 0
    upstream_down = False
    empty_streak = 0

    while time.time() < t_end:
        if total_pages and page > total_pages:
            break
        if empty_streak >= EMPTY_STREAK_LIMIT:
            break
        if page in acc["pages"]:          # 这一页今天已经拿到过，跳过
            page += 1
            continue
        base_url = PUSH2_HOSTS[host_i % len(PUSH2_HOSTS)]
        try:
            data = _fetch_page(session, base_url, _clist_params(page))
            items = data["data"].get("diff") or []
            if not expected_total:
                expected_total = data["data"].get("total", 0) or 0
                if items and expected_total:
                    total_pages = (expected_total + len(items) - 1) // len(items)
                    print(f"  📄 全市场 {expected_total} 只 → 共 {total_pages} 页"
                          f"（服务端每页上限 {len(items)}）")
            # 按代码入字典 = 天然去重
            for it in items:
                c = str(it.get("f12", "")).strip()
                if c:
                    acc["items"][c] = it
            acc["pages"].add(page)
            acc["expected_total"] = expected_total
            ts = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
            if not acc["first_ts"]:
                acc["first_ts"] = ts
            acc["last_ts"] = ts
            _save_accum(acc)              # 每页都落盘：进度绝不丢
            print(f"  📄 第{page}/{total_pages or '?'}页: +{len(items)} 条"
                  f" → 累计 {len(acc['items'])} 只")
            page += 1
            stall = 0
            srv_fail = 0
            empty_streak = empty_streak + 1 if not items else 0
            time.sleep(PAGE_GAP_S)
        except Exception as e:
            stall += 1
            host_i += 1
            status = getattr(getattr(e, "response", None), "status_code", 0)
            srv_fail = srv_fail + 1 if status in (500, 502, 503, 504) else 0
            if stall <= 3:
                print(f"  ⚠️ 第{page}页失败（第{stall}次）: {str(e)[:80]}")
            elif stall % 20 == 0:
                print(f"  ⚠️ 已连续失败 {stall} 次，继续重试（进度已存）…")
            if srv_fail >= SERVER_DOWN_LIMIT:
                print(f"  🚨 上游连续 {srv_fail} 次 5xx（服务端故障，不是限流）"
                      f"—— 本机已被整站拒绝，本轮立刻停手，不再白打请求")
                upstream_down = True
                break
            if stall >= STALL_LIMIT:
                print("  ⚠️ 连续失败过多，本次先收工（进度已存，下次接着补）")
                break
            time.sleep(RETRY_GAP_S)

    # ── 还差多少？用备用通道 ulist.np 补（换接口，且一次能带 200 只）──
    # ⚠️ clist 完全拿不到时 expected_total 会是 0 —— 这种情况**也要**走 ulist，
    #    否则「换接口」这条降级通道在最需要它的时候反而不生效。
    got = len(acc["items"])
    short = (not expected_total) or (got < expected_total * FULL_MIN_RATIO)
    if upstream_down:
        # 同一个行情集群已经不收我们的请求了，ulist 也在这个集群里 —— 再打 70 批只是加刑
        print("  ⏭️ 跳过备用通道 ulist（与 clist 同一集群，此刻同样会被拒）")
    elif time.time() < t_end and short:
        codes = known_codes()
        missing = [c for c in codes if c not in acc["items"]]
        if missing:
            print(f"  🔁 还缺 {len(missing)} 只已知代码，用备用通道 ulist.np 补…")
            added = 0
            for it in fetch_from_ulist(session, missing, deadline=t_end):
                c = str(it.get("f12", "")).strip()
                if c and c not in acc["items"]:
                    acc["items"][c] = it
                    added += 1
            if added:
                acc["last_ts"] = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
                _save_accum(acc)
                print(f"  🔁 备用通道补充 {added} 只 → 累计 {len(acc['items'])} 只")
        else:
            print("  ℹ️ 没有可用快照提供代码表，跳过备用通道")

    # 按代码升序还原成列表（与 f12 排序一致，输出稳定）
    all_stocks = [acc["items"][c] for c in sorted(acc["items"])]
    complete = bool(expected_total) and len(all_stocks) >= expected_total * FULL_MIN_RATIO

    FETCH_DIAG["pages_done"] = len(acc["pages"])
    FETCH_DIAG["expected_total"] = expected_total
    FETCH_DIAG["accum_rows"] = len(all_stocks)
    if acc["first_ts"] and acc["last_ts"] and acc["first_ts"] != acc["last_ts"]:
        FETCH_DIAG["accum_window"] = f"{acc['first_ts']} ~ {acc['last_ts']}"

    if not all_stocks:
        print("  📦 push2 全部域名都拿不到数据（进度已存，下次继续）")
        return [], False, expected_total
    if complete:
        print(f"  📊 东方财富返回 {len(all_stocks)}/{expected_total} 只（去重后唯一代码，完整）")
        if acc["first_ts"] != acc["last_ts"]:
            print(f"     ⚠️ 本次是跨多次运行拼接的：［{acc['first_ts']} ~ {acc['last_ts']}］"
                  f"（各股数据时点有差，但都在今天）")
        _clear_accum()                    # 拼齐了就清掉，下次运行重新抓新鲜的
    else:
        print(f"  ⚠️ 数据不完整：未能取得完整全市场数据，本次累计 {len(all_stocks)}"
              f"/{expected_total or '?'} 只（进度已存，下次运行继续补）")

    # 字段映射: f62=主力净额, f184=主力净占比, f66=超大单净额, f69=超大单净占比,
    #          f72=大单净额, f75=大单净占比, f78=中单净额, f81=中单净占比,
    #          f84=小单净额, f87=小单净占比, f124=5日涨跌, **f8=换手率(%)**,
    #          **f38=总股本, f39=流通A股（不含H股）** ← 这两个字段只在 clist/ulist 接口里是股本，
    #          在 stock/get 接口里 f84/f85 才是股本，语义随接口变，别混用
    #          ⚠️ f84 在本接口是「小单净额」，不是总股本
    stocks = []
    for item in all_stocks:
        code = str(item.get("f12", "")).strip()
        name = str(item.get("f14", "")).strip()
        if not code or not name:
            continue

        price = to_float(item.get("f2"))
        # fltt=2 时 f3 直接就是百分数（如 -4.07 表示 -4.07%），不做任何换算
        change_pct = to_float(item.get("f3"))

        super_net = to_float(item.get("f66"))
        large_net = to_float(item.get("f72"))
        medium_net = to_float(item.get("f78"))
        small_net = to_float(item.get("f84"))

        super_pct = to_float(item.get("f69"))
        large_pct = to_float(item.get("f75"))
        medium_pct = to_float(item.get("f81"))
        small_pct = to_float(item.get("f87"))

        yi = 100000000
        super_net_yi = round(super_net / yi, 4)
        large_net_yi = round(large_net / yi, 4)
        medium_net_yi = round(medium_net / yi, 4)
        small_net_yi = round(small_net / yi, 4)

        retail_net = small_net_yi
        # 主力净额直接用东财官方 f62（=超大单+大单），与东财页面口径一致
        main_net = round(to_float(item.get("f62")) / yi, 4)

        # 成交额：优先用东财直接给的 f6（元 → 亿元），反推只作兜底。
        # 为什么改（2026-09-15）：原实现是「f84 ÷ f87%」反推成交额，实测 378 只（6.4%）结果 = 0 ——
        # 散户净额小于 5000 元时被舍入成 0，0/x = 0，连贵州茅台都中招（f6 显示成交 9.94 亿、反推得 0），
        # 这些票会被下游「成交额 ≥5 亿」的流动性过滤直接踢出榜单。
        amount_yi = to_float(item.get("f6")) / yi
        if amount_yi > 0:
            total_amount = round(amount_yi, 2)
        elif small_pct != 0:
            total_amount = round(retail_net / (small_pct / 100), 2)
        elif super_pct != 0:
            total_amount = round(main_net / (super_pct / 100), 2)
        else:
            total_amount = 0

        # 动态占比：散户净额 / 个股总成交额（亿元）×100，保留正负号
        # 含义：散户净额在这只票当日总成交中的占比（>0 散户净买入主导，<0 散户净卖出主力主导）
        if total_amount > 0:
            dynamic_ratio = round(retail_net / total_amount * 100, 2)
        else:
            dynamic_ratio = 0

        # 换手率：东财官方 f8（%）。真实口径 = **成交量 ÷ 流通股本**
        # （所以下面用「成交额 ÷ 流通市值」现算只是近似：全市场中位差 0.01pp，
        #  但新股/暴涨暴跌股会明显偏 —— 2026-09-17 实测 N沈鼓 官方 79.62% vs 自算 49.66%，
        #  因为成交发生在不同价位，用收盘价做分母会失真。**以 f8 为准**。）
        # ⚠️ 别把 f8 和 f84 搞混：本接口里 f84 是「小单净额」
        turnover = round(to_float(item.get("f8")), 2)

        # ── 量价字段（2026-09-17 起，供「放量滞涨」榜使用）──────────────────
        #   f10 = 量比（当日每分钟均量 ÷ 前 5 日每分钟均量）→ 「相对自身」的放量倍数，
        #         比单看换手率更能识别「今天突然有资金在动」
        #   f24 = 60 日涨跌幅(%)   f25 = 年初至今涨跌幅(%) → 「价格涨没涨起来」
        #   f26 = 上市日期(YYYYMMDD) → 用来剔除新股（次新换手率天然极高，会污染换手率榜）
        # ⚠️ 这三个字段都只在 clist/ulist 接口里是量价，语义随接口变，别在 stock/get 上复用
        vol_ratio = round(to_float(item.get("f10")), 2)
        chg_60d = round(to_float(item.get("f24")), 2)
        chg_ytd = round(to_float(item.get("f25")), 2)
        ld_raw = str(item.get("f26") or "").strip()
        list_date = (f"{ld_raw[:4]}-{ld_raw[4:6]}-{ld_raw[6:8]}"
                     if len(ld_raw) == 8 and ld_raw.isdigit() else "")

        # f100 = 东财行业板块名称（真实行业），为空或 "-" 时退回关键词猜测
        sector = str(item.get("f100") or "").strip()
        if not sector or sector == "-":
            sector = guess_sector(name, code)

        stocks.append({
            "code": code,
            "name": name,
            "price": round(price, 2) if price else 0,
            "change_pct": round(change_pct, 2) if change_pct else 0,
            "retail_net": round(retail_net, 4),
            "main_net": round(main_net, 4),
            "super_net": super_net_yi,
            "large_net": large_net_yi,
            "medium_net": medium_net_yi,
            "small_net": small_net_yi,
            "super_pct": super_pct,
            "large_pct": large_pct,
            "medium_pct": medium_pct,
            "small_pct": small_pct,
            "total_amount": total_amount,
            "turnover": turnover,                        # 换手率（%，东财官方 f8）
            "vol_ratio": vol_ratio,                      # 量比（东财 f10）
            "chg_60d": chg_60d,                          # 60 日涨跌幅（%，东财 f24）
            "chg_ytd": chg_ytd,                          # 年初至今涨跌幅（%，东财 f25）
            "list_date": list_date,                      # 上市日期 YYYY-MM-DD（东财 f26）
            "dynamic_ratio": dynamic_ratio,
            "total_shares": to_float(item.get("f38")),   # 总股本（股）
            "circ_shares": to_float(item.get("f39")),    # 流通A股（股，不含H股）
            "sector": sector,
            "source": "eastmoney",
        })

    print(f"  ✅ 解析完成: {len(stocks)} 条")
    # 🩸 complete 必须传出去：调用方靠它决定能不能判 live / 能不能写缓存。
    #    只看行数会把"抓到 500 条"当成全市场（2026-09-23 全站数据被毁就是这么来的）。
    return stocks, complete, expected_total


def _pick_snapshot():
    """挑一份可用的完整快照，返回 (retail_flow, status) 或 None。

    行数 ≥ CACHE_HEALTHY_MIN 才算「健康快照」→ status='cached'；
    否则说明快照本身已被写坏 → 降级为 'partial'（前端会显式提示，不再假装正常）。
    """
    best = load_best_cache()
    if not best:
        return None
    n, cache, path = best
    healthy = n >= CACHE_HEALTHY_MIN
    print(f"  📦 选用快照 {os.path.basename(path)}：{n} 条 "
          f"[缓存于 {cache.get('last_updated')}]"
          + ("" if healthy else "  ← ⚠️ 行数偏少，该快照可能已被写坏"))
    # 快照的**真实数据时间**必须带出去：否则界面会把 3 天前的数据显示成"刚刚更新"
    LAST_FETCH_INFO["snapshot_time"] = cache.get("last_updated")
    LAST_FETCH_INFO["snapshot_rows"] = n
    return cache["retail_flow"], ("cached" if healthy else "partial")


def label_for(data_status, snapshot_time=None):
    """状态徽章文案。

    ⚠️ 缓存可能是几天前的快照 —— 必须把日期显示出来。2026-09-23 用户之所以觉得
       "数据看着是新的但内容是坏的"，一部分原因就是界面只写「收盘数据」、不写哪天的。
    ⚠️ 反过来说：**盘中的今天的快照不能叫「收盘数据」**（2026-09-24 见到的误导），
       那种情况写「快照 · HH:MM」——直接告诉用户这份数据是几点抓的。
    """
    if data_status == "live":
        return "实时数据"
    if data_status == "cached":
        t = str(snapshot_time or "")
        if len(t) >= 16:
            today = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
            if t[:10] == today:
                return f"快照 · {t[11:16]}"          # 今天的稍早快照（盘中常见）
            return f"收盘数据 · {int(t[5:7])}/{int(t[8:10])}"
        if len(t) >= 10:
            today = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
            if t[:10] != today:
                return f"收盘数据 · {int(t[5:7])}/{int(t[8:10])}"
        return "快照数据"
    if data_status == "partial":
        return "数据不完整"
    return "估算数据"


def should_save_cache(data_status):
    """🩸🩸 写缓存的**唯一**条件：必须是完整抓取（status == 'live'）。

    2026-09-23 全站数据被毁，就是因为这里放行了不完整数据 ——
    抽成纯函数，好处是这条铁律可以被单测直接钉住。
    """
    return data_status == "live"


def _seal(codes, status, fetched_rows, expected_total):
    """统一收口：打印结果 / 告警、记录状态，返回 (codes, status)"""
    LAST_FETCH_INFO["status"] = status
    if status == "live":
        print(f"  ✅ 使用东方财富全市场数据（{len(codes)} 条 / 预期 {expected_total}，完整）")
    else:
        print(f"  🚨 告警：本次未取得完整全市场数据（状态 = {status}）"
              f" —— 抓到 {fetched_rows} 条 / 预期 {expected_total} 条")
        print("     ↳ 对外供数：" + {
            "cached": "改用完整快照，缓存未被覆盖",
            "partial": "只有本次抓到的部分数据，前端会显式标注「数据不完整」",
            "static": "静态兜底数据",
        }.get(status, status))
    return codes, status


def fetch_retail_money_flow():
    """决定这一版对外供什么数据。返回 (retail_flow, data_status)。

    🩸 2026-09-23 的教训：原实现是 `if stocks and len(stocks) > 50: return "live"`，
       而 live 会 save_cache()。抓到 500 条也算 live → 好缓存被逐次覆盖成 200 条 → 全站损毁。
       **现在只有 complete=True 才算 live，也只有 live 才允许写缓存。**
    """
    global LAST_FETCH_INFO
    print("=" * 50)
    print("📡 小散研究院 - 数据抓取中...")
    print("=" * 50)

    stocks, complete, expected_total = fetch_from_eastmoney(budget_s=_CLI_BUDGET)
    n_got = len(stocks)
    LAST_FETCH_INFO = {"expected_total": expected_total,
                       "fetched_rows": n_got,
                       "complete": bool(complete)}
    LAST_FETCH_INFO.update(FETCH_DIAG)     # 备用通道是否被用过、取到多少

    # ① 完整抓取 —— 唯一允许写缓存的路径（main() 用 should_save_cache() 把关）
    if stocks and complete:
        return _seal(stocks, "live", n_got, expected_total)

    # ② 抓到相当量级但不完整 —— 🩸 绝不写缓存；优先用完整快照对外供数，
    #    因为「覆盖不全但很新」对用户不如「覆盖完整但略旧」有用
    if stocks and n_got >= PARTIAL_MIN_CODES:
        print(f"  ⚠️ 本次只抓到 {n_got} 条（预期 {expected_total}），判定为「不完整」")
        print("     ↳ 不完整的数据不写缓存，避免把好快照覆盖掉")
        picked = _pick_snapshot()
        if picked:
            return _seal(picked[0], picked[1], n_got, expected_total)
        print("  ⚠️ 也没有可用快照 → 本版标记为「数据不完整」，前端会显式提示")
        return _seal(stocks, "partial", n_got, expected_total)

    # ③ 完全拿不到 / 量级太小 —— 走快照，最后才是静态兜底
    if stocks:
        print(f"  ⚠️ 只抓到 {n_got} 条，量级太小（< {PARTIAL_MIN_CODES}），视同抓取失败")
    else:
        print("  📦 东方财富不可用，尝试缓存...")
    picked = _pick_snapshot()
    if picked:
        return _seal(picked[0], picked[1], n_got, expected_total)

    print("📦 使用备用数据...")
    fallback = get_fallback_data()
    print(f"  ✅ 备用 {len(fallback)} 条 [静态]")
    return _seal(fallback, "static", n_got, expected_total)


def get_fallback_data():
    """备用数据 - 含四档明细"""
    base = [
        ("600150", "中国船舶", 9.12, "军工/船舶"),
        ("601318", "中国平安", 4.78, "保险"),
        ("603256", "宏和科技", 4.68, "化工"),
        ("600519", "贵州茅台", 3.89, "白酒"),
        ("002594", "比亚迪", 3.56, "新能源汽车"),
        ("000725", "京东方A", 3.34, "面板/显示"),
        ("601012", "隆基绿能", 3.12, "光伏"),
        ("300750", "宁德时代", 2.98, "电池"),
        ("600036", "招商银行", 2.87, "银行"),
        ("000858", "五粮液", 2.65, "白酒"),
        ("002475", "立讯精密", 2.43, "消费电子"),
        ("600900", "长江电力", 2.21, "电力"),
        ("601899", "紫金矿业", 2.15, "有色金属"),
        ("300059", "东方财富", 1.98, "证券"),
        ("600276", "恒瑞医药", 1.87, "医药"),
        ("000333", "美的集团", 1.76, "家电"),
        ("002230", "科大讯飞", 1.65, "AI/算力"),
        ("688981", "中芯国际", 1.54, "半导体"),
        ("601628", "中国人寿", 1.43, "保险"),
        ("000977", "浪潮信息", 1.35, "算力服务器"),
        ("600584", "长电科技", 1.28, "半导体封测"),
        ("600522", "中天科技", 1.15, "光纤/光通信"),
        ("300308", "中际旭创", 1.08, "光模块"),
        ("300476", "胜宏科技", 0.98, "PCB"),
        ("603986", "兆易创新", 0.89, "存储芯片"),
        ("600707", "彩虹股份", 0.82, "玻璃基板"),
        ("002415", "海康威视", 0.75, "安防"),
        ("000063", "中兴通讯", 0.68, "通信设备"),
        ("600809", "山西汾酒", 0.61, "白酒"),
        ("000021", "深科技", 0.55, "存储/半导体"),
        ("600009", "上海机场", 0.48, "航空"),
        ("601857", "中国石油", 0.42, "石油"),
        ("600028", "中国石化", 0.38, "石油"),
        ("000651", "格力电器", 0.35, "家电"),
        ("002241", "歌尔股份", 0.32, "消费电子"),
        ("300015", "爱尔眼科", 0.28, "医疗"),
        ("603259", "药明康德", 0.25, "医药"),
        ("600690", "海尔智家", 0.22, "家电"),
        ("002352", "顺丰控股", 0.18, "物流"),
        ("600048", "保利发展", 0.15, "地产"),
        ("000002", "万科A", -0.12, "地产"),
    ]
    stocks = []
    for code, name, net, sector in base:
        super_n = round(net * 0.3, 4)
        large_n = round(net * 0.25, 4)
        medium_n = round(net * 0.15, 4)
        small_n = round(net, 4)
        main_n = round(super_n + large_n, 4)
        total_abs = abs(small_n) + abs(main_n)
        dyn = round(small_n / total_abs * 100, 1) if total_abs > 0.001 else 0
        total_amt = round(abs(net) * 10, 2)
        stocks.append({
            "code": code, "name": name,
            "price": 0, "change_pct": 0,
            "retail_net": small_n,
            "main_net": main_n,
            "super_net": super_n,
            "large_net": large_n,
            "medium_net": medium_n,
            "small_net": small_n,
            "super_pct": 0, "large_pct": 0, "medium_pct": 0, "small_pct": 0,
            "total_amount": total_amt,
            "dynamic_ratio": dyn,
            "sector": sector,
        })
    return stocks


DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# ── 财报（业绩报表 + 业绩预告）────────────────────────────────────────
# 数据源：datacenter-web 的 RPT_LICO_FN_CPD（业绩报表）与 RPT_PUBLIC_OP_NEWPREDICT（预告）
# 🩸 两个坑（2026-10-30 实测）：
#   ① 必须过滤 SECURITY_TYPE="A股" —— 不过滤会混进 5775 只新三板、77 只 B 股
#   ② 字段是 REPORTDATE（无下划线），不是 REPORT_DATE
FIN_CACHE_FILE = f"{OUTPUT_DIR}/finance_cache.json"
FIN_LY_FILE = f"{OUTPUT_DIR}/finance_ly.json"     # 去年同期累计值（历史数据，拉一次长期复用）
FIN_CACHE_HOURS = 6          # 兜底值；实际按披露日历由 _fin_cache_hours_for() 决定
FIN_PAGE = 500
FIN_MAX_PAGE = 30
FIN_MIN_LATEST = 100         # 最新期披露量少于此 → 认为还没开始披露，直接退到上一期
FIN_TYPE = "A股"

FIN_COLUMNS = ("SECURITY_CODE,SECURITY_NAME_ABBR,SECURITY_TYPE,REPORTDATE,NOTICE_DATE,"
               "BASIC_EPS,DEDUCT_BASIC_EPS,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT,"
               "WEIGHTAVG_ROE,YSTZ,SJLTZ,YSHZ,SJLHZ,XSMLL,MGJYXJJE,BPS,PUBLISHNAME")


def _fin_num(v, dp=2):
    """转 float 并保留 dp 位；None / 非数 / inf / nan → None"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return round(x, dp)


def _dc_query(report_name, columns, filt, sort_columns=None, sort_types="1",
              page_size=FIN_PAGE, page_number=1):
    params = {"reportName": report_name, "columns": columns,
              "pageSize": str(page_size), "pageNumber": str(page_number),
              "source": "WEB", "client": "WEB"}
    if filt:
        params["filter"] = filt
    if sort_columns:
        params.update({"sortColumns": sort_columns, "sortTypes": sort_types})
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(DATACENTER_URL, params=params, headers=EASTMONEY_HEADERS, timeout=25)
            payload = resp.json()
            if payload.get("success") is False:
                raise ValueError(payload.get("message") or "接口返回失败")
            return payload.get("result") or {}
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise last_err


def _dc_fetch_all(report_name, columns, filt):
    """按 SECURITY_CODE 升序翻页拉全。稳定性与 clist 同理：排序键必须与内容无关。"""
    out, page = [], 1
    while page <= FIN_MAX_PAGE:
        res = _dc_query(report_name, columns, filt,
                        sort_columns="SECURITY_CODE", sort_types="1",
                        page_size=FIN_PAGE, page_number=page)
        rows = res.get("data") or []
        if not rows:
            break
        out.extend(rows)
        total = int(res.get("count") or 0)
        if total and len(out) >= total:
            break
        if len(rows) < FIN_PAGE:
            break
        page += 1
    return out


def _fin_count(period):
    res = _dc_query("RPT_LICO_FN_CPD", "SECURITY_CODE",
                    f"(REPORTDATE='{period}')(SECURITY_TYPE=\"{FIN_TYPE}\")",
                    page_size=1, page_number=1)
    return int(res.get("count") or 0)


def _fin_cache_hours_for(today):
    """按财报披露日历决定缓存时长（小时）。

    A 股的披露节奏是固定的：
      · 4 月：年报 + 一季报（最密集，4/30 前必须披露完）
      · 8 月：中报（8/31 前）
      · 10 月：三季报（10/31 前）
    高峰月的最后 10 天一天能新增上千家，所以缩到 3 小时；
    其他月份财报基本不动，24 小时足够 —— 没必要每 6 小时白打 25 页请求。
    """
    m, d = today.month, today.day
    if m in (4, 8, 10):
        return 3 if d >= 21 else 6
    if m in (1, 3):
        return 6      # 年报业绩预告 / 一季报预约披露期
    return 24


def _pick_report_periods(today, count_fn=None, min_latest=FIN_MIN_LATEST):
    """选出本次要拉的两个报告期 [最新期, 上一期]。

    🩸 不能取 max(REPORTDATE)：季报是分批披露的，新一期刚开始时只有零星几家
       （2026-10-15 实测 09-30 只有 18 条），这时应该继续用上一期。
       判断标准是「披露量 ≥ min_latest」，不是日期。
    ⚠️ 跨年也要正确：1 月初时 12-31 还没有正式年报，应退到 09-30。
    """
    cands = _prev_quarter_ends(today, 3)
    if not cands:
        return []
    latest = cands[0]
    n = (count_fn or _fin_count)(latest)
    if n < min_latest and len(cands) > 1:
        return [cands[1], cands[2] if len(cands) > 2 else cands[1]]
    return [latest, cands[1]] if len(cands) > 1 else [latest]


def _fin_row(r, tag):
    """一行业绩报表 → 站内字段。金额统一换成「亿元」，与全站口径一致。"""
    code = str(r.get("SECURITY_CODE") or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return None
    eps = _fin_num(r.get("BASIC_EPS"), 3)
    cfps = _fin_num(r.get("MGJYXJJE"), 3)
    sector = (r.get("PUBLISHNAME") or "").strip() or None
    # 现金含量 = 每股经营现金流 ÷ 每股收益。>1 说明利润有现金支撑；
    # 亏损股（eps<=0）这个比值没有意义 → 置 None，别让负数反转出「-8 倍很健康」的假象
    cash_q = round(cfps / eps, 2) if (eps and eps > 0 and cfps is not None) else None
    # 🩸 金融股的经营现金流被存贷/客户资金扭曲（平安银行算出来 7.52 倍，纯属口径错配）
    if sector and re.search(r"银行|保险|证券|多元金融|信托", sector):
        cash_q = None
    return {
        "code": code,
        "period": tag,
        "notice": str(r.get("NOTICE_DATE") or "")[:10],
        "rev": _fin_num((_fin_num(r.get("TOTAL_OPERATE_INCOME"), 0) or 0) / 1e8, 2),
        "np": _fin_num((_fin_num(r.get("PARENT_NETPROFIT"), 0) or 0) / 1e8, 2),
        "eps": eps,
        "eps_d": _fin_num(r.get("DEDUCT_BASIC_EPS"), 3),
        "bps": _fin_num(r.get("BPS"), 2),
        "roe": _fin_num(r.get("WEIGHTAVG_ROE"), 2),
        "rev_yoy": _fin_num(r.get("YSTZ"), 2),
        "np_yoy": _fin_num(r.get("SJLTZ"), 2),
        "rev_qoq": _fin_num(r.get("YSHZ"), 2),      # 单季环比（已用 H1、Q1 数据交叉验证）
        "np_qoq": _fin_num(r.get("SJLHZ"), 2),
        "gross": _fin_num(r.get("XSMLL"), 2),       # 银行/券商为 None
        "cash_q": cash_q,
        "sector": sector,
    }


def _fetch_forecast(period):
    """业绩预告（还没发财报的公司）。同一家公司有多条（归母净利/扣非/EPS），
    优先取「归属于上市公司股东的净利润」那条 —— 与业绩报表的 SJLTZ 同口径。"""
    rows = _dc_fetch_all("RPT_PUBLIC_OP_NEWPREDICT", "ALL", f"(REPORT_DATE='{period}')")
    best = {}
    for r in rows:
        code = str(r.get("SECURITY_CODE") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        kind = str(r.get("PREDICT_FINANCE") or "")
        item = {
            "type": (r.get("PREDICT_TYPE") or "").strip() or None,
            "amp_low": _fin_num(r.get("ADD_AMP_LOWER"), 2),
            "amp_high": _fin_num(r.get("ADD_AMP_UPPER"), 2),
            "amt_low": _fin_num((_fin_num(r.get("PREDICT_AMT_LOWER"), 0) or 0) / 1e8, 2),
            "amt_high": _fin_num((_fin_num(r.get("PREDICT_AMT_UPPER"), 0) or 0) / 1e8, 2),
            "notice": str(r.get("NOTICE_DATE") or "")[:10],
        }
        prev = best.get(code)
        # 归母净利优先；同一家再按公告日取最新
        better = (prev is None
                  or ("归属于上市公司股东的净利润" in kind and "归属于上市公司股东的净利润" not in (prev.get("_kind") or ""))
                  or (prev.get("notice") or "") < item["notice"])
        if better:
            item["_kind"] = kind
            best[code] = item
    for v in best.values():
        v.pop("_kind", None)
    return best


def load_finance_cache(expect_period=None, ignore_ttl=False):
    """读缓存。

    expect_period：当前应该用的报告期。缓存里的期次对不上 → 直接作废，
      否则跨期那一刻（比如 1 月切到年报）会继续用上一期的旧数据。
    ignore_ttl：取旧缓存做对比（数量骤降保护要用），不看是否过期。
    """
    try:
        if not os.path.exists(FIN_CACHE_FILE):
            return None
        with open(FIN_CACHE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not d.get("rows"):
            return None
        if expect_period and d.get("period") and d["period"] != expect_period:
            print(f"  📚 财报：缓存期次是 {d.get('period')}，本期应为 {expect_period} → 作废重拉")
            return None
        if not ignore_ttl:
            ts = datetime.strptime(d.get("fetched_at", ""), "%Y-%m-%d %H:%M:%S")
            # 🩸 两边都换成北京时间再比：fetched_at 写的是 BJ_TZ，而服务器系统时区可能是 UTC，
            #    直接用 datetime.now() 相减会差 8 小时（缓存永不过期或立刻过期）
            now_bj = datetime.now(BJ_TZ).replace(tzinfo=None)
            hours = _fin_cache_hours_for(now_bj.date())
            if (now_bj - ts).total_seconds() > hours * 3600:
                return None
        return d
    except Exception:
        return None


def save_finance_cache(d):
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        tmp = FIN_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, FIN_CACHE_FILE)
    except Exception:
        pass


# ── 单季同比所需的去年同期数据 ────────────────────────────────────
# 🩸 东财的 YSTZ / SJLTZ 是**年初至今累计同比**（已用 2025 同期数据倒推验证），
#    而累计同比会被前几个季度拖累：五粮液 2026Q3 累计净利 +49.98%，单季却 −48.25%。
#    单季同比 = (本期累计 − 上期累计) ÷ (去年同期累计 − 去年同期上一期累计) − 1。
#    去年同期是历史数据、永不变化 → 单独落盘，拉一次就长期复用，
#    不必跟着财报刷新节奏重复打 11 页。

def load_ly_cache():
    try:
        with open(FIN_LY_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_ly_cache(d):
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        tmp = FIN_LY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, FIN_LY_FILE)
    except Exception:
        pass


def _shift_year(period, n=1):
    """'2026-09-30' → '2025-09-30'"""
    try:
        y, m, d = period.split("-")
        return f"{int(y) - n:04d}-{m}-{d}"
    except Exception:
        return None


def _prev_period(period):
    """给定季末，返回上一季季末：'2026-09-30' → '2026-06-30'"""
    try:
        dt = datetime.strptime(period, "%Y-%m-%d").date() - timedelta(days=1)
        seq = _prev_quarter_ends(dt, 1)
        return seq[0] if seq else None
    except Exception:
        return None


def _single_quarter_yoy(cur, prev, ly_cur, ly_prev):
    """单季同比（%）：(本期累计 − 上期累计) ÷ (去年同期累计 − 去年同期上期累计) − 1

    🩸 去年同季 ≤ 0（亏损）时不计算：那会算出 −200%~−500% 这种没有意义的数。
    """
    q_cur = (cur or 0) - (prev or 0)
    q_ly = (ly_cur or 0) - (ly_prev or 0)
    if q_ly <= 0:
        return None
    return round((q_cur / q_ly - 1) * 100, 2)


def _ensure_ly(period, cache):
    """确保 cache[period] 存在：{code: [营收累计, 净利累计]}。缺了才拉。"""
    if period in cache and cache[period]:
        return cache[period]
    try:
        rows = _dc_fetch_all("RPT_LICO_FN_CPD",
                             "SECURITY_CODE,TOTAL_OPERATE_INCOME,PARENT_NETPROFIT",
                             f"(REPORTDATE='{period}')(SECURITY_TYPE=\"{FIN_TYPE}\")")
    except Exception as e:
        print(f"  ⚠️ 财报：拉去年同期 {period} 失败（{e}）")
        return None
    out = {}
    for r in rows:
        code = str(r.get("SECURITY_CODE") or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            continue
        # ⚠️ 必须和当期一样换算成「亿元」：混着元来除会得到 −100%
        out[code] = [(_fin_num(r.get("TOTAL_OPERATE_INCOME"), 0) or 0) / 1e8,
                     (_fin_num(r.get("PARENT_NETPROFIT"), 0) or 0) / 1e8]
    cache[period] = out
    print(f"  📊 财报去年同期 {period}：{len(out)} 只（已落盘复用）")
    return out


def build_finance(codes=None):
    """构建 finance 块：每只股票一条「最新已披露」财报 + 预告。

    🩸 不能只用「最新报告期」：财报分批披露，10/30 实测 Q3 只有 4931 家 A 股，
       剩下 ~650 家还停在中报。所以按**逐股取最新**：Q3 有就用 Q3，没有退回 H1，
       并在行里标 period，前端显示「Q3 / 中报」，不许把中报数据当 Q3 展示。
    """
    today = datetime.now(BJ_TZ).date()
    try:
        periods = _pick_report_periods(today)
    except Exception as e:
        print(f"  ⚠️ 财报：探测最新报告期失败（{e}）→ 用旧缓存兜底")
        return load_finance_cache(ignore_ttl=True)
    if not periods:
        return None

    # 🩸 缓存必须校验「期次」：跨期那一刻（例：1 月从三季报切到年报），
    #    只靠 TTL 会继续用上一期的数据最多 24 小时。
    cached = load_finance_cache(expect_period=periods[0])
    if cached:
        print(f"  📚 财报：复用缓存（{cached.get('as_of')} · {len(cached.get('rows') or [])} 只"
              f" · 期次 {cached.get('period')}）")
        return cached
    merged = {}
    period_vals = {}          # tag -> {code: [营收累计, 净利累计]}，算单季同比要用
    for period in periods:
        tag = _period_tag(period)
        try:
            rows = _dc_fetch_all(
                "RPT_LICO_FN_CPD", FIN_COLUMNS,
                f"(REPORTDATE='{period}')(SECURITY_TYPE=\"{FIN_TYPE}\")")
        except Exception as e:
            print(f"  ⚠️ 财报：拉取 {period} 失败（{e}）")
            continue
        added = 0
        vals = {}
        for r in rows:
            row = _fin_row(r, tag)
            if not row:
                continue
            vals[row["code"]] = [row["rev"] or 0, row["np"] or 0]
            if codes and row["code"] not in codes:
                continue
            if row["code"] in merged:      # 已经有更新的报告期 → 不动
                continue
            merged[row["code"]] = row
            added += 1
        period_vals[tag] = vals
        print(f"  📊 财报 {tag}（{period}）：{len(rows)} 条 → 采纳 {added} 只")

    # ── 单季同比（累计口径会被前几个季度拖累，单季才看当下）────────────
    main = periods[0]
    main_tag = _period_tag(main)
    main_prev = _prev_period(main)
    prev_vals = period_vals.get(_period_tag(main_prev) if main_prev else "", {}) or {}
    ly_main = _shift_year(main)
    ly_prev = _prev_period(ly_main) if ly_main else None
    ly_cache = load_ly_cache()
    ly_a = _ensure_ly(ly_main, ly_cache) if ly_main else None
    ly_b = _ensure_ly(ly_prev, ly_cache) if ly_prev else None
    if ly_a or ly_b:
        save_ly_cache(ly_cache)
    q_n = 0
    if ly_a and ly_b:
        for row in merged.values():
            # 只给「主期」算：兜底期（中报）的"单季"是 Q2，与页面标注的期次不是一回事
            if row.get("period") != main_tag:
                continue
            code = row["code"]
            pv, cur_ly, prev_ly = prev_vals.get(code), ly_a.get(code), ly_b.get(code)
            if not pv or not cur_ly or not prev_ly:
                continue
            np_q = _single_quarter_yoy(row["np"] or 0, pv[1], cur_ly[1], prev_ly[1])
            if np_q is not None:
                row["np_q_yoy"] = np_q
            rev_q = _single_quarter_yoy(row["rev"] or 0, pv[0], cur_ly[0], prev_ly[0])
            if rev_q is not None:
                row["rev_q_yoy"] = rev_q
            q_n += 1
        print(f"  📐 单季同比：{q_n} 只有效（去年同期亏损的不算）")

    try:
        fc = _fetch_forecast(periods[0])
    except Exception as e:
        print(f"  ⚠️ 财报：预告拉取失败（{e}）")
        fc = {}
    hit = 0
    for code, item in fc.items():
        if code in merged:
            merged[code].update({
                "fc_type": item.get("type"),
                "fc_amp_low": item.get("amp_low"),
                "fc_amp_high": item.get("amp_high"),
                "fc_notice": item.get("notice"),
            })
            hit += 1
    print(f"  📈 财报预告：{len(fc)} 家（其中 {hit} 家与已披露数据重合）")

    if not merged:
        print("  ⚠️ 财报：一条都没拿到")
        return load_finance_cache(ignore_ttl=True)

    # 🩸 数量骤降保护（同 2026-09-23 缓存自毁的教训）：
    #    数据源抖动时不要把一份残缺的财报覆盖掉好数据，宁可继续用上一版。
    prev = load_finance_cache(ignore_ttl=True)
    prev_n = len((prev or {}).get("rows") or [])
    if prev_n >= FIN_MIN_LATEST and len(merged) < prev_n * 0.8:
        print(f"  ⚠️ 财报：本次仅 {len(merged)} 只，低于上版 {prev_n} 只的 80%"
              f" → 判定数据源异常，保留旧缓存（{prev.get('as_of')}）")
        return prev

    periods_used = []
    for row in merged.values():
        if row["period"] not in periods_used:
            periods_used.append(row["period"])
    out = {
        "as_of": today.strftime("%Y-%m-%d"),
        "fetched_at": datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "period": periods[0],
        "periods": sorted(periods_used, reverse=True),
        "rows": list(merged.values()),
    }
    save_finance_cache(out)
    print(f"  ✅ 财报：{len(merged)} 只（{', '.join(out['periods'])}）")
    return out


def _period_tag(period):
    """'2026-09-30' → '2026Q3'；'2026-06-30' → '2026H1'"""
    try:
        y, m, _ = period.split("-")
        q = {"03": "Q1", "06": "H1", "09": "Q3", "12": "FY"}[m]
        return f"{y}{q}"
    except Exception:
        return period


HOLDER_COLUMNS = ("SECURITY_CODE,SECURITY_NAME_ABBR,END_DATE,HOLDER_NUM,PRE_HOLDER_NUM,"
                  "HOLDER_NUM_CHANGE,HOLDER_NUM_RATIO,HOLD_NOTICE_DATE,"
                  "AVG_MARKET_CAP,AVG_HOLD_NUM,TOTAL_MARKET_CAP,INTERVAL_CHRATE")

# 户数变化的有效性门槛 —— 防次新股上市首期把比例算成天文数字
# 上市前公司只有发起人股东（上期户数常常只有 4~24 户），上市后一夜变成几万户，
# 算出来就是 +810100% 这种毫无意义的比例。北交所 920 开头的新股是高发区。
HOLDER_MIN_PRE_NUM = 1000       # 上期户数下限：低于此视为披露口径不可比
HOLDER_CHG_LIMIT = 1000.0       # 变化幅度上限（+1000%）；另剔除 ≤ -99.99% 的异常

# 口径版本号：改了结构就 +1，让当天缓存失效重算
# 11 = 自由流通改「性质 + 比例」组合判定（阈值 5%→3%）
#    + 户数变化与机构持股严格对齐报告期（避免 1 个月/3 个月变化混排）
#    + 新增锁定盘占比 lock_ratio
# 12 = 锁定盘主口径改为「A 股视角」（100 − 自由流通），另存 lock_ts 作总股本口径对照
HOLDER_DIST_VERSION = 12

# 机构持股分类（RPT_MAIN_ORGHOLD 的 ORG_TYPE）
ORGHOLD_ORG_TOTAL = "00"   # 机构汇总 = 机构 + 一般法人 合计占流通股比
ORGHOLD_ORG_LEGAL = "07"   # 其他 = 一般法人（大股东 / 产业资本）+ 陆股通等未单独分类的机构

# 🩸 选报告期的覆盖量下限。季报是**分批披露**的：新一季刚开始时只有极少数公司披露 ——
#    2026-10-15 实测 2026-09-30 只有 18 条、2026-06-30 有 5576 条。
#    若直接取 max(REPORT_DATE)，整张筹码表会只剩 18 行（资金占比动向因此几乎全空）。
#    完整季度的记录数稳定在 5000~5600，取 2000 作「披露充分」的门槛足够安全。
ORGHOLD_MIN_COUNT = 2000

# ── 自由流通股口径（2026-09-15 v2：按「股东性质 + 持股比例」组合判定）──
# 为什么改：用户指出「自由流通 = 真正会动的流通筹码」，而 v1 只看「≥5%」这一个比例，
# 会把 1~5% 的产业资本（国资平台、关联公司、员工持股）当成会动的筹码 → 系统性高估自由流通。
# 依据：以东财自带 FREELIQCI_SHARES（2026 年 3,591 只有值）为基准做参数扫描 ——
#   「性质 + ≥3%」组合的中位偏差 3.1pp；且必须把「个人大股东」也锁定（不锁偏差翻倍到 6.7pp）。
#   ① 产业资本类 → 无条件锁定（无论占比多少，都不参与日常博弈）
#   ② 财务投资类 → 无条件自由（它们本来就是在动的钱）
#   ③ 其余（个人、保险公司本身）→ 占流通A股 ≥ FREE_FLOAT_LOCK_PCT 才锁
#   ④ 陆股通 → 永不锁（北向资金本身就是自由流通的通道）
FREE_FLOAT_LOCK_PCT = 3.0

# 只拉「占流通A股 ≥1%」的股东记录（约 60 页）：<1% 的持仓对锁定比例影响可忽略，
# 又能把请求量压在接口深分页上限（约 100 页 / 5 万条）之内。
FREEHOLD_MIN_RATIO = 1.0

# 产业资本：控股平台 / 国资平台 / 未分类法人 / 员工持股 —— 长期不动
CAPITAL_TYPES = {"投资公司", "其它", "员工持股计划"}

# 财务投资：公募 / 社保 / 养老 / QFII / 私募 / 券商 / 保险资管产品 / 理财 / 信托 / 年金 —— 会调仓
# 注意「保险公司」（集团本身）与「保险产品」（资管产品）是两类：前者走 ③ 按比例判，后者在此不锁
FINANCIAL_TYPES = {"证券投资基金", "基金管理公司", "全国社保基金", "基本养老基金",
                   "保险产品", "证券公司", "QFII", "私募基金", "集合理财计划",
                   "信托计划", "其他理财产品", "企业年金"}

# 锁定筹码要落到「哪一段」里去扣（否则会出现 主力占自由流通 1300% 这种数）
# 依据 2026-09-14 实测：证券投资基金进 01 基金 → 主力；而「私募基金」和「券商资管 FOF」
# 都落在 其他(07) → 一般法人。国寿集团/平安集团这类保险母公司登记在 05 保险 → 主力。
LOCK_TYPE_SEGMENT = {
    "保险公司": "inst", "保险产品": "inst", "保险": "inst",
    "证券公司": "inst", "券商": "inst", "社保": "inst", "QFII": "inst",
    "证券投资基金": "inst", "信托": "inst",
    "私募基金": "legal", "集合理财计划": "legal", "投资公司": "legal", "其它": "legal",
    "个人": "retail",
}

# 筹码动向表（前端可排序，只渲染排序后的前 N 行；数据仍覆盖全市场）
CHIP_FLOW_MAX_ROWS = 300


def _holder_query(extra, page_size=200, page_number=1):
    """请求东财数据中心 RPT_HOLDERNUMLATEST（股东户数最新披露表）

    - 接口单页最多返回 500 条，pageSize 传更大也只给 500
    - 连续请求容易被掐（RemoteDisconnected），因此内置 3 次重试
    """
    params = {
        "reportName": "RPT_HOLDERNUMLATEST",
        "columns": HOLDER_COLUMNS,
        "pageSize": str(page_size),
        "pageNumber": str(page_number),
        "source": "WEB",
        "client": "WEB",
    }
    params.update(extra)
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(DATACENTER_URL, params=params, headers=EASTMONEY_HEADERS, timeout=25)
            payload = resp.json()
            if payload.get("success") is False:
                raise ValueError(payload.get("message") or "接口返回失败")
            return (payload.get("result") or {}).get("data") or []
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise last_err


def _holder_latest_and_window():
    """返回 (最新披露日, 有效窗口起点)——窗口 = 最新披露日往前 180 天"""
    head = _holder_query({"sortColumns": "END_DATE", "sortTypes": "-1"}, page_size=1)
    latest = (head[0].get("END_DATE") or "")[:10] if head else ""
    if not latest:
        raise ValueError("无法获取最新披露日")
    start = (datetime.strptime(latest, "%Y-%m-%d") - timedelta(days=180)).strftime("%Y-%m-%d")
    return latest, start


def _orghold_result(extra, page_size=500, page_number=1):
    """请求东财数据中心 RPT_MAIN_ORGHOLD，返回接口的 result 字典（含 data / count / pages）

    拆出 `_orghold_query` 是为了拿 `count` —— 判断某个报告期「披露得够不够全」
    只需要 count，不用真把数据拉下来。
    """
    params = {
        "reportName": "RPT_MAIN_ORGHOLD",
        "columns": ("SECURITY_CODE,SECURITY_NAME_ABBR,REPORT_DATE,ORG_TYPE,ORG_TYPE_NAME,"
                    "HOULD_NUM,TOTAL_SHARES,FREESHARES_RATIO,TOTALSHARES_RATIO"),
        "pageSize": str(page_size),
        "pageNumber": str(page_number),
        "source": "WEB",
        "client": "WEB",
    }
    params.update(extra)
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(DATACENTER_URL, params=params, headers=EASTMONEY_HEADERS, timeout=25)
            payload = resp.json()
            if payload.get("success") is False:
                raise ValueError(payload.get("message") or "接口返回失败")
            return payload.get("result") or {}
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise last_err


def _orghold_query(extra, page_size=500, page_number=1):
    return _orghold_result(extra, page_size, page_number).get("data") or []


def _orghold_count(period, org_type=ORGHOLD_ORG_TOTAL):
    """某报告期已披露的记录条数（只取 count，不拉数据）"""
    res = _orghold_result({"filter": f"(REPORT_DATE='{period}')(ORG_TYPE=\"{org_type}\")",
                           "columns": "SECURITY_CODE",
                           "pageSize": "1", "pageNumber": "1"})
    return int(res.get("count") or 0)


def _prev_quarter_ends(from_date, n=6):
    """给出 <= from_date 的最近 n 个季末日期（降序），如 ['2026-06-30','2026-03-31',…]"""
    seq = []
    for yy in (from_date.year, from_date.year - 1):
        for mm, dd in ((12, 31), (9, 30), (6, 30), (3, 31)):
            seq.append(datetime(yy, mm, dd))
    out = []
    for dt in sorted(seq, reverse=True):
        if dt.date() <= from_date and dt.strftime("%Y-%m-%d") not in out:
            out.append(dt.strftime("%Y-%m-%d"))
        if len(out) >= n:
            break
    return out


def _pick_periods(candidates, count_fn, min_count=None, need=2):
    """从候选期（降序）里挑出「披露充分」的最近 need 期。

    抽成纯函数（count_fn 可注入）是为了能被单测直接钉住 ——
    这条规则失效的代价是整个筹码表塌成十几行。
    """
    min_count = ORGHOLD_MIN_COUNT if min_count is None else min_count
    picked = []
    for per in candidates:
        try:
            if count_fn(per) >= min_count:
                picked.append(per)
        except Exception:
            continue
        if len(picked) >= need:
            break
    return picked


def _orghold_ratio_map(period, org_type):
    """某一报告期、某一机构类型的 {code: (占流通股比, 机构家数)}"""
    mapping, page = {}, 1
    while page <= 20:
        batch = _orghold_query(
            {"filter": f"(REPORT_DATE='{period}')(ORG_TYPE=\"{org_type}\")",
             "sortColumns": "SECURITY_CODE", "sortTypes": "1"},
            page_size=500, page_number=page)
        if not batch:
            break
        for r in batch:
            code = r.get("SECURITY_CODE")
            if code:
                mapping[code] = (float(r.get("FREESHARES_RATIO") or 0),
                                 int(r.get("HOULD_NUM") or 0))
        if len(batch) < 500:
            break
        page += 1
    return mapping


def _orghold_periods():
    """取机构持股的最近两个**披露充分**的报告期（降序）。

    🩸 不能直接取 max(REPORT_DATE)：季报分批披露，新一季刚开始时只有极少数公司披露
       （2026-10-15 实测 2026-09-30 只有 18 条、2026-06-30 有 5576 条）。
       取错期的后果不只是「户数变化」缺失 —— `total_map` 是按本期机构持股建的，
       它只有几个代码 → **整张筹码表只剩十几行**，前端「资金占比动向」随之几乎全空。
    改为：从最新 REPORT_DATE 往前逐季枚举，取记录数达标的最近两期。
    """
    head = _orghold_query({"sortColumns": "REPORT_DATE", "sortTypes": "-1"}, page_size=1)
    latest = (head[0].get("REPORT_DATE") or "")[:10] if head else ""
    if not latest:
        return []
    cands = _prev_quarter_ends(datetime.strptime(latest, "%Y-%m-%d").date())
    picked = _pick_periods(cands, _orghold_count)
    if not picked:
        # 兜底：连一个达标的都没有（接口异常），至少别把 latest 丢了
        print(f"  ⚠️ 没有披露充分的报告期，退回最新期 {latest}")
        return [latest]
    if picked[0] != latest:
        print(f"  ⚠️ 最新报告期 {latest} 披露量不足（门槛 {ORGHOLD_MIN_COUNT} 条），"
              f"改用披露充分的 {picked[0]}")
    return picked


def _freehold_query(extra, page_size=500, page_number=1):
    """请求东财数据中心 RPT_F10_EH_FREEHOLDERS（十大流通股东）"""
    params = {
        "reportName": "RPT_F10_EH_FREEHOLDERS",
        "columns": ("SECURITY_CODE,SECURITY_NAME_ABBR,END_DATE,HOLDER_NAME,HOLDER_RANK,"
                    "LISTED_SHARES_RATIO,FREE_HOLDNUM_RATIO,IS_LANDSTOCK,HOLDER_TYPE"),
        "pageSize": str(page_size),
        "pageNumber": str(page_number),
        "source": "WEB",
        "client": "WEB",
    }
    params.update(extra)
    resp = requests.get(DATACENTER_URL, params=params, headers=EASTMONEY_HEADERS, timeout=25)
    payload = resp.json()
    if payload.get("success") is False:
        raise ValueError(payload.get("message") or "接口返回失败")
    return (payload.get("result") or {}).get("data") or []


def fetch_float_lock(period):
    """自由流通股：算出每只股票的「锁定比例」（占流通A股），供前端算自由流通口径

    规则（2026-09-15 v2）：按「股东性质 + 持股比例」组合判定，详见上方 CAPITAL_TYPES 注释。
    核心是回答「哪些筹码是真正会动的」—— 大股东持股虽然也登记在流通盘里，但实际并不动。

    ⚠️ 两个比例字段的语义与直觉相反，已用中石油 / 工行实测校正，别搞反：
    - `LISTED_SHARES_RATIO` = **占流通A股**比例（中石油集团 92.997% = 1509.2÷1619.2）
    - `FREE_HOLDNUM_RATIO`  = **占总股本**比例（中石油集团  82.277% = 1509.2÷1830.2）
    两者相除即「流通A股 ÷ 总股本」。

    返回 `{code: {"inst": …, "legal": …, "retail": …}}`（均为**占流通A股 %**，按 LOCK_TYPE_SEGMENT
    归到锁定筹码所属的那一段）；不在表里的股票视为全 0，即全部自由流通。
    分子分母必须同源：锁定筹码要从它所在的段里扣掉，否则会出现「主力占自由流通 1300%」。
    """
    print("🔍 正在获取自由流通股口径（性质 + 比例组合判定）...")
    try:
        flt = f"(END_DATE='{period}')(LISTED_SHARES_RATIO>={FREEHOLD_MIN_RATIO})"
        rows, page = [], 1
        while page <= 120:
            batch = _freehold_query(
                {"filter": flt, "sortColumns": "SECURITY_CODE", "sortTypes": "1"},
                page_size=500, page_number=page)
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 500:
                break
            page += 1
        if not rows:
            raise ValueError("未取到十大流通股东数据")

        lock = {}
        for r in rows:
            code = r.get("SECURITY_CODE")
            if not code:
                continue
            if str(r.get("IS_LANDSTOCK")) == "1":
                continue                                  # ④ 北向资金本就是自由流通，永不锁定
            listed = float(r.get("LISTED_SHARES_RATIO") or 0)
            if listed <= 0:
                continue
            htype = str(r.get("HOLDER_TYPE") or "").strip()
            if htype in FINANCIAL_TYPES:
                continue                                  # ② 财务投资：就是在动的钱，不锁
            if htype not in CAPITAL_TYPES and listed < FREE_FLOAT_LOCK_PCT:
                continue                                  # ③ 个人 / 保险公司本身：按比例判
            seg = LOCK_TYPE_SEGMENT.get(htype, "legal")   # ① 产业资本：无条件锁
            slot = lock.setdefault(code, {"inst": 0.0, "legal": 0.0, "retail": 0.0})
            slot[seg] += listed

        for v in lock.values():
            for k in list(v.keys()):
                v[k] = round(min(v[k], 100.0), 4)         # 单段锁定不超过 100%

        heavy = sum(1 for v in lock.values() if sum(v.values()) > 100)
        print(f"  ✅ 自由流通口径 [{period}]: {len(rows)} 行 → {len(lock)} 只有锁定股东"
              + (f"（⚠️ {heavy} 只锁定合计 >100%，已逐段截断）" if heavy else ""))
        return lock
    except Exception as e:
        print(f"  ❌ 自由流通口径抓取失败: {e}")
        return {}


def build_chip_flow(by_code, holder_rows, periods, total_map, legal_map, prev_total, prev_legal,
                    lock_map=None):
    """筹码动向表（前端可排序，一行一只股票，全市场覆盖）

    把两个口径合成一张表 —— 这是本看板最核心的一组交叉信号：
    - `holder_chg`  户数变化（环比 %）     ← 谁的人数在变
    - `holders`     股东户数              ← 人数存量
    - `inst_pct`    主力（投资机构）持股占流通比 ← 筹码在不在机构手里
    - `inst_delta`  主力持股环比（百分点）  ← 机构在加还是减（比"高不高"更重要）
    - `retail_pct`  散户及其他占流通比      ← 散户锚点
    - `float_ratio` 自由流通A股/流通A股 (%) ← 剔除「不动的筹码」后的可博弈占比
    - `lock_ratio`  锁定盘/总股本 (%)       ← (总股本 − 自由流通A股)/总股本，看这家公司谁说了算
    - `circ_ratio`  流通A股/总股本 (%)      ← 换算总股本口径用

    三个占比列（inst_pct / legal / retail_pct）都以**流通A股**为分母，
    前端按 `float_ratio`、`circ_ratio` 现场换算成「占自由流通」「占总股本」两个口径，
    不重复存储，避免数据体积膨胀三倍。

    典型读法：户数降 + 主力环比升 = 筹码在向机构集中（吸筹）

    注意 `holder_chg` 有两道闸门，命中一律置为 None（不是 0），前端显示 `--` 并沉底：
    1. **时间口径闸门**：户数报告期必须等于机构持股报告期，否则「1 个月变化」和「3 个月变化」
       混在同一列排序，对比毫无意义。
    2. **有效性闸门**：上期户数低于 HOLDER_MIN_PRE_NUM 或幅度超过 HOLDER_CHG_LIMIT ——
       否则北交所新股那种「10 户 → 81,020 户 = +810100%」会霸占默认的升/降序榜首。
    """
    print("🔍 正在合成筹码动向表（户数变化 × 主力持股 × 环比）...")
    try:
        cur_period = periods[0] if periods else ""
        holder_map = {}
        invalid_chg = 0
        period_mismatch = 0
        for r in holder_rows:
            code = r.get("SECURITY_CODE")
            if not code:
                continue
            ratio = r.get("HOLDER_NUM_RATIO")
            chg = round(float(ratio), 2) if ratio is not None else None
            prev_num = int(r.get("PRE_HOLDER_NUM") or 0)
            end_date = (r.get("END_DATE") or "")[:10]
            # ⚠️ 时间口径闸门（用户 2026-09-15 明确要求）：户数的变化是「本期 vs 上期」，
            # 机构持股的变化是「本季 vs 上季」。A 股有部分公司**按月披露**股东户数，
            # 那类记录的变化窗口只有 1 个月 —— 和季频的 3 个月放在同一列排序毫无可比性。
            # 所以报告期与机构持股不一致的，一律不计算变化（置 None，前端显示 -- 并沉底）。
            if chg is not None and end_date != cur_period:
                chg = None
                period_mismatch += 1
            # 次新股上市首期：上市前只有发起人账户（上期常常只有几户），口径不可比
            elif chg is not None and (prev_num < HOLDER_MIN_PRE_NUM
                                      or chg >= HOLDER_CHG_LIMIT or chg <= -99.99):
                chg = None
                invalid_chg += 1
            holder_map[code] = (int(r.get("HOLDER_NUM") or 0), chg)

        rows = []
        for code, (total, orgs) in total_map.items():
            stock = by_code.get(code)
            if not stock:
                continue
            # 剔除「有机构家数却占流通比为 0」的自相矛盾记录（次新股流通股未定义）
            if total <= 0 and orgs >= 5:
                continue
            legal = legal_map.get(code, (0.0, 0))[0]
            inst = max(total - legal, 0.0)
            delta = None
            if periods and len(periods) > 1 and code in prev_total:
                p_total = prev_total[code][0]
                p_legal = prev_legal.get(code, (0.0, 0))[0]
                delta = round(inst - max(p_total - p_legal, 0.0), 2)
            holders, chg = holder_map.get(code, (0, None))
            # ── 自由流通股口径 ──
            # 分子分母同源：锁定筹码要从它所在的段里扣掉，再除以自由流通盘，
            # 否则「国寿集团 92.8% 被判定为锁定、却登记在保险档（主力）」会算出 1300%
            lk = (lock_map or {}).get(code) or {}
            li, ll, lr = lk.get("inst", 0.0), lk.get("legal", 0.0), lk.get("retail", 0.0)
            free = round(max(100.0 - (li + ll + lr), 0.0), 2)   # 自由流通A股 ÷ 流通A股 ×100
            retail_raw = max(100 - total, 0.0)
            # 三段先各自扣掉锁定部分并截断到 0；再按合计归一化到 100。
            # 不归一化的话，锁定归类与分段口径冲突的票（约 0.6%，主要是「个人持股 ≥5% 但被算进
            # 其他档」之类）会出现加总 ≠ 100。
            i_raw = max(inst - li, 0.0)
            l_raw = max(legal - ll, 0.0)
            r_raw = max(retail_raw - lr, 0.0)
            tot = i_raw + l_raw + r_raw
            if free > 0.5 and tot > 0.5:
                k = 100.0 / tot
                inst_ff = round(i_raw * k, 2)
                legal_ff = round(l_raw * k, 2)
                retail_ff = round(100.0 - inst_ff - legal_ff, 2)   # 兜底，保证严格加总 100
            else:
                inst_ff = legal_ff = retail_ff = None
            # circ_ratio = 流通A股 ÷ 总股本 ×100（push2 的 f39/f38，全市场覆盖）
            ts = stock.get("total_shares") or 0
            cs = stock.get("circ_shares") or 0
            circ_ratio = round(cs / ts * 100, 2) if ts > 0 and cs > 0 else 100.0
            # ── 锁定盘 ── 回答「A 股股价是谁说了算」
            # 主口径 = 100 − 自由流通/流通A股，即「A 股市场里被锁住、不参与日常博弈的筹码占比」。
            # ⚠️ 不能拿「占总股本」当主口径 —— A+H 公司会被 H 股带偏：建设银行 H 股占总股本 96%，
            #    (总股本−自由流通A股)/总股本 = 96.6%，看着像大股东一手遮天；
            #    但 A 股流通盘里其实只有 8% 被锁。所以两个都算，前端主用前者，后者留作对照。
            free_over_ts = free * circ_ratio / 100.0
            lock_ratio = round(max(100.0 - free, 0.0), 2)              # 占流通A股（主口径）
            lock_ts = round(max(100.0 - free_over_ts, 0.0), 2)         # 占总股本（含 H股/限售）
            rows.append({
                "code": code,
                "name": stock.get("name", ""),
                "holders": holders,
                "holder_chg": chg,
                "inst_pct": round(inst, 2),
                "inst_delta": delta,
                "retail_pct": round(retail_raw, 2),
                "legal_pct": round(legal, 2),
                "float_ratio": free,
                "circ_ratio": circ_ratio,
                "lock_ratio": lock_ratio,
                "lock_ts": lock_ts,
                "lock_inst": round(li, 2),
                "lock_legal": round(ll, 2),
                "lock_retail": round(lr, 2),
                "inst_ff": inst_ff,
                "legal_ff": legal_ff,
                "retail_ff": retail_ff,
                "inst_ts": round(inst * circ_ratio / 100, 2),
                "legal_ts": round(legal * circ_ratio / 100, 2),
                "retail_ts": round(retail_raw * circ_ratio / 100, 2),
            })

        if not rows:
            raise ValueError("聚合后无有效样本")
        # 默认按户数变化升序（降幅最大的在前），前端可任意列重排
        rows.sort(key=lambda x: (x["holder_chg"] is None,
                                 x["holder_chg"] if x["holder_chg"] is not None else 0))
        print(f"  ✅ 筹码动向 {len(rows)} 只（{periods[0]} vs {periods[1] if len(periods) > 1 else '无'}）"
              f"｜户数变化置空：报告期未对齐 {period_mismatch} 只 / 口径不可比 {invalid_chg} 只")
        return {
            "period": periods[0] if periods else "",
            "prev_period": periods[1] if len(periods) > 1 else None,
            "rows": rows,
        }
    except Exception as e:
        print(f"  ❌ 筹码动向合成失败: {e}")
        return None


def build_holder_chip(stocks):
    """股东户数 × 持股结构 数据集 —— 只产出「筹码动向」可排序表

    一次把两个数据源准备好，前端读 `holder_chip.chip_flow`：

    - 股东户数（RPT_HOLDERNUMLATEST，全量分页 ~11 页 / 约 1.4MB）→ 户数变化、股东户数
    - 机构持股（RPT_MAIN_ORGHOLD，本期 + 上期两期）→ 主力持股、主力环比、散户及其他

    股东户数是季度数据，没必要每 10 分钟重抓 → main() 里做了「同一天复用一次」的缓存。
    """
    print("🔍 正在获取全市场股东户数 / 机构持股...")
    try:
        latest, window_start = _holder_latest_and_window()
        flt = f"(HOLDER_NUM>0)(END_DATE>='{window_start}')"

        rows, page = [], 1
        while page <= 20:
            batch = _holder_query(
                {"sortColumns": "HOLDER_NUM", "sortTypes": "-1", "filter": flt},
                page_size=500, page_number=page)
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 500:
                break
            page += 1
        if not rows:
            raise ValueError("未取到户数数据")

        by_code = {s["code"]: s for s in stocks}

        # ── 机构持股：两期映射只拉一次，供筹码动向表使用 ──
        periods = _orghold_periods()
        if not periods:
            raise ValueError("无法获取机构持股报告期")
        cur_period = periods[0]
        prev_period = periods[1] if len(periods) > 1 else None
        total_map = _orghold_ratio_map(cur_period, ORGHOLD_ORG_TOTAL)
        legal_map = _orghold_ratio_map(cur_period, ORGHOLD_ORG_LEGAL)
        prev_total = _orghold_ratio_map(prev_period, ORGHOLD_ORG_TOTAL) if prev_period else {}
        prev_legal = _orghold_ratio_map(prev_period, ORGHOLD_ORG_LEGAL) if prev_period else {}

        # 自由流通股口径：前十大流通股东按「性质 + 比例」判定是否锁定（陆股通除外）
        lock_map = fetch_float_lock(cur_period)

        # 筹码动向表：户数变化 × 主力持股 × 环比（前端可排序）
        chip_flow = build_chip_flow(by_code, rows, periods,
                                    total_map, legal_map, prev_total, prev_legal,
                                    lock_map=lock_map)
        if not chip_flow:
            raise ValueError("筹码动向表合成失败")

        return {
            "version": HOLDER_DIST_VERSION,
            "window": f"{window_start} ~ {latest}",
            "float_window": cur_period,
            "chip_flow": chip_flow,
        }
    except Exception as e:
        print(f"  ❌ 持股/户数数据集抓取失败: {e}")
        return None


def load_today_chip():
    """复用当天已抓取的数据集（季度数据，避免每 10 分钟重复下载全量）"""
    try:
        with open(f"{OUTPUT_DIR}/radar_data.json", "r", encoding="utf-8") as f:
            prev = json.load(f)
        d = prev.get("holder_chip") or {}
        if (d.get("date") == datetime.now().strftime("%Y-%m-%d")
                and d.get("version") == HOLDER_DIST_VERSION
                and d.get("chip_flow")):
            return d
    except Exception:
        pass
    return None


def calc_overview(stocks):
    """计算KPI汇总（三档并列：散户=小单、主力=超大单+大单、中单独立成档）"""
    retail_inflow = [s for s in stocks if s.get("retail_net", 0) > 0]
    retail_outflow = [s for s in stocks if s.get("retail_net", 0) < 0]
    medium_inflow = [s for s in stocks if s.get("medium_net", 0) > 0]
    medium_outflow = [s for s in stocks if s.get("medium_net", 0) < 0]
    main_inflow = [s for s in stocks if s.get("main_net", 0) > 0]
    main_outflow = [s for s in stocks if s.get("main_net", 0) < 0]

    # 三档绝对值之和（用于占比：按资金绝对体量加权）
    retail_abs = sum(abs(s.get("retail_net", 0)) for s in stocks)
    medium_abs = sum(abs(s.get("medium_net", 0)) for s in stocks)
    main_abs = sum(abs(s.get("main_net", 0)) for s in stocks)
    tier_total = retail_abs + medium_abs + main_abs
    if tier_total > 0.01:
        retail_pct = round(retail_abs / tier_total * 100, 1)
        medium_pct = round(medium_abs / tier_total * 100, 1)
        main_pct = round(main_abs / tier_total * 100, 1)
    else:
        retail_pct = medium_pct = main_pct = 0.0

    total_super = round(sum(s.get("super_net", 0) for s in stocks), 4)
    total_large = round(sum(s.get("large_net", 0) for s in stocks), 4)
    total_medium = round(sum(s.get("medium_net", 0) for s in stocks), 4)
    total_small = round(sum(s.get("small_net", 0) for s in stocks), 4)
    total_main = round(sum(s.get("main_net", 0) for s in stocks), 4)

    return {
        # 散户（小单）
        "inflow_count": len(retail_inflow),
        "outflow_count": len(retail_outflow),
        "net_amount": round(sum(s.get("retail_net", 0) for s in stocks), 2),
        "net_count": len(retail_inflow),
        # 中单（独立一档，前端新 KPI 卡）
        "medium_amount": round(total_medium, 2),
        "medium_inflow_count": len(medium_inflow),
        "medium_outflow_count": len(medium_outflow),
        # 主力（超大单+大单）
        "main_amount": total_main,
        "main_inflow_count": len(main_inflow),
        "main_outflow_count": len(main_outflow),
        "super_total": total_super,
        "large_total": total_large,
        "medium_total": total_medium,
        "small_total": total_small,
        # 资金构成占比（三档并列，按绝对额加权）
        "tier_share": {
            "retail_pct": retail_pct,
            "medium_pct": medium_pct,
            "main_pct": main_pct,
            "tier_total": round(tier_total, 2),
        },
        # 总量
        "total_stocks": len(stocks),
        # 全市场成交额（亿元）：快照内所有个股 f6 之和。
        # ⚠️ 只当交叉校验用 —— 界面上的成交额走「指数口径」(market_volume)，
        #    因为这份快照可能是跨时段拼的，盘中会系统性偏低。
        "total_amount": round(sum(s.get("total_amount") or 0 for s in stocks), 2),
    }


# ── 全市场成交额（指数口径）─────────────────────────────────────────
# 为什么不用 sum(f6) 画走势：f6 只存在于「当前这一份快照」里，拿不到历史；
# 而走势图的意义正是「今天比过去 20 天放量还是缩量」。所以用指数日K：
#   上证综指 1.000001 = 沪市全部股票（含科创板、B股）
#   深证综指 0.399106 = 深市全部股票（含创业板、B股）
#   北证50   0.899050 = 北交所
# 三者相加 ≈ 全市场成交额。2026-11-03 实测：与 sum(f6) 的偏差主要来自
# 「快照跨时段拼页」（深市代码在前、抓得更早），不是口径问题。
VOL_INDEXES = (("1.000001", "上证综指"), ("0.399106", "深证综指"), ("0.899050", "北证50"))
VOL_HISTORY_DAYS = 21        # 图上画 20 个已完成交易日 + 今日
VOL_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


def _vol_kline(secid, klt=101, lmt=30, timeout=15):
    """指数K线原始行：'日期,开,收,高,低,量,额'

    ⚠️ 必须重试：push2his 偶发 connect timeout（实测一次超时就整块没数据），
       而成交额只有 3 个请求，重试成本极低。
    """
    params = {
        "secid": secid, "klt": str(klt), "fqt": "0", "lmt": str(lmt),
        "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(VOL_KLINE_URL, params=params,
                                headers=EASTMONEY_HEADERS, timeout=timeout)
            return ((resp.json().get("data") or {}).get("klines") or [])
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))
    raise last_err


def _vol_daily():
    """{日期: 全市场成交额(亿元)}；沪、深缺任一则返回 None（半个市场没有意义）"""
    per, ok = {}, []
    for secid, name in VOL_INDEXES:
        try:
            rows = _vol_kline(secid, 101, VOL_HISTORY_DAYS + 5)
        except Exception as e:
            print(f"  ⚠️ 成交额：{name} 拉取失败（{e}）")
            continue
        if not rows:
            print(f"  ⚠️ 成交额：{name} 返回空")
            continue
        ok.append(secid)
        for line in rows:
            f = line.split(",")
            if len(f) < 7:
                continue
            try:
                amt = float(f[6]) / 1e8          # 元 → 亿元
            except ValueError:
                continue
            per[f[0][:10]] = per.get(f[0][:10], 0.0) + amt
    if "1.000001" not in ok or "0.399106" not in ok:
        return None
    return per


VOL_INTRADAY_KLT = 5
VOL_INTRADAY_LMT = 600        # 600 根 5 分钟K ≈ 13 个交易日，够做「昨日同期 / 近 5 日同期」


def _vol_intraday_one(secid):
    """单指数：{日期: {HH:MM: 截至该时刻的累计成交额（亿元）}}

    🩸 用 5 分钟K，不用 1 分钟K：1 分钟K（klt=1）无论 lmt/end 怎么传都只回当天，
       而 klt=5 受根数限制天然跨多日（实测 600 根 = 13 天）。这是做「同期对比」的唯一路径。
    """
    rows = _vol_kline(secid, VOL_INTRADAY_KLT, VOL_INTRADAY_LMT)
    raw = {}
    for line in rows:
        f = line.split(",")
        if len(f) < 7 or len(f[0]) < 16:
            continue
        try:
            amt = float(f[6]) / 1e8          # 元 → 亿元
        except ValueError:
            continue
        d, hm = f[0][:10], f[0][11:16]
        raw.setdefault(d, {})
        raw[d][hm] = raw[d].get(hm, 0.0) + amt
    out = {}
    for d, m in raw.items():
        cum, acc = {}, 0.0
        for hm in sorted(m):
            acc += m[hm]
            cum[hm] = acc
        out[d] = cum
    return out


def _vol_intraday():
    """三大指数的分时累计相加 → {日期: {HH:MM: 亿元}}；缺任一市场返回 None。

    ⚠️ 只保留三个市场都有数据的时间点：否则某个市场少一根K，合计就会凭空变小，
       看起来像「缩量」，比缺数据更糟。
    """
    series = []
    for secid, name in VOL_INDEXES:
        try:
            one = _vol_intraday_one(secid)
        except Exception as e:
            print(f"  ⚠️ 分时成交额：{name} 拉取失败（{e}）")
            return None
        if not one:
            print(f"  ⚠️ 分时成交额：{name} 返回空")
            return None
        series.append(one)

    merged = {}
    all_days = set()
    for s in series:
        all_days |= set(s.keys())
    for d in all_days:
        per_index = [s.get(d, {}) for s in series]
        times = set(per_index[0].keys())
        for m in per_index[1:]:
            times &= set(m.keys())
        if not times:
            continue
        cum = {}
        for hm in sorted(times):
            cum[hm] = sum(m[hm] for m in per_index)   # 各市场各自累计后再加总
        merged[d] = cum
    return merged


def _vol_same_time(intra, now, lookback=5):
    """同一时刻的对比：今日 vs 昨日同期 vs 近 N 日同期均值。

    🩸 盘中拿「今天半天的量」比「昨天全天的量」，会得出「缩量 43%」这种假结论 ——
       那两个数根本不是同一段东西。必须按 HH:MM 切齐再比。
    """
    if not intra:
        return None
    today = now.strftime("%Y-%m-%d")
    if today not in intra:
        return None
    hhmm_now = now.strftime("%H:%M")
    # 只取「已经走完」的窗口：最后一根 5 分钟K往往是半根，用它会让今天天然偏低
    done = [t for t in sorted(intra[today]) if t <= hhmm_now]
    if len(done) < 2:
        return None
    anchor = done[-1]
    today_cum = intra[today][anchor]
    prev_days = [d for d in sorted(intra) if d < today][-lookback:]
    pairs = [(d, intra[d][anchor]) for d in prev_days if anchor in intra[d] and intra[d][anchor]]
    if not pairs:
        return None
    prev_v = pairs[-1][1]
    avg = sum(v for _, v in pairs) / len(pairs)
    return {
        "hhmm": anchor,
        "today": round(today_cum, 1),
        "prev": round(prev_v, 1),
        "prev_date": pairs[-1][0],
        "avg": round(avg, 1),
        "n": len(pairs),
        "vs_prev_pct": round((today_cum / prev_v - 1) * 100, 1) if prev_v else None,
        "vs_avg_pct": round((today_cum / avg - 1) * 100, 1) if avg else None,
    }


def _vol_summary(per, now, intra=None):
    """纯函数：{日期: 亿元} → 对外结构。抽出来是为了能单测「跨月/跨年/盘中」这些边界。"""
    today = now.strftime("%Y-%m-%d")
    dates = sorted(per)
    if not dates:
        return None
    # 盘中/收盘后判定：15:05 之后当天数据基本定型（集合竞价与尾盘撮合都已完成）
    partial = dates[-1] == today and now.strftime("%H:%M") < "15:05"
    days = [{"d": d, "v": round(per[d], 1)} for d in dates[-VOL_HISTORY_DAYS:]]
    # 基准一律取「今天之前」的最后一个交易日。
    # 🩸 收盘后 days[-1] 就是今天，直接取最后一天会拿今天比自己 → 前端显示「较昨日 0.0%」，
    #    20 日均值也会把今天算进基准（自己拉高自己的比较线，放量会被自己吃掉一截）。
    past = [x for x in days if x["d"] < today]
    prev = past[-1]["v"] if past else None
    base = past[-20:]
    avg20 = round(sum(x["v"] for x in base) / len(base), 1) if base else None
    cur_v = per.get(today)
    return {
        "as_of": now.strftime("%Y-%m-%d %H:%M"),
        "unit": "亿",
        "days": days,
        "today_v": round(cur_v, 1) if cur_v else None,
        "partial": partial,
        "prev": prev,
        "avg20": avg20,
        # 同期对比（盘中判断放量/缩量只能用这个）
        "same_time": _vol_same_time(intra, now),
    }


def build_market_volume():
    """全市场成交额：今日值 + 近 20 日走势 + **同期**对比。失败返回 None，绝不抛异常。

    🩸 盘中不能拿「今天半天的量」比「昨天全天的量」——得出的「缩量 43%」是假的。
       所以额外拉 5 分钟K（klt=5，600 根 ≈ 13 天），按 HH:MM 切齐，
       对比「昨日同一时刻」与「近 5 日同一时刻均值」。日K那份只用于画走势和 20 日均值。
    """
    try:
        per = _vol_daily()
        if not per:
            print("  ⚠️ 成交额：指数K线没拿到，本版不供成交额")
            return None
        intra = None
        try:
            intra = _vol_intraday()
        except Exception as e:
            print(f"  ⚠️ 分时成交额不可用（将缺少同期对比）：{e}")
        out = _vol_summary(per, datetime.now(BJ_TZ), intra)
        if not out:
            return None
        state = "盘中" if out["partial"] else "已收盘"
        st = out.get("same_time")
        if st:
            print("  📈 成交额：今日 %.2f 万亿（%s）｜%s 同期 %.2f（%+.1f%%）｜近 %d 日同期均 %.2f（%+.1f%%）"
                  % ((st["today"] or 0) / 1e4, state, st["hhmm"],
                     st["prev"] / 1e4, st["vs_prev_pct"] or 0,
                     st["n"], st["avg"] / 1e4, st["vs_avg_pct"] or 0))
        elif out["avg20"]:
            print("  📈 成交额：今日 %.2f 万亿（%s）｜昨日全天 %.2f ｜20日均 %.2f（无同期数据）"
                  % ((out["today_v"] or 0) / 1e4, state,
                     (out["prev"] or 0) / 1e4, out["avg20"] / 1e4))
        else:
            print("  📈 成交额：%d 天（无对比基准）" % len(out["days"]))
        return out
    except Exception as e:
        print(f"  ⚠️ 成交额构建失败（不影响其他数据）：{e}")
        return None


def main():
    global _CAL, _CLI_BUDGET
    force = "--force" in sys.argv
    # --budget N：覆盖本次抓取的时间预算。
    # 手动刷新走 API（api_server.py 的 RUN_TIMEOUT + nginx 220s），必须传更小的值。
    if "--budget" in sys.argv:
        try:
            _CLI_BUDGET = int(sys.argv[sys.argv.index("--budget") + 1])
            print(f"  ⏱️ 本次抓取时间预算：{_CLI_BUDGET} 秒")
        except (IndexError, ValueError):
            print("  ⚠️ --budget 参数无效，改用默认预算")
    _CAL = load_trading_calendar()
    if _CAL:
        if datetime.now(BJ_TZ).year in _CAL["years"]:
            print(f"  📅 交易日历：覆盖 {sorted(_CAL['years'])}，"
                  f"全年休市工作日 {len(_CAL['closed'])} 天")
        else:
            print(f"  ⚠️ 交易日历未覆盖 {datetime.now(BJ_TZ).year} 年"
                  f"（当前 {sorted(_CAL['years'])}）→ 已退回「只看工作日」")
            print("     ↳ 请跑：python3 scripts/update_calendar.py")

    if not force and not in_trading_window():
        now = datetime.now(BJ_TZ)
        print("=" * 50)
        if not is_trading_day(now.date()):
            why = "周末" if now.weekday() >= 5 else "法定节假日休市"
            print(f"😴 今天不是交易日（{why}），跳过本次抓取")
        else:
            print(f"😴 非交易时段（北京时间 {now.strftime('%Y-%m-%d %H:%M')}），跳过本次抓取")
        print(f"   当前：{now.strftime('%Y-%m-%d %H:%M')}（北京时间）")
        print("   抓取时段：交易日 9:15–11:35 / 12:55–15:35")
        print("   需要强制抓取请加 --force，例如：python3 scripts/fetch_data.py --force")
        print("=" * 50)
        return

    retail_flow, data_status = fetch_retail_money_flow()
    overview = calc_overview(retail_flow)

    # 股东户数 + 机构持股：季度数据，同一天只抓一次（避免每 10 分钟下载 1.4MB 全量）
    holder_chip = load_today_chip()
    if holder_chip:
        print("  ♻️ 复用当天已抓取的股东户数 / 持股数据")
    else:
        holder_chip = build_holder_chip(retail_flow)
        if holder_chip:
            holder_chip["date"] = datetime.now().strftime("%Y-%m-%d")

    status_labels = {"live": "实时数据", "cached": "收盘数据（缓存）",
                     "partial": "数据不完整", "static": "估算数据"}
    source_labels = {"live": "东方财富push2接口", "cached": "缓存数据",
                     "partial": "东方财富push2接口（不完整）", "static": "估算数据"}
    overview["update_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    overview["data_status"] = data_status
    # 徽章文案带**真实数据日期**：缓存可能是几天前的快照，只写「收盘数据」会误导用户
    overview["data_label"] = label_for(data_status, LAST_FETCH_INFO.get("snapshot_time"))

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 🩸 last_updated 写**数据的真实时间**，不是"本次生成时间"。
    #    供数来自旧快照时若写生成时间，界面会拿 3 天前的数据配上"刚刚更新"的时间戳，
    #    用户看到的就是"数据是新的、内容是坏的"—— 2026-09-23 就是这么被误导的。
    #    副作用（正面）：快照没变时输出字节不变，条件请求会回 304，前端也不会空闪。
    data_time = LAST_FETCH_INFO.get("snapshot_time") or now_str

    # ── 财报（独立于行情：抓失败只影响这一块，不能拖垮主数据）──────────────
    finance = None
    try:
        code_set = {s.get("code") for s in retail_flow if s.get("code")}
        finance = build_finance(code_set)
    except Exception as e:
        print(f"  ⚠️ 财报块构建失败（不影响行情）：{e}")

    # ── 全市场成交额（同样独立：拉不到就藏起这块，不影响主数据）──────────
    market_volume = build_market_volume()

    output = {
        "overview": overview,
        "retail_flow": retail_flow,
        "holder_chip": holder_chip,
        "finance": finance,
        "market_volume": market_volume,
        "last_updated": data_time,
        "generated_at": now_str,          # 本次运行时间（排障用；界面显示的是 last_updated）
        "data_source": source_labels.get(data_status, "估算数据"),
        "data_status": data_status,
        # 抓取健康度：排障用，不参与前端业务逻辑
        "health": dict(LAST_FETCH_INFO),
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    # 🩸🩸 写缓存的**唯一**入口：必须是完整抓取。
    #    2026-09-23 就是因为这里放行了不完整数据，把 5918 条好缓存覆盖成 200 条、全站损毁。
    #    （不完整时的 🚨 告警已在 fetch_retail_money_flow() 的 _seal() 里打过，这里不重复）
    if should_save_cache(data_status):
        save_cache(output)
        print("  💾 已更新缓存（完整快照；旧快照已轮转为 .bak1/.bak2）")

    output_path = f"{OUTPUT_DIR}/radar_data.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 健康度单独落一个小文件：巡检查管道状态时不用下整份 radar_data.json
    try:
        with open(f"{OUTPUT_DIR}/health.json", "w", encoding="utf-8") as f:
            json.dump({"time": output["last_updated"], "status": data_status,
                       "selected_rows": len(retail_flow), **LAST_FETCH_INFO},
                      f, ensure_ascii=False, indent=1)
    except Exception:
        pass

    print(f"\n✅ 数据保存成功: {output_path}")
    print(f"🕐 更新时间: {output['last_updated']}")
    print(f"📊 散户资金: {len(retail_flow)} 条 [{status_labels.get(data_status, data_status)}]")
    print(f"💰 散户净额: {overview['net_amount']}亿 | 中单净额: {overview['medium_amount']}亿 | 主力净额: {overview['main_amount']}亿")
    print(f"   散户 {overview['inflow_count']}流入/{overview['outflow_count']}流出 · 中单 {overview['medium_inflow_count']}/{overview['medium_outflow_count']} · 主力 {overview['main_inflow_count']}/{overview['main_outflow_count']}")
    ts = overview['tier_share']
    print(f"📊 三档占比: 散户{ts['retail_pct']}% · 中单{ts['medium_pct']}% · 主力{ts['main_pct']}%（按|净额|之和加权，总和{ts['tier_total']}亿）")
    print(f"📈 四档: 超大单{overview['super_total']}亿 | 大单{overview['large_total']}亿 | 中单{overview['medium_total']}亿 | 小单{overview['small_total']}亿")
    if holder_chip and holder_chip.get("chip_flow"):
        cf = holder_chip["chip_flow"]
        n_na = sum(1 for x in cf["rows"] if x["holder_chg"] is None)
        print(f"📦 筹码动向: {len(cf['rows'])} 只（{cf['period']} vs {cf.get('prev_period') or '无'}）｜户数变化不可比 {n_na} 只")
    print("=" * 50)


if __name__ == "__main__":
    main()
