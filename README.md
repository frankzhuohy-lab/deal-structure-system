# 交易结构图系统（Deal Structure System）

金融交易结构图的两种实现：独立可视化原型 + 皖信台账系统内置模块。附成品样图。

## 组成

### 1. `deal-structure-app/` — 独立原型（React + Express，v2.0.0）

- **画布**：@xyflow/react 节点编辑 + dagre 自动布局
- **AI 生成**：`src/utils/aiParser.ts` + `server/routes/ai.cjs` — 中文业务描述 → 结构图 JSON
- **数据**：better-sqlite3（首次运行自动建库）
- **运行**：
  ```bash
  cd deal-structure-app
  npm ci
  npm run dev:all   # 前端 vite + 后端 express
  ```
- 内置宁国项目模板：`src/data/ningguoTemplate.ts`

### 2. `wanxin-ledger/` — 皖信台账系统（Flask，生产版）

保理/融资业务台账，含完整交易结构图模块（`app.py` 约 2084~2534 行）：

- 结构图 CRUD API，标题自动取资金方名称（"XX交易结构图"）
- **LLM 抽取器**："金融交易结构图抽取器"——中文描述抽取为严格 JSON（不输出解释/Markdown）
- 安全：会话密钥与角色密码全部走环境变量（`LEDGER_SECRET_KEY`、`LEDGER_PASSWORD_*`），代码零硬编码
- 运行：`LEDGER_SECRET_KEY=$(openssl rand -hex 32) python3 app.py`

设计/评审文档：`AI_CHAT_PLAN.md`、`OPTIMIZATION_PLAN.md`、`code_review.md`、`task_list.md`

### 3. `samples/保理交易结构图.png` — 成品样例

## 说明

- 两个系统数据层不互通（原型 `data.db` / 皖信 `wanxin.db`，均为真实业务数据，**未纳入本仓库**）
- 数据库首次运行自动初始化；leaflet 地图组件为前端内嵌依赖
