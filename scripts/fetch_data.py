#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小散研究院 - 数据抓取脚本 v3
数据源：东方财富 push2 行情接口（四档资金流：超大单/大单/中单/小单，均为净额）
散户 = 小单净额，主力 = 超大单 + 大单（东财 f62 口径）
"""

import json
import os
import time
import requests
from datetime import datetime, timedelta

OUTPUT_DIR = "data"
CACHE_FILE = f"{OUTPUT_DIR}/radar_data_cache.json"

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
                while page <= total_pages:
                    params = {
                        "pn": page,
                        "pz": 5000,
                        "po": 1,
                        "np": 1,
                        "fltt": 2,
                        "invt": 2,
                        "fid": "f62",
                        "fs": "m:0 t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                        "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f100,f124",
                    }
                    data = _fetch_page(session, base_url, params)
                    items = data["data"].get("diff", [])
                    if page == 1:
                        expected_total = data["data"].get("total", 0)
                        # 不同域名单页上限不同（push2=5000，push2delay=100），按实际返回条数动态分页
                        if items:
                            total_pages = (expected_total + len(items) - 1) // len(items)
                    all_stocks.extend(items)
                    print(f"  📄 [{base_url.split('//')[1].split('.')[0]}] 第{page}/{total_pages}页: {len(items)} 条")
                    page += 1
                    if not items:
                        break

                if len(all_stocks) > len(best_stocks):
                    best_stocks = list(all_stocks)

                # 完整性校验：抓到条数需达到接口 total 的 98%，否则视为分页中途中断
                if expected_total and len(all_stocks) >= expected_total * 0.98:
                    print(f"  📊 东方财富返回 {len(all_stocks)}/{expected_total} 条（via {base_url.split('//')[1]}）")
                    complete = True
                    break
                print(f"  ⚠️ {base_url} 数据不完整 {len(all_stocks)}/{expected_total} 条，尝试其他节点...")
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
    #          f84=小单净额, f87=小单净占比, f124=5日涨跌
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

        if small_pct != 0:
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
            "dynamic_ratio": dynamic_ratio,
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
                  "AVG_MARKET_CAP,TOTAL_MARKET_CAP,INTERVAL_CHRATE")

# 股东户数「存量」分档（用于全市场分布统计）
# 注意：档位文案不要用 < / >，前端是直接拼进 HTML 的
HOLDER_BUCKETS = [
    ("1 万以下", 0, 10000),
    ("1 万 ~ 5 万", 10000, 50000),
    ("5 万 ~ 20 万", 50000, 200000),
    ("20 万以上", 200000, None),
]

# 分档口径版本号：改了档位定义就 +1，让当天缓存失效重算
HOLDER_DIST_VERSION = 2


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


def fetch_shareholder_count():
    """从东方财富数据中心获取股东户数变化数据（增幅榜 + 减幅榜）

    接口：RPT_HOLDERNUMLATEST —— 每只股票在该表里只有一条「最新」披露记录。

    三个必须注意的点（否则前端「户数减少 TOP」恒为空 / 榜单被垃圾数据污染）：
    1. **必须双向请求**：接口只返回第一页，按 HOLDER_NUM_RATIO 降序拿到的 200 条
       全是正值，减幅榜自然一条都筛不出来。所以要再按升序请求一次拿减幅榜。
    2. **必须剔除伪比例**：次新股上市后户数从「4 户 / 60 户」这种极小基数暴增，
       比例能算到 +576 万 %，完全无意义。用上期户数门槛（PRE_HOLDER_NUM >= 1000）剔除。
    3. **必须限制披露窗口**：退市 / 停更股票的记录停留在数年前，用「最新披露日 - 180 天」
       作为窗口，只保留仍在正常披露的股票。
    """
    print("🔍 正在从东方财富获取股东户数变化数据...")
    try:
        # 1) 最新披露日 → 有效性窗口
        latest, window_start = _holder_latest_and_window()

        # 2) 有效样本过滤：上期户数 ≥1000（剔除次新股伪比例）+ 当期户数 ≥5000 + 披露日在窗口内
        flt = (f"(PRE_HOLDER_NUM>=1000)(HOLDER_NUM>=5000)"
               f"(END_DATE>='{window_start}')")

        # 3) 双向请求：降序 = 增幅榜，升序 = 减幅榜
        inc_rows = _holder_query({"sortColumns": "HOLDER_NUM_RATIO", "sortTypes": "-1", "filter": flt})
        dec_rows = _holder_query({"sortColumns": "HOLDER_NUM_RATIO", "sortTypes": "1", "filter": flt})

        seen, stocks = set(), []
        for item in inc_rows + dec_rows:
            code = (item.get("SECURITY_CODE") or "").strip()
            name = (item.get("SECURITY_NAME_ABBR") or "").strip()
            ratio = item.get("HOLDER_NUM_RATIO")
            if not code or not name or ratio is None or code in seen:
                continue
            pct = round(float(ratio), 2)
            # 户数变化超过 10 倍的记录基本是次新股 / 上市口径差异造成，与「筹码集中分散」无关
            if pct <= -99.99 or pct >= 1000:
                continue
            seen.add(code)
            end_date = (item.get("END_DATE") or "")[:10]
            stocks.append({
                "code": code,
                "name": name,
                "current": int(item.get("HOLDER_NUM") or 0),
                "previous": int(item.get("PRE_HOLDER_NUM") or 0),
                "increase": int(item.get("HOLDER_NUM_CHANGE") or 0),
                "change_pct": pct,
                "period": end_date[:7],
                "avg_market_cap": round(float(item.get("AVG_MARKET_CAP") or 0), 2),
                "total_market_cap": round(float(item.get("TOTAL_MARKET_CAP") or 0), 2),
                "interval_chg": round(float(item.get("INTERVAL_CHRATE") or 0), 2),
                "notice_date": (item.get("HOLD_NOTICE_DATE") or "")[:10],
            })

        if not stocks:
            print("  ⚠️ API返回空，使用备用数据")
            return _fallback_shareholder_data()

        stocks.sort(key=lambda x: x["change_pct"], reverse=True)
        n_up = sum(1 for s in stocks if s["change_pct"] > 0)
        n_down = sum(1 for s in stocks if s["change_pct"] < 0)
        print(f"  ✅ 股东户数 {len(stocks)} 条（增幅 {n_up} / 减幅 {n_down}，窗口 {window_start}~{latest}）")
        return stocks

    except Exception as e:
        print(f"  ❌ 东方财富API失败: {e}")
        return _fallback_shareholder_data()


def _fallback_shareholder_data():
    print("  ⚠️ 使用备用股东户数数据")
    fallback = [
        {"code": "000725", "name": "京东方A", "current": 1897600, "previous": 971900, "increase": 925600, "change_pct": 95.23, "period": "2026Q2", "avg_market_cap": 8.5, "total_market_cap": 1613, "interval_chg": 121.99, "notice_date": "2026-08-30"},
        {"code": "600522", "name": "中天科技", "current": 818100, "previous": 226200, "increase": 591900, "change_pct": 261.64, "period": "2026Q2", "avg_market_cap": 12.3, "total_market_cap": 1006, "interval_chg": 85.32, "notice_date": "2026-08-28"},
        {"code": "600584", "name": "长电科技", "current": 804000, "previous": 304000, "increase": 500000, "change_pct": 164.54, "period": "2026Q2", "avg_market_cap": 45.6, "total_market_cap": 3666, "interval_chg": 78.45, "notice_date": "2026-08-29"},
        {"code": "600378", "name": "昊华科技", "current": 152400, "previous": 27300, "increase": 125100, "change_pct": 457.26, "period": "2026Q2", "avg_market_cap": 28.7, "total_market_cap": 437, "interval_chg": 45.23, "notice_date": "2026-08-25"},
        {"code": "603203", "name": "快克智能", "current": 64300, "previous": 14700, "increase": 49600, "change_pct": 335.83, "period": "2026Q2", "avg_market_cap": 35.2, "total_market_cap": 226, "interval_chg": 67.89, "notice_date": "2026-08-22"},
        {"code": "600707", "name": "彩虹股份", "current": 268900, "previous": 71000, "increase": 197900, "change_pct": 278.83, "period": "2026Q2", "avg_market_cap": 15.8, "total_market_cap": 425, "interval_chg": 92.56, "notice_date": "2026-08-26"},
        {"code": "603986", "name": "兆易创新", "current": 360300, "previous": 243800, "increase": 116500, "change_pct": 47.81, "period": "2026Q2", "avg_market_cap": 85.3, "total_market_cap": 3073, "interval_chg": -12.34, "notice_date": "2026-08-27"},
        {"code": "300308", "name": "中际旭创", "current": 205700, "previous": 154400, "increase": 51300, "change_pct": 33.20, "period": "2026Q2", "avg_market_cap": 156.7, "total_market_cap": 3224, "interval_chg": -8.76, "notice_date": "2026-08-28"},
        {"code": "300476", "name": "胜宏科技", "current": 281400, "previous": 206000, "increase": 75400, "change_pct": 36.60, "period": "2026Q2", "avg_market_cap": 42.1, "total_market_cap": 1185, "interval_chg": -5.43, "notice_date": "2026-08-26"},
        {"code": "000021", "name": "深科技", "current": 489589, "previous": 503900, "increase": -14311, "change_pct": -2.84, "period": "2026-08", "avg_market_cap": 22.5, "total_market_cap": 1102, "interval_chg": -3.21, "notice_date": "2026-09-01"},
        {"code": "300615", "name": "欣天科技", "current": 18700, "previous": 13760, "increase": 4940, "change_pct": 35.94, "period": "2026-08", "avg_market_cap": 18.6, "total_market_cap": 35, "interval_chg": 15.67, "notice_date": "2026-09-02"},
        {"code": "300006", "name": "莱美药业", "current": 29000, "previous": 23667, "increase": 5333, "change_pct": 22.56, "period": "2026-08", "avg_market_cap": 6.8, "total_market_cap": 20, "interval_chg": 8.92, "notice_date": "2026-09-03"},
        {"code": "600519", "name": "贵州茅台", "current": 218600, "previous": 265400, "increase": -46800, "change_pct": -17.63, "period": "2026-06", "avg_market_cap": 1250.4, "total_market_cap": 15520, "interval_chg": -9.87, "notice_date": "2026-08-29"},
        {"code": "601318", "name": "中国平安", "current": 1163400, "previous": 1358900, "increase": -195500, "change_pct": -14.39, "period": "2026-06", "avg_market_cap": 118.7, "total_market_cap": 9860, "interval_chg": -6.52, "notice_date": "2026-08-28"},
        {"code": "000858", "name": "五粮液", "current": 412000, "previous": 468300, "increase": -56300, "change_pct": -12.02, "period": "2026-06", "avg_market_cap": 342.6, "total_market_cap": 4210, "interval_chg": -4.11, "notice_date": "2026-08-30"},
        {"code": "002594", "name": "比亚迪", "current": 755000, "previous": 718600, "increase": 36400, "change_pct": 5.06, "period": "2026Q2", "avg_market_cap": 85.2, "total_market_cap": 6433, "interval_chg": -2.15, "notice_date": "2026-08-30"},
        {"code": "000977", "name": "浪潮信息", "current": 245000, "previous": 198000, "increase": 47000, "change_pct": 23.74, "period": "2026Q2", "avg_market_cap": 98.5, "total_market_cap": 2413, "interval_chg": 12.34, "notice_date": "2026-08-29"},
    ]
    fallback.sort(key=lambda x: x["change_pct"], reverse=True)
    return fallback


def build_holder_distribution(stocks):
    """全市场股东户数「存量」分布（按户数分档，含各档资金构成）

    与「户数变化榜」不同，这里要的是**存量**：不管涨了还是跌了，只看
    「每只股票现在有多少户」，据此统计全市场分布。

    - 需要全量拉取（接口单页上限 500 条 → 约 11 页），单次约 1.4MB
    - 股东户数是季度数据，没必要每 10 分钟重抓 → main() 里做了「同一天复用一次」的缓存
    - 各档的「资金构成」由当日三档净额按 |净额| 加权算出，用来观察
      不同户数规模（≈筹码分散程度）的股票，资金结构有没有差别
    """
    print("🔍 正在获取全市场股东户数存量分布...")
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
        total_n = len(rows)
        total_holders = sum(int(r.get("HOLDER_NUM") or 0) for r in rows)

        buckets = []
        for label, lo, hi in HOLDER_BUCKETS:
            grp = [r for r in rows
                   if lo <= int(r.get("HOLDER_NUM") or 0)
                   and (hi is None or int(r.get("HOLDER_NUM") or 0) < hi)]
            if not grp:
                continue
            holders = sum(int(r.get("HOLDER_NUM") or 0) for r in grp)
            # 该档的资金构成：三档 |净额| 加权
            ra = ma = na = 0.0
            for r in grp:
                s = by_code.get(r.get("SECURITY_CODE"))
                if not s:
                    continue
                ra += abs(s.get("retail_net", 0))
                ma += abs(s.get("medium_net", 0))
                na += abs(s.get("main_net", 0))
            tt = ra + ma + na
            buckets.append({
                "label": label,
                "count": len(grp),
                "count_pct": round(len(grp) / total_n * 100, 1),
                "holders": holders,
                "holders_pct": round(holders / total_holders * 100, 1) if total_holders else 0,
                "retail_pct": round(ra / tt * 100, 1) if tt > 0 else 0,
                "medium_pct": round(ma / tt * 100, 1) if tt > 0 else 0,
                "main_pct": round(na / tt * 100, 1) if tt > 0 else 0,
            })

        print(f"  ✅ 户数存量分布: {total_n} 只 / 总户数 {total_holders/1e8:.2f} 亿，{len(buckets)} 档")
        return {
            "version": HOLDER_DIST_VERSION,
            "total_stocks": total_n,
            "total_holders": total_holders,
            "window": f"{window_start} ~ {latest}",
            "buckets": buckets,
        }
    except Exception as e:
        print(f"  ❌ 户数存量分布抓取失败: {e}")
        return None


def load_today_distribution():
    """复用当天已抓取的户数分布（季度数据，避免每 10 分钟重复下载全量）"""
    try:
        with open(f"{OUTPUT_DIR}/radar_data.json", "r", encoding="utf-8") as f:
            prev = json.load(f)
        d = prev.get("holder_distribution") or {}
        if (d.get("date") == datetime.now().strftime("%Y-%m-%d")
                and d.get("version") == HOLDER_DIST_VERSION
                and d.get("buckets")):
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
    retail_flow, data_status = fetch_retail_money_flow()
    shareholder_count = fetch_shareholder_count()
    overview = calc_overview(retail_flow)

    # 户数存量分布：季度数据，同一天只抓一次（避免每 10 分钟下载 1.4MB 全量）
    holder_distribution = load_today_distribution()
    if holder_distribution:
        print("  ♻️ 复用当天已抓取的户数存量分布")
    else:
        holder_distribution = build_holder_distribution(retail_flow)
        if holder_distribution:
            holder_distribution["date"] = datetime.now().strftime("%Y-%m-%d")

    status_labels = {"live": "实时数据", "cached": "收盘数据（缓存）", "static": "估算数据"}
    overview["update_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    overview["data_status"] = data_status
    overview["data_label"] = status_labels.get(data_status, "")

    output = {
        "overview": overview,
        "retail_flow": retail_flow,
        "shareholder_count": shareholder_count,
        "holder_distribution": holder_distribution,
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
    print(f"👥 股东户数: {len(shareholder_count)} 条")
    if holder_distribution:
        print(f"📦 户数存量分布: {holder_distribution['total_stocks']} 只 / 总户数 {holder_distribution['total_holders']/1e8:.2f} 亿")
        for b in holder_distribution["buckets"]:
            print(f"   {b['label']:<12} {b['count']:>5} 只（{b['count_pct']}%） 户数占比 {b['holders_pct']}% 资金构成 {b['retail_pct']}/{b['medium_pct']}/{b['main_pct']}")
    print("=" * 50)


if __name__ == "__main__":
    main()
