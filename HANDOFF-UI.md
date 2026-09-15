# UI 交接说明 · 小散研究院（retail-radar）

> 给接手的 AI（Codex / Claude 等）或前端同学：**先读第 1、5 节再动手**，第 5 节是绝对不能改的业务口径。
> 最后有可直接粘贴给 Codex 的任务提示词（第 9 节）。

---

## 1. 技术红线（违反会导致线上坏掉）

| 规则 | 说明 |
|---|---|
| **单文件，零构建** | `retail-radar.html` 是唯一前端产物：所有 CSS 在一个 `<style>`、所有 JS 在一个 `<script>` 里。**没有 npm / webpack / vite / TypeScript / JSX** |
| **不引入框架与库** | 不用 React / Vue / Svelte / Tailwind / Bootstrap；**不要**换掉手写 SVG 图表（不要引入 ECharts——仓库里那份是废弃的） |
| **不加构建步骤** | 改完直接就是可发布的 HTML。不要生成 `dist/`、不要加 `.babelrc`、不要 `package.json` |
| **不改数据层** | 页面只 `fetch('data/radar_data.json')`。**不要修改 / 提交 `data/` 下任何文件**（那是服务端脚本产物，6.8MB，且每 10 分钟被覆盖） |
| **只用原生 ES6** | `const/let` + 箭头函数 + 模板字符串已经是现有风格；缩进 **2 空格**；新增函数放在 `<script>` 里对应注释区块内 |
| **不联网取数** | 页面内不发任何后端/第三方 API 请求（除了已存在的 Google Fonts） |

---

## 2. 怎么跑起来看效果

```bash
cd <repo 根目录>
python3 -m http.server 8080
# 浏览器打开 http://localhost:8080/retail-radar.html
```

- ⚠️ **必须走 http**，不能直接双击 html 文件（`file://` 下 `fetch` 会被 CORS 拦死，页面一直「加载中」）。
- `data/radar_data.json` 约 6.8MB / 5900+ 只股票，本地首次加载慢属正常。
- 线上环境：https://xiaosanlab.online/retail-radar.html

---

## 3. 文件地图

| 路径 | 作用 | 能不能改 |
|---|---|---|
| `retail-radar.html` | **唯一前端文件**（约 2250 行：CSS 66–1060 行，HTML 1100–1300 行，JS 1300–2254 行） | ✅ 改这里 |
| `data/radar_data.json` | 服务端生成的数据快照 | ❌ 只读，别提交改动 |
| `data/trading_calendar.json` | **交易日历**（周末 + 法定节假日休市日），前端也 fetch 它来决定刷新节奏 | ⚠️ 每年更新，见 4.4 |
| `scripts/fetch_data.py` | 服务端抓数脚本（东财 push2 + datacenter） | ⚠️ 与前端契约相关，改字段要同步改前端并升版本号 |
| `scripts/update_calendar.py` | 生成 / 校验 `data/trading_calendar.json` | 每年 12 月跑一次即可 |
| `index.html` | 跳转到 `retail-radar.html` | 一般不用动 |
| `about/terms/privacy/disclaimer.html` | 合规静态页 | 可改样式，但**文案别删**（合规要求） |
| `_shared/`、`assets/` | **历史遗留，已无人引用**（echarts、旧 app.js/charts.js） | 忽略，别浪费时间 |

---

## 4. 数据契约（前端唯一数据源）

```js
fetch('data/radar_data.json') → allData
```

```
allData.overview           // 全市场汇总（KPI 卡的数据）
allData.retail_flow[]      // 全市场逐股：行情 + 资金流（5915 只，含北交所）
allData.holder_chip.chip_flow.rows[]  // 逐股筹码结构（5497 只）
allData.holder_chip.chip_flow.period / prev_period   // 如 2026-06-30 / 2026-03-31
allData.last_updated, data_status, data_source
```

### 4.1 `overview`
```
inflow_count / outflow_count          散户净流入·净流出只数
medium_inflow_count / medium_outflow_count
main_inflow_count / main_outflow_count
net_amount / medium_amount / main_amount   三档净额（亿元）
super_total / large_total / medium_total / small_total  四档净额（亿元）
tier_share { retail_pct, medium_pct, main_pct, tier_total }  资金构成占比（按 |净额| 之和加权）
total_stocks, update_time, data_status(live|cached|static), data_label
```

