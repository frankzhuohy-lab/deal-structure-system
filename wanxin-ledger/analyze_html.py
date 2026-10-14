"""Analyze index.html: check for missing functions and UI issues"""
with open('templates/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Check for essential functions
essential_funcs = [
    'loadDashboard', 'loadClients', 'loadProjects', 'loadSchedule',
    'loadOpportunities', 'loadReminders', 'showImport', 'doImport',
    'exportCSV', 'exportExcel', 'openProjModal', 'saveProject',
    'openClientModal', 'saveClient', 'paySchedule', 'showDetail',
    'loadAuditLog', 'loadCalendar', 'createBackup'
]

print("=== 函数检查 ===")
for func in essential_funcs:
    found = f'function {func}' in content or f'async function {func}' in content or f'{func}=' in content
    tag = 'OK' if found else 'MISSING!'
    print(f"  [{tag}] {func}")

# Check for essential HTML elements
essential_elements = [
    'page-dashboard', 'page-projects', 'page-clients', 'page-schedule',
    'page-opportunities', 'page-reminders', 'page-calendar', 'page-audit',
    'projModal', 'clientModal', 'importModal', 'payModal', 'detailModal'
]

print("\n=== HTML元素检查 ===")
for elem in essential_elements:
    found = f'id="{elem}"' in content
    tag = 'OK' if found else 'MISSING!'
    print(f"  [{tag}] #{elem}")

# Check CSS variables
print("\n=== CSS变量检查 ===")
for var in ['--bg', '--card', '--accent', '--radius', '--shadow', '--sidebar-w']:
    found = var in content
    tag = 'OK' if found else 'MISSING!'
    print(f"  [{tag}] {var}")

# Check sidebar background
import re
sidebar_match = re.search(r'\.sidebar\{[^}]*background:([^;]+)', content)
if sidebar_match:
    print(f"\n=== 侧边栏背景: {sidebar_match.group(1)} ===")

# Check border-radius usage
radius_count = content.count('border-radius')
print(f"\n=== border-radius 使用次数: {radius_count} ===")

# File size
print(f"\n=== 文件大小: {len(content)} 字符, {content.count(chr(10))} 行 ===")
