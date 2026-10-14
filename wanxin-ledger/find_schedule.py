import re
with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()
# Find schedule route and its function
match = re.search(r"@app\.route\('/api/schedule'.*?\ndef (.*?)(?=\n@app|\nif __name__)", content, re.DOTALL)
if match:
    print(match.group(0)[:2000])
