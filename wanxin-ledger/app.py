# -*- coding: utf-8 -*-
"""
皖信投资固投项目台账管理系统 - Flask Backend
端口: 8090, 数据库: wanxin_v2.db (SQLite)
"""
import os, json, sqlite3, datetime, math, re, uuid, urllib.request, urllib.error, urllib.parse
import ipaddress
import secrets, time, threading, logging
from collections import defaultdict, deque
from logging.handlers import RotatingFileHandler
from flask import Flask, request, jsonify, render_template, send_file, Response, session, redirect, url_for
from io import BytesIO
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

APP_DIR = os.path.dirname(os.path.abspath(__file__))

def config_value(name, default=""):
    value = os.environ.get(name)
    if value:
        return value.strip()
    env_path = os.path.join(APP_DIR, ".env")
    if not os.path.exists(env_path):
        return default
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, raw = line.split("=", 1)
                if key.strip() == name:
                    return raw.strip().strip('"').strip("'") or default
    except OSError:
        return default
    return default

app = Flask(__name__)
app.secret_key = config_value("LEDGER_SECRET_KEY", "")
if not app.secret_key:
    raise RuntimeError("必须通过 LEDGER_SECRET_KEY 配置随机会话密钥")
app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(hours=int(config_value("LEDGER_SESSION_HOURS", "8") or 8))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = config_value("LEDGER_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
DB_PATH = os.path.join(APP_DIR, "wanxin.db")
REPORT_ATTACHMENT_DIR = os.path.join(APP_DIR, "private_uploads", "report_attachments")
INITIAL_CREDENTIALS_PATH = os.path.join(APP_DIR, "initial_role_credentials.txt")
LOG_DIR = os.path.join(APP_DIR, "logs")

os.makedirs(LOG_DIR, exist_ok=True)
if not any(isinstance(handler, RotatingFileHandler) for handler in app.logger.handlers):
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "wanxin.log"),
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"
    ))
    app.logger.addHandler(file_handler)
app.logger.setLevel(logging.INFO)

_db_init_lock = threading.Lock()
_db_initialized = False
_rate_limit_lock = threading.Lock()
_rate_limit_events = defaultdict(deque)
_login_failures = defaultdict(deque)
_ledger_cache_lock = threading.Lock()
_ledger_context_cache = {"value": "", "expires_at": 0.0}
_ai_health_lock = threading.Lock()
_ai_backend_health = {"unavailable_until": 0.0, "last_error": ""}

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SENSITIVE_AUDIT_KEY_TOKENS = (
    "password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
)
MAX_AUDIT_DEPTH = 4
MAX_SEARCH_LENGTH = 100
DEFAULT_ROLE_USERNAMES = {
    "leader": "leader",
    "general_manager": "general_manager",
    "investment_head": "investment_head",
    "risk_head": "risk_head",
    "staff": "staff",
}
MUTATION_EXEMPT_ENDPOINTS = {
    "api_login", "api_logout", "calculate", "ai_chat",
    "api_create_approval", "api_approve", "api_reject",
}


def request_ip():
    trust_proxy = config_value("LEDGER_TRUST_PROXY_HEADERS", "0").lower() in {"1", "true", "yes"}
    forwarded = request.headers.get("X-Forwarded-For", "") if trust_proxy else ""
    return (forwarded.split(",", 1)[0].strip() if forwarded else request.remote_addr) or "unknown"


def consume_rate_limit(bucket, key, limit, window_seconds):
    now = time.monotonic()
    with _rate_limit_lock:
        events = _rate_limit_events[(bucket, key)]
        while events and now - events[0] >= window_seconds:
            events.popleft()
        if len(events) >= limit:
            return False, max(1, int(window_seconds - (now - events[0])))
        events.append(now)
    return True, 0


def login_lock_status(ip):
    now = time.monotonic()
    with _rate_limit_lock:
        failures = _login_failures[ip]
        while failures and now - failures[0] >= 900:
            failures.popleft()
        if len(failures) >= 5:
            return True, max(1, int(900 - (now - failures[0])))
    return False, 0


def record_login_failure(ip):
    with _rate_limit_lock:
        _login_failures[ip].append(time.monotonic())


def clear_login_failures(ip):
    with _rate_limit_lock:
        _login_failures.pop(ip, None)

# ── 数据库初始化 ──────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn

def ok(**kwargs):
    return jsonify({"success": True, **kwargs})

def fail(message, status=400, code="BAD_REQUEST"):
    return jsonify({"success": False, "error": message, "code": code}), status


class InvalidRecordIdentifier(ValueError):
    pass


@app.before_request
def require_login():
    ensure_db_initialized()
    public_endpoints = {"login_page", "api_login", "static"}
    if request.endpoint in public_endpoints or request.path == "/favicon.ico":
        return None
    if session.get("logged_in"):
        # 会话版本校验：角色密码重置后旧会话自动失效
        role_id = session.get("role_id")
        session_ver = session.get("session_version")
        role_session_valid = False
        if role_id and session_ver is not None:
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT session_version FROM roles WHERE id=? AND is_active=1",
                    (role_id,),
                ).fetchone()
                role_session_valid = bool(
                    row and int(row["session_version"] or 0) == int(session_ver)
                )
            finally:
                conn.close()
        if not role_session_valid:
            session.clear()
            if request.path.startswith("/api/"):
                return fail("会话已过期，请重新登录", 401, "SESSION_EXPIRED")
            return redirect(url_for("login_page"))
        for segment in request.path.split("/"):
            if segment.startswith("__rowid__"):
                rowid = segment[len("__rowid__"):]
                if not rowid.isdigit() or int(rowid) <= 0:
                    return fail("记录标识格式无效", 400, "INVALID_IDENTIFIER")
        if request.method not in SAFE_METHODS:
            sent_token = request.headers.get("X-CSRF-Token", "")
            session_token = session.get("csrf_token", "")
            if not sent_token or not session_token or not secrets.compare_digest(sent_token, session_token):
                return fail("请求安全校验失败，请刷新页面后重试", 403, "CSRF_FAILED")
        permission_error = mutation_permission_error()
        if permission_error:
            return permission_error
        return None
    if request.path.startswith("/api/"):
        return fail("未登录", 401, "UNAUTHORIZED")
    return redirect(url_for("login_page"))


@app.after_request
def secure_and_audit_response(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://*.is.autonavi.com; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'",
    )
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    if request.method not in SAFE_METHODS and response.status_code < 400 and request.endpoint != "api_login":
        if request.endpoint not in {"ai_chat", "calculate", "api_logout"}:
            invalidate_ledger_context_cache()
        try:
            write_audit_log(response.status_code)
        except Exception:
            app.logger.exception("Failed to write audit log for endpoint=%s", request.endpoint)
    return response

@app.errorhandler(Exception)
def handle_error(err):
    status = getattr(err, "code", 500) or 500
    message = str(err) if status < 500 else "服务器内部错误"
    if status >= 500:
        app.logger.exception(
            "Unhandled error endpoint=%s method=%s ip=%s",
            request.endpoint, request.method, request_ip(),
        )
    if request.path.startswith("/api/"):
        return fail(message, status, "SERVER_ERROR" if status >= 500 else "BAD_REQUEST")
    raise err


@app.errorhandler(InvalidRecordIdentifier)
def handle_invalid_record_identifier(_err):
    return fail("记录标识格式无效", 400, "INVALID_IDENTIFIER")


def table_columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

def next_text_id(conn, table, prefix):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    rows = conn.execute(f"SELECT id FROM {table} WHERE id LIKE ? ORDER BY id", (f"{prefix}%",)).fetchall()
    max_num = 0
    for row in rows:
        value = str(row["id"] or "")
        digits = value[len(prefix):]
        if digits.isdigit():
            max_num = max(max_num, int(digits))
    return f"{prefix}{max_num + 1:03d}"

def project_status_for_db(status):
    return {"进行中": "active", "已完成": "completed", "已终止": "terminated", "已结清": "settled", "结清": "settled"}.get(status, status or "active")

def payment_status_for_db(status):
    return {"待付": "pending", "已付": "paid", "逾期": "overdue"}.get(status, status or "pending")

UNPAID_PAYMENT_STATUSES = ("pending", "overdue", "待付", "未付", "逾期", "已逾期")
PAID_PAYMENT_STATUSES = ("paid", "已付", "已收", "completed", "settled")

def status_for_project_db(status):
    return {
        "进行中": "active",
        "已完成": "completed",
        "已终止": "terminated",
        "已结清": "settled",
        "结清": "settled",
    }.get(status, status or "active")

def require_fields(data, fields):
    missing = [field for field in fields if not str(data.get(field, "")).strip()]
    if missing:
        return f"缺少必填字段：{', '.join(missing)}"
    return ""

def infer_region(*values):
    text = " ".join(str(v or "") for v in values)
    rules = [
        ("合肥", ["合肥", "庐阳", "安徽中安", "国元"]),
        ("安庆", ["安庆", "怀宁"]),
        ("宿州", ["宿州", "宿马"]),
        ("宣城", ["宣城", "宁国"]),
        ("上海", ["上海", "浦发银行上海", "工商银行上海", "易鑫租赁"]),
        ("马鞍山", ["马鞍山", "郑蒲港"]),
        ("滁州", ["明光"]),
        ("贵州", ["贵州"]),
        ("安徽省", ["安徽分行", "安徽"]),
    ]
    for region, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return region
    return "其他"

PROJECT_LOCATION_RULES = [
    {"city": "合肥（庐阳区）", "lat": 31.8847, "lng": 117.2648, "keywords": ["庐阳", "佳源地产"]},
    {"city": "安庆", "lat": 30.5435, "lng": 117.0638, "keywords": ["安庆", "怀宁"]},
    {"city": "宿州（宿马园区）", "lat": 33.5523, "lng": 117.1731, "keywords": ["宿马", "宿州"]},
    {"city": "宣城（宁国）", "lat": 30.6336, "lng": 118.9832, "keywords": ["宣城", "宁国"]},
    {"city": "滁州（明光）", "lat": 32.7819, "lng": 117.9894, "keywords": ["明光", "滁州"]},
    {"city": "马鞍山", "lat": 31.6705, "lng": 118.5061, "keywords": ["马鞍山", "郑蒲港"]},
    {"city": "贵州", "lat": 26.6470, "lng": 106.6302, "keywords": ["贵州"]},
    {"city": "上海", "lat": 31.2304, "lng": 121.4737, "keywords": ["上海", "浦发银行上海", "工商银行上海", "易鑫租赁"]},
    {"city": "安徽省本级", "lat": 31.8612, "lng": 117.2857, "keywords": ["安徽中安", "皖信", "国元", "安徽分行", "安徽"]},
]

def infer_project_location(*values):
    text = " ".join(str(v or "") for v in values)
    for rule in PROJECT_LOCATION_RULES:
        if any(keyword in text for keyword in rule["keywords"]):
            return rule["city"], rule["lat"], rule["lng"]
    return "安徽省本级", 31.8612, 117.2857

def infer_funding_source(*values):
    text = " ".join(str(v or "") for v in values)
    if any(keyword in text for keyword in ["银行", "信托", "租赁", "不良", "信用卡", "个贷"]):
        return "贷款资金"
    return "自有资金"

