#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分页抓取的回归测试 —— 2026-09-18「凯盛科技 600552 没数据」事故

事故链条
--------
1. push2 的 clist 接口**实测会把 pz 压到 100 条/页**（传 5000 也只回 100）
   → 全市场 ~5900 只要打 **60 次分页请求**
2. 原代码用 `fid=f62`（主力净额）排序 —— 这是盘中**每时每刻都在变**的字段
   → 60 次请求之间顺序一直在漂 → 分页边界上的票被重复抓 / 被整只跳过
3. 完成度校验用的是**原始行数**，重复行把数字灌水 → 判定"抓全了"
   → 180 只漏票**静默**混过去（600552 就是其中之一）

修复
----
A. `fid=f12`（证券代码）+ `po=0` —— 排序与盘中变化无关，分页边界不再漂
B. 入列前按代码去重 —— 万一还漂，也只少几条、绝不出重复行
C. 完成度校验改用**去重后的唯一代码数** —— 漏了就会说话，不再静默

跑法：  python3 tests/test_fetch_paging.py
"""
import contextlib
import importlib.util
import io
import json
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# fetch_data.py 顶层 import requests；这个测试会把 requests.Session 换成假服务端，
# 所以没装 requests 的环境（如裸 python3）塞个空壳就行，不必真装依赖。
try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    import types
    requests = types.ModuleType("requests")
    requests.Session = object
    sys.modules["requests"] = requests

_spec = importlib.util.spec_from_file_location(
    "fetch_data", os.path.join(ROOT, "scripts", "fetch_data.py"))
fd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fd)

PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS " + name)
    else:
        FAIL += 1
        print("FAIL " + name + (("  -> " + str(detail)) if detail else ""))


N = 250          # 假装全市场 250 只
PZ_CAP = 100     # 服务端的真实行为：pz 上限 100
VOLATILE = {"f62", "f3", "f6", "f184"}   # 盘中一直在变的字段


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self.encoding = "utf-8"
        self._p = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class FakeEM:
    """模拟 push2 clist：
       · pz 被压到 100
       · 用「实时变动」的字段排序时，**每次请求看到的顺序都不一样**（这就是事故的物理原因）
       · 用 f12（代码）排序时顺序恒定 —— 与时间无关
       · drop_page 可模拟某一页丢失
    """

    def __init__(self, drop_page=None, seed=12345):
        self.codes = ["%06d" % i for i in range(1, N + 1)]
        self.drop_page = drop_page
        self.seed = seed
        self.calls = []
        self.n = 0

    def _order(self, volatile):
        """本次请求看到的排序结果。
           volatile（f62 等）：只让**每页边界附近 ±4 名**的票重新洗牌 ——
             真实盘口里这些票的主力净额都≈0，谁排前面纯属噪声，正是它们来回跨过
             分页边界，造成「有的重复抓、有的整只漏」。整体顺序不会天翻地覆。
           stable（f12）：永远按代码升序，与盘中变化无关。
        """
        order = list(self.codes)
        if not volatile:
            return order
        rng = random.Random(self.seed + self.n)
        for k in range(1, len(order) // PZ_CAP + 1):
            b = k * PZ_CAP
            lo, hi = max(0, b - 4), min(len(order), b + 4)
            seg = order[lo:hi]
            rng.shuffle(seg)
            order[lo:hi] = seg
        return order

    def get(self, url, params=None, headers=None, timeout=None):
        p = dict(params or {})
        self.calls.append(p)
        self.n += 1
        pn = int(p.get("pn", 1))
        pz = min(int(p.get("pz", 100)), PZ_CAP)
        order = self._order(p.get("fid") in VOLATILE)
        diff = [] if pn == self.drop_page else [
            {"f12": c, "f14": "TEST" + c, "f2": 10.0, "f3": 1.0, "f6": 1e8,
             "f8": 1.0, "f10": 1.0, "f24": 0.0, "f25": 0.0, "f26": "20100101",
             "f38": 1e8, "f39": 1e8, "f100": "测试行业",
             "f62": 0.0, "f184": 0.0, "f66": 0.0, "f69": 0.0, "f72": 0.0,
             "f75": 0.0, "f78": 0.0, "f81": 0.0, "f84": 0.0, "f87": 0.0,
             "f124": 0.0}
            for c in order[(pn - 1) * pz: pn * pz]]
        return _Resp({"data": {"total": N, "diff": diff}})


def run(fake):
    """把假服务端塞进 requests.Session，跑真正的 fetch_from_eastmoney()"""
    import requests
    real_session = requests.Session
    real_sleep = time.sleep
    requests.Session = lambda *a, **k: fake
    time.sleep = lambda *a, **k: None
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            stocks = fd.fetch_from_eastmoney()
    finally:
        requests.Session = real_session
        time.sleep = real_sleep
    return stocks, buf.getvalue()


print("=== 1. 假服务端自检：它确实能复现「排序漂移」这件事 ===")
f0 = FakeEM()
seen = []
for pn in range(1, 4):
    seen += [x["f12"] for x in f0.get("u", {"pn": pn, "pz": 5000, "fid": "f62"}) \
             .json()["data"]["diff"]]
dup0 = len(seen) - len(set(seen))
skip0 = N - len(set(seen))
ok("用 f62（主力净额）排序 -> 确实出现重复抓取", dup0 > 0, "重复 %d" % dup0)
ok("用 f62（主力净额）排序 -> 确实出现整只漏掉", skip0 > 0, "漏 %d" % skip0)

print("\n=== 2. 修复后：按 f12（代码）排序，一只不漏、一行不重 ===")
fake = FakeEM()
stocks, out = run(fake)
codes = [s["code"] for s in stocks]
ok("★ 全部请求都用 f12（时间无关）排序", all(c.get("fid") == "f12" for c in fake.calls),
   sorted({c.get("fid") for c in fake.calls}))
ok("★ 升序（po=0），不是原来那个盘中乱变的降序", all(str(c.get("po")) == "0" for c in fake.calls),
   sorted({str(c.get("po")) for c in fake.calls}))
ok("★ 250 只一只不漏", len(set(codes)) == N, len(set(codes)))
ok("★ 没有任何重复行（同一个代码只出现一次）", len(codes) == len(set(codes)),
   "%d 行 / %d 唯一" % (len(codes), len(set(codes))))
ok("★ 完成度按「唯一代码」判定，并如实打印", ("250/250" in out) and ("唯一代码" in out),
   [l for l in out.splitlines() if "东方财富返回" in l])

print("\n=== 3. 修复后：真丢了一页，必须说话，不许静默放过 ===")
fake2 = FakeEM(drop_page=3)
stocks2, out2 = run(fake2)
codes2 = [s["code"] for s in stocks2]
ok("★ 丢页时明确报「数据不完整」（原来被重复行灌水掩盖）", "数据不完整" in out2,
   [l for l in out2.splitlines() if "不完整" in l][:1])
ok("★ 并且明确说「使用已抓取最多的 N 条」", "未能取得完整全市场数据" in out2)
ok("丢页时也不出现重复行", len(codes2) == len(set(codes2)),
   "%d 行 / %d 唯一" % (len(codes2), len(set(codes2))))

print("\nPASS=%d  FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
