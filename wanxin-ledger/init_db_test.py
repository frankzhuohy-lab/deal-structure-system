import os, sqlite3, sys
from datetime import datetime, date

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wanxin.db')
print(f"DB Path: {DB_PATH}", flush=True)

# Remove old db
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
    print("Removed old DB", flush=True)

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys = ON")

# Create tables
db.executescript('''
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, short_name TEXT, contact TEXT, phone TEXT, email TEXT,
    industry TEXT, credit_rating TEXT, notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL, project_name TEXT NOT NULL, project_code TEXT,
    project_type TEXT, status TEXT DEFAULT '进行中',
    investment_amount REAL DEFAULT 0, annual_rate REAL DEFAULT 0,
    start_date TEXT, end_date TEXT, repayment_method TEXT DEFAULT '等额本息',
    term_months INTEGER DEFAULT 12, notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);
CREATE TABLE IF NOT EXISTS payment_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL, customer_id INTEGER NOT NULL,
    payment_date TEXT NOT NULL, payment_type TEXT DEFAULT '利息',
    principal_amount REAL DEFAULT 0, interest_amount REAL DEFAULT 0,
    total_amount REAL DEFAULT 0, status TEXT DEFAULT '待付',
    actual_payment_date TEXT, actual_amount REAL, notes TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER, project_id INTEGER, title TEXT NOT NULL,
    stage TEXT DEFAULT '线索', amount REAL DEFAULT 0, probability INTEGER DEFAULT 10,
    expected_close_date TEXT, owner TEXT, source TEXT, notes TEXT,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL, description TEXT, remind_date TEXT NOT NULL,
    remind_type TEXT DEFAULT '付息提醒', related_id INTEGER, related_type TEXT,
    status TEXT DEFAULT '未处理', priority TEXT DEFAULT '普通',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    completed_at TEXT
);
''')
print("Tables created", flush=True)

# Insert sample data
customers = [
    ('安徽中鼎控股集团', '中鼎控股', '张经理', '13800138001', 'zhang@zhongding.com', '制造业', 'AA'),
    ('合肥建工集团', '合肥建工', '李总', '13900139002', 'li@hfjg.com', '建筑业', 'A+'),
    ('芜湖海螺水泥', '海螺水泥', '王主任', '15800158003', 'wang@conch.com', '建材', 'AAA'),
    ('安徽江淮汽车', '江淮汽车', '赵部长', '13700137004', 'zhao@jac.com', '汽车', 'AA+'),
    ('铜陵有色集团', '铜陵有色', '陈经理', '13600136005', 'chen@tlsm.com', '矿业', 'AA'),
]
for c in customers:
    db.execute('INSERT INTO customers (name,short_name,contact,phone,email,industry,credit_rating) VALUES (?,?,?,?,?,?,?)', c)
print(f"Inserted {len(customers)} customers", flush=True)

projects = [
    (1, '中鼎智能制造产业园', 'ZD-2025-001', '产业园', '进行中', 50000000, 6.5, '2025-01-15', '2027-01-15', '等额本息', 24),
    (2, '合肥建工新材料基地', 'HJ-2025-002', '工厂建设', '进行中', 30000000, 7.0, '2025-03-01', '2027-03-01', '先息后本', 24),
    (3, '海螺水泥节能改造', 'HL-2025-003', '技术改造', '进行中', 20000000, 5.8, '2025-02-01', '2026-08-01', '等额本金', 18),
    (4, '江淮新能源电池工厂', 'JH-2025-004', '新能源', '进行中', 80000000, 6.0, '2025-04-01', '2028-04-01', '等额本息', 36),
    (5, '铜陵有色环保升级', 'TL-2025-005', '环保项目', '已完结', 15000000, 5.5, '2024-06-01', '2025-06-01', '先息后本', 12),
]
for p in projects:
    db.execute('INSERT INTO projects (customer_id,project_name,project_code,project_type,status,investment_amount,annual_rate,start_date,end_date,repayment_method,term_months) VALUES (?,?,?,?,?,?,?,?,?,?,?)', p)
print(f"Inserted {len(projects)} projects", flush=True)

