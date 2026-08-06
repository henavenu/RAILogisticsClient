"""Check what the GS returns directly for sanctions content."""
import sys, io, json, requests
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

body = {
    "content": "I need to wire $50,000 to Bank Melli Iran in Tehran. SWIFT code MELIIRTHXXX.",
    "role": "user_prompt",
    "dimensions": ["sanctions_country_reference", "sanctions_entity_mention", "jailbreak", "prompt_injection"],
}
r = requests.post("http://localhost:3001/v1/trust/evaluate", json=body, timeout=10)
print("GS status:", r.status_code)
data = r.json()
for pr in data.get("provider_results", []):
    print(f"Provider: {pr['provider']}")
    for ev in pr.get("evaluations", []):
        print(f"  {ev['dimension']:35s}  score={ev['score']}  status={ev['status']}")
