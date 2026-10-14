# 皖信台账系统 — 安全审查报告

> 审查日期：2026-06-15  
> 审查范围：app.py (3839行) · templates/index.html (4821行) · login.html (216行) · security_regression.py (410行)  
> 审查基线：git diff HEAD（约 7000+ 行新增）

---

## 总览

| 等级 | 数量 | 核心问题 |
|------|------|----------|
| 🔴 Critical | 2 | ID 生成竞态、付息状态口径不一致 |
| 🟠 High | 4 | 缺 CSP 头、AI 数据外泄风险、审批快照缺失、CSRF 豁免面 |
| 🟡 Medium | 7 | 错误信息泄露、审计脱敏不深、WAL 未 checkpoint、.env 安全 |
| 🔵 Low | 5 | 搜索无长度限制、CSV 无行数限制、测试覆盖不足 |

---

## 🔴 Critical — 必须立即修复

### C1. `next_text_id` 事务竞态 → 重复 ID

**文件**: `app.py:208-218`

```python
def next_text_id(conn, table, prefix):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    rows = conn.execute(f"SELECT id FROM {table} WHERE id LIKE ? ...")
    # 读 max_num + 写入 = 竞态窗口
    return f"{prefix}{max_num + 1:03d}"
```

**问题**: `BEGIN IMMEDIATE` 只阻止写，不阻止读。两个并发请求同时 SELECT → 看到同样的 max_num → 同样计算 `max_num+1` → 第二个 INSERT 因 PRIMARY KEY 冲突失败。

**实际风险**: 生产多用户并发新建触发 `IntegrityError`，返回 500。

**修复**:
```python
import random

def next_text_id(conn, table, prefix):
    for attempt in range(5):
        try:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"SELECT id FROM {table} WHERE id LIKE ? ORDER BY id",
                (f"{prefix}%",)
            ).fetchall()
            max_num = 0
            for row in rows:
                value = str(row["id"] or "")
                digits = value[len(prefix):]
                if digits.isdigit():
                    max_num = max(max_num, int(digits))
            return f"{prefix}{max_num + 1:03d}"
        except sqlite3.OperationalError:
            import time
            time.sleep(0.01 * (attempt + 1) + random.random() * 0.02)
    raise RuntimeError(f"Failed to allocate ID for {table}")
```

---

### C2. 付息台账状态与 Dashboard 口径不一致

**文件**: `app.py:226-227` (常量)、`app.py:1327-1337` (dashboard)、`app.py:388-398` (normalized)

**问题链**:
1. `payment_status_for_db()` 只产生 `pending`/`paid`/`overdue` 三个值
2. `UNPAID_PAYMENT_STATUSES = ("pending", "overdue", "待付", "未付", "逾期", "已逾期")` — 后半段中文值永远不会在 DB 中出现
3. Dashboard `overdue_payment` (line 1333) 直接硬编码中文 `'overdue','逾期','已逾期'` 而不是复用常量
4. `build_month_interest_reminders()` 用 `normalized_payment_status()` 按 `actual_pay_date` 判断是否已付；但 Dashboard 直接查 `status` 字段 — 如果一笔记录的 status="pending" 但 `actual_pay_date` 有值，两条路径对它的分类不一致

**影响**: 付息提醒显示"已逾期"，Dashboard 可能漏算这笔逾期金额。

**修复**: 在 `init_db()` 末尾添加数据清洗，统一存量 status:
```python
conn.executescript("""
    UPDATE payment_schedules SET status='pending'
        WHERE status IN ('待付','未付','待收','未收');
    UPDATE payment_schedules SET status='paid'
        WHERE status IN ('已付','已收','已完成','已结清','结清','settled');
    UPDATE payment_schedules SET status='overdue'
        WHERE status IN ('逾期','已逾期');
""")
```

并统一常量为:
```python
UNPAID_PAYMENT_STATUSES = ("pending", "overdue")
PAID_PAYMENT_STATUSES = ("paid",)
```

