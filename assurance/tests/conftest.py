from __future__ import annotations

import os

os.environ.setdefault("ASSURANCE_PROVIDER", "mock")
os.environ.setdefault("STORE_BACKEND", "sqlite")
os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("ARIZE_API_KEY", "")
os.environ.setdefault("ARIZE_SPACE_ID", "")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("EVAL_FAIRNESS", "false")
os.environ.setdefault("EVAL_SAFETY", "false")
os.environ.setdefault("EVAL_RELIABILITY", "false")
os.environ.setdefault("EVAL_QUALITY", "false")
os.environ.setdefault("EVAL_PRIVACY", "false")
os.environ.setdefault("BIAS_AGENT_PROVIDER", "mock")
