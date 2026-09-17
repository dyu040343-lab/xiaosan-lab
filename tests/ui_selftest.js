#!/usr/bin/env node
/*
 * 前端自测（无浏览器环境）
 * ─────────────────────────────────────────────────────────────
 * 把 retail-radar.html 里最后一个 <script> 抽出来，在 vm + 假 DOM 里跑，
 * 用「真实数据」驱动渲染函数，然后对渲染结果做断言。
 *
 * 用法：
 *   node tests/ui_selftest.js                       # 用本地 data/radar_data.json
 *   RR_DATA=/tmp/live.json node tests/ui_selftest.js # 用指定的数据快照（推荐用线上拉下来的）
 *
 * ⚠️ 为什么放在仓库里：之前放在 /tmp，机器重启/清临时目录就全丢了，等于每次重写。
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const PROJ = path.resolve(__dirname, '..');
const HTML = fs.readFileSync(path.join(PROJ, 'retail-radar.html'), 'utf8');
const DATA_PATH = process.env.RR_DATA || path.join(PROJ, 'data/radar_data.json');
const raw = JSON.parse(fs.readFileSync(DATA_PATH, 'utf8'));
const cal = JSON.parse(fs.readFileSync(path.join(PROJ, 'data/trading_calendar.json'), 'utf8'));

let pass = 0, fail = 0;
function ok(name, cond, detail) {
  if (cond) { pass++; console.log('PASS ' + name); }
  else { fail++; console.log('FAIL ' + name + (detail === undefined ? '' : '  → ' + detail)); }
}
const num = (v) => Number(v);

/* ── 假 DOM ── */
function mkEl(id) {
  const st = new Set();
  const el = {
    id, textContent: '', innerHTML: '', value: '', disabled: false, hidden: false, title: '',
    style: {}, dataset: {}, offsetWidth: 100,
    classList: {
      add(c) { st.add(c); }, remove(c) { st.delete(c); },
      toggle(c, on) { if (on === undefined) { st.has(c) ? st.delete(c) : st.add(c); } else if (on) st.add(c); else st.delete(c); },
      contains(c) { return st.has(c); }, _set: st,
    },
    focus() {}, select() {}, click() {}, remove() {}, appendChild() {}, removeChild() {},
    scrollIntoView() {}, addEventListener() {}, removeEventListener() {},
    setAttribute(k, v) { this[k] = v; }, removeAttribute(k) { delete this[k]; },
    getAttribute(k) { return this[k] === undefined ? null : this[k]; },
    closest() { return null; }, querySelector() { return null; }, querySelectorAll() { return []; },
  };
  return el;
}
const els = {};
const doc = {
  getElementById(id) { return els[id] || (els[id] = mkEl(id)); },
  querySelector(sel) {
    // 任何选择器都给一个稳定元素（页面里有 document.querySelector('.tab-bar') 之类）
    if (sel === '.toggle span') return els['__toggle'] || (els['__toggle'] = mkEl('toggle'));
    const key = '__q:' + sel;
    return els[key] || (els[key] = mkEl(key));
  },
  querySelectorAll() { return []; },
  createElement(t) { return mkEl('created-' + t); },
  addEventListener() {},
  body: { appendChild() {}, removeChild() {} },
};
const store = {};
const loc = { origin: 'https://xiaosanlab.online', pathname: '/retail-radar.html', search: '', hash: '' };
Object.defineProperty(loc, 'href', { get() { return loc.origin + loc.pathname + loc.search + loc.hash; } });
const delays = [], intervals = [], calls = [];
const clip = { last: '' };