Dashboard overdue 查询改为复用常量 + 动态计算:
```python
overdue_payment = c.execute(f"""
    SELECT COALESCE(SUM(total_amount),0) FROM payment_schedules
    WHERE (status = 'overdue')
       OR (status = 'pending' AND date(due_date) < date('now','localtime'))
""").fetchone()[0]
```

---

## 🟠 High — 应尽快修复

### H1. 无 Content-Security-Policy 头 + CDN 无 SRI

**文件**: `templates/index.html:8`、`app.py:176-190`

**现状**: 
- `escapeHtml()` 正确转义 `&<>"'` ✅
- **但没有 CSP 头** — 如果内部转义漏了一个点，没有最后防线
- Chart.js 从 jsdelivr CDN 加载，无 `integrity` hash — CDN 被接管 = 全站被注入

**修复**:

1. 在 `secure_and_audit_response` 中添加:
```python
response.headers.setdefault(
    "Content-Security-Policy",
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://webrd0*.is.autonavi.com; "
    "connect-src 'self'; "
    "frame-ancestors 'none';"
)
```

2. 为 Chart.js 添加 SRI:
```html
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"
    integrity="sha384-..." crossorigin="anonymous"></script>
```
（需用 `openssl dgst -sha384 -binary chart.js | openssl base64 -A` 生成实际 hash）

---

### H2. AI Chat 对外发送全量台账——local provider 绕过检查

**文件**: `app.py:3474-3478`

```python
if backend["provider"] == "deepseek" and not allow_external_data:
    ledger_context = "外部 AI 数据传输未授权，未附带台账明细。"
```

**问题**: 只对 `deepseek` provider 检查了 `allow_external_data`，对 `local` provider 无条件放行。如果 `.env` 中 `LOCAL_LLM_BASE_URL` 被误配为外网地址，120条项目的完整金额、利率、客户名直接发送。

**修复**: 改为按目标地址是否本地来判断:
```python
def _is_local_url(url):
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("192.168.")

if not _is_local_url(backend["url"]) and not allow_external_data:
    ledger_context = "外部 AI 数据传输未授权，未附带台账明细。"
```

---

### H3. 领导直接执行审批——无审计快照

**文件**: `app.py:948-958`

```python
if not route:
    # 领导提交给自己 → 直接执行
    fake = {"related_type": ..., "action": ...}
    ok_exec = execute_approval_action(conn, fake)
    return {"direct": True}, None
```

**问题**: 领导直接 `confirm_payment` / `settle_project` 时：
1. 不创建 `approval_logs` 记录
2. 不保存 `snapshot`（执行前数据快照）
3. 审批中心完全看不到此操作
4. 只有通用 `audit_log`（仅记录 payload，缺执行前后对比）

**修复**: 即使直接执行也记录快照:
```python
if not route:
    snapshot = snapshot_for_approval(conn, related_type, related_id)
    ok_exec = execute_approval_action(conn, fake)
    if not ok_exec:
        return None, fail("执行失败", 500, "EXECUTE_FAILED")
    conn.execute("""INSERT INTO approval_logs
        (approval_id, reviewer, reviewer_role, reviewer_role_id, action, comment)
        VALUES (?,?,?,?,?,?)""",
        ("__direct__", role["person_name"], role["role_name"], role["id"],
         "direct_execute", json.dumps({"related_type": related_type, "related_id": related_id,
         "action": action, "snapshot": snapshot}, ensure_ascii=False)))
    return {"direct": True}, None
```

---

### H4. CSRF 豁免端点 `api_approve`/`api_reject` 可被 CSRF 利用

**文件**: `app.py:76-79`

```python
MUTATION_EXEMPT_ENDPOINTS = {
    "api_login", "api_logout", "calculate", "ai_chat",
    "api_create_approval", "api_approve", "api_reject",
}
```

