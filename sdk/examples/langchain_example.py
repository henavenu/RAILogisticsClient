"""
Example: RAI Trust SDK with LangChain

Shows zero-touch governance via RaiTrustCallbackHandler.
The handler evaluates:
  • USER_PROMPT  — before the LLM sees the input  (outer edge)
  • LLM_RESPONSE — after the LLM produces output  (outer edge)

A COERCIVE verdict raises GovernanceBlock, stopping the chain.

Install:
    pip install langchain-openai
    pip install 'rai-trust-sdk[langchain]'
"""
import os

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from rai_trust_sdk import GovernanceBlock, TrustClient
from rai_trust_sdk.langchain import RaiTrustCallbackHandler
from rai_trust_sdk.types import ContentRole, EvalMode

# ── 1. Trust client ────────────────────────────────────────────────────────────

client = TrustClient(
    base_url=os.environ.get("TRUST_PE_URL", "http://localhost:3000"),
    app_id="finapp",
)

# ── 2. Callback handler ────────────────────────────────────────────────────────

handler = RaiTrustCallbackHandler(
    client=client,
    # Default roles: USER_PROMPT + LLM_RESPONSE (outer edges only)
    outer_only=True,
    eval_mode=EvalMode.SYNC,          # raises GovernanceBlock on COERCIVE
    on_result=lambda r, role: print(f"[Trust] role={role.value} verdict={r.verdict.value}"),
)

# ── 3. Chain ───────────────────────────────────────────────────────────────────

llm    = ChatOpenAI(model="gpt-4o-mini")
prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a financial assistant."),
    ("human",  "{input}"),
])
chain = prompt | llm

# ── 4. Invoke with governance ──────────────────────────────────────────────────

try:
    result = chain.invoke(
        {"input": "What is the maximum daily wire transfer limit?"},
        config={"callbacks": [handler]},
    )
    print("Response:", result.content)

except GovernanceBlock as block:
    print(f"Blocked by governance: {block}")
    print(f"Violations: {block.result.violations}")
    print(f"Remediations: {block.result.remediations}")