### 4.2 `retail_flow[i]`
```
code name sector price change_pct
retail_net  小单净额(亿)   ← 「散户」
medium_net  中单净额(亿)   ← 「中单」
main_net    超大单+大单净额(亿) ← 「主力」
super_net large_net small_net super_pct large_pct medium_pct small_pct
total_amount  成交额(亿)   ← 占比维度分母；流动性过滤用它
dynamic_ratio = retail_net / total_amount × 100   ← 「散户占比」
total_shares circ_shares  （总股本 / 流通A股，股）
```
> `main_ratio` 前端现算：`main_net / total_amount × 100`。

### 4.3 `holder_chip.chip_flow.rows[i]`
```
code name holders(股东户数) holder_chg(户数季度环比 %)
lock_ratio(锁定盘 %) float_ratio(自由流通/流通A股 %)
retail_ff / inst_ff / legal_ff     三档「占自由流通」%（散户及其他 / 主力机构 / 一般法人，和为 100）
retail_ts / inst_ts / legal_ts     同上但分母为总股本（当前前端未展示，后端仍输出）
inst_pct / legal_pct / retail_pct  三档「占流通A股」%
inst_delta                         主力持股季度环比（百分点）
circ_ratio                         流通A股/总股本 %
```
> 注意 `_ff` 后缀 = free float（自由流通），`_ts` = total shares（总股本）。

### 4.4 `data/trading_calendar.json`（交易日历）
```json
{
  "version": 1,
  "updated": "2026-09-15",
  "source": "深圳证券交易所交易日历接口 + 上证指数日K交叉校验",
  "rule": "周六周日一律休市（含调休补班的周末）；closed_days 只列额外休市的【工作日】",
  "years_covered": [2026],
  "closed_days": ["2026-01-01", "2026-02-16", "...", "2026-09-25", "2026-10-01"]
}
```
- 前端 `loadCalendar()` 读它（失败/字段缺失 → `CALENDAR.closed` 为空集，退回「只看工作日」，**不会停摆**）。
- 判定顺序：`周末 或 closed_days 命中` → 休市；`years_covered` **没覆盖当前年份就不判定**（保守放行）。
- 服务端 `scripts/fetch_data.py` 用同一份文件决定要不要抓数，口径与前端一致。
- **年度维护**：交易所每年 12 月公布次年安排后跑
  `python3 scripts/update_calendar.py`（默认刷新「今年 + 明年」；`--verify-only` 只校验不写盘）。
  它会同时拉深交所日历和上证指数日 K 线做交叉校验，两边不一致会打印告警。

---

## 5. ⛔ 不许改的业务口径（UI 优化时最容易踩）

1. **三档守恒**：散户(小单) + 中单 + 主力(超大单+大单) 的三档净额之和 **恒等于 0**。不要试图让它们"各自独立"。
2. **A 股配色**：涨 = 红（`--red`），跌 = 绿（`--green`），**和欧美相反**。其中「主力」档刻意用蓝色（`--blue`）以示对手盘，不是配色错误。
3. **研究的锚点是散户**：散户段永远独立成档；主力是"对手盘"视角。不要把主力/机构变成页面主角。
4. **Tab2（资金占比动向）必须用占比维度**（`dynamic_ratio` / `main_ratio`），且**必须保留流动性过滤 `total_amount >= 5`（亿）**，否则会出现"成交 140 万 / 净额 102 万 = +73%"这种伪占比霸榜。
5. **`PRIMARY_FIELD()` 在占比 tab 恒返回 `retail_net`**：占比列的橙色高亮只属于「资金动向」tab，别"顺手统一"。
6. **BOTTOM 榜单必须是负值**（对齐净流出），不要搞成"最小正值"。
7. **筹码表口径**：`锁定盘 = 100 − 自由流通`（A 股视角）。`holder_chg === null` 表示该股股东户数是按月披露、与季频窗口不可比 → 必须显示 `--` **并沉底，不能当 0**。
8. **性能红线**：筹码表**只渲染排序后的前 300 行**（全量 5493 行会让页面卡死）；榜单本身也有限条。别改成全量渲染。
9. **表头 sticky 的两个坑**（现在的实现是对的，别"优化"坏）：
   - `.table-box` 的 `padding-top` 必须是 `0`（`padding: 0 12px 12px`），否则冻结表头上方会露出滚动内容；
   - 排序激活列表头背景必须是**不透明**色（`#f9f1e9`），半透明会让数据透上来。