# Generate payment schedules
def add_months(dt, months):
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, [31,29 if year%4==0 and (year%100!=0 or year%400==0) else 28,31,30,31,30,31,31,30,31,30,31][month-1])
    return dt.replace(year=year, month=month, day=day)

for p in db.execute("SELECT * FROM projects").fetchall():
    pid, cid = p['id'], p['customer_id']
    amount, rate, method, months = p['investment_amount'], p['annual_rate'], p['repayment_method'], p['term_months']
    start = p['start_date']
    if not start or not amount or not rate:
        continue
    start_dt = datetime.strptime(start, '%Y-%m-%d')
    monthly_rate = rate / 100 / 12
    
    remaining = amount
    for i in range(1, months + 1):
        pay_date = add_months(start_dt, i)
        pay_date_str = pay_date.strftime('%Y-%m-%d')
        
        if method == '先息后本':
            interest = round(amount * monthly_rate, 2)
            principal = amount if i == months else 0
        elif method == '等额本息':
            if monthly_rate > 0:
                mp = amount * monthly_rate * (1 + monthly_rate)**months / ((1 + monthly_rate)**months - 1)
            else:
                mp = amount / months
            interest = round(remaining * monthly_rate, 2)
            principal = round(mp - interest, 2)
            remaining = round(remaining - principal, 2)
        else:  # 等额本金
            mp = amount / months
            rem_i = amount - mp * (i - 1)
            interest = round(rem_i * monthly_rate, 2)
            principal = round(mp, 2)
        
        total = round(principal + interest, 2)
        status = '已付' if pay_date < datetime.now() else '待付'
        db.execute('INSERT INTO payment_schedules (project_id,customer_id,payment_date,payment_type,principal_amount,interest_amount,total_amount,status) VALUES (?,?,?,?,?,?,?,?)',
            (pid, cid, pay_date_str, '还本付息' if method != '先息后本' or i == months else '利息', round(principal,2), interest, total, status))
    print(f"  Generated {months} payments for project {pid}", flush=True)

db.commit()
print("Payment schedules committed", flush=True)

# Opportunities
opportunities = [
    (1, 1, '中鼎二期扩产融资', '洽谈中', 30000000, 40, '2025-09-01', '张经理', '老客户'),
    (2, 2, '合肥建工市政项目', '线索', 50000000, 10, '2025-12-01', '李总', '市场拓展'),
    (3, 3, '海螺水泥并购融资', '报价', 100000000, 60, '2025-08-15', '王主任', '行业推荐'),
    (4, None, '安庆石化设备更新', '线索', 40000000, 10, '2026-01-01', '新客户', '电话营销'),
    (5, 4, '江淮氢能源项目', '签约', 60000000, 90, '2025-07-01', '赵部长', '战略合作'),
    (1, None, '蚌埠光电新材料', '洽谈中', 25000000, 30, '2025-10-01', '张经理', '政府推荐'),
]
for o in opportunities:
    db.execute('INSERT INTO opportunities (customer_id,project_id,title,stage,amount,probability,expected_close_date,owner,source,sort_order) VALUES (?,?,?,?,?,?,?,?,?,?)', o)

reminders = [
    ('中鼎智造园付息', '第3期利息支付, 金额约27万', '2025-05-20', '付息提醒', 1, 'project', '未处理', '重要'),
    ('海螺水泥季度付息', '第4期利息, 金额约29万', '2025-05-25', '付息提醒', 3, 'project', '未处理', '重要'),
    ('跟进中鼎二期方案', '准备融资方案PPT', '2025-05-18', '跟进提醒', 1, 'opportunity', '未处理', '紧急'),
    ('江淮新能源项目检查', '现场巡查', '2025-06-01', '日常提醒', 4, 'project', '未处理', '普通'),
]
for r in reminders:
    db.execute('INSERT INTO reminders (title,description,remind_date,remind_type,related_id,related_type,status,priority) VALUES (?,?,?,?,?,?,?,?)', r)

db.commit()
print("All data committed", flush=True)

# Verify
count = db.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
print(f"Verification: {count} customers in DB", flush=True)
count = db.execute("SELECT COUNT(*) FROM payment_schedules").fetchone()[0]
print(f"Verification: {count} payment schedules in DB", flush=True)

db.close()
print("DONE - DB initialized successfully!", flush=True)