def to_float_or_none(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def infer_industry(customer_industry, *values):
    if customer_industry and str(customer_industry).strip():
        return str(customer_industry).strip()
    text = " ".join(str(v or "") for v in values)
    rules = [
        ("金融服务", ["银行", "信托", "基金", "皖信"]),
        ("租赁与特殊资产", ["租赁", "不良", "批转", "资产项目"]),
        ("城投与园区建设", ["国有资产", "建设投资", "产业园", "新区", "开发区", "控股"]),
        ("房地产", ["地产", "置业"]),
        ("新能源", ["新能源", "光伏"]),
        ("农业", ["农业"]),
        ("制造业", ["机械", "制造"]),
        ("新材料", ["材料"]),
    ]
    for industry, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return industry
    return "其他"

def build_distribution(rows, field_name, infer_func):
    grouped = {}
    for row in rows:
        name = infer_func(row)
        amount = float(row["investment_amount"] or 0)
        item = grouped.setdefault(name, {"cnt": 0, "total": 0})
        item["cnt"] += 1
        item["total"] += amount
    return [
        {field_name: name, "cnt": item["cnt"], "total": round(item["total"], 2)}
        for name, item in sorted(grouped.items(), key=lambda kv: kv[1]["total"], reverse=True)
    ]

def project_identifier_where(identifier):
    text = str(identifier)
    if text.startswith("__rowid__"):
        rowid = text[len("__rowid__"):]
        if not rowid.isdigit() or int(rowid) <= 0:
            raise InvalidRecordIdentifier()
        return "rowid=?", (int(rowid),)
    return "id=?", (identifier,)

def customer_identifier_where(identifier):
    text = str(identifier)
    if text.startswith("__rowid__"):
        rowid = text[len("__rowid__"):]
        if not rowid.isdigit() or int(rowid) <= 0:
            raise InvalidRecordIdentifier()
        return "rowid=?", (int(rowid),)
    return "id=?", (identifier,)

def add_months(dt, months):
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return dt.replace(year=year, month=month, day=min(dt.day, days[month - 1]))

def normalize_rate(rate):
    value = float(rate or 0)
    if value < 0 or value > 1:
        raise ValueError("年利率必须使用小数格式，例如 7.2% 应填写为 0.072")
    return value


def request_decimal_rate(value):
    try:
        return normalize_rate(value)
    except (TypeError, ValueError):
        return None

def parse_iso_date(value):
    if not value:
        return None
    if isinstance(value, datetime.date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.datetime.strptime(text[:10], fmt).date()
        except (TypeError, ValueError):
            pass
    try:
        return datetime.datetime.fromisoformat(text).date()
    except (TypeError, ValueError):
        return None

def parse_amount(value):
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("，", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0

def normalized_payment_status(status, actual_pay_date=None, actual_amount=None, paid_date=None, paid_amount=None):
    if actual_pay_date or paid_date or parse_amount(actual_amount) > 0 or parse_amount(paid_amount) > 0:
        return "paid"
    text = str(status or "").strip().lower()
    if text in {"paid", "done", "confirmed", "settled", "completed", "已付", "已收", "已完成", "已结清", "结清"}:
        return "paid"
    if text in {"overdue", "late", "逾期", "已逾期"}:
        return "overdue"
    if text in {"pending", "unpaid", "todo", "待付", "未付", "待收", "未收", ""}:
        return "pending"
    return "pending"

def reminder_interest_amount(row):
    item = dict(row)
    interest_amount = parse_amount(item.get("interest_amount"))
    principal_amount = parse_amount(item.get("principal_amount"))
    total_amount = parse_amount(item.get("total_amount"))
    if interest_amount > 0:
        return round(interest_amount, 2)
    if total_amount > principal_amount > 0:
        return round(total_amount - principal_amount, 2)
    if principal_amount <= 0 and total_amount > 0:
        return round(total_amount, 2)
    return 0.0

def build_month_interest_reminders(conn, today=None):
    today = today or datetime.date.today()
    month_start = today.replace(day=1)
    next_month = add_months(month_start, 1)
    month_end = next_month - datetime.timedelta(days=1)
    payment_cols = table_columns(conn, "payment_schedules")
    actual_pay_date_expr = "pp.actual_pay_date" if "actual_pay_date" in payment_cols else "NULL"
    actual_amount_expr = "pp.actual_amount" if "actual_amount" in payment_cols else "NULL"
    paid_date_expr = "pp.paid_date" if "paid_date" in payment_cols else "NULL"
    paid_amount_expr = "pp.paid_amount" if "paid_amount" in payment_cols else "NULL"
    rows = conn.execute(f"""SELECT pp.id, pp.project_id, pp.due_date as payment_date,
        COALESCE(pp.total_amount, 0) as total_amount,
        COALESCE(pp.interest_amount, 0) as interest_amount,
        COALESCE(pp.principal_amount, 0) as principal_amount,
        pp.status, {actual_pay_date_expr} as actual_pay_date,
        {actual_amount_expr} as actual_amount, {paid_date_expr} as paid_date,
        {paid_amount_expr} as paid_amount,
        c.short_name as customer_name, c.name as customer_full_name, p.project_name
        FROM payment_schedules pp
        LEFT JOIN projects p ON pp.project_id=p.id
        LEFT JOIN customers c ON p.customer_id=c.id""").fetchall()
    reminders = []
    projects_with_month_interest = set()
    for row in rows:
        item = dict(row)
        due = parse_iso_date(item.get("payment_date"))
        if not due or due < month_start or due > month_end:
            continue
        if reminder_interest_amount(item) > 0:
            projects_with_month_interest.add(item.get("project_id"))
        status = normalized_payment_status(
            item.get("status"),
            item.get("actual_pay_date"),
            item.get("actual_amount"),
            item.get("paid_date"),
            item.get("paid_amount"),
        )
        if status == "paid":
            continue
        reminder_amount = reminder_interest_amount(item)
        if reminder_amount <= 0:
            continue
        days_until = (due - today).days
        item["normalized_status"] = status
        item["days_until"] = days_until
        if days_until < 0:
            item["reminder_status"] = "已逾期"
        elif days_until == 0:
            item["reminder_status"] = "今日到期"
        else:
            item["reminder_status"] = f"{days_until}天后"
        item["reminder_amount"] = reminder_amount
        item["payment_date"] = due.isoformat()
        item["customer_name"] = item.get("customer_name") or item.get("customer_full_name") or ""
        item["source"] = "payment_schedule"
        item["needs_schedule_sync"] = False
        reminders.append(item)
    schedule_audit = []
    project_rows = conn.execute("""SELECT p.id, p.project_name, p.investment_amount, p.annual_rate,
        p.loan_term_months, p.repayment_method, p.start_date, p.end_date,
        c.short_name as customer_name, c.name as customer_full_name
        FROM projects p
        LEFT JOIN customers c ON p.customer_id=c.id
        WHERE COALESCE(p.status, 'active')='active'""").fetchall()
    for project in project_rows:
        project = dict(project)
        project_id = project.get("id")
        method = project.get("repayment_method")
        if not project_id or project_id in projects_with_month_interest or not supports_schedule_backstop(method):
            continue
        expected_rows = project_payment_schedule(
            project_id,
            project.get("investment_amount"),
            project.get("annual_rate"),
            project.get("loan_term_months"),
            project.get("start_date"),
            method,
            project.get("end_date"),
        )
        for expected in expected_rows:
            due = parse_iso_date(expected.get("due_date"))
            if not due or due < month_start or due > month_end:
                continue
            reminder_amount = reminder_interest_amount(expected)
            if reminder_amount <= 0:
                continue
            days_until = (due - today).days
            if days_until < 0:
                reminder_status = "已逾期"
            elif days_until == 0:
                reminder_status = "今日到期"
            else:
                reminder_status = f"{days_until}天后"
            schedule_audit.append({
                "id": f"expected-{project_id}-{due.isoformat()}",
                "project_id": project_id,
                "payment_date": due.isoformat(),
                "total_amount": expected.get("total_amount", 0),
                "interest_amount": expected.get("interest_amount", 0),
                "principal_amount": expected.get("principal_amount", 0),
                "status": "pending",
                "normalized_status": "pending",
                "customer_name": project.get("customer_name") or project.get("customer_full_name") or "",
                "customer_full_name": project.get("customer_full_name") or "",
                "project_name": project.get("project_name") or "",
                "days_until": days_until,
                "reminder_status": reminder_status,
                "reminder_amount": reminder_amount,
                "source": "project_terms",
                "needs_schedule_sync": True,
                "notes": "按项目基础信息推算，本月付息计划表未发现对应记录，请核对并同步付息台账。",
            })
    reminders.sort(key=lambda item: (
        item.get("payment_date") or "9999-12-31",
        item.get("project_id") or "",
        item.get("id") or "",
    ))
    schedule_audit.sort(key=lambda item: (
        item.get("payment_date") or "9999-12-31",
        item.get("project_id") or "",
        item.get("id") or "",
    ))
    return reminders, month_start, month_end, schedule_audit

def is_quarterly_interest_method(method):
    text = str(method or "").strip()
    lower = text.lower()
    return (
        lower in {"quarterly_interest", "interest_first", "quarterly", "interest-only"}
        or "按季" in text
        or "先息后本" in text
    )

def is_equal_installment_method(method):
    text = str(method or "").strip()
    return text == "equal_installment" or "等额本息" in text

def is_equal_principal_method(method):
    text = str(method or "").strip()
    return text == "equal_principal" or "等额本金" in text

def is_bullet_method(method):
    text = str(method or "").strip()
    return text == "bullet" or "一次性" in text

def repayment_method_key(method):
    if is_quarterly_interest_method(method):
        return "quarterly_interest"
    if is_equal_installment_method(method):
        return "equal_installment"
    if is_equal_principal_method(method):
        return "equal_principal"
    if is_bullet_method(method):
        return "bullet"
    return str(method or "").strip()

def supports_schedule_backstop(method):
    return (
        is_quarterly_interest_method(method)
        or is_equal_installment_method(method)
        or is_equal_principal_method(method)
        or is_bullet_method(method)
    )

def schedule_base_values(principal, annual_rate, months, start_date, end_date=None):
    principal = float(principal or 0)
    annual_rate = normalize_rate(annual_rate)
    months = int(months or 0)
    start = parse_iso_date(start_date)
    maturity = parse_iso_date(end_date)
    if not maturity and start and months > 0:
        maturity = add_months(start, months)
    if principal <= 0 or months <= 0 or not start or not maturity or maturity <= start:
        return None
    return principal, annual_rate, months, start, maturity

def interest_by_actual_days(principal, annual_rate, start, end):
    days = max((end - start).days, 0)
    interest = round(principal * annual_rate * days / 360, 2)
    return interest, days

def next_bank_quarter_settlement(current):
    for year in range(current.year, current.year + 3):
        for month in (3, 6, 9, 12):
            due_date = datetime.date(year, month, 20)
            if due_date > current:
                return due_date
    return add_months(current, 3)

def make_payment_row(project_id, period, due_date, principal_amount, interest_amount, notes=""):
    principal_amount = round(principal_amount, 2)
    interest_amount = round(interest_amount, 2)
    return {
        "id": f"{project_id}-{period:03d}",
        "project_id": project_id,
        "period_number": period,
        "due_date": due_date.strftime("%Y-%m-%d"),
        "principal_amount": principal_amount,
        "interest_amount": interest_amount,
        "total_amount": round(principal_amount + interest_amount, 2),
        "notes": notes,
    }

def bank_quarterly_interest_schedule(project_id, principal, annual_rate, months, start_date, end_date=None):
    base = schedule_base_values(principal, annual_rate, months, start_date, end_date)
    if not project_id or not base:
        return []
    principal, annual_rate, _months, start, maturity = base
    rows = []
    period_start = start
    due_date = next_bank_quarter_settlement(period_start)
    period = 1
    while due_date < maturity:
        interest_amount, days = interest_by_actual_days(principal, annual_rate, period_start, due_date)
        rows.append(make_payment_row(
            project_id, period, due_date, 0, interest_amount,
            f"按季结息；计息{days}天，日利率按年利率/360折算"
        ))
        period_start = due_date
        due_date = next_bank_quarter_settlement(period_start)
        period += 1

    interest_amount, days = interest_by_actual_days(principal, annual_rate, period_start, maturity)
    rows.append(make_payment_row(
        project_id, period, maturity, principal, interest_amount,
        f"到期还本；计息{days}天，日利率按年利率/360折算"
    ))
    return rows

def bullet_payment_schedule(project_id, principal, annual_rate, months, start_date, end_date=None):
    base = schedule_base_values(principal, annual_rate, months, start_date, end_date)
    if not project_id or not base:
        return []
    principal, annual_rate, _months, start, maturity = base
    interest_amount, days = interest_by_actual_days(principal, annual_rate, start, maturity)
    return [make_payment_row(
        project_id, 1, maturity, principal, interest_amount,
        f"一次性还本付息；计息{days}天，日利率按年利率/360折算"
    )]

def equal_installment_payment_schedule(project_id, principal, annual_rate, months, start_date, end_date=None):
    base = schedule_base_values(principal, annual_rate, months, start_date, end_date)
    if not project_id or not base:
        return []
    principal, annual_rate, months, start, _maturity = base
    monthly_rate = annual_rate / 12
    if monthly_rate == 0:
        monthly_payment = principal / months
    else:
        monthly_payment = principal * monthly_rate * (1 + monthly_rate) ** months / ((1 + monthly_rate) ** months - 1)
    rows = []
    balance = principal
    for period in range(1, months + 1):
        interest_amount = round(balance * monthly_rate, 2)
        principal_amount = round(monthly_payment - interest_amount, 2)
        if period == months:
            principal_amount = round(balance, 2)
        balance = max(balance - principal_amount, 0)
        rows.append(make_payment_row(project_id, period, add_months(start, period), principal_amount, interest_amount, "等额本息月度计划"))
    return rows

def equal_principal_payment_schedule(project_id, principal, annual_rate, months, start_date, end_date=None):
    base = schedule_base_values(principal, annual_rate, months, start_date, end_date)
    if not project_id or not base:
        return []
    principal, annual_rate, months, start, _maturity = base
    monthly_principal = round(principal / months, 2)
    rows = []
    balance = principal
    for period in range(1, months + 1):
        principal_amount = round(balance, 2) if period == months else monthly_principal
        interest_amount = round(balance * annual_rate / 12, 2)
        balance = max(balance - principal_amount, 0)
        rows.append(make_payment_row(project_id, period, add_months(start, period), principal_amount, interest_amount, "等额本金月度计划"))
    return rows

def legacy_quarterly_payment_schedule(project_id, principal, annual_rate, months, start_date, end_date=None):
    if not project_id or not start_date or float(principal or 0) <= 0 or int(months or 0) <= 0:
        return []
    principal = float(principal or 0)
    annual_rate = normalize_rate(annual_rate)
    months = int(months or 0)
    start = parse_iso_date(start_date)
    if not start:
        return []
    periods = math.ceil(months / 3)
    rows = []
    elapsed = 0
    for period in range(1, periods + 1):
        months_this_period = min(3, months - elapsed)
        elapsed += months_this_period
        due_date = add_months(start, elapsed)
        principal_amount = round(principal, 2) if period == periods else 0.0
        interest_amount = round(principal * annual_rate * months_this_period / 12, 2)
        total_amount = round(principal_amount + interest_amount, 2)
        rows.append({
            "id": f"{project_id}-{period:03d}",
            "project_id": project_id,
            "period_number": period,
            "due_date": due_date.strftime("%Y-%m-%d"),
            "principal_amount": principal_amount,
            "interest_amount": interest_amount,
            "total_amount": total_amount,
            "notes": "按每3个月整期生成",
        })
    return rows

def project_payment_schedule(project_id, principal, annual_rate, months, start_date, repayment_method="", end_date=None):
    if is_quarterly_interest_method(repayment_method):
        return bank_quarterly_interest_schedule(project_id, principal, annual_rate, months, start_date, end_date)
    if is_equal_installment_method(repayment_method):
        return equal_installment_payment_schedule(project_id, principal, annual_rate, months, start_date, end_date)
    if is_equal_principal_method(repayment_method):
        return equal_principal_payment_schedule(project_id, principal, annual_rate, months, start_date, end_date)
    if is_bullet_method(repayment_method):
        return bullet_payment_schedule(project_id, principal, annual_rate, months, start_date, end_date)
    return legacy_quarterly_payment_schedule(project_id, principal, annual_rate, months, start_date, end_date)

def replace_project_payment_schedule(conn, project_id, principal, annual_rate, months, start_date, repayment_method="", end_date=None):
    conn.execute("SAVEPOINT _replace_schedule")
    try:
        conn.execute("DELETE FROM payment_schedules WHERE project_id=?", (project_id,))
        rows = project_payment_schedule(project_id, principal, annual_rate, months, start_date, repayment_method, end_date)
        for row in rows:
            conn.execute("""INSERT INTO payment_schedules
                (id, project_id, period_number, due_date, principal_amount, interest_amount, total_amount, status, notes)
                VALUES (?,?,?,?,?,?,?,'pending',?)""",
                (row["id"], row["project_id"], row["period_number"], row["due_date"],
                 row["principal_amount"], row["interest_amount"], row["total_amount"], row.get("notes", "")))
        conn.execute("RELEASE _replace_schedule")
    except Exception:
        conn.execute("ROLLBACK TO _replace_schedule")
        conn.execute("RELEASE _replace_schedule")
        raise
    return len(rows)

def row_to_dict(row):
    return dict(row) if row else None

def get_role(conn, role_id):
    if not role_id:
        return None
    return conn.execute("SELECT * FROM roles WHERE id=? AND is_active=1", (role_id,)).fetchone()

def current_role(conn=None):
    role_id = session.get("role_id")
    close_conn = False
    if conn is None:
        conn = get_db()
        close_conn = True
    try:
        return get_role(conn, role_id)
    finally:
        if close_conn:
            conn.close()

def require_role(conn=None):
    role = current_role(conn)
    if not role:
        return None, fail("当前登录身份无效，请重新登录", 401, "ROLE_REQUIRED")
    return role, None

def is_leader(role):
    return bool(role and (role["role_key"] == "leader" or int(role["level"] or 99) == 1))


def normalize_username(value):
    return str(value or "").strip().lower()


def valid_username(value):
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,39}", normalize_username(value)))


def ensure_role_usernames(conn):
    rows = conn.execute(
        "SELECT id, role_key, username FROM roles ORDER BY sort_order, level, id"
    ).fetchall()
    used = set()
    changed = False
    for role in rows:
        username = normalize_username(role["username"])
        if username and valid_username(username) and username not in used:
            used.add(username)
            if username != role["username"]:
                conn.execute("UPDATE roles SET username=? WHERE id=?", (username, role["id"]))
                changed = True
            continue

        base = normalize_username(DEFAULT_ROLE_USERNAMES.get(role["role_key"]) or role["role_key"] or role["id"])
        base = re.sub(r"[^a-z0-9._-]+", "_", base).strip("._-")
        if len(base) < 3:
            base = f"user_{re.sub(r'[^a-z0-9]+', '', normalize_username(role['id'])) or 'role'}"
        base = base[:40]
        candidate = base
        suffix = 2
        while candidate in used:
            suffix_text = f"_{suffix}"
            candidate = base[:40 - len(suffix_text)] + suffix_text
            suffix += 1
        conn.execute("UPDATE roles SET username=? WHERE id=?", (candidate, role["id"]))
        used.add(candidate)
        changed = True
    return changed


def mutation_permission_error():
    if request.method in SAFE_METHODS or request.endpoint in MUTATION_EXEMPT_ENDPOINTS:
        return None
    role = current_role()
    if not role:
        return fail("当前登录身份无效，请重新登录", 401, "ROLE_REQUIRED")
    level = int(role["level"] or 99)
    if request.endpoint == "api_roles" and request.method == "PUT" and not is_leader(role):
        return fail("仅分管领导可维护角色配置", 403, "FORBIDDEN")
    if request.method == "DELETE" and level > 2:
        return fail("当前角色无权删除业务数据", 403, "FORBIDDEN")
    if not role["can_submit"]:
        return fail("当前角色无权修改业务数据", 403, "FORBIDDEN")
    return None


def ensure_role_passwords(conn):
    rows = conn.execute(
        "SELECT id, role_key, role_name, person_name, username, password_hash FROM roles WHERE is_active=1"
    ).fetchall()
    generated = []
    for role in rows:
        if role["password_hash"]:
            continue
        env_name = "LEDGER_PASSWORD_" + re.sub(r"[^A-Z0-9]+", "_", str(role["role_key"]).upper())
        initial_password = config_value(env_name, "") or secrets.token_urlsafe(12)
        conn.execute(
            "UPDATE roles SET password_hash=? WHERE id=?",
            (generate_password_hash(initial_password), role["id"]),
        )
        generated.append((role["username"], role["person_name"], role["role_name"], initial_password))
    if generated:
        is_new_file = not os.path.exists(INITIAL_CREDENTIALS_PATH)
        with open(INITIAL_CREDENTIALS_PATH, "a", encoding="utf-8") as fh:
            if is_new_file:
                fh.write("皖信台账系统初始角色密码\n")
                fh.write("请妥善保管，登录验证完成后删除本文件，并通过管理员重置密码。\n\n")
            fh.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n")
            for username, person_name, role_name, password in generated:
                fh.write(f"账号 {username}｜{person_name}（{role_name}）：{password}\n")
            fh.write("\n")
        try:
            os.chmod(INITIAL_CREDENTIALS_PATH, 0o600)
        except OSError:
            pass


def sanitize_audit_value(value, depth=0):
    if depth >= MAX_AUDIT_DEPTH:
        return "<max_depth>"
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_text = str(key)
            if any(token in key_text.lower() for token in SENSITIVE_AUDIT_KEY_TOKENS):
                sanitized[key_text] = "<redacted>"
            else:
                sanitized[key_text] = sanitize_audit_value(item, depth + 1)
        return sanitized
    if isinstance(value, (list, tuple)):
        items = [sanitize_audit_value(item, depth + 1) for item in value[:50]]
        if len(value) > 50:
            items.append("<truncated>")
        return items
    return value


def write_audit_log(status_code):
    if not session.get("logged_in"):
        return
    payload = request.get_json(silent=True)
    if request.endpoint == "ai_chat":
        detail = "<chat content not logged>"
    elif isinstance(payload, dict):
        sanitized = sanitize_audit_value(payload)
        detail = json.dumps(sanitized, ensure_ascii=False)[:2000]
    elif request.files:
        detail = "files=" + ",".join(request.files.keys())
    else:
        detail = ""
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO audit_logs
            (actor_role_id, actor_name, endpoint, method, path, status_code, ip_address, detail)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                session.get("role_id"), session.get("person_name"), request.endpoint,
                request.method, request.path, status_code, request_ip(), detail,
            ),
        )
        conn.commit()
    finally:
        conn.close()

def approval_route_for_submitter(conn, submitter_role):
    roles = [dict(r) for r in conn.execute("""SELECT * FROM roles
        WHERE is_active=1 AND can_approve=1 ORDER BY level, sort_order""").fetchall()]
    by_key = {r["role_key"]: r for r in roles}
    steps = []
    role_key = submitter_role["role_key"]
    if role_key == "leader":
        return []
    if role_key == "general_manager":
        keys = [["leader"]]
    elif role_key == "investment_head":
        keys = [["risk_head"], ["general_manager"], ["leader"]]
    elif role_key == "risk_head":
        keys = [["investment_head"], ["general_manager"], ["leader"]]
    else:
        keys = [["investment_head", "risk_head"], ["general_manager"], ["leader"]]
    for group in keys:
        step_roles = [by_key[k] for k in group if k in by_key and by_key[k]["id"] != submitter_role["id"]]
        if not step_roles:
            app.logger.warning(
                "Approval step omitted submitter_role=%s expected_roles=%s",
                submitter_role["id"], ",".join(group),
            )
            continue
        steps.append({
            "level": min(int(r["level"]) for r in step_roles),
            "roles": [{"id": r["id"], "name": r["role_name"], "person_name": r["person_name"]} for r in step_roles],
            "status": "pending",
            "approved_role_ids": [],
        })
    return steps

def snapshot_for_approval(conn, related_type, related_id):
    if related_type == "payment":
        row = conn.execute("""SELECT ps.*, p.project_name, c.short_name as customer_name
            FROM payment_schedules ps
            LEFT JOIN projects p ON ps.project_id=p.id
            LEFT JOIN customers c ON p.customer_id=c.id
            WHERE ps.id=?""", (related_id,)).fetchone()
    elif related_type == "project":
        row = conn.execute("""SELECT p.*, c.short_name as customer_name
            FROM projects p LEFT JOIN customers c ON p.customer_id=c.id
            WHERE p.id=?""", (related_id,)).fetchone()
    else:
        row = None
    return row_to_dict(row)

def execute_confirm_payment(conn, payment_id, data=None):
    data = data or {}
    payment = conn.execute("SELECT * FROM payment_schedules WHERE id=?", (payment_id,)).fetchone()
    if not payment:
        return False
    actual_date = data.get("actual_pay_date") or data.get("actual_payment_date") or datetime.datetime.now().strftime("%Y-%m-%d")
    actual_amount = data.get("actual_amount")
    if actual_amount in (None, ""):
        actual_amount = payment["total_amount"] if "total_amount" in payment.keys() else payment["amount"]
    cur = conn.execute("""UPDATE payment_schedules SET status='paid', actual_pay_date=?, actual_amount=?,
        notes=COALESCE(notes,'') || ? WHERE id=?""", (actual_date, actual_amount, "", payment_id))
    return cur.rowcount > 0

def execute_settle_project(conn, project_id, data=None):
    data = data or {}
    notes = data.get("remark") or data.get("notes") or "本金撤回/结清"
    cur = conn.execute("""UPDATE projects SET status='settled',
        notes=TRIM(COALESCE(notes,'') || ?),
        updated_at=datetime('now','localtime') WHERE id=?""", ("\n" + notes, project_id))
    return cur.rowcount > 0

def execute_approval_action(conn, approval):
    data = json.loads(approval["new_data"] or "{}")
    if approval["action"] == "confirm_payment":
        return execute_confirm_payment(conn, approval["related_id"], data)
    if approval["action"] == "settle_project":
        return execute_settle_project(conn, approval["related_id"], data)
    return False

