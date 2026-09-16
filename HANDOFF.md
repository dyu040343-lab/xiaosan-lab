# 交接说明 · 小散研究院（retail-radar）

> 一页纸：代码在哪、怎么跑、怎么发布、哪些坑别踩。
> UI 契约与设计规范见 [HANDOFF-UI.md](HANDOFF-UI.md)。最后更新：2026-09-15

---

## 1. 三个位置（唯一事实来源）

| 角色 | 位置 |
|---|---|
| **线上站点** | https://xiaosanlab.online/retail-radar.html |
| **GitHub（代码主仓库，公开）** | https://github.com/dyu040343-lab/xiaosan-lab |
| **服务器（nginx 根 = 部署目录）** | `ubuntu@43.129.201.84:/home/ubuntu/retail-radar` |
| **本地开发目录（原开发机）** | `~/Library/Application Support/TRAE SOLO CN/ModularData/ai-agent/work-mode-projects/6a9edb82000836d759aa9c80/retail-radar-deploy` |

⚠️ 本地目录藏在 macOS 的**应用数据目录**里（TRAE SOLO 的工作区目录），不要指望用访达找得到。
**换机器交接时不用拷贝它** —— 直接 `git clone https://github.com/dyu040343-lab/xiaosan-lab.git` 就是全部代码，
仓库里已经有 `data/radar_data.json`（最近一次快照）和下面第 5 节的所有口径。

> 本文件放在**公开仓库**里，所以不含任何密码 / Token。凭据由项目 owner 保管（见第 7 节）。

---

## 2. 文件地图

| 文件 | 作用 | 能不能动 |
|---|---|---|
| `retail-radar.html` | **整个前端**（约 2200 行，内联 CSS + 原生 JS，单文件零构建、无框架） | ✅ 主战场 |
| `HANDOFF-UI.md` | UI 交接契约：技术红线 / 数据契约 / 12+ 条不许改的口径 / 验收清单 | 改 UI 前先读 |
| `scripts/fetch_data.py` | 服务端抓数：东财 push2 行情 + datacenter 股东结构 → `data/radar_data.json` | ⚠️ 改字段要同步改前端 |
| `scripts/update_calendar.py` | 生成/校验 A 股交易日历（每年 12 月跑一次） | 每年跑一次 |
| `data/radar_data.json` | **数据产物**（约 6.8MB，每 10 分钟被服务器覆盖） | ❌ 别改别提交 |
| `data/trading_calendar.json` | 交易日历（周末 + 法定节假日休市日），前后端共用 | 由脚本生成 |
| `data/radar_data_cache.json` | 抓数失败时的降级缓存 | ❌ 别动 |
| `index.html` | 跳转到 `retail-radar.html` | 一般不动 |
| `about / terms / privacy / disclaimer.html` | 合规静态页（**文案别删**） | 可改样式 |
| `deploy.sh` | 最初的一次性环境初始化脚本（**已过时，别直接跑**，它会 `git clone` 覆盖部署目录） | 参考用 |
| `_shared/`、`assets/` | 历史遗留（echarts、旧 app.js/charts.js），**无页面引用** | 可删 |
| `.github/workflows/deploy.yml` | GitHub Actions：**只在 push 时**跑一次抓数并把数据提交回仓库 + 发 GitHub Pages（线上是服务器，不是它） | 一般不动 |

---

## 3. 日常操作