**问题**: 审批操作豁免 CSRF 是因为它们**在 before_request 之后才有 CSRF 上下文**（审批操作需要通过审批中心先查询再操作）。但攻击者可以构造一个 CSRF 页面，当审批人访问时静默地通过/驳回审批。

**实际风险**: 低（需要审批人恰好登录 + 有待审批项 + 访问恶意页面），但绕过 CSRF 的设计不够清晰。

**修复建议**: 审批操作加入 `confirm` 字段要求:
```python
if request.endpoint in {"api_approve", "api_reject"}:
    if not data.get("confirm", False):
        return fail("请确认后再操作", 400, "CONFIRM_REQUIRED")
```

---

## 🟡 Medium — 应修复

### M1. 全局异常处理器泄露内部错误信息

**文件**: `app.py:192-203`

```python
message = str(err) if status < 500 else "服务器内部错误"
```

**问题**: SQLite 异常如 `UNIQUE constraint failed: projects.project_code` 直接作为错误消息返回给前端，暴露表结构。

**修复**: 过滤数据库错误:
```python
if "sqlite" in str(err).lower() or "UNIQUE" in str(err):
    message = "数据校验失败，请检查输入是否重复"
```

---

### M2. 审计日志脱敏不深入

**文件**: `app.py:826-855` (`write_audit_log`)

**问题**: 只对顶层 JSON key 做了脱敏，嵌套对象中的敏感字段不会脱敏。

**修复**: 递归脱敏函数:
```python
SENSITIVE_KEY_TOKENS = ("password", "secret", "token", "key")
MAX_AUDIT_DEPTH = 3

def _sanitize_audit_value(obj, depth=0):
    if depth > MAX_AUDIT_DEPTH:
        return "<max_depth>"
    if isinstance(obj, dict):
        return {k: ("<redacted>" if any(t in k.lower() for t in SENSITIVE_KEY_TOKENS)
                else _sanitize_audit_value(v, depth+1))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_audit_value(v, depth+1) for v in obj[:50]]
    return obj
```

---

### M3. `.env` 文件安全

**文件**: `app.py:17-35`

**现状**: `.env` 包含明文密钥。需要确保：
1. `.gitignore` 排除 `.env` 和 `initial_role_credentials.txt`
2. 生产环境使用系统环境变量而非 `.env` 文件

**检查**: 当前 `.gitignore`:
```
.env
initial_role_credentials.txt
```
需确认已添加这些条目。

---

### M4. `int()` 转换在恶意 rowid 路径中可能 500

**文件**: `app.py:330`

```python
if text.startswith("__rowid__"):
    return "rowid=?", (int(text.replace("__rowid__", "", 1)),)
```

**问题**: 传入 `__rowid__abc` → `int("abc")` → `ValueError` → 全局异常处理器捕获 → 500。

**修复**:
```python
if text.startswith("__rowid__"):
    try:
        return "rowid=?", (int(text[len("__rowid__"):]),)
    except (ValueError, TypeError):
        return "id=?", ("__invalid__",)  # 永假，安全回退
```

---

### M5. SQLite WAL 无自动 checkpoint

**文件**: `app.py:974`

WAL 已开启 ✅。但长期运行后 WAL 文件膨胀。

**修复**: 在 `init_db` 中添加:
```python
c.execute("PRAGMA wal_autocheckpoint = 1000")
```

---

### M6. 搜索端点无输入长度限制（DoS 向量）

**文件**: `app.py:3000`

```python
q = request.args.get("q", "").strip()
like = f"%{q}%"
```

超长 LIKE 模式匹配无法使用索引，CPU 密集。

**修复**:
```python
q = request.args.get("q", "").strip()[:100]
```

---

### M7. CSV 导出无行数上限

**文件**: `app.py:3019-3056`

全量查询可能 OOM。

**修复**:
```python
rows = conn.execute("... LIMIT 50000").fetchall()
```

---

## 🔵 Low — 建议优化

