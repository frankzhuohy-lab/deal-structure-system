import re
with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()
# Find all route definitions
routes = re.findall(r"@app\.route\('([^']+)'", content)
for r in routes:
    print(r)
