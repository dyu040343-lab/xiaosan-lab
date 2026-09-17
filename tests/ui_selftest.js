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

  /* ═══ 3. 回归：别把别的 tab 弄坏 ═══ */
  console.log('\n=== 回归 ===');
  G(`switchTab('flow'); currentFlow='retail'; currentSub='net'; userSorted=false; applyDefaultSort(); viewList=getBaseList(); renderView();`);
  const flowHead = html('tableHead');
  ok('资金动向表仍 8 列', (flowHead.match(/<th /g) || []).length === 8, (flowHead.match(/<th /g) || []).length);
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