10. **点 ☆ 只改星标 DOM，不做整体重渲染**（否则表格滚动位置被重置，体验崩）。
11. **自选每次改动前先 `loadWatchlist()` 读盘**（多标签页防覆盖，别优化掉）。
12. **排序解耦**：`getBaseList()` 只做 filter，`applyDefaultSort()` 只设排序键与方向，`renderView()` 才真正渲染。别合并。
13. **休市判断必须走日历**：不要把节假日硬编码进前端（周末判定除外），也不要因为日历加载失败就把刷新关掉 —— 缺日历时的正确行为是**退回「只看工作日」**（宁可多刷一次）。`isMarketClosed()` / `isHoliday()` 的返回值语义别改。

---

## 6. DOM / 函数地图

### CSS 区块（`<style>` 内按顺序）
```
Header 76 · Section Headers 263 · KPI Cards 321 · Tabs 532 · Search 606
Content Grid 627 · Table 696 · 自选星标 799 · 速查/自选工具条 817
个股面板 877 · Toast 936 · 筹码表 955 · Compliance Footer 965
Loading Skeleton 1040 · Responsive 1060 · 合规子页 1068
```

### 页面区块（`<body>` 内）
```
.header                      顶栏：状态徽章 / 更新时间 / 刷新按钮 / AUTO 开关
.tool-bar                    个股速查输入框 + ★ 自选按钮
.stock-panel (#stockPanel)   速查/自选 共用的「全指标表」面板（19 列）
#realtime                    KPI 4 卡 + tab 栏 + subtab + 搜索框 + 图表 + 表格
#chipBox / 筹码区             筹码动向可排序表（9 列）
.compliance-footer           免责声明 / 页脚
```

### 关键 JS 函数
| 函数 | 职责 |
|---|---|
| `fetchData()` / `render()` | 拉数据 → 渲染全部区块；数据刷新后会 `_universe = null` 重建索引 |
| `getBaseList()` / `applyDefaultSort()` / `renderView()` | 榜单：filter / 设排序 / 渲染 |
| `renderChart(stocks, field)` | 手写 SVG 条形图 |
| `renderTable(stocks, field)` | 榜单表格（8 列，含 ☆） |
| `renderChipFlow()` / `chipSortTable(col)` | 筹码表（9 列，默认按户数变化升序，只渲染前 300 行） |
| `renderSubtabs()` / `switchTab()` / `switchFlowTab()` / `switchSub()` / `sortTable()` | tab 与排序交互 |
| `universe()` | 把 `retail_flow` × `chip_flow.rows` 按 code 合并成全指标 Map（速查/自选的数据源） |
| `onQuickInput()` / `drawStockPanel()` / `spCell()` / `spSort()` | 个股速查面板 |
| `loadWatchlist() / saveWatchlist() / toggleWatch() / starBtn() / syncStars() / exportWatch() / importWatch() / clearWatch()` | 自选股（localStorage：`xiaosan_watchlist_v1`） |
| `isWeekend()` / `isHoliday()` / `isMarketClosed()` / `isTradingNow()` / `loadCalendar()` | 交易日历判定（周末 + 法定节假日），决定刷新节奏与 AUTO 标签 |
| `getRefreshInterval()` / `scheduleAutoRefresh()` / `toggleAuto()` | 刷新节奏：交易时段 60s；休市/非交易时段**暂停**（只留 5 分钟看门狗等开盘） |

---

## 7. 设计令牌（`<style>` 顶部 `:root`）