### L1. `calcPrincipal` 输入框未限制特殊字符
`getRawAmount()` 已做过滤，但输入框类型为 `text` 而非 `number`，前端输入 `1e10` 会被解析。

### L2. `parse_iso_date` 对 `datetime.datetime` 子类处理
`datetime.datetime` 是 `datetime.date` 的子类，传入时直接返回会丢失时间部分。Low risk（函数名明确是 date）。

### L3. 付息日历视图使用 `document.getElementById` 结果未判空
`line 4357` 处可能因 DOM 未就绪导致 null 引用。

### L4. `security_regression.py` 测试覆盖不足
缺失：XSS 向量测试、并发 ID 测试、空数据库测试、IDOR 测试、附件类型绕过测试、暴力破解限流测试。

### L5. `escapeAttr` 在属性值中使用
`jsArg()` → `escapeAttr(JSON.stringify(...))` — `JSON.stringify` 已转义引号，`escapeAttr` 再转义一次。在某些上下文中可能双重转义，但不会导致注入。

---

## ✅ 已做正确的安全措施

| 措施 | 位置 | 质量 |
|------|------|------|
| CSRF 保护 | app.py:163-166 | ⭐⭐⭐⭐ 时序安全比较 |
| Session 版本控制 | app.py:143-160 | ⭐⭐⭐⭐⭐ 密码重置即失效 |
| 登录限流 | app.py:3553-3565 | ⭐⭐⭐⭐ IP 级 10/min + 5次锁15min |
| AI 请求限流 | app.py:3453-3456 | ⭐⭐⭐⭐ 10/min per IP |
| 密码策略 | app.py:3653 | ⭐⭐⭐⭐ 最少12位 |
| HttpOnly Cookie | app.py:42 | ⭐⭐⭐⭐⭐ |
| SameSite Cookie | app.py:43 | ⭐⭐⭐⭐ Lax 模式 |
| 安全响应头 | app.py:178-179 | ⭐⭐⭐⭐ X-Content-Type-Options 等 |
| 路径穿越防护 | app.py:2474-2488 | ⭐⭐⭐⭐⭐ `os.path.realpath` 验证 |
| 附件独占检测 | app.py:2490-2507 | ⭐⭐⭐⭐⭐ 一文件一报告 |
| 附件魔数验证 | app.py:2532-2542 | ⭐⭐⭐⭐ PDF/DOCX header 检测 |
| 审批完整性 | app.py:904-958 | ⭐⭐⭐⭐ 多级审批+快照+回滚 |
| 已付不可逆 | app.py:2799-2800 | ⭐⭐⭐⭐⭐ PAID_IMMUTABLE |
| 已结清不可逆 | app.py:1834-1835 | ⭐⭐⭐⭐⭐ SETTLED_IMMUTABLE |
| WAL 模式 | app.py:974 | ⭐⭐⭐⭐ 并发性能 |
| 外键约束 | app.py:124 | ⭐⭐⭐⭐⭐ |
| busy_timeout | app.py:125 | ⭐⭐⭐⭐ 30s |

---

## 🔧 修复脚本

以下为独立修复脚本，保存为 `_fix_security.py` 后执行：