def create_approval_record(conn, related_type, related_id, action, new_data=None):
    role, error = require_role(conn)
    if error:
        return None, error
    if not role["can_submit"]:
        return None, fail("当前身份不能提报审批", 403, "FORBIDDEN")
    pending = conn.execute("""SELECT id FROM approvals
        WHERE related_type=? AND related_id=? AND action=? AND status='pending'""",
        (related_type, related_id, action)).fetchone()
    if pending:
        return None, fail("该事项已有审批单在流转中", 409, "APPROVAL_EXISTS")
    route = approval_route_for_submitter(conn, role)
    snapshot = snapshot_for_approval(conn, related_type, related_id)
    if not snapshot:
        return None, fail("审批对象不存在", 404, "NOT_FOUND")
    if not route:
        aid = next_text_id(conn, "approvals", "ap")
        fake = {
            "related_type": related_type,
            "related_id": related_id,
            "action": action,
            "new_data": json.dumps(new_data or {}, ensure_ascii=False),
        }
        ok_exec = execute_approval_action(conn, fake)
        if not ok_exec:
            return None, fail("执行失败", 500, "EXECUTE_FAILED")
        conn.execute("""INSERT INTO approvals
            (id, related_type, related_id, action, submitter_role_id, submitter, submitter_level,
             route, current_level, current_role_ids, completed_role_ids, snapshot, new_data, status, executed)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'approved',1)""",
            (aid, related_type, related_id, action, role["id"], role["person_name"], role["level"],
             "[]", role["level"], "[]", json.dumps([role["id"]], ensure_ascii=False),
             json.dumps(snapshot, ensure_ascii=False), json.dumps(new_data or {}, ensure_ascii=False)))
        conn.execute("""INSERT INTO approval_logs
            (approval_id, reviewer, reviewer_role, reviewer_role_id, action, comment)
            VALUES (?,?,?,?,?,?)""",
            (aid, role["person_name"], role["role_name"], role["id"], "direct_execute", "领导直接执行"))
        return {"id": aid, "direct": True}, None
    aid = next_text_id(conn, "approvals", "ap")
    first = route[0]
    current_role_ids = [r["id"] for r in first["roles"]]
    conn.execute("""INSERT INTO approvals
        (id, related_type, related_id, action, submitter_role_id, submitter, submitter_level,
         route, current_level, current_role_ids, completed_role_ids, snapshot, new_data, status, executed)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
        (aid, related_type, related_id, action, role["id"], role["person_name"], role["level"],
         json.dumps(route, ensure_ascii=False), first["level"], json.dumps(current_role_ids, ensure_ascii=False),
         "[]", json.dumps(snapshot, ensure_ascii=False), json.dumps(new_data or {}, ensure_ascii=False), "pending"))
    return {"id": aid, "direct": False}, None

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("PRAGMA journal_mode = WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS customers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        short_name TEXT,
        industry TEXT,
        contact_person TEXT,
        contact_phone TEXT,
        address TEXT,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        project_name TEXT NOT NULL,
        project_code TEXT UNIQUE,
        customer_id TEXT,
        project_type TEXT DEFAULT '固定资产投资',
        investment_amount REAL DEFAULT 0,
        annual_rate REAL DEFAULT 0,
        loan_term_months INTEGER DEFAULT 0,
        repayment_method TEXT DEFAULT '等额本息',
        start_date TEXT,
        end_date TEXT,
        status TEXT DEFAULT '进行中',
        guarantee_method TEXT,
        description TEXT,
        notes TEXT,
        city TEXT,
        latitude REAL,
        longitude REAL,
        funding_source TEXT DEFAULT '自有资金',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS payment_schedules (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        customer_id TEXT,
        period_number INTEGER DEFAULT 1,
        due_date TEXT NOT NULL,
        principal_amount REAL DEFAULT 0,
        interest_amount REAL DEFAULT 0,
        total_amount REAL DEFAULT 0,
        amount REAL DEFAULT 0,
        type TEXT DEFAULT '付息',
        status TEXT DEFAULT '待付',
        actual_pay_date TEXT,
        actual_amount REAL,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS opportunities (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        customer_id TEXT,
        project_id TEXT,
        stage TEXT DEFAULT '线索',
        amount REAL DEFAULT 0,
        probability INTEGER DEFAULT 10,
        expected_close_date TEXT,
        owner TEXT,
        assigned_to TEXT,
        source TEXT,
        notes TEXT,
        description TEXT,
        sort_order INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL,
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS reminders (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        remind_date TEXT NOT NULL,
        type TEXT DEFAULT '付息提醒',
        remind_type TEXT,
        related_id TEXT,
        related_type TEXT,
        status TEXT DEFAULT '未读',
        priority TEXT DEFAULT '中',
        notes TEXT,
        description TEXT,
        is_done TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS roles (
        id TEXT PRIMARY KEY,
        role_key TEXT,
        username TEXT COLLATE NOCASE,
        role_name TEXT NOT NULL,
        person_name TEXT NOT NULL,
        level INTEGER NOT NULL,
        can_approve INTEGER DEFAULT 0,
        can_submit INTEGER DEFAULT 1,
        is_active INTEGER DEFAULT 1,
        sort_order INTEGER DEFAULT 0,
        password_hash TEXT,
        session_version INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS approvals (
        id TEXT PRIMARY KEY,
        related_type TEXT NOT NULL,
        related_id TEXT NOT NULL,
        action TEXT NOT NULL,
        submitter_role_id TEXT NOT NULL,
        submitter TEXT NOT NULL,
        submitter_level INTEGER NOT NULL,
        route TEXT NOT NULL,
        current_level INTEGER NOT NULL,
        current_role_ids TEXT,
        completed_role_ids TEXT,
        snapshot TEXT,
        new_data TEXT,
        status TEXT DEFAULT 'pending',
        executed INTEGER DEFAULT 0,
        last_reminded_at TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS approval_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        approval_id TEXT NOT NULL,
        reviewer TEXT NOT NULL,
        reviewer_role TEXT NOT NULL,
        reviewer_role_id TEXT,
        action TEXT NOT NULL,
        comment TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_role_id TEXT,
        actor_name TEXT,
        endpoint TEXT,
        method TEXT,
        path TEXT,
        status_code INTEGER,
        ip_address TEXT,
        detail TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS transaction_structures (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        title TEXT NOT NULL,
        description TEXT,
        facts TEXT NOT NULL DEFAULT '{}',
        entities TEXT NOT NULL DEFAULT '[]',
        relations TEXT NOT NULL DEFAULT '[]',
        created_by TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS due_diligence_reports (
        id TEXT PRIMARY KEY,
        project_id TEXT,
        title TEXT NOT NULL,
        file_name TEXT,
        report_date TEXT,
        submitter TEXT,
        department TEXT,
        summary TEXT,
        business_plan TEXT,
        structure_json TEXT NOT NULL DEFAULT '{}',
        full_text TEXT,
        attachment_name TEXT,
        attachment_path TEXT,
        evaluation_note TEXT,
        evaluation_author TEXT,
        evaluation_at TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
    );
    """)
    role_cols = table_columns(conn, "roles")
    username_added = False
    if "username" not in role_cols:
        c.execute("ALTER TABLE roles ADD COLUMN username TEXT")
        username_added = True
    if "password_hash" not in role_cols:
        c.execute("ALTER TABLE roles ADD COLUMN password_hash TEXT")
    if "session_version" not in role_cols:
        c.execute("ALTER TABLE roles ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
    project_cols = table_columns(conn, "projects")
    project_column_defs = {
        "city": "TEXT",
        "latitude": "REAL",
        "longitude": "REAL",
        "funding_source": "TEXT DEFAULT '自有资金'",
        "notes": "TEXT",
    }
    for col, ddl in project_column_defs.items():
        if col not in project_cols:
            c.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
    transaction_cols = table_columns(conn, "transaction_structures")
    if "facts" not in transaction_cols:
        c.execute("ALTER TABLE transaction_structures ADD COLUMN facts TEXT NOT NULL DEFAULT '{}'")
    due_cols = table_columns(conn, "due_diligence_reports")
    due_column_defs = {
        "attachment_name": "TEXT",
        "attachment_path": "TEXT",
        "evaluation_note": "TEXT",
        "evaluation_author": "TEXT",
        "evaluation_at": "TEXT",
    }
    for col, ddl in due_column_defs.items():
        if col not in due_cols:
            c.execute(f"ALTER TABLE due_diligence_reports ADD COLUMN {col} {ddl}")
    ensure_role_usernames(conn)
    if username_added:
        c.execute("UPDATE roles SET session_version=session_version+1")
    c.executescript("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_roles_username_unique ON roles(username COLLATE NOCASE);
    CREATE INDEX IF NOT EXISTS idx_projects_customer_id ON projects(customer_id);
    CREATE INDEX IF NOT EXISTS idx_projects_city ON projects(city);
    CREATE INDEX IF NOT EXISTS idx_payment_schedules_project_id ON payment_schedules(project_id);
    CREATE INDEX IF NOT EXISTS idx_payment_schedules_due_date ON payment_schedules(due_date);
    CREATE INDEX IF NOT EXISTS idx_opportunities_customer_id ON opportunities(customer_id);
    CREATE INDEX IF NOT EXISTS idx_reminders_date ON reminders(remind_date);
    CREATE INDEX IF NOT EXISTS idx_approvals_related ON approvals(related_type, related_id, action, status);
    CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, current_level);
    CREATE INDEX IF NOT EXISTS idx_transaction_structures_project_id ON transaction_structures(project_id);
    CREATE INDEX IF NOT EXISTS idx_due_diligence_reports_project_id ON due_diligence_reports(project_id);
    """)
    if c.execute("SELECT COUNT(*) FROM roles").fetchone()[0] == 0:
        c.executemany("""INSERT INTO roles
            (id, role_key, username, role_name, person_name, level, can_approve, can_submit, is_active, sort_order)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", [
            ("r1", "leader", "leader", "分管领导", "分管领导", 1, 1, 1, 1, 1),
            ("r2", "general_manager", "general_manager", "皖信总经理", "皖信总经理", 2, 1, 1, 1, 2),
            ("r3a", "investment_head", "investment_head", "投资部负责人", "投资部负责人", 3, 1, 1, 1, 3),
            ("r3b", "risk_head", "risk_head", "风险部负责人", "风险部负责人", 3, 1, 1, 1, 4),
            ("r4", "staff", "staff", "职工", "职工", 4, 0, 1, 1, 5),
        ])
        ensure_role_usernames(conn)
    ensure_role_passwords(conn)
    project_rows = c.execute("""SELECT rowid, project_name, project_type, city, latitude, longitude, funding_source
        FROM projects""").fetchall()
    for row in project_rows:
        city, lat, lng = infer_project_location(row["project_name"], row["project_type"], row["city"])
        funding_source = row["funding_source"] or infer_funding_source(row["project_name"], row["project_type"])
        c.execute("""UPDATE projects
            SET city=COALESCE(NULLIF(city,''), ?),
                latitude=COALESCE(latitude, ?),
                longitude=COALESCE(longitude, ?),
                funding_source=COALESCE(NULLIF(funding_source,''), ?)
            WHERE rowid=?""", (city, lat, lng, funding_source, row["rowid"]))
    conn.commit()
    # Seed demo data if empty
    if c.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0:
        seed_demo_data(conn)
    conn.close()


def ensure_db_initialized():
    global _db_initialized
    if _db_initialized:
        return
    with _db_init_lock:
        if not _db_initialized:
            init_db()
            _db_initialized = True


def seed_demo_data(conn):
    c = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d")
    # 客户
    customers = [
        ("安徽科创新能源科技有限公司","科创能源","新能源","张明","13800001111","合肥市高新区"),
        ("芜湖恒达机械制造有限公司","恒达机械","制造业","李强","13900002222","芜湖市经开区"),
        ("蚌埠中盛新材料股份有限公司","中盛新材料","新材料","王芳","13700003333","蚌埠市高新区"),
        ("安庆华信置业有限公司","华信置业","房地产","赵磊","13600004444","安庆市开发区"),
        ("阜阳鼎丰农业科技有限公司","鼎丰农业","农业","刘洋","13500005555","阜阳市颍州区"),
    ]
    for idx, c1 in enumerate(customers, 1):
        c.execute("INSERT INTO customers (id,name,short_name,industry,contact_person,contact_phone,address) VALUES (?,?,?,?,?,?,?)",
                  (f"c{idx:03d}", *c1))
    # 项目
    projects = [
        ("WX-2025-001","科创能源光伏基地项目",1,"固定资产投资",5000000,7.2,36,"等额本息","2025-01-15","2028-01-15","进行中","不动产抵押","光伏生产基地建设"),
        ("WX-2025-002","恒达机械产线升级项目",2,"固定资产投资",3000000,7.5,24,"先息后本","2025-03-01","2027-03-01","进行中","设备抵押","智能化产线改造"),
        ("WX-2025-003","中盛新材料研发中心",3,"固定资产投资",8000000,6.8,48,"等额本息","2025-02-01","2029-02-01","进行中","不动产抵押+保证","研发中心建设"),
        ("WX-2025-004","华信置业商业综合体",4,"固定资产投资",15000000,8.0,60,"等额本金","2024-06-01","2029-06-01","进行中","土地抵押","商业综合体开发"),
        ("WX-2025-005","鼎丰农业冷链仓储",5,"固定资产投资",2000000,7.0,12,"一次性还本付息","2025-06-01","2026-06-01","待启动","仓单质押","冷链仓储设施建设"),
    ]
    for idx, p in enumerate(projects, 1):
        customer_ref = f"c{int(p[2]):03d}" if str(p[2]).isdigit() else p[2]
        c.execute("""INSERT INTO projects
            (id,project_code,project_name,customer_id,project_type,investment_amount,annual_rate,loan_term_months,repayment_method,start_date,end_date,status,guarantee_method,description)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"p{idx:03d}", p[0], p[1], customer_ref, *p[3:]))
    # 付息计划
    payment_schedules = [
        (1,1,"2025-04-15",30000,"付息","已付","2025-04-14",30000,""),
        (1,1,"2025-07-15",30000,"付息","待付","","",""),
        (1,1,"2025-10-15",30000,"付息","待付","","",""),
        (2,2,"2025-06-01",18750,"付息","待付","","",""),
        (2,2,"2025-09-01",18750,"付息","待付","","",""),
        (3,3,"2025-05-01",45333,"付息","待付","","",""),
        (3,3,"2025-08-01",45333,"付息","待付","","",""),
        (4,4,"2025-07-01",100000,"付息","待付","","",""),
        (4,4,"2025-10-01",100000,"付息","待付","","",""),
    ]
    project_periods = {}
    for idx, pp in enumerate(payment_schedules, 1):
        project_ref = f"p{int(pp[0]):03d}" if str(pp[0]).isdigit() else pp[0]
        customer_ref = f"c{int(pp[1]):03d}" if str(pp[1]).isdigit() else pp[1]
        project_periods[project_ref] = project_periods.get(project_ref, 0) + 1
        c.execute("""INSERT INTO payment_schedules
            (id,project_id,customer_id,period_number,due_date,interest_amount,total_amount,amount,type,status,actual_pay_date,actual_amount,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"pay{idx:03d}", project_ref, customer_ref, project_periods[project_ref],
             pp[2], pp[3], pp[3], pp[3], pp[4], payment_status_for_db(pp[5]), pp[6], pp[7], pp[8]))
    # 商机
    opportunities = [
        ("科创能源二期扩建",1,None,"意向",10000000,20,"2025-12-01","业务一部","主动开拓","",0),
        ("恒达机械并购融资",2,None,"需求确认",5000000,40,"2025-09-01","业务二部","客户转介","",1),
        ("中盛新材料设备采购",3,None,"方案报价",4000000,60,"2025-08-15","业务一部","老客户续贷","",0),
        ("华信置业二期地块",4,None,"商务谈判",20000000,75,"2025-10-01","业务三部","招投标","",0),
        ("鼎丰农业种植基地",5,None,"线索",3000000,10,"2026-03-01","业务二部","行业展会","",0),
    ]
    for idx, o in enumerate(opportunities, 1):
        customer_ref = f"c{int(o[1]):03d}" if str(o[1]).isdigit() else o[1]
        project_ref = f"p{int(o[2]):03d}" if str(o[2]).isdigit() else o[2]
        c.execute("""INSERT INTO opportunities
            (id,title,customer_id,project_id,stage,amount,probability,expected_close_date,owner,assigned_to,source,notes,description,sort_order)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"o{idx:03d}", o[0], customer_ref, project_ref, o[3], o[4], o[5], o[6], o[7], o[7], o[8], o[9], o[9], o[10]))
    # 提醒
    reminders = [
        ("恒达机械付息日临近","2025-06-01","付息提醒",4,"payment_plan","未读","高","请提前3天联系客户确认付息"),
        ("科创能源季度付息","2025-07-15","付息提醒",2,"payment_plan","未读","中",""),
        ("中盛新材料付息","2025-05-01","付息提醒",6,"payment_plan","未读","中",""),
        ("华信置业商机跟进","2025-07-01","商机提醒",4,"opportunity","未读","高","地块竞拍临近，请加快方案确认"),
    ]
    for idx, r in enumerate(reminders, 1):
        c.execute("""INSERT INTO reminders
            (id,title,remind_date,type,remind_type,related_id,related_type,status,priority,notes,description)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (f"r{idx:03d}", r[0], r[1], r[2], r[2], r[3], r[4], r[5], r[6], r[7], r[7]))
    conn.commit()


# ── 工作台统计 ────────────────────────────────────────────────
@app.route("/api/dashboard")
@app.route("/api/stats/overview")
def dashboard():
    conn = get_db()
    c = conn.cursor()
    
    # Stats using actual column names
    project_count = c.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    total_investment = c.execute("SELECT COALESCE(SUM(investment_amount),0) FROM projects WHERE status='active'").fetchone()[0]
    unpaid_placeholders = ",".join("?" for _ in UNPAID_PAYMENT_STATUSES)
    pending_payment = c.execute(
        f"SELECT COALESCE(SUM(total_amount),0) FROM payment_schedules WHERE status IN ({unpaid_placeholders})",
        UNPAID_PAYMENT_STATUSES,
    ).fetchone()[0]
    overdue_payment = c.execute(
        f"""SELECT COALESCE(SUM(total_amount),0) FROM payment_schedules
            WHERE status IN ('overdue','逾期','已逾期')
               OR (status IN ('pending','待付','未付')
                   AND date(due_date) < date('now','localtime'))"""
    ).fetchone()[0]
    opportunity_count = c.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    customer_count = c.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    
    # Type distribution
    type_distribution = c.execute("""SELECT p.project_type, COUNT(p.id) as cnt, COALESCE(SUM(p.investment_amount),0) as total 
        FROM projects p WHERE p.status='active' GROUP BY p.project_type""").fetchall()
    type_distribution = [{"project_type": r[0] or "未分类", "cnt": r[1], "total": r[2]} for r in type_distribution]

    active_projects = c.execute("""SELECT p.project_name, p.project_type, p.investment_amount,
        c.name as customer_name, c.short_name as customer_short_name, c.industry, c.address
        FROM projects p
        LEFT JOIN customers c ON p.customer_id=c.id
        WHERE p.status='active'""").fetchall()
    region_distribution = build_distribution(
        active_projects,
        "region",
        lambda r: infer_region(r["address"], r["customer_name"], r["customer_short_name"], r["project_name"])
    )
    industry_distribution = build_distribution(
        active_projects,
        "industry",
        lambda r: infer_industry(r["industry"], r["customer_name"], r["customer_short_name"], r["project_name"], r["project_type"])
    )
    
    # Stage distribution 
    stage_distribution = c.execute("""SELECT stage, COUNT(*) as cnt 
        FROM opportunities GROUP BY stage 
        ORDER BY CASE stage WHEN '线索' THEN 1 WHEN '需求确认' THEN 2 
        WHEN '方案报价' THEN 3 WHEN '商务谈判' THEN 4 WHEN '赢单' THEN 5 WHEN '输单' THEN 6 END""").fetchall()
    stage_distribution = [{"stage": r[0], "cnt": r[1]} for r in stage_distribution]
    
    today = datetime.date.today()
    # Interest reminders are computed in Python so mixed status/date formats do not silently drop rows.
    month_interest_reminders, month_start, month_end, interest_reminder_audit = build_month_interest_reminders(conn, today)
    this_month_due_amount = sum(float(item.get("reminder_amount") or 0) for item in month_interest_reminders)
    this_month_due_count = len(month_interest_reminders)

    # Upcoming payments in the next 30 days, kept for broader dashboard context.
    upcoming_end = today + datetime.timedelta(days=30)
    upcoming_payments = c.execute("""SELECT pp.id, pp.project_id, pp.due_date as payment_date, pp.total_amount,
        pp.status, c.short_name as customer_name, p.project_name
        FROM payment_schedules pp 
        LEFT JOIN projects p ON pp.project_id=p.id
        LEFT JOIN customers c ON p.customer_id=c.id 
        WHERE pp.status='pending'
          AND date(pp.due_date) BETWEEN date(?) AND date(?)
        ORDER BY date(pp.due_date), pp.project_id, COALESCE(pp.period_number, 999999)
        LIMIT 8""", (today.isoformat(), upcoming_end.isoformat())).fetchall()
    upcoming_payments = [dict(r) for r in upcoming_payments]
    
    # Monthly trend
    monthly_payments = c.execute("""SELECT strftime('%Y-%m', due_date) as month, 
        SUM(total_amount) as total, 
        SUM(CASE WHEN status='paid' THEN total_amount ELSE 0 END) as paid 
        FROM payment_schedules GROUP BY month ORDER BY month""").fetchall()
    
    # Industry breakdown
    by_industry = c.execute("""SELECT c.industry, COUNT(p.id) as cnt, COALESCE(SUM(p.investment_amount),0) as total 
        FROM projects p LEFT JOIN customers c ON p.customer_id=c.id WHERE p.status='active' GROUP BY c.industry""").fetchall()
    by_repay = c.execute("""SELECT repayment_method, COUNT(*) as cnt, COALESCE(SUM(investment_amount),0) as total 
        FROM projects WHERE status='active' GROUP BY repayment_method""").fetchall()
    
    # Funnel
    funnel = c.execute("""SELECT stage, COUNT(*) as cnt, COALESCE(SUM(amount),0) as total 
        FROM opportunities GROUP BY stage 
        ORDER BY CASE stage WHEN '线索' THEN 1 WHEN '意向' THEN 2 WHEN '需求确认' THEN 3 
        WHEN '方案报价' THEN 4 WHEN '商务谈判' THEN 5 WHEN '赢单' THEN 6 END""").fetchall()
    
    unpaid_amount = pending_payment
    unpaid_count = c.execute(
        f"SELECT COUNT(*) FROM payment_schedules WHERE status IN ({unpaid_placeholders})",
        UNPAID_PAYMENT_STATUSES,
    ).fetchone()[0]
    unread_reminders = c.execute("SELECT COUNT(*) FROM reminders WHERE status='未读'").fetchone()[0]
    opp_count = opportunity_count
    
    conn.close()
    return jsonify({
        "project_count": project_count,
        "total_investment": total_investment,
        "pending_payment": pending_payment,
        "overdue_payment": overdue_payment,
        "opportunity_count": opportunity_count,
        "customer_count": customer_count,
        "type_distribution": type_distribution,
        "region_distribution": region_distribution,
        "industry_distribution": industry_distribution,
        "stage_distribution": stage_distribution,
        "month_interest_reminders": month_interest_reminders,
        "interest_reminder_audit": interest_reminder_audit,
        "interest_reminder_audit_count": len(interest_reminder_audit),
        "this_month_interest_window": {"start": month_start.isoformat(), "end": month_end.isoformat()},
        "this_month_due_amount": this_month_due_amount,
        "this_month_due_count": this_month_due_count,
        "upcoming_payments": upcoming_payments,
        "upcoming_window": {"start": today.isoformat(), "end": upcoming_end.isoformat()},
        "monthly_payments": [dict(r) for r in monthly_payments],
        "by_industry": [dict(r) for r in by_industry],
        "by_repay": [dict(r) for r in by_repay],
        "funnel": [dict(r) for r in funnel],
        "unpaid_amount": unpaid_amount,
        "unpaid_count": unpaid_count,
        "unread_reminders": unread_reminders,
        "opp_count": opp_count
    })
