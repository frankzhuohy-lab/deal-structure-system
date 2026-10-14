# 皖信投资台账系统 — 优化方案

> 生成时间：2026-05-25
> 项目路径：`C:\Users\frank\Desktop\皖信台账系统\`
> Codex CLI：`C:\Users\frank\AppData\Local\OpenAI\Codex\bin\3f4fb8cdd344abc7\codex.exe`

---

## 一、已知问题（需修复）

### 1. 🔴 前端 JS 错误 — `Uncaught (in promise) TypeError`
- **位置**：`templates/index.html` ~line 830，`loadDashboard` 函数
- **症状**：`Cannot read properties of undefined (reading 'map')`
- **原因**：API 返回数据格式与前端期望不匹配，或某些统计字段为 null/undefined
- **修复方案**：在 `.map()` 前加 `?.` 或 `|| []` 保护，确保即使数据缺失也不会崩溃

### 2. 🟠 付息台账数据混乱
- **症状**：用户反馈"都是按季付息的，台账显示乱的一塌糊涂"
- **原因**：
  - `payment_schedules` 表重新生成时算法可能对部分还款方式处理不当
  - 前端渲染时未按项目/期数排序
  - 合计/统计计算逻辑有误
- **修复方案**：
  - 检查 `payment_schedules` 生成的 SQL 逻辑
  - 前端按 `project_id, period` 排序显示
  - 修复合计行计算

### 3. 🟠 利率显示逻辑
- **现状**：`annual_rate` 在数据库存小数（如 0.075 表示 7.5%）
- **前端**：已做 `×100` 显示，但需验证所有编辑/新增场景
- **需要**：增删改查全链路验证

### 4. 🟡 onclick 引用修复验证
- **修复**：所有 `onclick` 中 ID 加了引号（如 `'${p.id}'`）
- **验证**：增、删、改、查看付息计划等操作逐一测试

### 5. 🟡 `api()` 函数兼容修复
- **修复**：前端 `api()` 自动为无 error 的响应补 `success: true`
- **验证**：所有 API 调用场景测试

---

## 二、代码质量优化（建议）

### 2.1 后端 `app.py`（733行）

| 问题 | 建议 |
|------|------|
| 无路由前缀 | 加上 `/api/` 路由前缀划分，前后端分离更清晰 |
| SQLite 无连接池管理 | 每次请求都用 `get_db()` 但无超时/重连逻辑 |
| 无输入验证 | 所有 POST/PUT 数据需做类型、必填校验 |
| 无错误处理 | API 异常时返回统一错误格式 `{"error": msg, "code": xxx}` |
| 硬编码 `DB_PATH` | 改为环境变量或配置类 |
| 付息计划生成的业务逻辑 | 提取为独立函数/模块，便于测试 |
| 缺少数据类型定义 | 增加 Pydantic/dataclass 或至少常量定义 |

### 2.2 前端 `index.html`（1571行）

| 问题 | 建议 |
|------|------|
| 单文件过大 | 拆分为：`app.js`（业务逻辑）, `styles.css`（样式）, `components/`（组件） |
| 无加载状态 | 所有 AJAX 请求需显示 loading 动画 |
| 无错误提示 | API 失败时 toast 通知 |
| 全局函数过多 | 封装为命名空间或使用模块模式 |
| 硬编码 API URL | 使用相对路径或配置常量 |

### 2.3 数据库

| 问题 | 建议 |
|------|------|
| schema 版本管理 | 添加 alembic 或 migrations 文件夹 |
| 缺少索引 | `project_id`, `customer_id`, `due_date` 等字段加索引 |
| 外键约束松散 | 开启 PRAGMA foreign_keys=ON，添加 ON DELETE CASCADE |

---

## 三、功能增强（可选）

- [ ] **搜索/筛选**：项目名、客户名、状态、日期范围
- [ ] **数据导出**：Excel 导出（当前支持 CSV）
- [ ] **批量操作**：批量确认收款、批量修改状态
- [ ] **资金成本分析**：贷款-自有资金息差计算
- [ ] **审计日志**：记录所有增删改操作（已有 audit_log 表？）
- [ ] **数据备份**：自动备份功能（已有 wanxin.db.backup）

---

## 四、执行计划

### 阶段一：修复阻塞问题（优先）
1. 修复 `loadDashboard` TypeError → `?.` + 默认值保护
2. 排序付息台账 → `ORDER BY project_id, period`
3. 全链路验证增删改查功能

### 阶段二：优化代码结构
4. 拆分前端单文件 → app.js + styles.css
5. 添加加载状态 + 错误提示
6. 后端加输入验证 + 统一错误格式

### 阶段三：增强功能
7. 付息计划生成算法优化
8. 搜索/筛选功能
9. Excel 导出增强

---

## 五、测试验证清单

- [ ] 首页加载无 JS 错误（F12 Console）
- [ ] 项目列表显示正常（15个项目）
- [ ] 新增项目 → 保存成功
- [ ] 编辑项目 → 保存成功
- [ ] 删除项目 → 确认删除成功
- [ ] 利率显示正确（如 7.5%）
- [ ] 付息台账按项目/期排序
- [ ] 客户 CRUD 正常
- [ ] 数据导出正常
- [ ] API 返回统一格式
- [ ] 前端错误有提示

---

## 六、快速启动

```bash
# 进入项目目录
cd "C:\Users\frank\Desktop\皖信台账系统"

# 启动虚拟环境
.venv\Scripts\python.exe app.py

# 访问
http://localhost:8090
```

> **注意**：main 数据库使用根目录 `wanxin.db`，Schema 中 `annual_rate` 存小数（0.075 表示 7.5%）。