```bash
# ① 本地预览（必须走 http，不能双击文件：fetch 会被 CORS 拦死）
cd <仓库根目录> && python3 -m http.server 8080
# 打开 http://localhost:8080/retail-radar.html

# ② 手工抓一次数据（默认只在交易时段生效；--force 强制）
python3 scripts/fetch_data.py            # 需要 requests
python3 scripts/fetch_data.py --force

# ③ 刷新交易日历（交易所每年 12 月公布次年安排后跑）
python3 scripts/update_calendar.py                    # 今年 + 明年
python3 scripts/update_calendar.py --verify-only      # 只校验不写盘

# ④ 语法自检（无浏览器环境的最低保障）
#    抽出 <script> 内容 → node --check

# ⑤ 服务器上「立即刷新」接口（页面「刷新」按钮打的就是它）
curl -s https://xiaosanlab.online/api/refresh                      # GET：只报状态，不触发
curl -s -X POST -H 'Origin: https://xiaosanlab.online' \
     https://xiaosanlab.online/api/refresh                          # POST：真的抓一次（约 21–30 秒）
#    限制：单飞 + 两次间隔 ≥120s + 每小时 ≤10 次 + 仅交易时段
#    看日志：journalctl -u retail-radar-api -n 50 --no-pager
#    重启：sudo systemctl restart retail-radar-api
```

**部署（目前是 SFTP 直传，不是 git）：**

```bash
# 用 paramiko / scp 把 **改动的那几个文件** 传到 /home/ubuntu/retail-radar/ 对应位置，
# 传完做一次 md5 校验，再 curl 一次线上确认：
curl -sk https://127.0.0.1/retail-radar.html -H 'Host: xiaosanlab.online' -o /dev/null -w '%{http_code}\n'
```

⛔ **绝对不要用 `scp -r` 或 rsync 整目录覆盖**：会把 `data/radar_data.json` 一起覆盖掉，
那是服务器每 10 分钟自己生成的数据。

---

## 4. 服务器环境事实（已实测）

| 项 | 值 |
|---|---|
| 主机 | 腾讯云轻量应用服务器（**中国香港**，免备案），`ubuntu@43.129.201.84` |
| 域名 / HTTPS | `xiaosanlab.online`（DNSPod 解析）→ nginx + certbot，80 会跳 443，**证书自动续期**（`sudo certbot certificates` 查） |
| nginx 站点配置 | `/etc/nginx/sites-available/xiaosanlab`（软链在 `sites-enabled/`），`root /home/ubuntu/retail-radar` |
| gzip | 配在 `/etc/nginx/nginx.conf` 的 `gzip_types`（**别再往 `conf.d/` 写 `gzip on`，会 duplicate 报错**） |
| 时区 | `Asia/Shanghai`（= 北京时间，cron 直接按北京时间写） |
| 抓数 cron | 6 行，**只在交易时段**：`9:10–11:30 / 13:00–15:20`，周一至周五，共 30 次/天 |
| cron 日志 | **`/tmp/radar_cron.log`**（不在项目目录里） |
| Python | `/usr/bin/python3` = 3.12.3，已装 `requests` 2.31.0 |
| 「立即刷新」接口 | `retail-radar-api.service`（systemd）跑 `scripts/refresh_api.py`，**只监听 `127.0.0.1:8081`**，由 nginx `location = /api/refresh` 反代 |
| 接口限流参数 | 环境变量可调：`REFRESH_MIN_INTERVAL`(120s) / `REFRESH_MAX_PER_HOUR`(10) / `REFRESH_TIMEOUT`(180s) / `REFRESH_ALLOW_ORIGINS` |
| 接口口令（可选加固） | 写 `data/.refresh_token`（`chmod 600`）后，请求必须带 `X-Refresh-Token`；不写就是"公开但严格限流" |
| ⚠️ 已停用的遗留服务 | `retail-radar.service`（`python3 -m http.server 8080`）—— **曾是公网可达的裸 HTTP 目录服务**（能读到 `.git/`、`data/` 等），已 `disable --now`。**别重新启用**（nginx 已覆盖 80/443 的全部需求） |

---

## 5. 数据口径速查（改前端前必读，详版见 HANDOFF-UI.md）

- **三档（资金维度）**：散户 = 小单净额、中单 = 中单净额、主力 = 超大单 + 大单；**三者净额之和恒等于 0**。
- **占比维度**：`dynamic_ratio = retail_net / total_amount × 100`，**必须保留 `LIQUIDITY_MIN`（5 亿）流动性过滤**，
  否则成交额几十万的小票会算出 +70% 的伪占比霸榜（实测：不筛的话金发科技能挤进散户占比前 10，成交额只有 1.85 亿）。