const sandbox = {
  console, document: doc, location: loc,
  history: { replaceState: (a, b, u) => { loc.search = String(u).indexOf('?') >= 0 ? String(u).slice(String(u).indexOf('?')) : ''; } },
  navigator: { clipboard: { writeText: async (t) => { clip.last = String(t); } } },
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
  setTimeout: (fn, ms) => { delays.push(ms); return setTimeout(fn, ms); },
  clearTimeout: (id) => clearTimeout(id),
  setInterval: (fn, ms) => { intervals.push(ms); return setInterval(fn, ms); },
  clearInterval: (id) => clearInterval(id),
  fetch: async (url, opts) => {
    calls.push({ url: String(url), opts: opts || null });
    const copy = (o) => JSON.parse(JSON.stringify(o));
    if (String(url).indexOf('trading_calendar') >= 0) return { ok: true, status: 200, json: async () => copy(cal) };
    return { ok: true, status: 200, json: async () => copy(raw) };
  },
  addEventListener() {},
  ResizeObserver: function () { return { observe() {}, unobserve() {}, disconnect() {} }; },
  MutationObserver: function () { return { observe() {}, disconnect() {}, takeRecords() { return []; } }; },
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
  event: { target: { classList: { add() {}, remove() {} }, closest: () => null } },
  URL, URLSearchParams,
  Number, Math, Date, JSON, String, RegExp, isNaN, Object, Array, Map, Set, Boolean,
  parseInt, parseFloat, Promise, Error, isFinite, Symbol, WeakMap, Intl,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

/* ── 跑页面脚本 ── */
const scripts = HTML.match(/<script>([\s\S]*?)<\/script>/g) || [];
const code = scripts[scripts.length - 1].replace(/^<script>/, '').replace(/<\/script>$/, '');
const ctx = vm.createContext(sandbox);
vm.runInContext(code, ctx, { filename: 'retail-radar.html:script' });
const G = (expr) => vm.runInContext(expr, ctx);
const html = (id) => String(G(`document.getElementById('${id}').innerHTML`));
const txt = (id) => String(G(`document.getElementById('${id}').textContent`));

/* ── 等首屏数据到位 ── */
async function boot() {
  for (let i = 0; i < 60; i++) {
    if (G('allData && allData.retail_flow && allData.retail_flow.length')) return true;
    await new Promise((r) => setTimeout(r, 25));
  }
  return false;
}

(async () => {
  const loaded = await boot();
  ok('首屏数据加载完成', loaded, G('allData ? (allData.retail_flow || []).length : "no allData"'));
  if (!loaded) { console.log(`\nPASS=${pass} FAIL=${fail}`); process.exit(1); }

  const total = G('allData.retail_flow.length');
  const chipRows = G('(allData.holder_chip && allData.holder_chip.chip_flow.rows || []).length');
  console.log(`\n数据：retail_flow ${total} 只 · chip_flow ${chipRows} 只 · 版本 ${G('allData.holder_chip.version')}`);
  console.log(`时间：${G('allData.last_updated')}\n`);

  /* ═══ 1. 占比 tab：条形与配色 ═══ */
  console.log('=== 占比 tab（条形满格 = 本榜最大） ===');
  const enterRatioSub = (sub, flow) => {
    G(`currentTab = 'dynamic'; currentFlow = ${flow ? `'${flow}'` : 'currentFlow'}; currentSub = '${sub}'; userSorted = false; forceCodes = []; searchText = '';`);
    G('applyDefaultSort(); viewList = getBaseList(); renderView();');
  };
  // 独立算一遍期望值（不依赖页面函数）
  const ffOf = (() => {
    const chip = {};
    for (const r of (raw.holder_chip.chip_flow.rows || [])) if (r.float_ratio > 0) chip[r.code] = r.float_ratio;
    const m = {};
    for (const s of raw.retail_flow) {
      const fr = chip[s.code];
      if (fr && s.circ_shares > 0 && s.price > 0) m[s.code] = s.circ_shares / 1e8 * s.price * fr / 100;
    }
    return m;
  })();
  const expectBoard = (tier, dir) => {
    const pool = raw.retail_flow
      .filter((s) => (s.total_amount || 0) >= 5 && ffOf[s.code] > 0)
      .map((s) => ({ code: s.code, v: (tier === 'main' ? (s.main_net || 0) : (s.retail_net || 0)) / ffOf[s.code] * 100 }))
      .filter((x) => (dir === 'top' ? x.v > 0 : x.v < 0));
    pool.sort((a, b) => (dir === 'top' ? b.v - a.v : a.v - b.v));
    return pool.slice(0, 30);
  };

  // 表格里的占比单元格（数字 + 条宽）按行解析
  const parseRatioCells = () => {
    const body = html('tableBody');
    const out = [];
    const re = /<span class="ratio-num" style="color:([^"]+)">([-\d.]+)%<\/span><span class="ratio-bar"[^>]*><span class="ratio-fill" style="width:([\d.]+)%;background:([^"]+)"/g;
    let m;
    while ((m = re.exec(body))) out.push({ numColor: m[1], num: num(m[2]), width: num(m[3]), fillColor: m[4] });
    return out;
  };
  const parseChartBars = () => {
    const area = html('chartArea');
    const out = [];
    const re = /bar-fill (\w+)" style="width:([\d.]+)%/g;
    let m;
    while ((m = re.exec(area))) out.push({ cls: m[1], width: num(m[2]) });
    return out;
  };

  for (const [tier, sub, flow, colorWord, cls] of [
    ['retail', 'retail_ratio_top', 'retail', 'var(--red)', 'red'],
    ['retail', 'retail_ratio_bottom', 'retail', 'var(--green)', 'green'],
    ['main', 'main_ratio_top', 'main', 'var(--blue)', 'blue'],
    ['main', 'main_ratio_bottom', 'main', 'var(--green)', 'green'],
  ]) {
    enterRatioSub(sub, flow);
    const exp = expectBoard(tier, sub.indexOf('_bottom') >= 0 ? 'bottom' : 'top');
    const cells = parseRatioCells();
    const bars = parseChartBars();
    const title = txt('chartTitle');
    const tag = `${flow === 'main' ? '主力' : '散户'}·${sub.indexOf('bottom') >= 0 ? 'BOTTOM' : 'TOP'}`;

    ok(`[${tag}] 表格渲染 30 行占比单元格`, cells.length === 30, cells.length);
    ok(`[${tag}] 第一行数值 = 独立算出的第 1 名 ${exp[0].v.toFixed(2)}%`,
       cells.length && Math.abs(cells[0].num - exp[0].v) < 0.005, cells.length ? cells[0].num : '-');
    ok(`[${tag}] 榜首条形满格 100%`, cells.length && Math.abs(cells[0].width - 100) < 0.05, cells.length ? cells[0].width : '-');
    ok(`[${tag}] 条形从上到下单调不增（排行榜能一眼扫）`,
       cells.every((c, i) => i === 0 || c.width <= cells[i - 1].width + 0.6),
       cells.map((c) => c.width).slice(0, 8).join(','));
    ok(`[${tag}] 条形宽度 = |值| ÷ 本榜最大`,
       cells.every((c, i) => Math.abs(c.width - Math.max(Math.min(Math.abs(c.num) / Math.abs(cells[0].num) * 100, 100), c.num !== 0 ? 6 : 0)) < 0.8),
       JSON.stringify(cells[0]));
    ok(`[${tag}] 数值颜色 = ${colorWord}`, cells.every((c) => c.numColor === colorWord), cells.length ? cells[0].numColor : '-');
    ok(`[${tag}] 条形填充色与数值同色`, cells.every((c) => c.fillColor === colorWord));
    ok(`[${tag}] 全榜 30 行的代码序列与独立计算一致`, (() => {
      const codes = [...html('tableBody').matchAll(/<span class="star-btn[^"]*" data-star="(\d{6})"/g)].map((m) => m[1]);
      return codes.length === 30 && codes.join() === exp.map((x) => x.code).join();
    })(), html('tableBody').slice(0, 60));
    ok(`[${tag}] 图表 30 根条、榜首满格、配色 ${cls}`,
       bars.length === 30 && Math.abs(bars[0].width - 100) < 0.05 && bars.every((b) => b.cls === cls),
       bars.length + ' | ' + JSON.stringify(bars[0]));
    ok(`[${tag}] 图表条宽与表格条宽一致（同一基准）`,
       bars.length === 30 && bars.every((b, i) => Math.abs(b.width - cells[i].width) < 0.6),
       JSON.stringify([bars[0], cells[0]]));
    ok(`[${tag}] 标题标出满格值 = 本榜最大 ${Math.abs(exp[0].v).toFixed(2)}%`,
       title.indexOf('条形满格 = 本榜最大 ' + Math.abs(exp[0].v).toFixed(2) + '%') >= 0, title);
    ok(`[${tag}] 标题不再塞整句口径`, title.length < 60, title);
  }
  ok('当前基准值 = 榜单最大值', Math.abs(G('ratioScale') - Math.abs(expectBoard('main', 'bottom')[0].v)) < 0.005,
     G('ratioScale'));

  /* ═══ 1b. 换手率 ═══ */
  console.log('\n=== 换手率（成交额 ÷ 流通市值，东财 f8 口径）===');
  const toExpect = {};   // code -> 独立算出的换手率
  for (const x of raw.retail_flow) {
    const mcap = (x.circ_shares || 0) / 1e8 * (x.price || 0);
    toExpect[x.code] = mcap > 0 ? Math.round((x.total_amount || 0) / mcap * 100 * 100) / 100 : null;
  }
  ok('有价有流通盘的股票都算出了 turnover',
     G(`allData.retail_flow.filter(s => s.price > 0 && s.circ_shares > 0).every(s => s.turnover !== null && s.turnover !== undefined)`),
     G(`(() => { const b = allData.retail_flow.filter(s => s.price > 0 && s.circ_shares > 0 && (s.turnover === null || s.turnover === undefined)); return b.length + ' 只缺失 ' + JSON.stringify(b.slice(0,3).map(s=>s.code)); })()`));
  ok('兜底路径下，没价/没流通盘（退市整理、停牌）的算不出 → null（不是 0）', (() => {
    G(`allData.retail_flow.forEach(s => { delete s.turnover; }); ensureTurnover();`);
    const nulls = G(`allData.retail_flow.filter(s => !s.price || !s.circ_shares).every(s => s.turnover === null)`);
    G(`allData.retail_flow.forEach(s => { delete s.turnover; }); ensureTurnover();`);
    return nulls;
  })(), G(`allData.retail_flow.filter(s => !s.price).length + ' 只无价（如退市/停牌）'`));
  // 强制走兜底路径（先删掉官方 f8）来验证公式本身，与数据里有没有 f8 无关
  ok('兜底换算 = 成交额 ÷ 流通市值 × 100（抽查 300 只）', (() => {
    const rows = G('allData.retail_flow.slice(0, 300)');
    const bad = rows.filter(s => toExpect[s.code] !== null && s.turnover !== null && Math.abs(s.turnover - toExpect[s.code]) > 0.011);
    return bad.length === 0;
  })(), (() => {
    const rows = G('allData.retail_flow.slice(0, 300)');
    return JSON.stringify(rows.filter(s => toExpect[s.code] !== null && s.turnover !== null && Math.abs(s.turnover - toExpect[s.code]) > 0.011).slice(0, 3).map(s => [s.code, s.turnover, toExpect[s.code]]));
  })());
  ok('官方值不会被兜底覆盖（哨兵值验证）', (() => {
    const keep = G('allData.retail_flow[0].turnover');
    G(`allData.retail_flow[0].turnover = 12.34; ensureTurnover();`);
    const held = G('allData.retail_flow[0].turnover') === 12.34;
    G(`allData.retail_flow[0].turnover = ${JSON.stringify(keep)};`);   // ⚠️ 必须还原：否则会带偏后面的分位分布
    return held;
  })());

  G(`switchTab('flow'); currentFlow='retail'; currentSub='net'; userSorted=false; applyDefaultSort(); viewList=getBaseList(); renderView();`);
  const toCells = [...html('tableBody').matchAll(/<td class="num-cell">([\d.]+)%<\/td>/g)].map(m => num(m[1]));
  ok('资金动向表把换手率渲染成「x.xx%」并用了等宽中性样式', toCells.length === 30, toCells.length + ' | ' + JSON.stringify(toCells.slice(0, 5)));
  ok('表头有「换手率」列且带口径说明',
     /title="[^"]*东财官方换手率[^"]*"[^>]*>换手率/.test(html('tableHead')), html('tableHead').slice(0, 200));

  // 按换手率排序：榜单内部（净流入 board）应为降序，且榜首 = 该 board 内换手最高
  G(`sortCol='turnover'; sortAsc=false; viewList=getBaseList(); renderView();`);
  const toSorted = [...html('tableBody').matchAll(/<td class="num-cell">([\d.]+)%<\/td>/g)].map(m => num(m[1]));
  ok('点表头能按换手率降序（表格内 30 行单调不增）',
     toSorted.length === 30 && toSorted.every((v, i) => i === 0 || v <= toSorted[i - 1] + 1e-9),
     JSON.stringify(toSorted.slice(0, 8)));
  ok('排序作用于整张榜单（榜首 = 榜单内换手最高，不是只排显示的 30 行）',
     Math.abs(toSorted[0] - G(`(() => { const l = getBaseList(); return Math.max(...l.map(s => s.turnover || 0)); })()`)) < 0.005,
     toSorted[0] + ' | ' + JSON.stringify(toSorted.slice(0, 3)));
  G(`sortCol=null; sortAsc=false; applyDefaultSort();`);

  // 速查面板
  G(`panelMode = 'search'; quickLookup('600519');`);
  const spHead = html('spHead');
  ok('速查面板新增「换手率」列（在成交额之后）',
     spHead.indexOf('换手率') >= 0 && spHead.indexOf('成交额') < spHead.indexOf('换手率'), spHead.slice(0, 260));
  ok('速查面板的换手率 = 独立算出的值',
     html('spBody').indexOf('>' + toExpect['600519'].toFixed(2) + '%<') >= 0,
     toExpect['600519'] + ' | ' + html('spBody').slice(0, 300));
  G(`closeStockPanel(); panelMode = null;`);

  /* ═══ 2. 占比列的显示细节 ═══ */
  console.log('\n=== 占比列 UI 细节 ===');
  G(`currentTab='dynamic'; currentFlow='retail'; currentSub='retail_ratio_top'; userSorted=false; forceCodes=[]; searchText=''; applyDefaultSort(); viewList = getBaseList(); renderView();`);
  const body0 = html('tableBody');
  ok('数字右对齐容器存在（.ratio-num，等宽字体 + 小数点对齐）',
     body0.indexOf('class="ratio-num"') >= 0 && /\.ratio-num \{[\s\S]{0,120}?text-align: right/.test(HTML));
  ok('条形高度从 3px 提到 7px（原来在浅色卡片上几乎看不见）',
     /\.ratio-bar \{[\s\S]{0,160}?height: 7px/.test(HTML), (HTML.match(/\.ratio-bar \{[\s\S]{0,160}?height: \d+px/) || [''])[0]);
  ok('条形轨道用 --bg-2（比原来的 --bg-1 深一档，白底可见）',
     /\.ratio-bar \{[\s\S]{0,200}?background: var\(--bg-2\)/.test(HTML));
  ok('条形总宽 58px（比原来 36px 宽）', /\.ratio-bar \{[\s\S]{0,80}?width: 58px/.test(HTML));
  ok('旧的 3px 细线已彻底移除（mini-bar / mini-fill 无残留）',
     HTML.indexOf('mini-bar') < 0 && HTML.indexOf('mini-fill') < 0);
  ok('旧的固定基准已彻底移除（RATIO_BAR_FULL / RATIO_FULL 无残留）',
     HTML.indexOf('RATIO_BAR_FULL') < 0 && HTML.indexOf('RATIO_FULL') < 0);
  ok('算不出分母的行显示 --', (() => {
    const noChip = raw.retail_flow.find((s) => !ffOf[s.code]);
    if (!noChip) return true;
    G(`searchText='${noChip.code}'; forceCodes=[]; renderFlow();`);
    const b = html('tableBody');
    const r = b.indexOf('ratio-num') >= 0;
    G(`searchText=''; forceCodes=[]; renderView();`);
    return !r;   // 没有筹码数据 → 不进榜，空态里说明原因（不渲染占比单元格）
  })());
  ok('列头 tooltip 说清「条形 = 该值 ÷ 本榜最大值」',
     /条形长度 = 该值 ÷ 本榜最大值（榜首满格）/.test(HTML));
  ok('列头 tooltip 说清颜色含义', /红色=净买入、绿色=净卖出/.test(HTML));

  /* ═══ 2b. KPI「资金构成占比」：数字与 % 必须同一行 ═══ */
  console.log('\n=== KPI 资金构成占比（数字 + % 不换行）===');
  const shareRow = HTML.slice(HTML.indexOf('id="kpiShareValue"'));
  const shareHtml = shareRow.slice(0, shareRow.indexOf('</div>') + 6);
  ok('三个占比单元格都把「数字 + %」包进 .share-num', (shareHtml.match(/class="share-num"/g) || []).length === 3, shareHtml);
  ok('不再有「</b>%」这种裸文本百分号（就是它被 display:block 挤到下一行的）',
     shareHtml.indexOf('</b>%') < 0 && (shareHtml.match(/<i>%<\/i>/g) || []).length === 3, shareHtml);
  ok('% 与数字是同一个 flex 行、按 baseline 对齐',
     /#kpiShareValue \.share-num \{display:flex;align-items:baseline/.test(HTML));
  ok('数字与 % 之间不留空格（中文排版「35%」）',
     /#kpiShareValue \.share-num \{[^}]*gap:0/.test(HTML));
  ok('b 不再被写成 display:block（会把它从同一行挤出去）',
     !/#kpiShareValue b \{[^}]*display:block/.test(HTML));
  ok('标签仍在数字上方（.share-num 之前有「散户/中单/主力」文本）',
     /<span>散户<span class="share-num">/.test(shareHtml) && /<span>中单<span class="share-num">/.test(shareHtml)
     && /<span>主力<span class="share-num">/.test(shareHtml), shareHtml.slice(0, 120));
  const ts = raw.overview.tier_share || {};
  ok('渲染值仍写进三个 <b>（结构改动没打断赋值）',
     txt('kpiShareRetail') === String(ts.retail_pct) && txt('kpiShareMedium') === String(ts.medium_pct)
     && txt('kpiShareMain') === String(ts.main_pct),
     [ts.retail_pct, ts.medium_pct, ts.main_pct].join('/') + ' vs ' + [txt('kpiShareRetail'), txt('kpiShareMedium'), txt('kpiShareMain')].join('/'));

  /* ═══ 2c. 放量滞涨榜（量价分）═══ */
  console.log('\n=== 放量滞涨（活跃分 × 蓄势分）===');
  // ⚠️ 前面「换手率兜底」测试把 allData 里的官方 f8 换成了近似值（两者能差几十 pp），
  //    会带偏 turnover 分位 → 量价分对不上。这里重新装一次干净数据，保证断言可比。
  await G('fetchData()');
  // 独立复算样本池与三档分（不复用页面函数）
  const vs = (() => {
    const now = Date.now();
    const pool = [];
    for (const s of raw.retail_flow) {
      if (!(s.price > 0) || !(s.circ_shares > 0)) continue;
      if (s.vol_ratio === undefined || s.chg_60d === undefined) continue;
      if (s.list_date) { const t = Date.parse(s.list_date + 'T00:00:00'); if (isFinite(t) && (now - t) / 86400000 < 60) continue; }
      if ((s.total_amount || 0) < 1) continue;
      if ((s.chg_60d || 0) < -40) continue;
      pool.push(s);
    }
    const ranks = (arr) => { const a = arr.slice().sort((x, y) => x - y);
      return (v) => { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] <= v) lo = m + 1; else hi = m; } return lo / a.length * 100; }; };
    const P_to = ranks(pool.map(s => s.turnover || 0)), P_vr = ranks(pool.map(s => s.vol_ratio || 0));
    const P_a0 = ranks(pool.map(s => Math.abs(s.change_pct || 0))), P_a60 = ranks(pool.map(s => Math.abs(s.chg_60d || 0)));
    const out = new Map();
    for (const s of pool) {
      const act = 0.5 * P_to(s.turnover || 0) + 0.5 * P_vr(s.vol_ratio || 0);
      const stab = 100 - (0.5 * P_a0(Math.abs(s.change_pct || 0)) + 0.5 * P_a60(Math.abs(s.chg_60d || 0)));
      out.set(s.code, { act, stab, vp: act * stab / 100 });
    }
    return { pool, out, hasFields: raw.retail_flow.filter(s => s.vol_ratio !== undefined).length };
  })();
  // 这条只报告不判定：旧数据快照本来就没有量价字段，那是数据版本问题、不是缺陷
  console.log('数据带量价字段: ' + vs.hasFields + '/' + raw.retail_flow.length
    + (vs.hasFields ? '' : '  → 旧版数据，跳过放量滞涨断言'));
  if (!vs.hasFields) { /* 跳过下面全部放量滞涨断言 */ }
  else {
    ok('顶栏第三个 tab「放量滞涨」存在', /onclick="switchTab\('volspan'\)">放量滞涨/.test(HTML));
    G(`switchTab('volspan');`);
    ok('切换后子榜 = 放量滞涨TOP，一级 subtab 隐藏',
       G('currentSub') === 'volspan_top' && G(`document.getElementById('subtabFlow').classList.contains('hidden')`),
       G('currentSub'));
    const poolSize = G('volspanPool().size');
    ok('样本池数量 = 独立复算值（' + vs.pool.length + '）', poolSize === vs.pool.length, poolSize);

    // 四道闸门
    const byCode = {}; for (const s of raw.retail_flow) byCode[s.code] = s;
    const pick = (pred) => { const x = vs.pool.find(() => false) || raw.retail_flow.find(pred); return x; };
    const newOne = pick(s => s.list_date && (Date.now() - Date.parse(s.list_date + 'T00:00:00')) / 86400000 < 60);
    if (newOne) ok('新股被剔除（上市不足 60 天）', !G(`volspanPool().map.has('${newOne.code}')`) && G(`volspanPool().reasons.get('${newOne.code}')`) === 'newlist',
       newOne.code + ' ' + newOne.list_date);
    const illiq = pick(s => (s.total_amount || 0) > 0 && (s.total_amount || 0) < 1 && s.vol_ratio !== undefined && s.price > 0);
    if (illiq) ok('冷清票被剔除（成交额 < 1 亿）', G(`volspanPool().reasons.get('${illiq.code}')`) === 'illiquid', illiq.code + ' ' + illiq.total_amount);
    const crash = pick(s => (s.chg_60d || 0) < -40);
    if (crash) ok('放量崩盘被剔除（60日跌幅 > 40%）', G(`volspanPool().reasons.get('${crash.code}')`) === 'crash', crash.code + ' ' + crash.chg_60d);
    else console.log('  （今天没有 60 日跌超 40% 的票，跳过该断言）');

    // 分数公式
    const topRows = G('[...volspanPool().map.values()].slice(0, 400)');
    const badScore = topRows.filter(s => {
      const e = vs.out.get(s.code);
      return !e || Math.abs(s._vp - e.vp) > 0.06 || Math.abs(s._vs.act - e.act) > 0.06 || Math.abs(s._vs.stab - e.stab) > 0.06;
    });
    ok('量价分 = 活跃分 × 蓄势分 ÷ 100（抽查 400 只，与独立复算一致）', badScore.length === 0,
       JSON.stringify(badScore.slice(0, 3).map(s => [s.code, s._vp, vs.out.get(s.code) && vs.out.get(s.code).vp])));

    // ⭐ 关键行为：不能因为「跌得多」就拿高分（沐曦股份那种放量下跌必须被压下去）
    const ranked = G('[...volspanPool().map.values()].slice().sort((a,b)=>b._vp-a._vp)');
    const top30 = ranked.slice(0, 30);
    const medAbs60 = (() => { const a = top30.map(s => Math.abs(s.chg_60d)).sort((x, y) => x - y); return a[Math.floor(a.length / 2)]; })();
    ok('★ 榜首 30 只以「横盘」为主（|60日涨幅| 中位数 < 15%）', medAbs60 < 15, medAbs60.toFixed(1) + '%');
    // 直接量「横盘票 vs 大幅波动票」的分位差：这才是设计意图（蓄势分用 |60日涨幅|）
    const pctOf = (list) => { let sum = 0; for (const s of list) sum += ranked.indexOf(s); return 100 - sum / list.length / ranked.length * 100; };
    const flatOnes = ranked.filter(s => Math.abs(s.chg_60d) <= 8);
    const wildOnes = ranked.filter(s => Math.abs(s.chg_60d) > 25);
    ok('★ 横盘票（|60日涨幅|≤8%）的平均排名分位显著高于大幅波动票（>25%）',
       flatOnes.length > 20 && wildOnes.length > 20 && pctOf(flatOnes) - pctOf(wildOnes) > 20,
       '横盘 ' + pctOf(flatOnes).toFixed(0) + ' vs 波动 ' + pctOf(wildOnes).toFixed(0) + '（差 ' + (pctOf(flatOnes) - pctOf(wildOnes)).toFixed(0) + 'pp，样本 ' + flatOnes.length + '/' + wildOnes.length + '）');
    const downAll = top30.every(s => s.change_pct < 0);
    const medAbs0 = (() => { const a = top30.map(s => Math.abs(s.change_pct)).sort((x, y) => x - y); return a[Math.floor(a.length / 2)]; })();
    ok('★ 榜首 30 不会全是当日下跌股（否则「今日涨幅」只罚了上涨）', !downAll,
       '当日下跌 ' + top30.filter(s => s.change_pct < 0).length + '/30');
    ok('★ 榜首 30 的 |今日涨幅| 中位数 < 2%（说明确实是"今天没大动"）', medAbs0 < 2, medAbs0.toFixed(2) + '%');
    const avgTop = top30.reduce((a, s) => a + Math.abs(s.chg_60d), 0) / top30.length;
    const avgPool = ranked.reduce((a, s) => a + Math.abs(s.chg_60d), 0) / ranked.length;
    ok('★ 榜首 30 的 |60日涨幅| 均值不到全池的一半（分数确实偏向横盘）',
       avgTop < avgPool * 0.5, '榜首 ' + avgTop.toFixed(1) + '% vs 全池 ' + avgPool.toFixed(1) + '%');

    // 表格（先清掉前面测试留下的搜索词，否则会被筛空）
    G(`searchText = ''; forceCodes = []; sortCol = null; userSorted = false; applyDefaultSort(); viewList = getBaseList(); renderView();`);
    const vsHead = html('tableHead');
    const vsLabels = [...vsHead.replace(/<span class="sort-icon"><\/span>/g, '').matchAll(/>([^<>]+)<\/th>/g)].map(m => m[1].trim());
    ok('表格 11 列且顺序正确（换手率已挪到名称之后）',
       vsLabels.join('/') === '代码/名称/换手率/价格/今日涨跌/量比/60日涨幅/年初至今/量价分/主力净额/行业', vsLabels.join('/'));
    // 🩸 挪列序最容易只改表头忘了改行 → 列错位。这里逐格核对第 1 行
    const row0 = html('tableBody').split('</tr>')[0];
    const tds0 = [...row0.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map(m => m[1]);
    const txt0 = (i) => tds0[i].replace(/<[^>]*>/g, '').trim();
    const lead = ranked[0];
    ok('第 1 行格子数 = 表头列数（没错位）', tds0.length === 11, tds0.length + ' 格');
    ok('★ 换手率是「名称」之后的第一列（第 3 格 = 换手率值）',
       txt0(2) === (lead.turnover === null || lead.turnover === undefined ? '--' : lead.turnover.toFixed(2) + '%'),
       txt0(2) + ' vs 数据 ' + lead.turnover);
    ok('价格 / 今日涨跌 顺次跟在换手率之后',
       txt0(3) === (lead.price > 0 ? lead.price.toFixed(2) : '--'), txt0(3) + ' | ' + txt0(4));
    ok('默认按量价分降序', G('sortCol') === '_vp' && G('sortAsc') === false, G('sortCol') + '/' + G('sortAsc'));
    // ⚠️ 用宿主侧字符串做 matchAll：在 vm 里对 vm 字符串跑 matchAll 会拿到空结果（踩过）
    const rendered = [...html('tableBody').matchAll(/data-star="(\d{6})"/g)].map(m => m[1]);
    ok('表格 30 行，且榜首 = 独立复算的量价分最高', rendered.length === 30 && rendered[0] === ranked[0].code,
       rendered.length + ' | ' + rendered[0] + ' vs ' + ranked[0].code + ' | HTML: ' + html('tableBody').slice(0, 200));
    ok('量价分渲染成「数字 + 条」', /class="ratio-num"[^>]*>[\d.]+<\/span><span class="ratio-bar"/.test(html('tableBody')));

    // 图表
    const vsBars = [...html('chartArea').matchAll(/bar-fill (\w+)" style="width:([\d.]+)%/g)].map(m => ({ cls: m[1], w: num(m[2]) }));
    ok('图表 30 根条、榜首满格、配色 accent',
       vsBars.length === 30 && Math.abs(vsBars[0].w - 100) < 0.05 && vsBars.every(b => b.cls === 'accent'),
       vsBars.length + ' | ' + JSON.stringify(vsBars[0]));
    ok('图表数值带「分」单位', /[\d.]+ 分<\/span>/.test(html('chartArea')), html('chartArea').slice(0, 160));

    // 口径提示
    ok('图表下方显示口径提示（样本量 + 风险）',
       html('chartNote').indexOf('样本') >= 0 && html('chartNote').indexOf('派发') >= 0 && els['chartNote'].hidden === false,
       html('chartNote').slice(0, 110));

    // 主力净流入子榜
    G(`switchSub('volspan_inst'); viewList = getBaseList(); renderView();`);
    const instNets = G(`viewList.slice(0,30).map(s => s.main_net || 0)`);
    ok('「主力净流入」子榜里 30 行主力净额全为正', instNets.length === 30 && instNets.every(v => v > 0),
       JSON.stringify(instNets.slice(0, 5)));

    // 空态解释闸门
    if (newOne) {
      G(`currentSub='volspan_top'; searchText='${newOne.code}'; forceCodes=[]; viewList=getBaseList(); renderView();`);
      ok('搜新股 → 空态说明「上市不满 60 天」', html('tableBody').indexOf('上市不满') >= 0
         || html('chartArea').indexOf('上市不满') >= 0,
         (html('chartArea') + html('tableBody')).slice(0, 200));
      G(`searchText=''; forceCodes=[]; viewList=getBaseList(); renderView();`);
    }
    // 回到其他 tab 时提示行要收起
    G(`switchTab('flow'); currentFlow='retail'; currentSub='net'; viewList=getBaseList(); renderView();`);
    ok('切回资金动向 tab → 口径提示隐藏', els['chartNote'].hidden === true, String(els['chartNote'].hidden));
    ok('速查面板新增 量比/60日涨幅/年初至今 三列',
       html('spHead').indexOf('量比') >= 0 && html('spHead').indexOf('60日涨幅') >= 0 && html('spHead').indexOf('年初至今') >= 0);
  }

  /* ═══ 3. 回归：别把别的 tab 弄坏 ═══ */
  console.log('\n=== 回归 ===');
  G(`switchTab('flow'); currentFlow='retail'; currentSub='net'; userSorted=false; applyDefaultSort(); viewList=getBaseList(); renderView();`);
  const flowHead = html('tableHead');
  ok('资金动向表 9 列（新增换手率）', (flowHead.match(/<th /g) || []).length === 9, (flowHead.match(/<th /g) || []).length);
  // ⚠️ 表头里有个 <span class="sort-icon"></span>（排序箭头），要先去干净再取标签
  const flowLabels = [...flowHead.replace(/<span class="sort-icon"><\/span>/g, '').matchAll(/>([^<>]+)<\/th>/g)].map(m => m[1].trim());
  ok('资金动向表列顺序 = 代码/名称/价格/涨跌/换手率/散户/中单/主力净额/行业',
     flowLabels.join('/') === '代码/名称/价格/涨跌/换手率/散户净额/中单净额/主力净额/行业', flowLabels.join('/'));
  ok('资金动向表不出现占比单元格', html('tableBody').indexOf('ratio-num') < 0);
  ok('资金动向表仍渲染 30 行', (html('tableBody').match(/<tr>/g) || []).length === 30,
     (html('tableBody').match(/<tr>/g) || []).length);
  ok('筹码表仍渲染 30 行', (html('chipBody').match(/<tr>/g) || []).length === 30,
     (html('chipBody').match(/<tr>/g) || []).length);
  ok('自选星标仍在（两张表都有）',
     html('tableBody').indexOf('data-star') >= 0 && html('chipBody').indexOf('data-star') >= 0);
  store['xiaosan_watchlist_v1'] = '[]';
  G('watchlist = []; saveWatchlist(); toggleWatch("600519", { stopPropagation(){} });');
  ok('自选增删 + 落盘仍正常',
     G('watchlist').join() === '600519' && String(store['xiaosan_watchlist_v1']).indexOf('600519') >= 0,
     store['xiaosan_watchlist_v1']);
  ok('顶栏「登录 / 注册」入口仍在', HTML.indexOf('id="btnAccount"') >= 0 && txt('btnAccount') === '登录 / 注册', txt('btnAccount'));
  ok('速查面板仍在 .tool-bar 内部（浮层锚点）', (() => {
    const i = HTML.indexOf('<div class="tool-bar">');
    let d = 0, k = i;
    const re = /<div\b|<\/div>/g; re.lastIndex = i;
    let m;
    while ((m = re.exec(HTML))) { d += m[0] === '</div>' ? -1 : 1; if (d === 0) { k = m.index + m[0].length; break; } }
    return HTML.slice(i, k).indexOf('id="stockPanel"') >= 0;
  })());
  ok('取数仍走条件请求（URL 无 ?t=）',
     calls.filter((c) => c.url.indexOf('radar_data') >= 0).every((c) => c.url.indexOf('?t=') < 0),
     JSON.stringify(calls.filter((c) => c.url.indexOf('radar_data') >= 0).slice(0, 1)));
  ok('自动刷新标签不写轮询秒数', G(`(function(){ getRefreshInterval = () => 60000; updateAutoLabel(); return document.querySelector('.toggle span').textContent; })()`) === '自动刷新',
     G(`document.querySelector('.toggle span').textContent`));

  console.log(`\nPASS=${pass}  FAIL=${fail}`);
  process.exit(fail ? 1 : 0);
})();