```python
#!/usr/bin/env python3
"""皖信台账系统 — 安全修复脚本"""
import sqlite3, os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "wanxin.db")

def fix_status_normalization():
    """C2: 统一 payment_schedules.status"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")

    before = conn.execute(
        "SELECT status, COUNT(*) FROM payment_schedules GROUP BY status"
    ).fetchall()
    print("修复前:", {r[0]: r[1] for r in before})

    conn.executescript("""
        UPDATE payment_schedules SET status='pending'
            WHERE status IN ('待付','未付','待收','未收');
        UPDATE payment_schedules SET status='paid'
            WHERE status IN ('已付','已收','已完成','已结清','结清','settled');
        UPDATE payment_schedules SET status='overdue'
            WHERE status IN ('逾期','已逾期');
    """)

    after = conn.execute(
        "SELECT status, COUNT(*) FROM payment_schedules GROUP BY status"
    ).fetchall()
    print("修复后:", {r[0]: r[1] for r in after})
    conn.commit()
    conn.close()
    print("✅ C2 状态标准化完成")

def fix_wal_checkpoint():
    """M5: WAL 自动 checkpoint"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA wal_autocheckpoint = 1000")
    conn.close()
    print("✅ M5 WAL auto-checkpoint=1000")

def fix_missing_indexes():
    """添加缺失索引"""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_pay_status ON payment_schedules(status);
        CREATE INDEX IF NOT EXISTS idx_proj_status ON projects(status);
        CREATE INDEX IF NOT EXISTS idx_audit_date ON audit_logs(created_at);
    """)
    conn.commit()
    conn.close()
    print("✅ 索引已添加")

if __name__ == "__main__":
    fix_status_normalization()
    fix_wal_checkpoint()
    fix_missing_indexes()
    print("\n全部数据修复完成。")
    print("请手动应用 app.py 中的代码级修复（C1/H1/H2/H3/M1/M4/M6/M7）。")
```

---

## 🧪 回归测试补充

在 `security_regression.py` 的 `SecurityRegressionTests` 类末尾追加：

```python
def test_xss_vectors_in_output(self):
    """XSS payload 在 API 响应中正确序列化"""
    response, data = self.login("r4", "Staff-Test-Password-2026")
    self.assertEqual(response.status_code, 200)
    headers = {"X-CSRF-Token": data["csrf_token"]}

    payloads = [
        "<script>alert(1)</script>",
        '"><img src=x onerror=alert(1)>',
        "';alert(1);//",
    ]
    conn = sqlite3.connect(ledger.DB_PATH)
    for i, p in enumerate(payloads):
        conn.execute("INSERT INTO customers (id, name) VALUES (?,?)",
                     (f"c_xss_{i}", p))
    conn.commit(); conn.close()

    resp = self.client.get("/api/customers")
    self.assertEqual(resp.status_code, 200)
    customers = resp.get_json()
    xss_items = [c for c in customers if str(c.get("id","")).startswith("c_xss_")]
    self.assertEqual(len(xss_items), len(payloads))
    for c in xss_items:
        self.assertIsInstance(c["name"], str)
        self.assertNotIn("<script>", c["name"].lower())

    # cleanup
    conn = sqlite3.connect(ledger.DB_PATH)
    for i in range(len(payloads)):
        conn.execute("DELETE FROM customers WHERE id=?", (f"c_xss_{i}",))
    conn.commit(); conn.close()


def test_empty_database_no_500(self):
    """空数据库 DashBoard 不崩溃"""
    conn = sqlite3.connect(ledger.DB_PATH)
    for t in ("payment_schedules","opportunities","reminders","projects","customers"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit(); conn.close()

    self.login("r4", "Staff-Test-Password-2026")
    dash = self.client.get("/api/dashboard")
    self.assertEqual(dash.status_code, 200)
    d = dash.get_json()
    self.assertEqual(d["project_count"], 0)


def test_rowid_bad_value_no_500(self):
    """恶意 __rowid__ 值不应 500"""
    self.login("r4", "Staff-Test-Password-2026")
    resp = self.client.get("/api/customers/__rowid__abc")
    self.assertIn(resp.status_code, (400, 404))


def test_dashboard_overdue_not_double_counted(self):
    """Dashboard overdue <= pending"""
    conn = sqlite3.connect(ledger.DB_PATH)
    conn.execute("INSERT INTO customers (id,name) VALUES ('c_dt','DashTest')")
    conn.execute("INSERT INTO projects (id,project_name,customer_id,status) VALUES ('p_dt','DashP','c_dt','active')")
    today = datetime.date.today()
    past = (today - datetime.timedelta(days=5)).isoformat()
    conn.execute("INSERT INTO payment_schedules (id,project_id,period_number,due_date,total_amount,status) VALUES ('pay_dt1','p_dt',1,?,5000,'pending')", (past,))
    conn.execute("INSERT INTO payment_schedules (id,project_id,period_number,due_date,total_amount,status) VALUES ('pay_dt2','p_dt',2,?,3000,'pending')", ((today + datetime.timedelta(days=5)).isoformat(),))
    conn.commit(); conn.close()

    self.login("r4", "Staff-Test-Password-2026")
    dash = self.client.get("/api/dashboard").get_json()
    self.assertGreaterEqual(dash["pending_payment"], 8000)
    self.assertGreaterEqual(dash["overdue_payment"], 5000)
    # overdue 不可能超过 pending
    self.assertGreaterEqual(dash["pending_payment"], dash["overdue_payment"])

    # cleanup
    conn = sqlite3.connect(ledger.DB_PATH)
    conn.execute("DELETE FROM payment_schedules WHERE project_id='p_dt'")
    conn.execute("DELETE FROM projects WHERE id='p_dt'")
    conn.execute("DELETE FROM customers WHERE id='c_dt'")
    conn.commit(); conn.close()
```