@app.route("/api/calculate", methods=["POST"])
@app.route("/api/calc/repayment", methods=["POST"])
def calculate():
    data = request.json or {}
    try:
        principal = float(data.get("principal", 0))
        annual_rate_percent = float(data.get("annual_rate", 7.2))
        months = int(data.get("months", 12))
    except (TypeError, ValueError):
        return fail("计算参数格式错误", 400, "INVALID_CALCULATION_INPUT")
    if principal <= 0:
        return fail("本金必须大于 0", 400, "INVALID_PRINCIPAL")
    if annual_rate_percent < 0 or annual_rate_percent > 100:
        return fail("年利率必须在 0% 到 100% 之间", 400, "INVALID_RATE")
    if months < 1 or months > 600:
        return fail("期限必须在 1 到 600 个月之间", 400, "INVALID_TERM")
    annual_rate = annual_rate_percent / 100
    method = data.get("method", "equal_installment")
    
    monthly_rate = annual_rate / 12
    schedule = []
    total_interest = 0
    total_payment = 0
    
    if is_equal_installment_method(method):
        if monthly_rate == 0:
            mp = principal / months
        else:
            mp = principal * monthly_rate * (1 + monthly_rate)**months / ((1 + monthly_rate)**months - 1)
        balance = principal
        for i in range(1, months + 1):
            interest = balance * monthly_rate
            principal_part = mp - interest
            balance -= principal_part
            if balance < 0: balance = 0
            total_interest += interest
            total_payment += mp
            schedule.append({"month": i, "payment": round(mp, 2), "principal": round(principal_part, 2),
                           "interest": round(interest, 2), "balance": round(balance, 2)})
    
    elif is_equal_principal_method(method):
        monthly_principal = principal / months
        balance = principal
        for i in range(1, months + 1):
            interest = balance * monthly_rate
            payment = monthly_principal + interest
            balance -= monthly_principal
            if balance < 0: balance = 0
            total_interest += interest
            total_payment += payment
            schedule.append({"month": i, "payment": round(payment, 2), "principal": round(monthly_principal, 2),
                           "interest": round(interest, 2), "balance": round(balance, 2)})
    
    elif is_quarterly_interest_method(method):
        elapsed = 0
        period = 1
        while elapsed < months:
            months_this_period = min(3, months - elapsed)
            elapsed += months_this_period
            interest = principal * annual_rate * months_this_period / 12
            principal_part = principal if elapsed >= months else 0
            payment = principal_part + interest
            total_interest += interest
            total_payment += payment
            schedule.append({"month": period, "payment": round(payment, 2), "principal": round(principal_part, 2),
                           "interest": round(interest, 2), "balance": 0 if principal_part else round(principal, 2)})
            period += 1
    
    elif is_bullet_method(method):
        total = principal * (1 + annual_rate * months / 12)
        total_interest = total - principal
        total_payment = total
        schedule.append({"month": months, "payment": round(total, 2), "principal": round(principal, 2),
                        "interest": round(total_interest, 2), "balance": 0})
    method_names = {
        "quarterly_interest": "按季付息，到期还本",
        "interest_first": "按季付息，到期还本",
        "equal_installment": "等额本息",
        "equal_principal": "等额本金",
        "bullet": "一次性还本付息",
    }
    for item in schedule:
        item["remaining"] = item.get("balance", 0)

    return jsonify({
        "method": method_names.get(method, method),
        "principal": principal,
        "annual_rate": annual_rate_percent,
        "months": months,
        "monthly_payment": schedule[0]["payment"] if schedule else 0,
        "total_payment": round(total_payment, 2),
        "total_interest": round(total_interest, 2),
        "interest_ratio": round(total_interest / principal * 100, 2) if principal else 0,
        "schedule": schedule
    })


# ── 客户 CRUD ─────────────────────────────────────────────────
@app.route("/api/customers", methods=["GET"])
def list_customers():
    conn = get_db()
    rows = conn.execute("""SELECT *,
        CASE WHEN id IS NULL OR id='' THEN '__rowid__' || rowid ELSE id END as record_id
        FROM customers ORDER BY id DESC""").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/customers", methods=["POST"])
def create_customer():
    data = request.json or {}
    missing = require_fields(data, ["name"])
    if missing:
        return fail(missing)
    conn = get_db()
    cid = next_text_id(conn, "customers", "c")
    conn.execute("""INSERT INTO customers (id,name,short_name,industry,contact_person,contact_phone,address,notes) 
        VALUES (?,?,?,?,?,?,?,?)""",
        (cid, data["name"], data.get("short_name",""), data.get("industry",""), data.get("contact_person",""),
         data.get("contact_phone",""), data.get("address",""), data.get("notes","")))
    conn.commit()
    conn.close()
    return ok(id=cid, msg="创建成功")

@app.route("/api/customers/<string:cid>", methods=["GET"])
def get_customer(cid):
    conn = get_db()
    where_sql, params = customer_identifier_where(cid)
    customer = conn.execute(f"""SELECT *,
        CASE WHEN id IS NULL OR id='' THEN '__rowid__' || rowid ELSE id END as record_id
        FROM customers WHERE {where_sql}""", params).fetchone()
    customer_id = customer["id"] if customer else cid
    projects = conn.execute("SELECT * FROM projects WHERE customer_id=?", (customer_id,)).fetchall()
    payments = conn.execute("""SELECT pp.*, pp.due_date as payment_date, p.project_name FROM payment_schedules pp 
        LEFT JOIN projects p ON pp.project_id=p.id WHERE p.customer_id=?
        ORDER BY pp.project_id, COALESCE(pp.period_number, 999999), pp.due_date""", (customer_id,)).fetchall()
    opps = conn.execute("SELECT * FROM opportunities WHERE customer_id=?", (customer_id,)).fetchall()
    conn.close()
    return jsonify({
        "customer": dict(customer) if customer else None,
        "projects": [dict(r) for r in projects],
        "payments": [dict(r) for r in payments],
        "opportunities": [dict(r) for r in opps],
    })

@app.route("/api/customers/<string:cid>", methods=["PUT"])
def update_customer(cid):
    data = request.json or {}
    missing = require_fields(data, ["name"])
    if missing:
        return fail(missing)
    conn = get_db()
    where_sql, id_params = customer_identifier_where(cid)
    cur = conn.execute(f"""UPDATE customers SET name=?,short_name=?,industry=?,contact_person=?,contact_phone=?,
        address=?,notes=?,updated_at=datetime('now','localtime') WHERE {where_sql}""",
        (data["name"], data.get("short_name",""), data.get("industry",""), data.get("contact_person",""),
         data.get("contact_phone",""), data.get("address",""), data.get("notes",""), *id_params))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return fail("客户不存在", 404, "NOT_FOUND")
    return ok(msg="更新成功")

@app.route("/api/customers/<string:cid>", methods=["DELETE"])
def delete_customer(cid):
    conn = get_db()
    try:
        where_sql, id_params = customer_identifier_where(cid)
        customer = conn.execute(f"SELECT id FROM customers WHERE {where_sql}", id_params).fetchone()
        if not customer:
            return fail("客户不存在", 404, "NOT_FOUND")
        customer_id = customer["id"]
        if customer_id is None:
            with conn:
                deleted_customer = conn.execute(f"DELETE FROM customers WHERE {where_sql}", id_params).rowcount
            return ok(msg="删除成功", deleted={
                "customers": deleted_customer,
                "projects": 0,
                "payments": 0,
                "opportunities": 0,
                "reminders": 0,
            })
        project_ids = [row["id"] for row in conn.execute("SELECT id FROM projects WHERE customer_id=?", (customer_id,)).fetchall()]
        with conn:
            deleted_payments = 0
            deleted_projects = 0
            for pid in project_ids:
                deleted_payments += conn.execute("DELETE FROM payment_schedules WHERE project_id=?", (pid,)).rowcount
            deleted_projects += conn.execute("DELETE FROM projects WHERE customer_id=?", (customer_id,)).rowcount
            opp_cols = table_columns(conn, "opportunities")
            deleted_opps = conn.execute("DELETE FROM opportunities WHERE customer_id=?", (customer_id,)).rowcount if "customer_id" in opp_cols else 0
            rem_cols = table_columns(conn, "reminders")
            deleted_reminders = 0
            if "related_customer_id" in rem_cols:
                deleted_reminders += conn.execute("DELETE FROM reminders WHERE related_customer_id=?", (customer_id,)).rowcount
            if "related_type" in rem_cols and "related_id" in rem_cols:
                deleted_reminders += conn.execute("DELETE FROM reminders WHERE related_type='customer' AND related_id=?", (customer_id,)).rowcount
            deleted_customer = conn.execute(f"DELETE FROM customers WHERE {where_sql}", id_params).rowcount
        if deleted_customer == 0:
            return fail("客户不存在", 404, "NOT_FOUND")
        return ok(msg="删除成功", deleted={
            "customers": deleted_customer,
            "projects": deleted_projects,
            "payments": deleted_payments,
            "opportunities": deleted_opps,
            "reminders": deleted_reminders,
        })
    finally:
        conn.close()


# ── 项目 CRUD ─────────────────────────────────────────────────
@app.route("/api/customers/<string:cid>/detail", methods=["GET"])
def get_customer_detail(cid):
    """Return customer with its projects and payment schedules"""
    conn = get_db()
    c = conn.cursor()
    where_sql, params = customer_identifier_where(cid)
    customer = c.execute(f"""SELECT *,
        CASE WHEN id IS NULL OR id='' THEN '__rowid__' || rowid ELSE id END as record_id
        FROM customers WHERE {where_sql}""", params).fetchone()
    if not customer:
        return jsonify({"error": "客户不存在"}), 404
    customer_id = customer["id"] if customer else cid
    projects = c.execute("SELECT * FROM projects WHERE customer_id=?", (customer_id,)).fetchall()
    payments = c.execute("""SELECT pp.*, pp.due_date as payment_date, p.project_name FROM payment_schedules pp 
        LEFT JOIN projects p ON pp.project_id=p.id 
        WHERE p.customer_id=? ORDER BY pp.project_id, COALESCE(pp.period_number, 999999), pp.due_date""", (customer_id,)).fetchall()
    opps = c.execute("SELECT * FROM opportunities WHERE customer_id=?", (customer_id,)).fetchall()
    conn.close()
    return jsonify({
        "customer": dict(customer),
        "projects": [dict(p) for p in projects],
        "payments": [dict(pp) for pp in payments],
        "opportunities": [dict(o) for o in opps]
    })
@app.route("/api/projects", methods=["GET"])
def list_projects():
    conn = get_db()
    rows = conn.execute("""SELECT p.*,
        CASE WHEN p.id IS NULL OR p.id='' THEN '__rowid__' || p.rowid ELSE p.id END as record_id,
        p.loan_term_months as investment_period,
        c.short_name as customer_name, c.short_name as customer_short_name FROM projects p 
        LEFT JOIN customers c ON p.customer_id=c.id ORDER BY p.id DESC""").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/projects/map", methods=["GET"])