```css
--bg-0 #f4f6fa  --bg-1 #eceff5  --bg-2 #e2e8f2  --card #ffffff
--border #dfe5ee  --border-hi #c2cde0
--text #2a3548  --text-hi #101b2e  --dim #64748b  --dimmer #98a4b8
--accent #b45309 (琥珀，主强调)  --accent-dim #f6e0b8
--cyan #0e7490  --red #dc2626  --green #16a34a  --purple #7c3aed  --blue #2563eb
```
- **浅色主题**（无深色模式）；卡片圆角 `14px`，小元素 `6–10px`；表格容器高 `540px`（筹码区 `600px`）内部滚动。
- 字体：正文 `Noto Sans SC`，数字/表头 `JetBrains Mono`（等宽，务必保留数字列等宽）。
- 半透明色大量使用 `rgba(<变量RGB>, 0.0x)`，改令牌时注意同步。

---

## 8. 已知可优化点（方向建议，供参考）

- **移动端**：断点只有 `max-width: 768px` 一个，8–19 列表格在手机上只能横向硬滚；可考虑卡片式/分组折叠。
- **19 列速查面板**：横向滚动时缺少列分组视觉提示（行情/资金/筹码三块），容易看错列。
- **数字对齐**：金额/百分比列建议统一右对齐 + 等宽字体，便于纵向比大小。
- **视觉层级**：KPI 卡 / 图表 / 表格三层信息密度接近，主次可再拉开（字号、留白、分隔）。
- **可访问性**：列头口径说明只用了 `title`（移动端和键盘用户看不到）；sticky 表头没有 `aria-sort`。
- **加载/空态**：只有一处骨架屏，速查无结果、自选为空的样式较朴素。
- （可选）深色模式：注意红涨绿跌在深色底上的对比度。

---

## 9. 验收清单 + 交付要求

**改完自查：**
1. `node --check` 通过：把 `<script>` 内容抽出来单独跑一遍语法检查。
2. 本地 `python3 -m http.server` 打开：4 张 KPI 卡、图表、榜单表、筹码表、速查、自选全部正常。
3. 逐个切 tab / subtab / 点表头排序，无报错（F12 console 干净）。
4. 缩小到 375px 宽看一遍，没有横向溢出把整页撑破。
5. **业务回归**：占比 tab 的榜单仍只含成交额 ≥ 5 亿的票；筹码表"户数变化"为空的行仍在最底部显示 `--`；点 ☆ 不跳滚动位置。
6. **日历回归**：把 `data/trading_calendar.json` 临时改名 → 页面仍能正常加载（退回只看工作日）；再放回来 → 休市日（如 2026-09-25）在控制台里 `isTradingNow(new Date(2026,8,25,10,0))` 应返回 `false`。
7. **AUTO 标签**：交易时段 `AUTO 60s`、收盘后 `已收盘 · 暂停`、周末 `休市 · 周末`、节假日在工作日位置 `休市 · 节假日`。

**交付要求：**
- 只改 `retail-radar.html`（如必须动其他文件，先说明原因）。
- 不要提交 `data/` 下的文件。
- 给出一份改动说明：改了什么、为什么、怎么验证的。
- 部署由项目 owner 执行（本地文件通过 SFTP 推到服务器 + md5 校验），AI 不需要也不应该持有服务器凭据。

---

## 10. 可直接粘给 Codex 的提示词

```
我在做一个纯静态的 A 股看板「小散研究院」，前端全部在 retail-radar.html 这一个文件里
（内联 CSS + 内联原生 JS，无构建、无框架、无 npm）。

请你先读仓库根目录的 HANDOFF-UI.md（完整的约束、数据契约、函数地图、验收清单都在里面），
然后按它的约束帮我优化 UI。

本次的目标是：<在这里写你想优化什么，例如「移动端 375px 下的表格可读性」/「整体视觉层级与间距」>

硬性要求（也写在 HANDOFF-UI.md 第 1、5 节）：
- 不要引入任何框架或构建工具，保持单文件、原生 JS/CSS。
- 不要改业务口径、过滤条件、排序逻辑（尤其是占比 tab 的成交额 ≥5 亿过滤、筹码表户数变化空值显示 -- 并沉底、只渲染前 300 行）。
- 不要动 data/ 目录。
- 改完自己跑一遍第 9 节的验收清单，并给我一份改动说明。
```

---

*最后更新：2026-09-15*
