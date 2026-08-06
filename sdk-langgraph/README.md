# rai-sdk-langgraph

RAI Trust SDK — LangGraph + OTel distribution.

Single install, single import, full governance for LangGraph applications.

```bash
pip install rai-sdk-langgraph
```

```python
from rai_sdk_langgraph import instrument, EnforcementMode

app = instrument(
    app,
    base_url="http://localhost:3000",
    app_id="myapp",
    enforcement_mode=EnforcementMode.REDACT,
    otel_endpoint="http://localhost:4317",
)
```