def project_map_data():
    conn = get_db()
    rows = conn.execute("""SELECT p.*,
        CASE WHEN p.id IS NULL OR p.id='' THEN '__rowid__' || p.rowid ELSE p.id END as record_id,
        c.name as customer_full_name, c.short_name as customer_name
        FROM projects p
        LEFT JOIN customers c ON p.customer_id=c.id
        WHERE COALESCE(p.status, 'active')='active'
        ORDER BY COALESCE(p.city, ''), p.investment_amount DESC""").fetchall()
    conn.close()

    projects = []
    cities = {}
    for row in rows:
        inferred_city, inferred_lat, inferred_lng = infer_project_location(
            row["project_name"], row["project_type"], row["city"]
        )
        city = row["city"] or inferred_city
        latitude = row["latitude"] if row["latitude"] is not None else inferred_lat
        longitude = row["longitude"] if row["longitude"] is not None else inferred_lng
        funding_source = row["funding_source"] or infer_funding_source(row["project_name"], row["project_type"])
        amount = float(row["investment_amount"] or 0)
        project = {
            "id": row["record_id"],
            "project_id": row["id"],
            "project_name": row["project_name"],
            "project_code": row["project_code"],
            "customer_name": row["customer_name"] or row["customer_full_name"] or "",
            "project_type": row["project_type"],
            "investment_amount": amount,
            "annual_rate": row["annual_rate"],
            "loan_term_months": row["loan_term_months"],
            "repayment_method": row["repayment_method"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "status": row["status"],
            "guarantee_method": row["guarantee_method"],
            "description": row["description"],
            "city": city,
            "latitude": latitude,
            "longitude": longitude,
            "funding_source": funding_source,
        }
        projects.append(project)
        city_item = cities.setdefault(city, {
            "city": city,
            "latitude": latitude,
            "longitude": longitude,
            "cnt": 0,
            "total": 0,
            "funding_sources": {},
            "projects": [],
        })
        city_item["cnt"] += 1
        city_item["total"] += amount
        city_item["funding_sources"][funding_source] = city_item["funding_sources"].get(funding_source, 0) + amount
        city_item["projects"].append(project)

    city_list = []
    for city_item in cities.values():
        funding_sources = city_item.pop("funding_sources")
        dominant = sorted(funding_sources.items(), key=lambda kv: kv[1], reverse=True)[0][0] if funding_sources else "自有资金"
        city_item["dominant_funding_source"] = dominant
        city_item["total"] = round(city_item["total"], 2)
        city_list.append(city_item)
    city_list.sort(key=lambda item: item["total"], reverse=True)

    return jsonify({"projects": projects, "cities": city_list})

@app.route("/api/projects", methods=["POST"])
def create_project():
    data = request.json or {}
    missing = require_fields(data, ["project_name", "customer_id"])
    if missing:
        return fail(missing)
    annual_rate = request_decimal_rate(data.get("annual_rate", 0))
    if annual_rate is None:
        return fail("年利率格式错误，接口应传小数值，例如 7.2% 传 0.072", 400, "INVALID_RATE")
    conn = get_db()
    try:
        role, error = require_role(conn)
        if error:
            return error
        requested_status = project_status_for_db(data.get("status", "active"))
        if requested_status == "settled" and not is_leader(role):
            return fail("本金撤回或结清必须通过审批流程", 403, "APPROVAL_REQUIRED")
        pid = next_text_id(conn, "projects", "p")
        city, default_lat, default_lng = infer_project_location(data.get("project_name"), data.get("project_type"))
        latitude = to_float_or_none(data.get("latitude"))
        longitude = to_float_or_none(data.get("longitude"))
        funding_source = data.get("funding_source") or infer_funding_source(data.get("project_name"), data.get("project_type"))
        conn.execute("""INSERT INTO projects (id,project_name,project_code,customer_id,project_type,investment_amount,
            annual_rate,loan_term_months,repayment_method,start_date,end_date,status,guarantee_method,description,notes,
            city,latitude,longitude,funding_source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, data["project_name"], data.get("project_code") or None, data.get("customer_id"), data.get("project_type","固定资产投资"),
             data.get("investment_amount",0), annual_rate, data.get("loan_term_months",0),
             data.get("repayment_method","等额本息"), data.get("start_date",""), data.get("end_date",""),
             project_status_for_db(data.get("status","active")), data.get("guarantee_method",""), data.get("description",""), data.get("notes",""),
             data.get("city") or city, latitude if latitude is not None else default_lat, longitude if longitude is not None else default_lng, funding_source))
        schedule_count = replace_project_payment_schedule(
            conn, pid, data.get("investment_amount", 0), annual_rate,
            data.get("loan_term_months", 0), data.get("start_date", ""),
            data.get("repayment_method", ""), data.get("end_date", "")
        )
        conn.commit()
        return ok(id=pid, msg="创建成功", payment_schedule_count=schedule_count)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

@app.route("/api/projects/<string:pid>", methods=["GET"])
def get_project(pid):
    conn = get_db()
    where_sql, params = project_identifier_where(pid)
    project = conn.execute(f"""SELECT p.*,
        CASE WHEN p.id IS NULL OR p.id='' THEN '__rowid__' || p.rowid ELSE p.id END as record_id,
        p.loan_term_months as investment_period, c.short_name as customer_name FROM projects p 
        LEFT JOIN customers c ON p.customer_id=c.id WHERE p.{where_sql}""", params).fetchone()
    project_id = project["id"] if project else pid
    payments = conn.execute("""SELECT *, due_date as payment_date FROM payment_schedules
        WHERE project_id=? ORDER BY COALESCE(period_number, 999999), due_date""", (project_id,)).fetchall()
    conn.close()
    return jsonify({"project": dict(project) if project else None, "payments": [dict(r) for r in payments]})

@app.route("/api/projects/<string:pid>", methods=["PUT"])
def update_project(pid):
    data = request.json or {}
    missing = require_fields(data, ["project_name", "customer_id"])
    if missing:
        return fail(missing)
    annual_rate = request_decimal_rate(data.get("annual_rate", 0))
    if annual_rate is None:
        return fail("年利率格式错误，接口应传小数值，例如 7.2% 传 0.072", 400, "INVALID_RATE")
    conn = get_db()
    try:
        where_sql, id_params = project_identifier_where(pid)
        existing = conn.execute(f"SELECT * FROM projects WHERE {where_sql}", id_params).fetchone()
        if not existing:
            return fail("项目不存在", 404, "NOT_FOUND")
        existing_status = project_status_for_db(existing["status"]) if existing else ""
        requested_status = project_status_for_db(data.get("status", existing_status))
        if existing_status == "settled" and requested_status != "settled":
            return fail("已结清项目不可恢复为其他状态", 409, "SETTLED_IMMUTABLE")
        if existing and requested_status == "settled" and existing_status != "settled":
            return fail('请通过"本金撤回"操作发起结清审批', 409, "APPROVAL_REQUIRED")

        schedule_values = {
            "investment_amount": float(data.get("investment_amount", existing["investment_amount"]) or 0),
            "annual_rate": annual_rate,
            "loan_term_months": int(data.get("loan_term_months", existing["loan_term_months"]) or 0),
            "start_date": str(data.get("start_date", existing["start_date"]) or ""),
            "end_date": str(data.get("end_date", existing["end_date"]) or ""),
            "repayment_method": str(data.get("repayment_method", existing["repayment_method"]) or ""),
        }
        schedule_changed = (
            abs(schedule_values["investment_amount"] - float(existing["investment_amount"] or 0)) > 0.005
            or abs(schedule_values["annual_rate"] - float(existing["annual_rate"] or 0)) > 0.0000001
            or schedule_values["loan_term_months"] != int(existing["loan_term_months"] or 0)
            or schedule_values["start_date"] != str(existing["start_date"] or "")
            or schedule_values["end_date"] != str(existing["end_date"] or "")
            or repayment_method_key(schedule_values["repayment_method"]) != repayment_method_key(existing["repayment_method"])
        )
        project_id = existing["id"]
        if schedule_changed and project_id:
            paid_placeholders = ",".join("?" for _ in PAID_PAYMENT_STATUSES)
            paid_count = conn.execute(
                f"""SELECT COUNT(*) FROM payment_schedules
                    WHERE project_id=? AND status IN ({paid_placeholders})""",
                (project_id, *PAID_PAYMENT_STATUSES),
            ).fetchone()[0]
            if paid_count:
                return fail(
                    "该项目已有收款记录，不能直接修改金额、利率、期限、日期或还款方式",
                    409,
                    "PAYMENT_HISTORY_LOCKED",
                )
        city, default_lat, default_lng = infer_project_location(data.get("project_name"), data.get("project_type"), data.get("city"))
        latitude = to_float_or_none(data.get("latitude"))
        longitude = to_float_or_none(data.get("longitude"))
        funding_source = data.get("funding_source") or infer_funding_source(data.get("project_name"), data.get("project_type"))
        project_code = data.get("project_code", existing["project_code"])
        project_code = str(project_code).strip() if project_code not in (None, "") else None
        cur = conn.execute(f"""UPDATE projects SET project_name=?,project_code=?,customer_id=?,project_type=?,
            investment_amount=?,annual_rate=?,loan_term_months=?,repayment_method=?,start_date=?,end_date=?,
            status=?,guarantee_method=?,description=?,notes=?,city=?,latitude=?,longitude=?,funding_source=?,
            updated_at=datetime('now','localtime') WHERE {where_sql}""",
            (data["project_name"], project_code, data.get("customer_id"), data.get("project_type",""),
             schedule_values["investment_amount"], annual_rate, schedule_values["loan_term_months"],
             schedule_values["repayment_method"], schedule_values["start_date"], schedule_values["end_date"],
             requested_status, data.get("guarantee_method",""), data.get("description",""), data.get("notes",""),
             data.get("city") or city, latitude if latitude is not None else default_lat, longitude if longitude is not None else default_lng,
             funding_source, *id_params))
        if cur.rowcount == 0:
            return fail("项目不存在", 404, "NOT_FOUND")
        schedule_count = 0
        if schedule_changed and project_id:
            schedule_count = replace_project_payment_schedule(
                conn, project_id, schedule_values["investment_amount"], annual_rate,
                schedule_values["loan_term_months"], schedule_values["start_date"],
                schedule_values["repayment_method"], schedule_values["end_date"]
            )
        conn.commit()
        return ok(msg="更新成功", payment_schedule_count=schedule_count)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

@app.route("/api/projects/<string:pid>", methods=["DELETE"])
def delete_project(pid):
    conn = get_db()
    try:
        where_sql, id_params = project_identifier_where(pid)
        project = conn.execute(f"SELECT id FROM projects WHERE {where_sql}", id_params).fetchone()
        if not project:
            return fail("项目不存在", 404, "NOT_FOUND")
        project_id = project["id"]
        with conn:
            deleted_payments = conn.execute("DELETE FROM payment_schedules WHERE project_id=?", (project_id,)).rowcount if project_id else 0
            opp_cols = table_columns(conn, "opportunities")
            deleted_opps = conn.execute("DELETE FROM opportunities WHERE project_id=?", (project_id,)).rowcount if project_id and "project_id" in opp_cols else 0
            rem_cols = table_columns(conn, "reminders")
            deleted_reminders = 0
            if "related_project_id" in rem_cols:
                deleted_reminders += conn.execute("DELETE FROM reminders WHERE related_project_id=?", (project_id or pid,)).rowcount
            if "related_type" in rem_cols and "related_id" in rem_cols:
                deleted_reminders += conn.execute("DELETE FROM reminders WHERE related_type='project' AND related_id=?", (project_id or pid,)).rowcount
            deleted_project = conn.execute(f"DELETE FROM projects WHERE {where_sql}", id_params).rowcount
        return ok(msg="删除成功", deleted={
            "projects": deleted_project,
            "payments": deleted_payments,
            "opportunities": deleted_opps,
            "reminders": deleted_reminders,
        })
    finally:
        conn.close()


@app.route("/api/projects/<string:pid>/settle", methods=["POST"])
def settle_project(pid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        where_sql, id_params = project_identifier_where(pid)
        project = conn.execute(f"SELECT id FROM projects WHERE {where_sql}", id_params).fetchone()
        if not project:
            return fail("项目不存在", 404, "NOT_FOUND")
        project_id = project["id"]
        role, error = require_role(conn)
        if error:
            return error
        result, error = create_approval_record(conn, "project", project_id, "settle_project", data)
        if error:
            conn.rollback()
            return error
        conn.commit()
        if result.get("direct"):
            return ok(msg="项目已结清", executed=True, approval_id=result["id"])
        return ok(msg="已提交本金撤回审批", approval_id=result["id"], pending_approval=True)
    finally:
        conn.close()


# ── 交易结构图 ─────────────────────────────────────────────────
def normalize_json_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []

def resolve_project_id(conn, project_id):
    if not project_id:
        return ""
    where_sql, params = project_identifier_where(project_id)
    row = conn.execute(f"SELECT id FROM projects WHERE {where_sql}", params).fetchone()
    return row["id"] if row else ""

def transaction_structure_to_dict(row):
    item = dict(row)
    item["facts"] = normalize_json_object(item.get("facts"))
    item["entities"] = normalize_json_list(item.get("entities"))
    item["relations"] = normalize_json_list(item.get("relations"))
    return item

def normalize_json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}

STRUCTURE_ENTITY_TYPES = {"party", "asset", "guarantee", "special"}
STRUCTURE_RELATION_TYPES = {"transfer", "payment", "guarantee", "control"}
STRUCTURE_LANES = {"source", "asset", "transfer", "repayment", "guarantee", "other"}


def _clean_structure_name(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip(" ：:，,。；;、")
    text = re.sub(r"^(为|由|以|向|对|和|及)", "", text).strip(" ：:，,。；;、")
    text = re.sub(r"(担任|作为|提供服务|负责服务|负责管理)$", "", text).strip(" ：:，,。；;、")
    return text[:40]


def _split_structure_names(value):
    text = str(value or "").strip()
    text = re.sub(r"(等|作为.*|负责.*|承担.*)$", "", text).strip()
    parts = re.split(r"[、,，;/；]|(?:\s+和\s+)|(?:\s+及\s+)|(?:以及)", text)
    names = [_clean_structure_name(p) for p in parts]
    return [n for n in names if len(n) >= 2]


def _extract_structure_facts(text):
    facts = {"mode": "", "amount": "", "yield": "", "term": ""}
    compact = re.sub(r"\s+", "", text or "")
    mode_rules = [
        ("应收账款收益权转让", ["应收账款", "收益权"]),
        ("保理/应收账款转让", ["保理"]),
        ("信托贷款", ["信托", "贷款"]),
        ("融资租赁收益权", ["融资租赁", "收益权"]),
        ("有限合伙基金投资", ["有限合伙", "基金"]),
        ("股权投资", ["股权投资", "增资"]),
    ]
    for mode, words in mode_rules:
        if any(word in compact for word in words):
            facts["mode"] = mode
            break
    amount_match = re.search(r"(?:规模|金额|投资|本金|价款|转让价款|融资)[^，。；;\n]{0,12}?([0-9,.]+(?:亿元|万元|亿|万|元))", compact)
    if amount_match:
        facts["amount"] = amount_match.group(1)
    yield_match = re.search(r"(?:收益率|综合收益|利率|年化|收益)[^，。；;\n]{0,10}?([0-9]+(?:\.[0-9]+)?%[^，。；;\n]{0,8})", compact)
    if yield_match:
        facts["yield"] = yield_match.group(1)
    term_match = re.search(r"(?:期限|期间|存续期|业务期限)[^，。；;\n]{0,10}?([0-9]+(?:个月|月|年))", compact)
    if term_match:
        facts["term"] = term_match.group(1)
    return facts


def _fallback_parse_transaction_structure(text, project=None):
    entities = []
    role_by_id = {}

    def add_entity(name, role, etype="party", lane="other"):
        name = _clean_structure_name(name)
        if not name:
            return ""
        for item in entities:
            if item["name"] == name:
                return item["id"]
        eid = f"e{len(entities) + 1}"
        etype = etype if etype in STRUCTURE_ENTITY_TYPES else "party"
        lane = lane if lane in STRUCTURE_LANES else "other"
        entities.append({"id": eid, "name": name, "role": role, "type": etype, "lane": lane})
        role_by_id[eid] = role
        return eid

    role_rules = [
        (r"融资方|转让方|出让方|卖方|借款人|项目公司|客户|申请人", "融资方/转让方", "party", "transfer"),
        (r"受让方|买方|投资人|投资方|资金方|委托人", "受让方/投资方", "party", "transfer"),
        (r"债务人|付款人|付款义务人|核心企业|回款方|承租人", "付款义务人/核心债务人", "party", "repayment"),
        (r"担保方|保证人|担保人|差补方|回购方|增信方", "担保/增信方", "guarantee", "guarantee"),
        (r"债权人|原始债权人|施工方|供货方|出租人", "原始债权人", "party", "source"),
        (r"底层资产|基础资产|标的资产|应收账款|收益权|抵押物|质押物|租赁资产", "底层资产/增信措施", "asset", "asset"),
        (r"保理服务商|保理服务机构|资产服务商|资产服务机构|服务商|服务机构|催收机构", "保理/资产服务机构", "special", "other"),
        (r"托管行|监管行|监管账户|管理人|GP|SPV|信托计划|合伙企业", "托管/管理/载体", "special", "other"),
    ]

    lines = [x.strip() for x in re.split(r"[\n\r]+", text or "") if x.strip()]
    segments = []
    for line in lines:
        segments.extend([x.strip() for x in re.split(r"[；;。]", line) if x.strip()])

    for segment in segments:
        kv = re.match(r"([^：:]{2,18})[：:]\s*(.+)$", segment)
        if kv:
            key, value = kv.group(1).strip(), kv.group(2).strip()
            for pattern, role, etype, lane in role_rules:
                if re.search(pattern, key):
                    for name in _split_structure_names(value):
                        add_entity(name, role, etype, lane)
                    break

    sentence_patterns = [
        (r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,28}?)(?:作为|为|系)(?:融资方|转让方|借款人|项目公司|申请人)", "融资方/转让方", "party", "transfer"),
        (r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,28}?)(?:作为|为|系)(?:受让方|投资方|投资人|资金方)", "受让方/投资方", "party", "transfer"),
        (r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,28}?)(?:作为|为|系)(?:债务人|付款义务人|核心企业|回款方)", "付款义务人/核心债务人", "party", "repayment"),
        (r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,28}?)(?:提供|承担|作为|为|系)(?:连带责任)?(?:担保|保证|差补|回购)", "担保/增信方", "guarantee", "guarantee"),
        (r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,28}?)(?:作为|为|系|担任|提供)(?:保理服务商|保理服务机构|资产服务商|资产服务机构|服务商|服务机构|催收机构|资产管理服务)", "保理/资产服务机构", "special", "other"),
        (r"(?:底层资产|基础资产|标的资产|交易标的)(?:为|是|系)?([\u4e00-\u9fa5A-Za-z0-9（）()·、，,]{2,36})", "底层资产/增信措施", "asset", "asset"),
    ]
    full_text = "。".join(segments)
    for pattern, role, etype, lane in sentence_patterns:
        for match in re.finditer(pattern, full_text):
            for name in _split_structure_names(match.group(1)):
                add_entity(name, role, etype, lane)
    for match in re.finditer(r"(?:对|应收)([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,32}?)(?:的)?(?:应收账款|付款义务|债权)", full_text):
        add_entity(match.group(1), "付款义务人/核心债务人", "party", "repayment")
    for match in re.finditer(r"(?:将|把).*?(应收账款收益权|应收账款债权|租赁资产收益权|收益权).*?(?:转让|让与).*?(?:给|至|予)([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,28})", full_text):
        add_entity(match.group(1), "底层资产/收益权", "asset", "asset")
        add_entity(match.group(2), "受让方/投资方", "party", "transfer")
    if "监管账户" in full_text:
        add_entity("监管账户", "资金归集/监管账户", "special", "other")
    for match in re.finditer(r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,28}?)(?:负责|提供|承担)(?:应收账款)?(?:保理服务|资产服务|催收服务|资产管理服务|回款管理|账款管理)", full_text):
        add_entity(match.group(1), "保理/资产服务机构", "special", "other")
    for match in re.finditer(r"(?:保理服务商|保理服务机构|资产服务商|资产服务机构|服务商|服务机构)(?:由|为|是|系)?([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,28}?)(?:担任|提供|负责|，|。|；|;|$)", full_text):
        add_entity(match.group(1), "保理/资产服务机构", "special", "other")
    if "托管" in full_text and not any("托管" in e["name"] or "托管" in e["role"] for e in entities):
        add_entity("托管机构", "资金托管/账户管理", "special", "other")

    if project:
        customer = project.get("customer_name") or project.get("customer_short_name")
        if customer:
            add_entity(customer, "融资方/项目主体", "party", "transfer")
        if project.get("project_name"):
            add_entity(project.get("project_name"), "底层项目/资产", "asset", "asset")
        if project.get("guarantee_method"):
            add_entity(project.get("guarantee_method"), "担保/增信措施", "guarantee", "guarantee")
    add_entity("皖信投资", "受让方/投资方", "party", "transfer")

    def first_by_role(*keywords):
        for item in entities:
            role_text = item.get("role", "") + item.get("name", "")
            if any(word in role_text for word in keywords):
                return item
        return None

    investor = first_by_role("皖信", "受让方", "投资方", "资金方")
    financier = first_by_role("融资方", "转让方", "借款人", "项目主体")
    debtor = first_by_role("付款义务人", "核心债务人", "债务人", "回款方")
    creditor = first_by_role("原始债权人", "施工方", "供货方")
    asset = next((e for e in entities if e["type"] == "asset"), None)
    guarantees = [e for e in entities if e["type"] == "guarantee"]
    special = [e for e in entities if e["type"] == "special"]
    servicers = [e for e in special if any(word in (e.get("role", "") + e.get("name", "")) for word in ["服务", "催收", "管理"])]

    relations = []

    def add_relation(src, dst, label, rtype):
        if not src or not dst or src["id"] == dst["id"]:
            return
        item = {"from": src["id"], "to": dst["id"], "label": label, "type": rtype if rtype in STRUCTURE_RELATION_TYPES else "transfer"}
        key = (item["from"], item["to"], item["label"])
        if key not in {(r["from"], r["to"], r["label"]) for r in relations}:
            relations.append(item)

    facts = _extract_structure_facts(text or "")
    transfer_label = "转让收益权/基础资产" if any(word in (facts.get("mode") or "") for word in ["收益权", "保理", "转让"]) else "形成交易关系"
    add_relation(financier, investor, transfer_label, "transfer")
    add_relation(investor, financier, "支付投资款/转让价款", "payment")
    add_relation(creditor or financier, debtor, "形成应收账款/付款义务", "transfer")
    add_relation(creditor or financier, asset, "对应底层资产", "transfer")
    add_relation(debtor, investor, "到期回款/还本付息", "payment")
    add_relation(asset, investor, "收益权/现金流归集", "control")
    for g in guarantees:
        add_relation(g, investor, "担保/差补/回购增信", "guarantee")
    for s in servicers:
        add_relation(s, asset or financier or investor, "保理/资产服务", "control")
        add_relation(s, investor, "回款管理/信息服务", "control")
    for s in special:
        if s in servicers:
            continue
        add_relation(s, investor, "托管/监管/管理安排", "control")

    title = ""
    if project and project.get("project_name"):
        title = f"{project.get('project_name')}交易结构"
    elif financier:
        title = f"{financier['name']}交易结构图"
    else:
        title = "交易结构图"
    return {
        "title": title,
        "description": (text or "").strip()[:1000],
        "facts": facts,
        "entities": entities,
        "relations": relations,
    }


def _sanitize_structure_parse_result(parsed, text="", project=None):
    if not isinstance(parsed, dict):
        parsed = {}
    fallback = _fallback_parse_transaction_structure(text, project)
    raw_entities = parsed.get("entities")
    if not isinstance(raw_entities, list) or not raw_entities:
        raw_entities = fallback["entities"]
    entities = []
    id_map = {}
    for raw in raw_entities:
        if not isinstance(raw, dict):
            continue
        old_id = str(raw.get("id") or f"e{len(entities) + 1}")
        name = _clean_structure_name(raw.get("name"))
        if not name or any(e["name"] == name for e in entities):
            continue
        eid = f"e{len(entities) + 1}"
        etype = str(raw.get("type") or "party")
        lane = str(raw.get("lane") or "other")
        entities.append({
            "id": eid,
            "name": name,
            "role": _clean_structure_name(raw.get("role") or "交易参与方"),
            "type": etype if etype in STRUCTURE_ENTITY_TYPES else "party",
            "lane": lane if lane in STRUCTURE_LANES else "other",
        })
        id_map[old_id] = eid
        id_map[name] = eid

    if not entities:
        return fallback

    entity_ids = {e["id"] for e in entities}
    raw_relations = parsed.get("relations")
    if not isinstance(raw_relations, list):
        raw_relations = []
    relations = []
    for raw in raw_relations:
        if not isinstance(raw, dict):
            continue
        src = id_map.get(str(raw.get("from") or ""), raw.get("from"))
        dst = id_map.get(str(raw.get("to") or ""), raw.get("to"))
        if src not in entity_ids or dst not in entity_ids or src == dst:
            continue
        rtype = str(raw.get("type") or "transfer")
        label = _clean_structure_name(raw.get("label") or "交易关系") or "交易关系"
        item = {"from": src, "to": dst, "label": label, "type": rtype if rtype in STRUCTURE_RELATION_TYPES else "transfer"}
        if (item["from"], item["to"], item["label"]) not in {(r["from"], r["to"], r["label"]) for r in relations}:
            relations.append(item)

    if not relations:
        fallback_by_name = {e["name"]: e["id"] for e in entities}
        for rel in fallback["relations"]:
            src_name = next((e["name"] for e in fallback["entities"] if e["id"] == rel["from"]), "")
            dst_name = next((e["name"] for e in fallback["entities"] if e["id"] == rel["to"]), "")
            if src_name in fallback_by_name and dst_name in fallback_by_name:
                relations.append({"from": fallback_by_name[src_name], "to": fallback_by_name[dst_name], "label": rel["label"], "type": rel["type"]})

    facts = parsed.get("facts") if isinstance(parsed.get("facts"), dict) else {}
    fallback_facts = fallback.get("facts") or {}
    facts = {key: str(facts.get(key) or fallback_facts.get(key) or "")[:80] for key in ("mode", "amount", "yield", "term")}
    return {
        "title": str(parsed.get("title") or fallback.get("title") or "交易结构图")[:80],
        "description": str(parsed.get("description") or text or "")[:1000],
        "facts": facts,
        "entities": entities,
        "relations": relations,
    }


def _extract_json_object(text):
    content = str(text or "").strip()
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        content = content[start:end + 1]
    return json.loads(content)


def _ai_complete(messages, temperature=0.1, max_tokens=1800, timeout=25):
    backend, backend_error = _chat_backend_config()
    if backend_error:
        return None, "AI 未配置"
    payload = json.dumps({
        "model": backend["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        backend["url"],
        data=payload,
        headers={"Content-Type": "application/json", **backend["headers"]},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return None, f"AI 服务异常({err.code})"
    except Exception as err:
        return None, f"AI 请求失败：{err}"
    content = ((result.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
    if not content:
        return None, "AI 未返回内容"
    return content, ""

@app.route("/api/transaction-structures", methods=["GET"])
def list_transaction_structures():
    conn = get_db()
    project_id = request.args.get("project_id", "").strip()
    params = []
    where = ""
    if project_id:
        project_id = resolve_project_id(conn, project_id)
        where = "WHERE ts.project_id=?"
        params.append(project_id)
    rows = conn.execute(f"""SELECT ts.*, p.project_name, c.short_name as customer_name
        FROM transaction_structures ts
        LEFT JOIN projects p ON ts.project_id=p.id
        LEFT JOIN customers c ON p.customer_id=c.id
        {where}
        ORDER BY ts.updated_at DESC, ts.id DESC""", params).fetchall()
    conn.close()
    return jsonify([transaction_structure_to_dict(r) for r in rows])

@app.route("/api/transaction-structures/<string:sid>", methods=["GET"])
def get_transaction_structure(sid):
    conn = get_db()
    row = conn.execute("""SELECT ts.*, p.project_name, c.short_name as customer_name
        FROM transaction_structures ts
        LEFT JOIN projects p ON ts.project_id=p.id
        LEFT JOIN customers c ON p.customer_id=c.id
        WHERE ts.id=?""", (sid,)).fetchone()
    conn.close()
    if not row:
        return fail("交易结构图不存在", 404, "NOT_FOUND")
    return jsonify(transaction_structure_to_dict(row))

@app.route("/api/transaction-structures", methods=["POST"])
def create_transaction_structure():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    if not title:
        return fail("缺少必填字段：title")
    entities = normalize_json_list(data.get("entities"))
    relations = normalize_json_list(data.get("relations"))
    conn = get_db()
    sid = next_text_id(conn, "transaction_structures", "ts")
    project_id = resolve_project_id(conn, str(data.get("project_id") or "").strip())
    conn.execute("""INSERT INTO transaction_structures
        (id, project_id, title, description, facts, entities, relations, created_by)
        VALUES (?,?,?,?,?,?,?,?)""", (
        sid, project_id or None, title, data.get("description", ""),
        json.dumps(normalize_json_object(data.get("facts")), ensure_ascii=False),
        json.dumps(entities, ensure_ascii=False), json.dumps(relations, ensure_ascii=False),
        session.get("person_name", ""),
    ))
    conn.commit()
    conn.close()
    return ok(id=sid, msg="交易结构图已保存")

@app.route("/api/transaction-structures/<string:sid>", methods=["PUT"])
def update_transaction_structure(sid):
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    if not title:
        return fail("缺少必填字段：title")
    entities = normalize_json_list(data.get("entities"))
    relations = normalize_json_list(data.get("relations"))
    conn = get_db()
    project_id = resolve_project_id(conn, str(data.get("project_id") or "").strip())
    cur = conn.execute("""UPDATE transaction_structures
        SET project_id=?, title=?, description=?, facts=?, entities=?, relations=?,
            updated_at=datetime('now','localtime')
        WHERE id=?""", (
        project_id or None, title, data.get("description", ""),
        json.dumps(normalize_json_object(data.get("facts")), ensure_ascii=False),
        json.dumps(entities, ensure_ascii=False), json.dumps(relations, ensure_ascii=False), sid,
    ))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return fail("交易结构图不存在", 404, "NOT_FOUND")
    return ok(msg="交易结构图已更新")

@app.route("/api/transaction-structures/<string:sid>", methods=["DELETE"])
def delete_transaction_structure(sid):
    conn = get_db()
    cur = conn.execute("DELETE FROM transaction_structures WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return fail("交易结构图不存在", 404, "NOT_FOUND")
    return ok(msg="交易结构图已删除", deleted={"transaction_structures": cur.rowcount})


@app.route("/api/transaction-structures/parse", methods=["POST"])
def parse_transaction_structure():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        return fail("请输入交易结构描述")

    project = None
    project_id = str(data.get("project_id") or "").strip()
    if project_id:
        conn = get_db()
        try:
            resolved = resolve_project_id(conn, project_id)
            if resolved:
                project = conn.execute("""SELECT p.*, c.name as customer_name, c.short_name as customer_short_name
                    FROM projects p LEFT JOIN customers c ON p.customer_id=c.id WHERE p.id=?""", (resolved,)).fetchone()
                project = dict(project) if project else None
        finally:
            conn.close()

    fallback = _fallback_parse_transaction_structure(text, project)
    use_ai = data.get("use_ai", True) is not False
    if not use_ai:
        return ok(source="rules", structure=fallback)

    system_prompt = (
        "你是金融交易结构图抽取器。请把中文交易结构描述抽取成严格 JSON，不要输出解释、Markdown 或代码块。"
        "字段必须为：title、description、facts、entities、relations。"
        "facts 只允许包含 mode、amount、yield、term。"
        "entities 每项必须包含 id、name、role、type、lane；type 只能是 party、asset、guarantee、special；"
        "lane 只能是 source、asset、transfer、repayment、guarantee、other。"
        "relations 每项必须包含 from、to、label、type；from/to 必须引用 entities 的 id；"
        "type 只能是 transfer、payment、guarantee、control。"
        "抽取原则：皖信投资通常是投资方/受让方；融资方/转让方/借款人放在 transfer；"
        "原始债权人/施工方/供货方放在 source；债务人/付款义务人/核心企业放在 repayment；"
        "应收账款、收益权、租赁资产、抵押物、质押物放在 asset；担保人、差补方、回购方放在 guarantee。"
        "保理服务商、资产服务机构、服务商、催收机构、回款管理机构必须单独作为 special 类型主体，lane=other，"
        "role 写为保理/资产服务机构，不要并入融资方或转让方。"
        "关系要体现资产形成、收益权/债权转让、资金支付、回款还本付息、担保增信、托管监管、保理/资产服务。"
    )
    project_text = json.dumps(project or {}, ensure_ascii=False)[:1200]
    user_prompt = (
        "请抽取下面交易结构描述。\n\n"
        f"关联项目资料：{project_text}\n\n"
        f"交易结构描述：\n{text[:5000]}"
    )
    ai_content, ai_error = _ai_complete([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ], temperature=0.05, max_tokens=2200, timeout=28)
    if ai_content:
        try:
            parsed = _extract_json_object(ai_content)
            structure = _sanitize_structure_parse_result(parsed, text, project)
            if len(structure.get("entities") or []) >= 2:
                return ok(source="ai", structure=structure)
        except Exception:
            pass
    return ok(source="rules", warning=ai_error or "AI 解析结果不可用，已使用规则解析", structure=fallback)


# ── 尽调报告 ───────────────────────────────────────────────────
def due_diligence_report_to_dict(row, include_text=False):
    item = dict(row)
    item["structure_json"] = normalize_json_object(item.get("structure_json"))
    if item.get("attachment_path"):
        item["attachment_url"] = url_for(
            "download_report_attachment",
            filename=os.path.basename(item["attachment_path"]),
        )
    else:
        item["attachment_url"] = ""
    if not include_text:
        text = item.pop("full_text", "") or ""
        item["text_excerpt"] = text[:360]
    return item


def report_evaluation_author(data):
    role = current_role()
    if role and role["person_name"]:
        return role["person_name"]
    return data.get("evaluation_author") or data.get("submitter") or session.get("role_name") or ""


def allowed_report_attachment(filename):
    return os.path.splitext(filename or "")[1].lower() in {".doc", ".docx", ".pdf", ".wps"}

def report_attachment_full_path(attachment_path):
    normalized = str(attachment_path or "").strip().replace("\\", "/")
    prefix = "report_attachments/"
    if not normalized:
        return ""
    if not normalized.startswith(prefix) or normalized.count("/") != 1:
        return None
    filename = normalized[len(prefix):]
    if not filename or filename != os.path.basename(filename):
        return None
    base_dir = os.path.realpath(REPORT_ATTACHMENT_DIR)
    full_path = os.path.realpath(os.path.join(base_dir, filename))
    if os.path.dirname(full_path) != base_dir:
        return None
    return full_path

def normalize_report_attachment(conn, attachment_path, current_report_id=None):
    if not attachment_path:
        return "", None
    full_path = report_attachment_full_path(attachment_path)
    if not full_path or not os.path.isfile(full_path):
        return None, fail("附件引用无效，请重新上传", 400, "INVALID_ATTACHMENT")
    normalized = f"report_attachments/{os.path.basename(full_path)}"
    rows = conn.execute(
        """SELECT id, attachment_path FROM due_diligence_reports
           WHERE COALESCE(attachment_path,'')<>''"""
    ).fetchall()
    for row in rows:
        if current_report_id and row["id"] == current_report_id:
            continue
        other_path = report_attachment_full_path(row["attachment_path"])
        if other_path and os.path.normcase(other_path) == os.path.normcase(full_path):
            return None, fail("该附件已关联其他尽调报告", 409, "ATTACHMENT_IN_USE")
    return normalized, None

def remove_report_attachment_if_unreferenced(attachment_path):
    full_path = report_attachment_full_path(attachment_path)
    if not full_path:
        return
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT attachment_path FROM due_diligence_reports
               WHERE COALESCE(attachment_path,'')<>''"""
        ).fetchall()
        for row in rows:
            referenced_path = report_attachment_full_path(row["attachment_path"])
            if referenced_path and os.path.normcase(referenced_path) == os.path.normcase(full_path):
                return
    finally:
        conn.close()
    try:
        if os.path.isfile(full_path):
            os.remove(full_path)
    except OSError:
        app.logger.warning("Failed to remove report attachment path=%s", attachment_path)


def valid_report_attachment_content(file, filename):
    header = file.stream.read(8)
    file.stream.seek(0)
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".pdf":
        return header.startswith(b"%PDF")
    if ext in {".doc", ".wps"}:
        return header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") or header.startswith(b"PK")
    if ext == ".docx":
        return header.startswith(b"PK")
    return False


@app.route("/api/due-diligence-reports/upload", methods=["POST"])
def upload_due_diligence_report_attachment():
    file = request.files.get("file")
    if not file or not file.filename:
        return fail("请选择要上传的 Word/PDF 文件")
    if not allowed_report_attachment(file.filename):
        return fail("仅支持上传 .doc、.docx、.pdf、.wps 文件")
    if not valid_report_attachment_content(file, file.filename):
        return fail("文件内容与扩展名不匹配", 400, "INVALID_FILE")
    os.makedirs(REPORT_ATTACHMENT_DIR, exist_ok=True)
    original_name = os.path.basename(file.filename)
    safe_name = secure_filename(original_name)
    ext = os.path.splitext(safe_name or original_name)[1].lower()
    stored_name = f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
    stored_path = os.path.join(REPORT_ATTACHMENT_DIR, stored_name)
    file.save(stored_path)
    return ok(
        attachment_name=original_name,
        attachment_path=f"report_attachments/{stored_name}",
        attachment_url=url_for("download_report_attachment", filename=stored_name),
    )


@app.route("/api/due-diligence-reports/attachments/<string:filename>", methods=["GET"])
def download_report_attachment(filename):
    filename = os.path.basename(filename)
    base_dir = os.path.realpath(REPORT_ATTACHMENT_DIR)
    path = os.path.realpath(os.path.join(base_dir, filename))
    if os.path.dirname(path) != base_dir or not os.path.isfile(path):
        return fail("附件不存在", 404, "NOT_FOUND")
    return send_file(path, as_attachment=True, download_name=filename)

@app.route("/api/due-diligence-reports", methods=["GET"])
def list_due_diligence_reports():
    conn = get_db()
    rows = conn.execute("""SELECT dr.*, p.project_name
        FROM due_diligence_reports dr
        LEFT JOIN projects p ON dr.project_id=p.id
        ORDER BY dr.report_date DESC, dr.updated_at DESC, dr.id DESC""").fetchall()
    conn.close()
    return jsonify([due_diligence_report_to_dict(r, include_text=False) for r in rows])

@app.route("/api/due-diligence-reports/<string:rid>", methods=["GET"])
def get_due_diligence_report(rid):
    conn = get_db()
    row = conn.execute("""SELECT dr.*, p.project_name
        FROM due_diligence_reports dr
        LEFT JOIN projects p ON dr.project_id=p.id
        WHERE dr.id=?""", (rid,)).fetchone()
    conn.close()
    if not row:
        return fail("尽调报告不存在", 404, "NOT_FOUND")
    return jsonify(due_diligence_report_to_dict(row, include_text=True))

@app.route("/api/due-diligence-reports", methods=["POST"])
def create_due_diligence_report():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    if not title:
        return fail("缺少必填字段：title")
    conn = get_db()
    try:
        rid = next_text_id(conn, "due_diligence_reports", "dd")
        attachment_path, attachment_error = normalize_report_attachment(
            conn, data.get("attachment_path", "")
        )
        if attachment_error:
            conn.rollback()
            return attachment_error
        project_id = resolve_project_id(conn, str(data.get("project_id") or "").strip())
        evaluation_note = str(data.get("evaluation_note") or "").strip()
        evaluation_author = report_evaluation_author(data) if evaluation_note else ""
        evaluation_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") if evaluation_note else ""
        conn.execute("""INSERT INTO due_diligence_reports
            (id, project_id, title, file_name, report_date, submitter, department,
             summary, business_plan, structure_json, full_text, attachment_name, attachment_path,
             evaluation_note, evaluation_author, evaluation_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            rid, project_id or None, title, data.get("file_name", ""), data.get("report_date", ""),
            data.get("submitter", ""), data.get("department", ""), data.get("summary", ""),
            data.get("business_plan", ""), json.dumps(normalize_json_object(data.get("structure_json")), ensure_ascii=False),
            data.get("full_text", ""), data.get("attachment_name", ""), attachment_path,
            evaluation_note, evaluation_author, evaluation_at,
        ))
        conn.commit()
        return ok(id=rid, msg="尽调报告已入库")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

@app.route("/api/due-diligence-reports/<string:rid>", methods=["PUT"])
def update_due_diligence_report(rid):
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    if not title:
        return fail("缺少必填字段：title")
    conn = get_db()
    old_attachment_path = ""
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT evaluation_note, evaluation_author, evaluation_at, attachment_path
               FROM due_diligence_reports WHERE id=?""",
            (rid,),
        ).fetchone()
        if not existing:
            conn.rollback()
            return fail("尽调报告不存在", 404, "NOT_FOUND")
        old_attachment_path = existing["attachment_path"] or ""
        attachment_path, attachment_error = normalize_report_attachment(
            conn, data.get("attachment_path", ""), current_report_id=rid
        )
        if attachment_error:
            conn.rollback()
            return attachment_error
        project_id = resolve_project_id(conn, str(data.get("project_id") or "").strip())
        evaluation_note = str(data.get("evaluation_note") or "").strip()
        if evaluation_note != (existing["evaluation_note"] or ""):
            evaluation_author = report_evaluation_author(data) if evaluation_note else ""
            evaluation_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") if evaluation_note else ""
        else:
            evaluation_author = existing["evaluation_author"]
            evaluation_at = existing["evaluation_at"]
        conn.execute("""UPDATE due_diligence_reports SET
            project_id=?, title=?, file_name=?, report_date=?, submitter=?, department=?,
            summary=?, business_plan=?, structure_json=?, full_text=?, attachment_name=?, attachment_path=?,
            evaluation_note=?, evaluation_author=?, evaluation_at=?,
            updated_at=datetime('now','localtime')
            WHERE id=?""", (
            project_id or None, title, data.get("file_name", ""), data.get("report_date", ""),
            data.get("submitter", ""), data.get("department", ""), data.get("summary", ""),
            data.get("business_plan", ""), json.dumps(normalize_json_object(data.get("structure_json")), ensure_ascii=False),
            data.get("full_text", ""), data.get("attachment_name", ""), attachment_path,
            evaluation_note, evaluation_author, evaluation_at, rid,
        ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if old_attachment_path and old_attachment_path != attachment_path:
        remove_report_attachment_if_unreferenced(old_attachment_path)
    return ok(msg="尽调报告已更新")


@app.route("/api/due-diligence-reports/<string:rid>/evaluation", methods=["PUT"])
def update_due_diligence_report_evaluation(rid):
    data = request.get_json(silent=True) or {}
    evaluation_note = str(data.get("evaluation_note") or "").strip()
    evaluation_author = report_evaluation_author(data) if evaluation_note else ""
    evaluation_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") if evaluation_note else ""
    conn = get_db()
    cur = conn.execute("""UPDATE due_diligence_reports SET
        evaluation_note=?, evaluation_author=?, evaluation_at=?, updated_at=datetime('now','localtime')
        WHERE id=?""", (evaluation_note, evaluation_author, evaluation_at, rid))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return fail("尽调报告不存在", 404, "NOT_FOUND")
    return ok(msg="评价附言已保存", evaluation_note=evaluation_note, evaluation_author=evaluation_author, evaluation_at=evaluation_at)

@app.route("/api/due-diligence-reports/<string:rid>", methods=["DELETE"])
def delete_due_diligence_report(rid):
    conn = get_db()
    try:
        row = conn.execute("SELECT attachment_path FROM due_diligence_reports WHERE id=?", (rid,)).fetchone()
        if not row:
            return fail("尽调报告不存在", 404, "NOT_FOUND")
        attachment_path = row["attachment_path"]
        cur = conn.execute("DELETE FROM due_diligence_reports WHERE id=?", (rid,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    remove_report_attachment_if_unreferenced(attachment_path)
    return ok(msg="尽调报告已删除", deleted={"due_diligence_reports": cur.rowcount})


# ── 付息计划 CRUD ──────────────────────────────────────────────
@app.route("/api/payments", methods=["GET"])
def list_payments():
    conn = get_db()
    project_id = request.args.get("project_id")
    params = []
    where = ""
    if project_id:
        where = "WHERE pp.project_id=?"
        params.append(project_id)
    rows = conn.execute(f"""SELECT pp.*, pp.due_date as payment_date,
        c.short_name as customer_name, p.project_name 
        FROM payment_schedules pp 
        LEFT JOIN projects p ON pp.project_id=p.id
        LEFT JOIN customers c ON p.customer_id=c.id 
        {where}
        ORDER BY pp.project_id, COALESCE(pp.period_number, 999999), pp.due_date""", params).fetchall()
    pending_ids = {r["related_id"] for r in conn.execute("""SELECT related_id FROM approvals
        WHERE related_type='payment' AND action='confirm_payment' AND status='pending'""").fetchall()}
    conn.close()
    data = []
    for row in rows:
        item = dict(row)
        item["pending_approval"] = item.get("id") in pending_ids
        data.append(item)
    return jsonify(data)

@app.route("/api/payments", methods=["POST"])
def create_payment():
    data = request.json or {}
    missing = require_fields(data, ["project_id"])
    if missing:
        return fail(missing)
    due_date = data.get("due_date") or data.get("payment_date")
    if not due_date:
        return fail("缺少必填字段：payment_date")
    conn = get_db()
    role, error = require_role(conn)
    if error:
        conn.close()
        return error
    requested_status = payment_status_for_db(data.get("status", "pending"))
    if requested_status == "paid" and not is_leader(role):
        conn.close()
        return fail("确认收款必须通过审批流程", 403, "APPROVAL_REQUIRED")
    pid = next_text_id(conn, "payment_schedules", "pay")
    period_number = conn.execute("SELECT COUNT(*) + 1 FROM payment_schedules WHERE project_id=?", (data["project_id"],)).fetchone()[0]
    conn.execute("""INSERT INTO payment_schedules (id,project_id,period_number,due_date,principal_amount,interest_amount,total_amount,status,notes) 
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (pid, data["project_id"], period_number, due_date,
         data.get("principal_amount",0), data.get("interest_amount",0), data.get("total_amount", data.get("amount",0)),
         payment_status_for_db(data.get("status","pending")), data.get("notes","")))
    conn.commit()
    conn.close()
    return ok(id=pid, msg="创建成功")

@app.route("/api/payments/<string:pid>", methods=["PUT"])
def update_payment(pid):
    data = request.json or {}
    due_date = data.get("due_date") or data.get("payment_date")
    if not due_date:
        return fail("缺少必填字段：payment_date")
    conn = get_db()
    try:
        existing = conn.execute("SELECT status FROM payment_schedules WHERE id=?", (pid,)).fetchone()
        if not existing:
            return fail("付息记录不存在", 404, "NOT_FOUND")
        existing_status = payment_status_for_db(existing["status"])
        requested_status = payment_status_for_db(data.get("status", existing_status))
        if requested_status == "paid" and existing_status != "paid":
            return fail("请通过“确认收款”操作发起审批", 409, "APPROVAL_REQUIRED")
        if existing_status == "paid" and requested_status != "paid":
            return fail("已收款记录不可直接改回待付", 409, "PAID_IMMUTABLE")
        conn.execute("""UPDATE payment_schedules SET due_date=?,principal_amount=?,interest_amount=?,total_amount=?,
            status=?,actual_pay_date=?,actual_amount=?,notes=? WHERE id=?""",
            (due_date,
             data.get("principal_amount",0), data.get("interest_amount",0), data.get("total_amount", data.get("amount",0)),
             requested_status,
             data.get("actual_pay_date",""), data.get("actual_amount"), data.get("notes",""), pid))
        conn.commit()
        return ok(msg="更新成功")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

@app.route("/api/payments/<string:pid>/confirm", methods=["POST"])
def confirm_payment(pid):
    data = request.json or {}
    conn = get_db()
    try:
        role, error = require_role(conn)
        if error:
            return error
        result, error = create_approval_record(conn, "payment", pid, "confirm_payment", data)
        if error:
            conn.rollback()
            return error
        conn.commit()
        if result.get("direct"):
            return ok(msg="确认到款成功", executed=True, approval_id=result["id"])
        return ok(msg="已提交确认收款审批", approval_id=result["id"], pending_approval=True)
    finally:
        conn.close()

@app.route("/api/payments/<string:pid>", methods=["DELETE"])
def delete_payment(pid):
    conn = get_db()
    cur = conn.execute("DELETE FROM payment_schedules WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return fail("付息记录不存在", 404, "NOT_FOUND")
    return ok(msg="删除成功", deleted={"payments": cur.rowcount})


# ── 商机 CRUD ─────────────────────────────────────────────────
@app.route("/api/opportunities", methods=["GET"])
def list_opportunities():
    conn = get_db()
    rows = conn.execute("""SELECT o.*, o.amount as expected_amount,
        o.assigned_to as owner, o.description as notes, c.short_name as customer_name 
        FROM opportunities o LEFT JOIN customers c ON o.customer_id=c.id 
        ORDER BY o.sort_order, o.id DESC""").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/opportunities", methods=["POST"])
def create_opportunity():
    data = request.json or {}
    missing = require_fields(data, ["title"])
    if missing:
        return fail(missing)
    conn = get_db()
    oid = next_text_id(conn, "opportunities", "o")
    amount = data.get("amount", data.get("expected_amount", 0))
    owner = data.get("owner", data.get("assigned_to", ""))
    notes = data.get("notes", data.get("description", ""))
    conn.execute("""INSERT INTO opportunities (id,title,customer_id,stage,amount,probability,
        expected_close_date,assigned_to,source,description,sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (oid, data["title"], data.get("customer_id"), data.get("stage","线索"),
         amount, data.get("probability",10), data.get("expected_close_date",""),
         owner, data.get("source",""), notes, data.get("sort_order",0)))
    conn.commit()
    conn.close()
    return ok(id=oid, msg="创建成功")

@app.route("/api/opportunities/<string:oid>", methods=["PUT"])
def update_opportunity(oid):
    data = request.json or {}
    missing = require_fields(data, ["title"])
    if missing:
        return fail(missing)
    conn = get_db()
    amount = data.get("amount", data.get("expected_amount", 0))
    owner = data.get("owner", data.get("assigned_to", ""))
    notes = data.get("notes", data.get("description", ""))
    cur = conn.execute("""UPDATE opportunities SET title=?,customer_id=?,stage=?,amount=?,probability=?,
        expected_close_date=?,assigned_to=?,source=?,description=?,sort_order=?,updated_at=datetime('now','localtime') WHERE id=?""",
        (data["title"], data.get("customer_id"), data.get("stage",""),
         amount, data.get("probability",10), data.get("expected_close_date",""),
         owner, data.get("source",""), notes, data.get("sort_order",0), oid))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return fail("商机不存在", 404, "NOT_FOUND")
    return ok(msg="更新成功")

@app.route("/api/opportunities/<string:oid>/stage", methods=["PATCH", "PUT"])
def update_stage(oid):
    data = request.json or {}
    missing = require_fields(data, ["stage"])
    if missing:
        return fail(missing)
    conn = get_db()
    cur = conn.execute("UPDATE opportunities SET stage=?, updated_at=datetime('now','localtime') WHERE id=?",
        (data["stage"], oid))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return fail("商机不存在", 404, "NOT_FOUND")
    return ok(msg="阶段更新成功")

@app.route("/api/opportunities/<string:oid>", methods=["DELETE"])
def delete_opportunity(oid):
    conn = get_db()
    cur = conn.execute("DELETE FROM opportunities WHERE id=?", (oid,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return fail("商机不存在", 404, "NOT_FOUND")
    return ok(msg="删除成功", deleted={"opportunities": cur.rowcount})


# ── 提醒 CRUD ─────────────────────────────────────────────────
@app.route("/api/reminders", methods=["GET"])
def list_reminders():
    conn = get_db()
    status = request.args.get("status")
    cols = table_columns(conn, "reminders")
    type_col = "remind_type" if "remind_type" in cols else "type"
    notes_col = "description" if "description" in cols else "notes"
    done_expr = "COALESCE(is_done, '') as is_done" if "is_done" in cols else "CASE WHEN status IN ('done','已读','已完成') THEN '1' ELSE '' END as is_done"
    if status:
        rows = conn.execute(f"""SELECT *, {type_col} as remind_type, {notes_col} as notes, {done_expr}
            FROM reminders WHERE status=? ORDER BY remind_date""", (status,)).fetchall()
    else:
        rows = conn.execute(f"""SELECT *, {type_col} as remind_type, {notes_col} as notes, {done_expr}
            FROM reminders ORDER BY CASE priority WHEN '高' THEN 1 WHEN '中' THEN 2 WHEN '低' THEN 3 ELSE 4 END, remind_date""").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/reminders/<string:rid>/read", methods=["PATCH", "PUT"])
@app.route("/api/reminders/<string:rid>/done", methods=["PATCH", "PUT"])
def mark_read(rid):
    conn = get_db()
    cols = table_columns(conn, "reminders")
    if "is_done" in cols:
        cur = conn.execute("UPDATE reminders SET status='done', is_done='1' WHERE id=?", (rid,))
    else:
        cur = conn.execute("UPDATE reminders SET status='已读' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return fail("提醒不存在", 404, "NOT_FOUND")
    return ok(msg="已标记完成")

@app.route("/api/reminders", methods=["POST"])
def create_reminder():
    data = request.json or {}
    missing = require_fields(data, ["title", "remind_date"])
    if missing:
        return fail(missing)
    conn = get_db()
    rid = next_text_id(conn, "reminders", "r")
    cols = table_columns(conn, "reminders")
    remind_type = data.get("remind_type") or data.get("type") or "自定义"
    notes = data.get("notes") or data.get("description") or ""
    if "remind_type" in cols:
        conn.execute("""INSERT INTO reminders (id,title,remind_date,remind_type,related_id,related_type,status,priority,description,is_done) 
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (rid, data["title"], data["remind_date"], remind_type, data.get("related_id"),
             data.get("related_type",""), data.get("status","unread"), data.get("priority","中"), notes, ""))
    else:
        conn.execute("""INSERT INTO reminders (id,title,remind_date,type,related_id,related_type,status,priority,notes) 
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (rid, data["title"], data["remind_date"], remind_type, data.get("related_id"),
             data.get("related_type",""), data.get("status","未读"), data.get("priority","中"), notes))
    conn.commit()
    conn.close()
    return ok(id=rid, msg="创建成功")

@app.route("/api/reminders/<string:rid>", methods=["DELETE"])
def delete_reminder(rid):
    conn = get_db()
    cur = conn.execute("DELETE FROM reminders WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return fail("提醒不存在", 404, "NOT_FOUND")
    return ok(msg="删除成功", deleted={"reminders": cur.rowcount})


# ── 全局搜索 ──────────────────────────────────────────────────
@app.route("/api/search")
def global_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"customers": [], "projects": [], "opportunities": []})
    if len(q) > MAX_SEARCH_LENGTH:
        return fail(f"搜索关键词不能超过 {MAX_SEARCH_LENGTH} 个字符", 400, "SEARCH_TOO_LONG")
    like = f"%{q}%"
    conn = get_db()
    customers = conn.execute("""SELECT * FROM customers
        WHERE name LIKE ? OR short_name LIKE ? OR contact_person LIKE ?
        LIMIT 50""", (like, like, like)).fetchall()
    projects = conn.execute("""SELECT p.*, c.short_name as customer_name FROM projects p 
        LEFT JOIN customers c ON p.customer_id=c.id
        WHERE p.project_name LIKE ? OR p.project_code LIKE ?
        LIMIT 50""", (like, like)).fetchall()
    opps = conn.execute("""SELECT o.*, c.short_name as customer_name FROM opportunities o 
        LEFT JOIN customers c ON o.customer_id=c.id WHERE o.title LIKE ?
        LIMIT 50""", (like,)).fetchall()
    conn.close()
    return jsonify({
        "customers": [dict(r) for r in customers],
        "projects": [dict(r) for r in projects],
        "opportunities": [dict(r) for r in opps],
    })


# ── Excel 导出 ────────────────────────────────────────────────
@app.route("/api/export/<table_name>")
def export_csv(table_name):
    conn = get_db()
    if table_name == "projects":
        rows = conn.execute("""SELECT p.project_code as 项目编号, p.project_name as 项目名称, c.short_name as 客户,
            p.investment_amount as 投资金额, p.annual_rate as 年利率, p.loan_term_months as 期限月,
            p.repayment_method as 还款方式, p.start_date as 起始日, p.end_date as 到期日, p.status as 状态,
            p.guarantee_method as 担保方式 FROM projects p LEFT JOIN customers c ON p.customer_id=c.id ORDER BY p.id""").fetchall()
    elif table_name == "payments":
        rows = conn.execute("""SELECT pp.due_date as 计划日期, c.short_name as 客户, p.project_name as 项目,
            pp.principal_amount as 本金, pp.interest_amount as 利息, pp.total_amount as 合计, pp.status as 状态, pp.actual_pay_date as 实际日期,
            pp.actual_amount as 实际金额 FROM payment_schedules pp 
            LEFT JOIN projects p ON pp.project_id=p.id
            LEFT JOIN customers c ON p.customer_id=c.id
            ORDER BY pp.project_id, COALESCE(pp.period_number, 999999), pp.due_date""").fetchall()
    elif table_name == "customers":
        rows = conn.execute("SELECT name as 客户名称, short_name as 简称, industry as 行业, contact_person as 联系人, contact_phone as 电话 FROM customers ORDER BY id").fetchall()
    else:
        conn.close()
        return jsonify({"error": "不支持的导出类型"}), 400
    
    if not rows:
        conn.close()
        return jsonify({"error": "无数据"}), 400
    
    columns = rows[0].keys()
    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row[col] for col in columns])
    conn.close()
    
    bom = '\ufeff'
    response = Response(bom + output.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f"attachment; filename={table_name}_{datetime.datetime.now().strftime('%Y%m%d')}.csv"
    return response

@app.route("/api/export/<table_name>.xlsx")
def export_excel(table_name):
    conn = get_db()
    if table_name == "projects":
        rows = conn.execute("""SELECT p.project_code as 项目编号, p.project_name as 项目名称, c.short_name as 客户,
            p.investment_amount as 投资金额, p.annual_rate as 年利率, p.loan_term_months as 期限月,
            p.repayment_method as 还款方式, p.start_date as 起始日, p.end_date as 到期日, p.status as 状态,
            p.guarantee_method as 担保方式 FROM projects p LEFT JOIN customers c ON p.customer_id=c.id ORDER BY p.id""").fetchall()
    elif table_name == "payments":
        rows = conn.execute("""SELECT pp.due_date as 计划日期, c.short_name as 客户, p.project_name as 项目,
            pp.principal_amount as 本金, pp.interest_amount as 利息, pp.total_amount as 合计, pp.status as 状态, pp.actual_pay_date as 实际日期,
            pp.actual_amount as 实际金额 FROM payment_schedules pp 
            LEFT JOIN projects p ON pp.project_id=p.id
            LEFT JOIN customers c ON p.customer_id=c.id
            ORDER BY pp.project_id, COALESCE(pp.period_number, 999999), pp.due_date""").fetchall()
    elif table_name == "customers":
        rows = conn.execute("SELECT name as 客户名称, short_name as 简称, industry as 行业, contact_person as 联系人, contact_phone as 电话 FROM customers ORDER BY id").fetchall()
    else:
        conn.close()
        return jsonify({"error": "不支持的导出类型"}), 400
    
    if not rows:
        conn.close()
        return jsonify({"error": "无数据"}), 400
    
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    wb = Workbook()
    ws = wb.active
    ws.title = table_name
    
    # 写入表头
    columns = list(rows[0].keys())
    ws.append(columns)
    
    # 表头样式
    header_fill = PatternFill(start_color="1a56db", end_color="1a56db", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, size=11)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    
    # 写入数据
    for row in rows:
        ws.append([row[col] for col in columns])
    
    # 调整列宽
    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except:
                pass
        adjusted_width = min(max_length + 2, 50)
        ws.column_dimensions[column_letter].width = adjusted_width
    
    # 冻结首行
    ws.freeze_panes = 'A2'
    
    conn.close()
    
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    
    response = Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response.headers["Content-Disposition"] = f"attachment; filename={table_name}_{datetime.datetime.now().strftime('%Y%m%d')}.xlsx"
    return response


# ── AI 聊天助手 ────────────────────────────────────────────────
def _env_value(name):
    return config_value(name, "")


def _deepseek_api_key():
    return _env_value("DEEPSEEK_API_KEY") or _env_value("DEEPSEEK_KEY")


def _ai_provider():
    return (_env_value("AI_PROVIDER") or "deepseek").strip().lower()


def _local_llm_chat_url():
    base = (_env_value("LOCAL_LLM_BASE_URL") or _env_value("AI_BASE_URL") or "http://192.168.50.148:8080").strip()
    if base.endswith("/chat/completions"):
        return base
    return base.rstrip("/") + "/v1/chat/completions"


def _local_llm_model():
    return (_env_value("LOCAL_LLM_MODEL") or _env_value("AI_MODEL") or "Qwen3.6-27B-Q3_K_M.gguf").strip()


def _deepseek_chat_url():
    return (_env_value("DEEPSEEK_API_URL") or "https://api.deepseek.com/v1/chat/completions").strip()


def _is_private_ai_url(url):
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost"):
            return True
        address = ipaddress.ip_address(host)
        return address.is_loopback or address.is_private or address.is_link_local
    except ValueError:
        return False


def _chat_backend_config():
    provider = _ai_provider()
    if provider in ("deepseek", "ds"):
        api_key = _deepseek_api_key()
        if not api_key:
            return None, fail("AI 密钥未配置，请设置 DEEPSEEK_API_KEY 环境变量", 500, "AI_KEY_MISSING")
        return {
            "provider": "deepseek",
            "url": _deepseek_chat_url(),
            "model": _env_value("DEEPSEEK_MODEL") or "deepseek-chat",
            "headers": {"Authorization": f"Bearer {api_key}"},
        }, None
    headers = {}
    local_key = _env_value("LOCAL_LLM_API_KEY") or _env_value("AI_API_KEY")
    if local_key:
        headers["Authorization"] = f"Bearer {local_key}"
    return {
        "provider": "local",
        "url": _local_llm_chat_url(),
        "model": _local_llm_model(),
        "headers": headers,
    }, None


def _mark_ai_backend_unavailable(error):
    with _ai_health_lock:
        _ai_backend_health["unavailable_until"] = time.monotonic() + 60
        _ai_backend_health["last_error"] = str(error)[:500]


def _ai_backend_unavailable():
    with _ai_health_lock:
        return time.monotonic() < _ai_backend_health["unavailable_until"]


def _offline_chat_response(question, reason=""):
    answer = _offline_ledger_answer(question)
    if reason:
        app.logger.warning("AI backend fallback provider=%s reason=%s", _ai_provider(), reason)
    return ok(
        answer=answer,
        source="ledger_fallback",
        model_online=False,
        notice="模型服务当前不可用，以上内容由本地台账直接查询生成。",
    )


def _money_text(value):
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if abs(amount) >= 100000000:
        return f"{amount / 100000000:.2f}亿元"
    if abs(amount) >= 10000:
        return f"{amount / 10000:.2f}万元"
    return f"{amount:.2f}元"


def _rate_text(value):
    try:
        rate = float(value or 0)
    except (TypeError, ValueError):
        return "未录入"
    if rate <= 0:
        return "未录入"
    return f"{rate * 100:.2f}%" if rate <= 1 else f"{rate:.2f}%"


def _row_get(row, key, default="-"):
    value = row.get(key) if isinstance(row, dict) else row[key]
    return default if value in (None, "") else value


def _project_status_text(status):
    return {
        "active": "进行中",
        "completed": "已完成",
        "settled": "已结清",
        "terminated": "已终止",
    }.get(str(status or "").strip(), str(status or "未标注"))


def _offline_ledger_answer(question):
    text = str(question or "").strip()
    compact = re.sub(r"\s+", "", text)
    conn = get_db()
    try:
        projects = [dict(row) for row in conn.execute("""
            SELECT p.*, c.short_name AS customer_name
            FROM projects p LEFT JOIN customers c ON p.customer_id=c.id
            ORDER BY p.investment_amount DESC, p.id
        """).fetchall()]
        customers = [dict(row) for row in conn.execute(
            "SELECT * FROM customers ORDER BY id"
        ).fetchall()]

        matched_projects = [
            item for item in projects
            if item.get("project_name") and (
                item["project_name"] in text
                or any(
                    len(token) >= 2 and token in item["project_name"]
                    for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text)
                )
            )
        ]
        if matched_projects:
            lines = []
            for item in matched_projects[:5]:
                rate = float(item.get("annual_rate") or 0) * 100
                lines.append(
                    f"{item.get('project_name')}：客户{item.get('customer_name') or '未关联'}，"
                    f"投资金额{_money_text(item.get('investment_amount'))}，"
                    f"年利率{rate:.2f}% ，期限至{item.get('end_date') or '未录入'}，"
                    f"状态{_project_status_text(item.get('status'))}。"
                )
            return "\n".join(lines)

        matched_customers = [
            item for item in customers
            if any(
                value and str(value) in text
                for value in (item.get("name"), item.get("short_name"))
            )
        ]
        if matched_customers:
            lines = []
            for item in matched_customers[:5]:
                project_count = conn.execute(
                    "SELECT COUNT(*) FROM projects WHERE customer_id=?",
                    (item["id"],),
                ).fetchone()[0]
                lines.append(
                    f"{item.get('name')}：简称{item.get('short_name') or '-'}，"
                    f"行业{item.get('industry') or '-'}，关联项目{project_count}个。"
                )
            return "\n".join(lines)

        if any(word in compact for word in ("本月付息", "本月应付", "这个月付息", "当月付息")):
            reminders, month_start, month_end, audit = build_month_interest_reminders(conn)
            total = sum(float(item.get("reminder_amount") or 0) for item in reminders)
            lines = [
                f"{month_start.isoformat()}至{month_end.isoformat()}正式付息台账共{len(reminders)}笔，"
                f"应付息合计{_money_text(total)}。"
            ]
            for item in reminders[:8]:
                lines.append(
                    f"{item.get('payment_date')}：{item.get('project_name')}，"
                    f"{_money_text(item.get('reminder_amount'))}，{item.get('reminder_status')}。"
                )
            if audit:
                lines.append(f"另有{len(audit)}项疑似漏项，需人工核对，不计入上述合计。")
            return "\n".join(lines)

        if any(word in compact for word in ("逾期", "过期", "超期")):
            today = datetime.date.today().isoformat()
            rows = [dict(row) for row in conn.execute("""
                SELECT ps.due_date, ps.total_amount, p.project_name
                FROM payment_schedules ps
                LEFT JOIN projects p ON ps.project_id=p.id
                WHERE ps.status NOT IN ('paid','已付')
                  AND date(ps.due_date) < date(?)
                ORDER BY date(ps.due_date), ps.project_id
                LIMIT 20
            """, (today,)).fetchall()]
            total = sum(float(item.get("total_amount") or 0) for item in rows)
            if not rows:
                return "当前付息台账未发现逾期未付记录。"
            lines = [f"当前查询到逾期未付记录{len(rows)}笔，列表金额合计{_money_text(total)}。"]
            for item in rows[:10]:
                lines.append(
                    f"{item.get('due_date')}：{item.get('project_name') or '未关联项目'}，"
                    f"{_money_text(item.get('total_amount'))}。"
                )
            return "\n".join(lines)

        if any(word in compact for word in ("即将到期", "近期到期", "到期项目", "未来30天")):
            today = datetime.date.today()
            end = today + datetime.timedelta(days=30)
            rows = [
                item for item in projects
                if parse_iso_date(item.get("end_date"))
                and today <= parse_iso_date(item.get("end_date")) <= end
            ]
            if not rows:
                return f"{today.isoformat()}起未来30天内没有录入到期日的项目。"
            lines = [f"{today.isoformat()}起未来30天内到期项目{len(rows)}个："]
            for item in sorted(rows, key=lambda row: row.get("end_date") or ""):
                lines.append(
                    f"{item.get('end_date')}：{item.get('project_name')}，"
                    f"投资金额{_money_text(item.get('investment_amount'))}。"
                )
            return "\n".join(lines)

        if any(word in compact for word in ("投资总额", "项目总额", "总投资", "项目数量", "多少项目", "台账概况")):
            total = sum(float(item.get("investment_amount") or 0) for item in projects)
            active = sum(1 for item in projects if item.get("status") == "active")
            return (
                f"项目台账共{len(projects)}个项目，其中进行中{active}个，"
                f"投资金额合计{_money_text(total)}；客户台账共{len(customers)}家。"
            )

        if any(word in compact for word in ("最大项目", "金额最高", "投资最多", "前五")):
            lines = ["按投资金额排序前5个项目："]
            for index, item in enumerate(projects[:5], 1):
                lines.append(
                    f"{index}. {item.get('project_name')}，{_money_text(item.get('investment_amount'))}。"
                )
            return "\n".join(lines)

        return (
            "本地模型当前离线。我仍可直接查询台账回答：项目概况、投资总额、"
            "项目或客户信息、本月付息、逾期记录、未来30天到期项目和金额最高项目。"
            "请换一种更具体的问法。"
        )
    finally:
        conn.close()


def invalidate_ledger_context_cache():
    with _ledger_cache_lock:
        _ledger_context_cache["value"] = ""
        _ledger_context_cache["expires_at"] = 0.0


def _ledger_context():
    now = time.monotonic()
    with _ledger_cache_lock:
        if _ledger_context_cache["value"] and now < _ledger_context_cache["expires_at"]:
            return _ledger_context_cache["value"]
    conn = get_db()
    try:
        pay_cols = table_columns(conn, "payment_schedules")
        amount_expr = "ps.amount" if "amount" in pay_cols else "0"
        principal_expr = "COALESCE(ps.principal_amount, 0)" if "principal_amount" in pay_cols else "0"
        interest_expr = "COALESCE(ps.interest_amount, 0)" if "interest_amount" in pay_cols else f"COALESCE({amount_expr}, 0)"
        if "total_amount" in pay_cols and "amount" in pay_cols:
            total_expr = "COALESCE(ps.total_amount, ps.amount, 0)"
        elif "total_amount" in pay_cols:
            total_expr = "COALESCE(ps.total_amount, 0)"
        else:
            total_expr = f"COALESCE({amount_expr}, 0)"
        period_expr = "ps.period_number" if "period_number" in pay_cols else "NULL"

        projects = [dict(r) for r in conn.execute("""
            SELECT p.*, c.name AS customer_name, c.short_name AS customer_short_name,
                   c.industry AS customer_industry
            FROM projects p
            LEFT JOIN customers c ON p.customer_id = c.id
            ORDER BY p.id
            LIMIT 120
        """).fetchall()]

        pending = [dict(r) for r in conn.execute(f"""
            SELECT ps.id, ps.project_id, {period_expr} AS period_number,
                   {principal_expr} AS principal_amount,
                   {interest_expr} AS interest_amount,
                   {total_expr} AS total_amount,
                   ps.due_date, ps.status AS pay_status, p.project_name,
                   c.short_name AS customer_short_name
            FROM payment_schedules ps
            LEFT JOIN projects p ON ps.project_id = p.id
            LEFT JOIN customers c ON p.customer_id = c.id
            WHERE ps.status IN ('pending', '待付', '未付', 'overdue', '逾期')
            ORDER BY date(ps.due_date), ps.project_id, COALESCE({period_expr}, 999999)
            LIMIT 60
        """).fetchall()]

        paid = [dict(r) for r in conn.execute(f"""
            SELECT p.project_name, c.short_name AS customer_short_name,
                   COUNT(*) AS paid_periods,
                   ROUND(SUM({interest_expr}), 2) AS paid_interest,
                   ROUND(SUM({principal_expr}), 2) AS paid_principal,
                   ROUND(SUM({total_expr}), 2) AS paid_total
            FROM payment_schedules ps
            LEFT JOIN projects p ON ps.project_id = p.id
            LEFT JOIN customers c ON p.customer_id = c.id
            WHERE ps.status IN ('paid', '已付')
            GROUP BY ps.project_id
            ORDER BY p.id
        """).fetchall()]

        customers = [dict(r) for r in conn.execute("""
            SELECT id, name, short_name, industry
            FROM customers
            ORDER BY id
            LIMIT 120
        """).fetchall()]

        today = datetime.date.today()
        month_interest, month_start, month_end, interest_audit = build_month_interest_reminders(conn, today)
    finally:
        conn.close()

    lines = ["=== 项目信息 ==="]
    for p in projects:
        lines.append(
            f"编号:{p.get('id')} | 项目:{_row_get(p, 'project_name')} | "
            f"客户:{_row_get(p, 'customer_short_name', _row_get(p, 'customer_name'))} | "
            f"金额:{_money_text(p.get('investment_amount'))} | 年利率:{_rate_text(p.get('annual_rate'))} | "
            f"期限:{_row_get(p, 'loan_term_months')}个月 | 还款方式:{_row_get(p, 'repayment_method')} | "
            f"状态:{_row_get(p, 'status')} | 资金来源:{_row_get(p, 'funding_source', '自有资金')}"
        )

    lines.append("\n=== 待收/逾期款项（按到期日排序） ===")
    for item in pending:
        period = f"第{item['period_number']}期" if item.get("period_number") else "-"
        lines.append(
            f"项目:{_row_get(item, 'project_name')} | 客户:{_row_get(item, 'customer_short_name')} | "
            f"{period} | 到期:{_row_get(item, 'due_date')} | "
            f"利息:{_money_text(item.get('interest_amount'))} | 本金:{_money_text(item.get('principal_amount'))} | "
            f"合计:{_money_text(item.get('total_amount'))} | 状态:{_row_get(item, 'pay_status')}"
        )

    lines.append(
        f"\n=== 本月付息提醒（正式台账口径，{month_start.isoformat()}至{month_end.isoformat()}） ==="
    )
    lines.append(
        f"正式提醒数量:{len(month_interest)} | 正式应付息合计:{_money_text(sum(item.get('reminder_amount', 0) for item in month_interest))}"
    )
    for item in month_interest:
        lines.append(
            f"项目:{_row_get(item, 'project_name')} | 客户:{_row_get(item, 'customer_name')} | "
            f"付息日:{_row_get(item, 'payment_date')} | 应付息:{_money_text(item.get('reminder_amount'))} | "
            f"状态:{_row_get(item, 'reminder_status')} | 来源:付息台账"
        )

    lines.append("\n=== 本月付息疑似漏项核对（不计入正式应付息，需人工核对后同步付息台账） ===")
    if interest_audit:
        lines.append(f"待核对数量:{len(interest_audit)}")
        for item in interest_audit:
            lines.append(
                f"项目:{_row_get(item, 'project_name')} | 客户:{_row_get(item, 'customer_name')} | "
                f"推算付息日:{_row_get(item, 'payment_date')} | 推算利息:{_money_text(item.get('reminder_amount'))} | "
                f"提示:{_row_get(item, 'notes')}"
            )
    else:
        lines.append("暂无疑似漏项。")

    lines.append("\n=== 已收款项汇总 ===")
    for item in paid:
        lines.append(
            f"{_row_get(item, 'project_name')}: 已收{item.get('paid_periods', 0)}期，"
            f"利息{_money_text(item.get('paid_interest'))}，本金{_money_text(item.get('paid_principal'))}，"
            f"合计{_money_text(item.get('paid_total'))}"
        )

    lines.append("\n=== 客户信息 ===")
    for c in customers:
        lines.append(
            f"{_row_get(c, 'short_name', _row_get(c, 'name'))} | "
            f"行业:{_row_get(c, 'industry')}"
        )

    context = "\n".join(lines)
    if len(context) > 14000:
        context = context[:14000] + "\n...(数据已截断)"
    with _ledger_cache_lock:
        _ledger_context_cache["value"] = context
        _ledger_context_cache["expires_at"] = time.monotonic() + 60
    return context


@app.route("/api/chat", methods=["POST"])
def ai_chat():
    allowed, retry_after = consume_rate_limit("ai_chat", request_ip(), 10, 60)
    if not allowed:
        response, status = fail("AI 请求过于频繁，请稍后重试", 429, "RATE_LIMITED")
        response.headers["Retry-After"] = str(retry_after)
        return response, status
    data = request.get_json(silent=True) or {}
    question = str(data.get("question", "")).strip()[:2000]
    if not question:
        return fail("问题不能为空")
    page_context = data.get("context") or {}
    if isinstance(page_context, dict):
        page_context_text = json.dumps(page_context, ensure_ascii=False)[:2000]
    else:
        page_context_text = str(page_context)[:2000]

    backend, backend_error = _chat_backend_config()
    if backend_error:
        return _offline_chat_response(question, "backend not configured")
    if _ai_backend_unavailable():
        return _offline_chat_response(question, "backend cooldown")

    allow_external_data = config_value("AI_ALLOW_EXTERNAL_LEDGER_DATA", "0").lower() in {"1", "true", "yes"}
    ledger_context = _ledger_context()
    target_is_private = backend["provider"] == "local" and _is_private_ai_url(backend["url"])
    if not allow_external_data and not target_is_private:
        ledger_context = "外部 AI 数据传输未授权，未附带台账明细。"
        page_context_text = ""

    system_prompt = (
        "你是皖信投资台账 AI 助手，名字叫“小信”。请只基于下方台账数据回答用户问题。"
        "如果数据中没有相关信息，明确说明“数据中暂无此信息”。回答要简洁、有条理，涉及金额、利率、"
        "到期日、项目状态时必须核对数据再回答。状态可解释为：active/进行中，completed/已完成，"
        "terminated/已终止，pending/待付，paid/已付，overdue/逾期。还款方式如有英文值，"
        "可解释为 equal_installment=等额本息，equal_principal=等额本金，interest_first=先息后本，"
        "bullet=到期还本付息。回答本月付息时，必须区分“正式台账口径”和“疑似漏项核对”；"
        "疑似漏项只能提示人工核对，不能直接并入正式应收/应付合计。"
        "用户当前界面上下文只用于理解用户指代，不可替代台账数据。\n\n"
        f"=== 当前界面上下文 ===\n{page_context_text}\n\n"
        f"=== 台账数据 ===\n{ledger_context}"
    )

    user_content = question
    if backend["provider"] == "local":
        user_content = "/no_think\n请直接给出最终答案，不要输出推理过程。\n" + question

    payload = json.dumps({
        "model": backend["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": 1500,
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        backend["url"],
        data=payload,
        headers={
            "Content-Type": "application/json",
            **backend["headers"],
        },
        method="POST",
    )
    try:
        timeout = max(2, min(int(config_value("AI_REQUEST_TIMEOUT_SECONDS", "8") or 8), 30))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        _mark_ai_backend_unavailable(f"HTTP {err.code}")
        return _offline_chat_response(question, f"HTTP {err.code}")
    except Exception as err:
        _mark_ai_backend_unavailable(err)
        return _offline_chat_response(question, err)

    answer = ((result.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
    if not answer:
        return _offline_chat_response(question, "empty response")
    with _ai_health_lock:
        _ai_backend_health["unavailable_until"] = 0.0
        _ai_backend_health["last_error"] = ""
    return ok(answer=answer, source=backend["provider"], model_online=True)


# ── 登录认证 ──────────────────────────────────────────────────
@app.route("/login")
def login_page():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    ip = request_ip()
    allowed, retry_after = consume_rate_limit("login", ip, 10, 60)
    if not allowed:
        response, status = fail("登录请求过于频繁，请稍后重试", 429, "RATE_LIMITED")
        response.headers["Retry-After"] = str(retry_after)
        return response, status
    locked, retry_after = login_lock_status(ip)
    if locked:
        response, status = fail("登录失败次数过多，请 15 分钟后再试", 429, "LOGIN_LOCKED")
        response.headers["Retry-After"] = str(retry_after)
        return response, status
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", ""))
    username = normalize_username(data.get("username"))
    conn = get_db()
    role = conn.execute(
        "SELECT * FROM roles WHERE username=? COLLATE NOCASE AND is_active=1",
        (username,),
    ).fetchone() if username else None
    conn.close()
    if not role:
        record_login_failure(ip)
        return fail("账号或密码错误", 401, "LOGIN_FAILED")
    if role["password_hash"] and check_password_hash(role["password_hash"], password):
        clear_login_failures(ip)
        session.clear()
        session.permanent = True
        session["logged_in"] = True
        session["role_id"] = role["id"]
        session["username"] = role["username"]
        session["role_key"] = role["role_key"]
        session["role_name"] = role["role_name"]
        session["person_name"] = role["person_name"]
        session["level"] = role["level"]
        session["session_version"] = int(role["session_version"] or 0)
        session["csrf_token"] = secrets.token_urlsafe(32)
        app.logger.info("Login success username=%s role_id=%s ip=%s", role["username"], role["id"], ip)
        return ok(msg="登录成功", user={
            "role_id": role["id"],
            "username": role["username"],
            "role_name": role["role_name"],
            "person_name": role["person_name"],
            "level": role["level"],
        }, csrf_token=session["csrf_token"])
    record_login_failure(ip)
    app.logger.warning("Login failed username=%s ip=%s", username or "<empty>", ip)
    return fail("账号或密码错误", 401, "LOGIN_FAILED")


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return ok(msg="已退出")


@app.route("/api/session", methods=["GET"])
def api_session():
    return ok(user={
        "role_id": session.get("role_id"),
        "username": session.get("username"),
        "role_key": session.get("role_key"),
        "role_name": session.get("role_name"),
        "person_name": session.get("person_name"),
        "level": session.get("level"),
    }, csrf_token=session.get("csrf_token"))


@app.route("/api/roles", methods=["GET", "PUT"])
def api_roles():
    conn = get_db()
    if request.method == "GET":
        rows = conn.execute("""SELECT id, role_key, username, role_name, person_name, level,
            can_approve, can_submit, is_active, sort_order
            FROM roles ORDER BY sort_order, level""").fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    data = request.get_json(silent=True) or {}
    roles = data.get("roles") or []
    requested_usernames = {}
    for item in roles:
        if not item.get("id") or "username" not in item:
            continue
        username = normalize_username(item.get("username"))
        if not valid_username(username):
            conn.close()
            return fail("账号需为 3-40 位小写字母、数字、点、下划线或短横线", 400, "INVALID_USERNAME")
        owner = requested_usernames.get(username)
        if owner and owner != item["id"]:
            conn.close()
            return fail("账号不能重复", 409, "USERNAME_EXISTS")
        requested_usernames[username] = item["id"]
    for item in roles:
        if not item.get("id"):
            continue
        username = normalize_username(item.get("username")) if "username" in item else None
        try:
            conn.execute("""UPDATE roles SET username=COALESCE(?, username), role_name=?, person_name=?,
                can_approve=?, can_submit=?, is_active=?,
                session_version=session_version + CASE
                    WHEN ? IS NOT NULL AND username <> ? COLLATE NOCASE THEN 1 ELSE 0 END
            WHERE id=?""", (
                username,
                item.get("role_name", ""),
                item.get("person_name", ""),
                1 if item.get("can_approve") else 0,
                1 if item.get("can_submit", True) else 0,
                1 if item.get("is_active", True) else 0,
                username,
                username,
                item["id"],
            ))
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.close()
            return fail("账号已被其他角色使用", 409, "USERNAME_EXISTS")
    conn.commit()
    conn.close()
    return ok(msg="角色已更新")


@app.route("/api/roles/<string:role_id>/password", methods=["PUT"])
def update_role_password(role_id):
    role, error = require_role()
    if error:
        return error
    if not is_leader(role):
        return fail("仅分管领导可重置角色密码", 403, "FORBIDDEN")
    data = request.get_json(silent=True) or {}
    new_password = str(data.get("new_password", ""))
    if len(new_password) < 12:
        return fail("新密码至少需要 12 位", 400, "WEAK_PASSWORD")
    conn = get_db()
    cur = conn.execute(
        "UPDATE roles SET password_hash=?, session_version=session_version+1 WHERE id=? AND is_active=1",
        (generate_password_hash(new_password), role_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        return fail("角色不存在", 404, "NOT_FOUND")
    return ok(msg="角色密码已重置，所有该角色的现有会话均已失效")


@app.route("/api/approvals/create", methods=["POST"])
def api_create_approval():
    data = request.get_json(silent=True) or {}
    related_type = data.get("related_type") or data.get("type")
    related_id = str(data.get("related_id") or data.get("id") or "")
    action = data.get("action")
    if related_type not in ("payment", "project") or not related_id or action not in ("confirm_payment", "settle_project"):
        return fail("审批参数不完整")
    conn = get_db()
    try:
        result, error = create_approval_record(conn, related_type, related_id, action, data.get("new_data") or {})
        if error:
            conn.rollback()
            return error
        conn.commit()
        if result.get("direct"):
            return ok(msg="已直接执行", executed=True, approval_id=result["id"])
        return ok(msg="审批已提交", approval_id=result["id"], pending_approval=True)
    finally:
        conn.close()


def approval_to_dict(row, conn=None):
    item = dict(row)
    item["route"] = json.loads(item.get("route") or "[]")
    item["current_role_ids"] = json.loads(item.get("current_role_ids") or "[]")
    item["completed_role_ids"] = json.loads(item.get("completed_role_ids") or "[]")
    item["snapshot"] = json.loads(item.get("snapshot") or "{}")
    item["new_data"] = json.loads(item.get("new_data") or "{}")
    item["action_label"] = "确认收款" if item["action"] == "confirm_payment" else "本金撤回"
    if conn is not None:
        logs = conn.execute("""SELECT reviewer, reviewer_role, reviewer_role_id, action, comment, created_at
            FROM approval_logs WHERE approval_id=? ORDER BY id ASC""", (item["id"],)).fetchall()
        item["logs"] = [dict(log) for log in logs]
    else:
        item["logs"] = []
    return item


@app.route("/api/approvals", methods=["GET"])
def api_list_approvals():
    role, error = require_role()
    if error:
        return error
    scope = request.args.get("scope", "todo")
    conn = get_db()
    if scope == "mine":
        rows = conn.execute("""SELECT * FROM approvals WHERE submitter_role_id=?
            ORDER BY created_at DESC""", (role["id"],)).fetchall()
    elif scope == "all":
        if int(role["level"] or 99) > 2:
            conn.close()
            return fail("当前角色无权查看全部审批", 403, "FORBIDDEN")
        rows = conn.execute("SELECT * FROM approvals ORDER BY created_at DESC LIMIT 200").fetchall()
    else:
        rows = conn.execute("""SELECT * FROM approvals WHERE status='pending'
            AND current_role_ids LIKE ? ORDER BY created_at DESC""", (f'%"{role["id"]}"%',)).fetchall()
    result = [approval_to_dict(r, conn) for r in rows]
    conn.close()
    return jsonify(result)


@app.route("/api/approvals/summary", methods=["GET"])
def api_approval_summary():
    role, error = require_role()
    if error:
        return error
    conn = get_db()
    todo = conn.execute("""SELECT COUNT(*) FROM approvals WHERE status='pending'
        AND current_role_ids LIKE ?""", (f'%"{role["id"]}"%',)).fetchone()[0]
    mine = conn.execute("""SELECT COUNT(*) FROM approvals WHERE submitter_role_id=?
        AND status='pending'""", (role["id"],)).fetchone()[0]
    conn.close()
    return ok(todo=todo, mine=mine)


@app.route("/api/approvals/<string:aid>/approve", methods=["POST"])
def api_approve(aid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        role, error = require_role(conn)
        if error:
            return error
        approval = conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        if not approval:
            return fail("审批单不存在", 404, "NOT_FOUND")
        if approval["status"] != "pending":
            return fail("审批单已结束", 409, "APPROVAL_CLOSED")
        current_role_ids = json.loads(approval["current_role_ids"] or "[]")
        if role["id"] not in current_role_ids:
            return fail("当前身份无权审批此单", 403, "FORBIDDEN")
        route = json.loads(approval["route"] or "[]")
        completed = set(json.loads(approval["completed_role_ids"] or "[]"))
        completed.add(role["id"])
        current_done = all(rid in completed for rid in current_role_ids)
        conn.execute("""INSERT INTO approval_logs
            (approval_id, reviewer, reviewer_role, reviewer_role_id, action, comment)
            VALUES (?,?,?,?,?,?)""", (aid, role["person_name"], role["role_name"], role["id"], "approve", data.get("comment", "")))
        if not current_done:
            conn.execute("""UPDATE approvals SET completed_role_ids=?, updated_at=datetime('now','localtime')
                WHERE id=?""", (json.dumps(list(completed), ensure_ascii=False), aid))
            conn.commit()
            return ok(msg="已通过，等待同级其他审批人")
        step_index = 0
        for i, step in enumerate(route):
            ids = [r["id"] for r in step["roles"]]
            if set(ids) == set(current_role_ids):
                step_index = i
                step["status"] = "approved"
                step["approved_role_ids"] = ids
                break
        if step_index + 1 < len(route):
            next_step = route[step_index + 1]
            next_ids = [r["id"] for r in next_step["roles"]]
            conn.execute("""UPDATE approvals SET route=?, current_level=?, current_role_ids=?,
                completed_role_ids=?, updated_at=datetime('now','localtime') WHERE id=?""",
                (json.dumps(route, ensure_ascii=False), next_step["level"], json.dumps(next_ids, ensure_ascii=False),
                 json.dumps(list(completed), ensure_ascii=False), aid))
            conn.commit()
            return ok(msg="已通过，流转至下一审批人")
        ok_exec = execute_approval_action(conn, approval)
        if not ok_exec:
            conn.rollback()
            return fail("审批通过但执行失败", 500, "EXECUTE_FAILED")
        conn.execute("""UPDATE approvals SET route=?, status='approved', executed=1,
            completed_role_ids=?, current_role_ids='[]', updated_at=datetime('now','localtime') WHERE id=?""",
            (json.dumps(route, ensure_ascii=False), json.dumps(list(completed), ensure_ascii=False), aid))
        conn.commit()
        return ok(msg="审批通过，事项已执行", executed=True)
    finally:
        conn.close()


@app.route("/api/approvals/<string:aid>/reject", methods=["POST"])
def api_reject(aid):
    data = request.get_json(silent=True) or {}
    conn = get_db()
    try:
        role, error = require_role(conn)
        if error:
            return error
        approval = conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        if not approval:
            return fail("审批单不存在", 404, "NOT_FOUND")
        current_role_ids = json.loads(approval["current_role_ids"] or "[]")
        if approval["status"] != "pending" or role["id"] not in current_role_ids:
            return fail("当前身份无权驳回此单", 403, "FORBIDDEN")
        conn.execute("""INSERT INTO approval_logs
            (approval_id, reviewer, reviewer_role, reviewer_role_id, action, comment)
            VALUES (?,?,?,?,?,?)""", (aid, role["person_name"], role["role_name"], role["id"], "reject", data.get("comment", "")))
        conn.execute("""UPDATE approvals SET status='rejected', current_role_ids='[]',
            updated_at=datetime('now','localtime') WHERE id=?""", (aid,))
        conn.commit()
        return ok(msg="已驳回")
    finally:
        conn.close()


# ── 数据备份 ──────────────────────────────────────────────────────
@app.route("/api/backup", methods=["POST"])
def create_backup():
    """创建数据库备份"""
    import shutil
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_filename = f"wanxin_backup_{timestamp}.db"
    backup_path = os.path.join(APP_DIR, "backups", backup_filename)
    
    # 确保备份目录存在
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    
    try:
        shutil.copy2(DB_PATH, backup_path)
        return ok(msg="备份成功", filename=backup_filename, path=backup_path)
    except Exception as e:
        return fail(f"备份失败: {str(e)}", 500, "BACKUP_FAILED")

@app.route("/api/backups", methods=["GET"])
def list_backups():
    """列出所有备份文件"""
    backups_dir = os.path.join(APP_DIR, "backups")
    if not os.path.exists(backups_dir):
        return ok(backups=[])
    
    backups = []
    for f in os.listdir(backups_dir):
        if f.startswith("wanxin_backup_") and f.endswith(".db"):
            path = os.path.join(backups_dir, f)
            stat = os.stat(path)
            backups.append({
                "filename": f,
                "size": stat.st_size,
                "created": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
            })
    
    backups.sort(key=lambda x: x["created"], reverse=True)
    return ok(backups=backups)

# ── 主页 ──────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    ensure_db_initialized()
    print("=" * 50)
    print("皖信投资台账管理系统启动")
    print("访问: http://localhost:8090")
    print("=" * 50)
    app.run(host="0.0.0.0", port=8090, debug=False, use_reloader=False)
