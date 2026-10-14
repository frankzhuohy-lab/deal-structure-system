# -*- coding: utf-8 -*-
"""按季度重新生成所有项目的付息计划。"""
import math
import sqlite3
from datetime import datetime

DB = r"C:\Users\frank\Desktop\皖信台账系统\wanxin.db"


def add_months(dt, months):
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return dt.replace(year=year, month=month, day=min(dt.day, days[month - 1]))


def normalize_rate(rate):
    value = float(rate or 0)
    return value / 100 if value > 1 else value


def calc_quarterly_interest(principal, annual_rate, months, start_date):
    schedules = []
    if not start_date or principal <= 0 or months <= 0:
        return schedules

    annual_rate = normalize_rate(annual_rate)
    start = datetime.strptime(start_date, "%Y-%m-%d")
    periods = math.ceil(months / 3)
    elapsed = 0

    for period in range(1, periods + 1):
        months_this_period = min(3, months - elapsed)
        elapsed += months_this_period
        due_date = add_months(start, elapsed)
        principal_part = round(principal, 2) if period == periods else 0.0
        interest = round(principal * annual_rate * months_this_period / 12, 2)
        total = round(principal_part + interest, 2)
        schedules.append({
            "period": period,
            "due_date": due_date.strftime("%Y-%m-%d"),
            "principal": principal_part,
            "interest": interest,
            "total": total,
        })
    return schedules


def regenerate():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("DELETE FROM payment_schedules")
    print("已清空旧付息记录")

    projects = conn.execute("SELECT * FROM projects WHERE id IS NOT NULL ORDER BY id").fetchall()
    total = 0

    for p in projects:
        pid = p["id"]
        name = p["project_name"]
        principal = float(p["investment_amount"] or 0)
        rate = normalize_rate(p["annual_rate"] or 0)
        months = int(p["loan_term_months"] or 0)
        start_date = p["start_date"]

        if not start_date or principal <= 0 or rate <= 0 or months <= 0:
            print(f"  跳过 {pid}: {name[:20]} - 缺起始日期/金额/利率/期限")
            continue

        schedules = calc_quarterly_interest(principal, rate, months, start_date)
        print(f"  计算 {pid}: {name[:20]}  {principal:>10.0f}元 {rate:.2%} {months}月 -> {len(schedules)}期")

        for s in schedules:
            sid = f"{pid}-{s['period']:03d}"
            conn.execute("""INSERT INTO payment_schedules
                (id, project_id, period_number, due_date, principal_amount, interest_amount, total_amount, status)
                VALUES (?,?,?,?,?,?,?,'pending')""",
                (sid, pid, s["period"], s["due_date"], s["principal"], s["interest"], s["total"]))
            total += 1

    conn.commit()
    conn.close()
    print(f"\n完成！共生成 {total} 条季度付息记录")


if __name__ == "__main__":
    regenerate()
