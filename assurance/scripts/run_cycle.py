import os
os.environ.setdefault("ASSURANCE_PROVIDER", "arize")

from metrics.tier2metrics import run_cycle

results = run_cycle(since_hours=1)
for r in results:
    print(r.metric_name, r.value, r.status, "spans=" + str(r.span_count))