- **筹码维度**：`锁定盘 = 100% − 自由流通`（A 股视角，避免 A+H 公司的 H 股带偏）；
  三档持股统一**占自由流通**；`holder_chg === null` 表示该股按月披露户数、与季频窗口不可比 → 显示 `--` 并沉底（**不能当 0**）。
- **交易日历**：`data/trading_calendar.json`，周末一律休市（含调休补班的周末），`closed_days` 只列额外休市的**工作日**；
  **日历缺失或未覆盖当年时一律判定为「开市」**（宁可多跑一次，绝不停摆）。
- **配色**：涨 = 红、跌 = 绿（A 股规则）；主力档刻意用蓝色标识"对手盘"。

---

## 6. ⛔ 交接陷阱（都是踩过的）

1. **不要在服务器上 `git pull` / `git checkout`**。服务器那是个 2026-09-11 的旧 clone（HEAD `2a0638c`），
   工作区早就被 SFTP 直传改脏了；真去 reset 会把线上版本**回退**。**部署以 SFTP 为准，服务器上的 git 只当历史残留。**
2. **部署时别覆盖 `data/radar_data.json`**（服务器每 10 分钟自己生成的东西）。
3. **改了 `build_chip_flow` 的字段结构，必须把 `HOLDER_DIST_VERSION` +1**，否则当天缓存会复用旧结构却"跑成功"（静默失效，踩过两次）。
4. **表头冻结（sticky）的两个坑**：`.table-box` 的 `padding-top` 必须是 `0`；排序激活列的背景必须**不透明**。改了就会露出滚动内容。
5. **空态不能只说"暂无数据"**。用户会直接判定"功能坏了"。空态必须说清：这个词有没有、这只票现在什么方向、下一步点哪里。
6. **「搜索范围」≠「榜单口径」**：榜单表只展示 TOP30，但筛选扫的是整张榜单（几千只）—— 这是刻意的，别"优化"成只搜显示的 30 行。
7. **cron 的时段和脚本里的闸门是两套**：cron 管"什么时候跑"，`fetch_data.py` 里的 `in_trading_window()` 管"该不该跑"（双保险）。
8. **东财接口有 IP 限流**：主接口 `push2` 偶发 502 时会自动降级到 `push2delay` 镜像（分 60 页抓，慢但能成），别把降级逻辑删了。
9. **`/api/refresh` 的限流不能放宽**：它替用户立刻跑一次 `fetch_data.py`（约 21–30 秒、要打 60 页东财接口）。无节制触发会被东财按 IP 限流，**反过来把正常的数据管道一起弄坏**。要改就改环境变量，别去注释掉判定。
10. **别用 `python3 -m http.server` 对外提供服务**：它没有鉴权、会把整个目录（含 `.git/`、`data/`）明文暴露。2026-09-16 就是这样挂了一个公网可达的 8080，已关闭。真要对外，走 nginx。

---

## 7. 凭据与安全（owner 待办）

- 服务器登录密码、DNSPod API Token 曾在聊天记录里出现过 → **建议轮换**，并改用 SSH key 登录（关掉密码登录）。
- 本仓库是**公开**的，所以任何密码 / Token / 私钥都不要提交进来。
- 部署动作建议只由 owner 执行，不要把服务器凭据交给外部 AI 工具。
- 2026-09-16 已实测确认：暴露的 8080 目录里**没有**凭据（`.git/config` 无 token、`deploy.sh` 无密码），
  但 `.git/` 历史与 `data/` 全量数据确实可被任意人读取 —— 这类暴露属于"能用就不该留"，已关闭。

## 8. 建议的下一步

- 把服务器上的旧 `.git` 删掉（或重新 clone 到别处、只保留静态文件），避免后人误用。
- 停用 `retail-radar.service`（8080 的遗留 http.server）。
- 清理仓库里没人引用的 `_shared/`、`assets/`。
- 如果要做"换设备/换浏览器自选股不丢"，需要给站点加账号或后端存储（目前自选只存在浏览器 localStorage）。
