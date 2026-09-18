# 报关通 · 报关行自研报关作业与合规风控平台

进出口报关单**全生命周期**管理 + 合规风控骨架。本期落地「**报关作战台**」与「**委托与客户**」两条主线。

- 后端：Python 3.11 + FastAPI + SQLAlchemy + SQLite，监听 **7106**
- 前端：零构建原生 SPA（HTML/CSS/JS），由 Nginx 在 **8106** 托管并反代 `/api → 7106`
- 一键拉起：`docker compose up -d --build`，打开 http://localhost:8106

---

## 一键启动

```bash
docker compose up -d --build
# 前端/入口：http://localhost:8106
# 后端直连：http://localhost:7106/api/health
```

首次启动后端会**自动建表并预置演示数据**（SQLite 存于命名卷 `bgt-data:/data`）。
重置演示数据：

```bash
docker compose down -v && docker compose up -d --build
```

## 预置账号（统一口令 `bgt123456`）

| 账号 | 角色 | 说明 |
| --- | --- | --- |
| `zhang_wei` | 报关员 | 张伟，盐田/蛇口口岸，可立项派单、录入、离线作业、二次确认看全名 |
| `li_na` | 报关员 | 李娜，蛇口/南沙口岸 |
| `wang_hua` | 海关审单员 | 王华，审单、布控查验、放行、结关、排查验计划 |
| `admin_huateng` | 企业管理员 | 陈志成，华腾企业（仅能看本企业单据） |
| `admin_yuanhang` / `admin_xianchi` | 企业管理员 | 远航 / 鲜驰企业管理员 |
| `zhao_jing` | 监管员 | 赵静，备案审核、留痕审计、全局态势 |

预置数据：**4 家企业**（3 家已备案 + 1 家备案中）、生效/待签委托合同、**13 张报关单**覆盖
`委托中 / 已录入 / 审单中 / 查验中 / 已放行 / 已结关 / 已撤销`，含 1 张逾期税款、1 张 3 日内到期税款、
今日查验排期与 1 条逾期查验任务。

---

## 功能与验收点对照

### 1. 委托到客户做成链路，非法回退拦下说原因
链路强约束（见 `app/deps.py::ensure_active_contract`、`app/routers/clients.py`）：

```
企业备案(filing) ──审核通过──> 已备案(active) ──签署──> 委托合同生效(active) ──> 报关单立项(entrusted)
```

- 给**备案中/被驳回**企业签合同 → 409，返回具体原因（含驳回原因）。
- 凭**未生效/他企业**合同立项 → 409，说明必须先签生效合同。
- 状态机集中在 `app/models.py::guard_transition`：终态不可变更、不允许跳态/回退，
  非法时返回形如「不允许从 X 直接变为 Y，当前允许的下一步是…」的可读原因。
  唯一合规回退通道 `审单中 → 已录入（退单补录）`、`查验中 → 审单中（查验异常重审）`已显式放行。

### 2. 企业名脱敏 + 二次确认 + 留痕
- 默认展示名 = 字号缩写 + 编号（如「华腾 E0101」），统一社会信用代码同步掩码。
- 报关员查看全名必须**二次确认并填写 ≥10 字理由**，写入 `name_reveals` 表。
- 「全名查看留痕」页（监管员看全部、报关员看自己的）可审计追溯。

### 3. 报关作战台
- 全行六态待审漏斗 + 按企业聚合的迷你漏斗/待审 KPI。
- 查验排期表（今日数、逾期红点）。
- 征税红点：**逾期未缴（红）**、**3 日内到期（黄）**，可一键跳单据处置。
- 响应式适配 **1440 桌面 / 1024 平板 / 390 手机**（断点 1200px、720px，侧栏在窄屏转横向标签）。

### 4. 企业间隔离，越权给错误态而非空白页
- 企业管理员只能读本企业数据；列表带他企业 `enterprise_id` → 403；
  直接打开他企业报关单详情 → **明确的错误态页**（错误码 + 原因 + 返回入口），不是空白页。
- 越权执行非本角色操作同样被状态机角色表拦下（403 + 原因）。

### 5. 口岸离线：不能拿旧状态糊弄，恢复后合并且幂等
- 断网时顶部红色横幅常驻；所有缓存视图打「🕓 离线缓存 · 截至 …」水印，
  并明示**非实时状态**、不提供误导性的操作按钮。
- 断网可：①本地起草报关单（带稳定 `idem_key` + `client_ref`）②本地排队流转（带 `client_event_id`）。
- 恢复联网后自动补传：
  - 建单走 `Idempotency-Key` 与 `client_ref` 双保险，**绝不产生重复报关单**，重放返回首次结果；
  - 流转走 `/declarations/{id}/sync-offline`，在服务端逐条重放状态机：
    已应用→跳过（幂等）、非法/构成回退→进**冲突清单**弹给人工，绝不静默覆盖。
- 在线流转带 `expected_version` 乐观锁：版本过期返回 409 `version_conflict`，
  前端拉最新快照后弹出「合并确认」，基于新版本重提。

> 浏览器里演示断网：DevTools → Network → Offline，或直接拔网；恢复后会自动触发合并。

## 目录结构

```
.
├── docker-compose.yml          # 一键拉起 backend(7106)+frontend(8106)
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py             # FastAPI 入口、统一错误格式
│       ├── models.py           # ORM + 状态机（guard_transition 单一事实来源）
│       ├── deps.py             # 登录态、企业间隔离、委托链路校验
│       ├── security.py         # pbkdf2 口令哈希、会话 token
│       ├── schemas.py          # 入参/序列化
│       ├── seed.py             # 预置数据
│       └── routers/ (auth / clients / declarations / dashboard)
└── frontend/
    ├── Dockerfile
    ├── nginx.conf              # 8106 静态托管 + /api 反代 7106
    ├── index.html
    └── css / js (api、offline、app)
```

## 本地开发（不用 Docker）

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
BGT_DATA_DIR=./data uvicorn app.main:app --reload --port 7106
# 前端可用任意静态服务器，把 /api 代理到 127.0.0.1:7106
```

## 后续骨架可扩展点

电子缴款回执对接、HS 归类/价格合规风控规则引擎、证件与许可证校验、单一窗口报文对接、
查验异常处置工作流、企业信用分级看板。
