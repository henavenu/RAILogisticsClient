import json, sqlite3
conn = sqlite3.connect(r"C:\Users\shara\Cursor Projects\rai-trust-platform\pap\config\pap.db")
conn.row_factory = sqlite3.Row
row = conn.execute(
    "SELECT config_json, seeded_from FROM config_service_configs "
    "WHERE service_name='pe' AND config_key='tool_risk'"
).fetchone()
if not row:
    print("ERROR: pe/tool_risk not found!")
else:
    data = json.loads(row["config_json"])
    tools = data.get("tools", {})
    high = [k for k, v in tools.items() if v.get("risk_level") == "HIGH"]
    low  = [k for k, v in tools.items() if v.get("risk_level") == "LOW"]
    print(f"pe/tool_risk blob: {len(tools)} tools")
    print(f"  HIGH ({len(high)}): {high}")
    print(f"  LOW  ({len(low)}): {low}")
    print(f"  seeded_from: {row['seeded_from']}")
conn.close()
