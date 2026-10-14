import urllib.request
import json
try:
    resp = urllib.request.urlopen('http://localhost:8090/api/dashboard', timeout=5)
    data = json.loads(resp.read())
    print(f"Status: {resp.status}")
    print(f"Customers: {data.get('customer_count', 'N/A')}")
    print(f"Projects: {data.get('project_count', 'N/A')}")
    print(f"Active Projects: {data.get('active_projects', 'N/A')}")
    print(f"Opps: {data.get('opp_count', 'N/A')}")
    print("Server is running correctly!")
except Exception as e:
    print(f"Error: {e}")