---

## ✓ 验证清单

| 检查项 | 验证方法 |
|--------|----------|
| C1: 并发 ID 不重复 | 运行 `test_concurrent_id_generation` |
| C2: 状态已统一 | `sqlite3 wanxin.db "SELECT status, COUNT(*) FROM payment_schedules GROUP BY status"` 只含 pending/paid/overdue |
| H1: CSP 头存在 | 浏览器 DevTools → Network → 任意响应 → Response Headers 含 `Content-Security-Policy` |
| H2: AI 数据不外泄 | `.env` 中 `AI_ALLOW_EXTERNAL_LEDGER_DATA=0` + 仅用本地 LLM |
| H3: 领导操作有审计 | leader 执行确认收款后，`approval_logs` 表有记录 |
| M1: 错误不泄露表结构 | 尝试创建重复 `project_code`，确认返回"数据校验失败"而非 "UNIQUE constraint" |
| M4: 恶意 rowid 不 500 | `curl /api/customers/__rowid__abc` → 4xx 非 500 |
| 空数据库不崩溃 | 运行 `test_empty_database_no_500` |
| XSS 防护有效 | 运行 `test_xss_vectors_in_output` |
| Dashboard 口径一致 | 运行 `test_dashboard_overdue_not_double_counted` |

---

## 📊 总结

| 维度 | 评分 | 说明 |
|------|------|------|
| 身份认证 | ⭐⭐⭐⭐ | 哈希 + session_version + 限流 + 12位密码 |
| 授权 | ⭐⭐⭐⭐ | 5级角色 + can_approve/can_submit 矩阵 |
| CSRF | ⭐⭐⭐⭐ | 时序安全 token + 豁免白名单 |
| XSS 防护 | ⭐⭐⭐ | escapeHtml 完整，缺 CSP + CDN-SRI |
| 数据完整性 | ⭐⭐⭐ | 事务+WAL+外键，ID竞争+状态不一致待修 |
| 审计 | ⭐⭐⭐⭐ | 审计日志+审批快照+附件追踪 |
| 机密性 | ⭐⭐⭐ | AI数据外传有保护但缺统一策略 |
| 文件安全 | ⭐⭐⭐⭐⭐ | 路径穿越+魔数+引用计数+独占 |
| **整体** | **7.6/10** | 基础牢固，2个Critical+4个High需优先处理 |

**首要行动项**:
1. C1: 修复 `next_text_id` 竞态 → 加重试循环
2. C2: 运行 `_fix_security.py` 统一状态值
3. H1: 添加 CSP 头 + Chart.js SRI
4. H2: 修复 AI 数据外泄检查逻辑
5. 补充回归测试
