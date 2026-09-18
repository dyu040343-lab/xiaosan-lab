#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小散研究院 - 数据抓取脚本 v3
数据源：东方财富 push2 行情接口（四档资金流：超大单/大单/中单/小单，均为净额）
散户 = 小单净额，主力 = 超大单 + 大单（东财 f62 口径）
"""

import json
import os
import sys
import time
import requests
from datetime import datetime, timedelta, timezone

OUTPUT_DIR = "data"
CACHE_FILE = f"{OUTPUT_DIR}/radar_data_cache.json"

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


def load_cache():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return None


def save_cache(data):
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except:
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


def _fetch_page(session, base_url, params):
    """请求单页数据，失败时输出详细诊断信息"""
    resp = session.get(base_url + "/api/qt/clist/get", params=params, headers=EM_HEADERS, timeout=15)
    resp.encoding = "utf-8"
    if resp.status_code != 200:
        print(f"  ⚠️ HTTP {resp.status_code} 来自 {base_url}，响应头200字节: {resp.text[:200]}")
        resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or data.get("data") is None:
        print(f"  ⚠️ {base_url} 返回非数据内容: {str(data)[:200]}")
        raise ValueError("API返回无数据（可能被反爬拦截）")
    return data


def fetch_from_eastmoney():
    """东方财富 push2 API - 四档完整数据
    多域名容灾：push2 主站 / push2delay 延迟镜像（主站节点故障时自动切换）
    """
    print("🔍 [数据源] 东方财富 push2 API（四档资金流）...")

    # 域名按优先级排列：主站 -> 延迟行情镜像
    BASE_URLS = [
        "https://push2.eastmoney.com",
        "https://push2delay.eastmoney.com",
    ]

    session = requests.Session()
    best_stocks = []
    complete = False

    for attempt in range(3):
        for base_url in BASE_URLS:
            all_stocks = []
            page = 1
            total_pages = 1
            expected_total = 0
            try:
                # 已抓到的代码集合：分页漂移的兜底，也是"完成度"的真相来源
                seen_codes = set()
                while page <= total_pages:
                    params = {
                        "pn": page,
                        # ⚠️ 实测服务端会把 pz 压到 100（传 5000 也只回 100）→
                        #    全市场 ~5900 只要打 60 页，分页期间"顺序稳定性"是生死线
                        "pz": 5000,
                        "po": 0,          # 0 = 升序
                        "np": 1,
                        "fltt": 2,
                        "invt": 2,
                        # 🩸 fid 必须用**与盘中变化无关**的字段排序（f12 = 证券代码）。
                        #    原来用 f62（主力净额）：盘中每次请求之间排序都在变，
                        #    60 页打下来的边界漂移 = 有的票重复抓、有的票整只漏掉。
                        #    2026-09-18 实测：5917 行里 183 个代码重复、180 只整只漏掉，
                        #    用户报的「凯盛科技 600552 没数据」就是被漏掉的其中之一
                        #    （东财侧它一直正常交易：10.4 亿成交额、6.25% 换手）。
                        "fid": "f12",
                        "fs": "m:0 t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                        "fields": "f12,f14,f2,f3,f6,f8,f10,f24,f25,f26,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f100,f124,f38,f39",
                    }
                    data = _fetch_page(session, base_url, params)
                    items = data["data"].get("diff", [])
                    if page == 1:
                        expected_total = data["data"].get("total", 0)
                        # 不同域名单页上限不同（push2=5000，push2delay=100），按实际返回条数动态分页
                        if items:
                            total_pages = (expected_total + len(items) - 1) // len(items)
                    # 按代码去重再入列：万一顺序还是漂了，宁可少几条也绝不出重复行
                    # （重复会让下面的完成度校验虚高 —— 原来 5917 行其实只有 5731 个唯一代码，
                    #   却判定"抓全了"，于是 180 只漏票静默混过去）
                    for it in items:
                        c = str(it.get("f12", "")).strip()
                        if c and c not in seen_codes:
                            seen_codes.add(c)
                            all_stocks.append(it)
                    print(f"  📄 [{base_url.split('//')[1].split('.')[0]}] 第{page}/{total_pages}页: {len(items)} 条（累计唯一 {len(all_stocks)}）")
                    page += 1
                    if not items:
                        break

                if len(all_stocks) > len(best_stocks):
                    best_stocks = list(all_stocks)

                # 完整性校验：**唯一代码数**需达到接口 total 的 98%，否则视为分页中途中断
                #   ⚠️ 必须用 all_stocks（已去重）而不是原始行数：原始行数被重复行灌水，
                #      会把"漏了 180 只"误判成"抓全了"（2026-09-18 踩过）
                if expected_total and len(all_stocks) >= expected_total * 0.98:
                    print(f"  📊 东方财富返回 {len(all_stocks)}/{expected_total} 只（去重后唯一代码，via {base_url.split('//')[1]}）")
                    complete = True
                    break
                print(f"  ⚠️ {base_url} 数据不完整 {len(all_stocks)}/{expected_total} 只（唯一代码），尝试其他节点...")
            except Exception as e:
                if len(all_stocks) > len(best_stocks):
                    best_stocks = list(all_stocks)
                print(f"  ⚠️ {base_url} 第{attempt+1}/3次获取失败: {e}")
            time.sleep(2)
        if complete:
            break
        time.sleep(2)

    all_stocks = best_stocks
    if not all_stocks:
        print("  📦 push2 全部域名不可用，尝试缓存...")
        return []
    if not complete:
        print(f"  ⚠️ 未能取得完整全市场数据，使用已抓取最多的 {len(all_stocks)} 条")

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
    return stocks


def fetch_retail_money_flow():
    print("=" * 50)
    print("📡 小散研究院 - 数据抓取中...")
    print("=" * 50)

    stocks = fetch_from_eastmoney()
    if stocks and len(stocks) > 50:
        print(f"  ✅ 使用东方财富全市场数据（{len(stocks)} 条）")
        return stocks, "live"

    print("📦 东方财富不可用，尝试缓存...")
    cache = load_cache()
    if cache and cache.get("retail_flow"):
        cached = cache["retail_flow"]
        print(f"  ✅ 缓存 {len(cached)} 条 [缓存于 {cache.get('last_updated')}]")
        return cached, "cached"

    print("📦 使用备用数据...")
    fallback = get_fallback_data()
    print(f"  ✅ 备用 {len(fallback)} 条 [静态]")
    return fallback, "static"


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


def _orghold_query(extra, page_size=500, page_number=1):
    """请求东财数据中心 RPT_MAIN_ORGHOLD（机构持股，季度披露）"""
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
            return (payload.get("result") or {}).get("data") or []
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise last_err


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
    """取机构持股的最近两个报告期（降序）"""
    head = _orghold_query({"sortColumns": "REPORT_DATE", "sortTypes": "-1"}, page_size=1)
    cur = (head[0].get("REPORT_DATE") or "")[:10] if head else ""
    if not cur:
        return []
    prev_head = _orghold_query(
        {"sortColumns": "REPORT_DATE", "sortTypes": "-1",
         "filter": f"(REPORT_DATE<'{cur}')"}, page_size=1)
    prev = (prev_head[0].get("REPORT_DATE") or "")[:10] if prev_head else ""
    return [p for p in (cur, prev) if p]


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
    }


def main():
    global _CAL
    force = "--force" in sys.argv
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

    status_labels = {"live": "实时数据", "cached": "收盘数据（缓存）", "static": "估算数据"}
    overview["update_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    overview["data_status"] = data_status
    overview["data_label"] = status_labels.get(data_status, "")

    output = {
        "overview": overview,
        "retail_flow": retail_flow,
        "holder_chip": holder_chip,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_source": "东方财富push2接口" if data_status == "live" else ("缓存数据" if data_status == "cached" else "估算数据"),
        "data_status": data_status,
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if data_status == "live":
        save_cache(output)
        print("  💾 已更新缓存")

    output_path = f"{OUTPUT_DIR}/radar_data.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

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
