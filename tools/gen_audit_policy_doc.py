#!/usr/bin/env python3
"""Generate the Audit-Evaluation & Policy/Enforcement reference Word document."""
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

OUT = r"docs/Audit_Evaluation_and_Policy_Reference.docx"

NAVY = RGBColor(0x1F, 0x38, 0x64)
HDR_FILL = "1F3864"
ALT_FILL = "EEF2F8"

doc = Document()

# ── base style: Arial 10 ──
style = doc.styles["Normal"]
style.font.name = "Arial"
style.font.size = Pt(10)

for sec in doc.sections:
    sec.top_margin = Inches(0.8); sec.bottom_margin = Inches(0.8)
    sec.left_margin = Inches(0.8); sec.right_margin = Inches(0.8)


def _shade(cell, fill):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto"); shd.set(qn("w:fill"), fill)
    tcPr.append(shd)


def _set_widths(table, widths):
    table.autofit = False
    table.allow_autofit = False
    for row in table.rows:
        for i, w in enumerate(widths):
            row.cells[i].width = w


def heading(text, size=15, space_before=10):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(space_before)
    p.paragraph_format.space_after = Pt(4)
    r = p.add_run(text); r.bold = True; r.font.size = Pt(size); r.font.color.rgb = NAVY
    return p


def body(text, size=10, italic=False, space_after=6):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    r = p.add_run(text); r.font.size = Pt(size); r.italic = italic
    return p


def make_table(headers, rows, widths):
    """rows: list of lists; a cell may be a (text, supnum) tuple to add a superscript marker."""
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr = t.rows[0].cells
    for i, h in enumerate(headers):
        hdr[i].text = ""
        p = hdr[i].paragraphs[0]
        run = p.add_run(h); run.bold = True; run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF); run.font.size = Pt(9.5)
        _shade(hdr[i], HDR_FILL)
    for ridx, row in enumerate(rows):
        cells = t.add_row().cells
        for i, val in enumerate(row):
            cells[i].text = ""
            p = cells[i].paragraphs[0]
            if isinstance(val, tuple):
                base, sup = val
                r = p.add_run(base); r.font.size = Pt(9)
                if i == 0:
                    r.bold = True; r.font.name = "Consolas"
                s = p.add_run(str(sup)); s.font.superscript = True; s.bold = True
                s.font.size = Pt(9); s.font.color.rgb = NAVY
            else:
                r = p.add_run(val); r.font.size = Pt(9)
                if i == 0:
                    r.font.name = "Consolas"
            if ridx % 2 == 1:
                _shade(cells[i], ALT_FILL)
    _set_widths(t, widths)
    return t


# ════════════════════ TITLE ════════════════════
ttl = doc.add_paragraph()
ttl.alignment = WD_ALIGN_PARAGRAPH.LEFT
r = ttl.add_run("RAI Trust Platform"); r.bold = True; r.font.size = Pt(20); r.font.color.rgb = NAVY
sub = doc.add_paragraph()
r = sub.add_run("Audit-Evaluation Schema & Policy / Enforcement Reference")
r.bold = True; r.font.size = Pt(13)
body("Developer reference. Source of truth: pap/alembic/versions/0001_initial_schema.py "
     "(+ 0002, 0011, 0017) and policy-engine/audit/sqlite_writer.py. Every evaluate() call "
     "writes one audit_evaluation_audit row, N audit_evaluation_dimensions rows, and "
     "(when an edge is derivable) one audit_interaction_events row.", italic=True, space_after=10)

# ════════════════════ 1. audit_evaluation_audit ════════════════════
heading("1.  audit_evaluation_audit  —  one row per evaluation")
body("Identifier columns carry superscript markers; their semantics for headless vs UI "
     "agentic apps are detailed in the Footnotes section (Section 3).")

audit_rows = [
    ["id", "INTEGER (PK)", "No", "Auto-increment row id; FK target for the dimensions table."],
    [("interaction_id", 1), "STRING(36) UNIQUE", "No", "UUID of this evaluation; audit primary / dedup key (INSERT OR IGNORE)."],
    [("request_id", 2), "STRING(128)", "Yes", "Cross-system correlation handle (typically the OTel trace id)."],
    [("app_id", 3), "STRING(128)", "Yes", "Governed application identity; selects which policy packs apply."],
    [("session_id", 4), "STRING(128)", "Yes", "Conversation / workflow session grouping (null = stateless)."],
    [("agent_id", 5), "STRING(128)", "Yes", "Agent/graph-node that produced the content; drives the interaction graph."],
    [("turn_number", 6), "INTEGER", "Yes", "1-based turn index within the session (advances only on user_prompt)."],
    ["role", "STRING(32)", "No", "user_prompt | llm_response | agent_to_agent | tool_call | hitl_checkpoint."],
    ["verdict", "STRING(16)", "No", "Worst dimension verdict: PERMISSIVE | NORMATIVE | COERCIVE."],
    ["violations", "JSON", "Yes", "Array of dimension violations recorded this evaluation."],
    ["remediations", "JSON", "Yes", "Array of remediation actions / messages."],
    ["fail_reason", "STRING(64)", "Yes", "Why the eval failed (e.g. timeout, db_unavailable)."],
    ["session_verdict", "STRING(16)", "Yes", "Running session-level verdict (accumulated across turns)."],
    ["session_escalated", "BOOLEAN", "No", "True if the session breached the trust floor and was escalated to HITL."],
    ["latency_ms", "FLOAT", "Yes", "Evaluation latency in milliseconds."],
    ["enforcement_action", "STRING(16)", "Yes", "Resolved action: BLOCK | WARN | ALLOW (see Section 5)."],
    ["model_id", "STRING(128)", "Yes", "LLM model identifier (from metadata.agent_tokens.model)."],
    ["input_tokens", "INTEGER", "Yes", "Prompt token count for this turn/agent."],
    ["output_tokens", "INTEGER", "Yes", "Completion token count for this turn/agent."],
    ["metadata_json", "JSON", "Yes", "Full metadata: agent_name, tool_name, source/target_agent, event_type, governance_verdict, …"],
    ["pack_trace", "JSON", "Yes", "Per-pack execution trace (which packs ran and their verdicts)."],
    ["timestamp", "DATETIME", "No", "Naive-UTC ISO-8601 timestamp of the evaluation."],
    ["content_snippet", "TEXT", "Yes", "First N chars of evaluated content (only if STORE_CONTENT_SNIPPETS=true)."],
    ["content_hash", "VARCHAR(64)", "Yes", "SHA-256 of the full content (dedup / external lookup)."],
    ["arize_trace_url", "VARCHAR(512)", "Yes", "Placeholder for an Arize Phoenix deep-link."],
    [("enterprise_id", 7), "STRING(128)", "Yes", "Tenant / enterprise id for multi-tenant deployments."],
]
W = [Inches(1.45), Inches(1.35), Inches(0.55), Inches(3.55)]
make_table(["Attribute", "Type", "Null", "Description"], audit_rows, W)
body("Indexes: interaction_id, app_id, session_id, verdict, timestamp, enterprise_id (+ a "
     "functional index on json_extract(metadata_json,'$.function_name')).  Unique: interaction_id.",
     size=8.5, italic=True, space_after=8)

# ════════════════════ 2. audit_evaluation_dimensions ════════════════════
heading("2.  audit_evaluation_dimensions  —  one row per dimension scored")
dim_rows = [
    ["id", "INTEGER (PK)", "No", "Row id."],
    ["evaluation_audit_id", "INTEGER (FK)", "No", "-> audit_evaluation_audit.id (CASCADE delete). The 1:N join."],
    ["dimension_id", "STRING(64)", "No", "The evaluated dimension (e.g. hate, pii, misconduct, sanctions)."],
    ["score", "INTEGER", "Yes", "Raw 1-9 guardrail score (null if the evaluator failed)."],
    ["score_normalized", "FLOAT", "Yes", "(score - 1) / 8.0  ->  0.0 (risky) … 1.0 (safe)."],
    ["status", "STRING(16)", "No", "PASS (permissive) | WARNING (normative) | FLAGGED (coercive)."],
    ["provider", "STRING(64)", "Yes", "Evaluator provider: prompt_guard, llm_judge, embedding, mock, sentinel."],
    ["is_violation", "BOOLEAN", "No", "True when status is WARNING or FLAGGED."],
    ["policy_id", "INTEGER", "Yes", "Soft ref to the config_policies row that produced the verdict."],
]
make_table(["Attribute", "Type", "Null", "Description"], dim_rows, W)
body("Unique: (evaluation_audit_id, dimension_id) — one result per dimension per evaluation.",
     size=8.5, italic=True, space_after=8)

# ════════════════════ 3. FOOTNOTES ════════════════════
heading("3.  Footnotes  —  Identifier semantics for headless vs UI agentic apps")
foot = [
    ("1", "interaction_id",
     "PE-generated UUID v4, one per evaluate() call. ALWAYS present for both modes — every "
     "governed action (a UI chat turn, or a single headless job/agent step) yields exactly one "
     "interaction_id. It is the audit primary / dedup key and is never supplied by the caller."),
    ("2", "request_id",
     "Caller-supplied cross-system correlation handle, typically the OpenTelemetry trace id; the "
     "SDK auto-fills a UUID if omitted. Present for both modes. UI apps carry the browser "
     "request/trace; HEADLESS apps SHOULD pass their job-run / pipeline trace id so platform "
     "audit rows line up with the app's own logs."),
    ("3", "app_id",
     "The governed application's identity, set once at instrument(app_id=...). Identical for "
     "headless and UI apps and ALWAYS present — it is what selects which policy packs apply."),
    ("4", "session_id",
     "OPTIONAL; groups turns into one stateful conversation/workflow, enabling cross-turn score "
     "accumulation and session-level escalation. UI agentic apps: one per chat conversation. "
     "HEADLESS agentic apps with no conversation may OMIT it — the PE then evaluates statelessly "
     "(per-call only; no session_verdict/summary) — OR pass a meaningful unit-of-work id (a "
     "job-run id, a workflow-execution id) to get cross-step accumulation without it being a "
     "literal 'chat'. The PE never fabricates a session_id; its absence is meaningful (stateless mode)."),
    ("5", "agent_id",
     "The agent / graph-node that produced the content (e.g. intl_wire_agent, coordinator). Drives "
     "the interaction-graph source/target edges. Present whenever the SDK can attribute the "
     "producing node; for a headless single-agent app it is that agent's name."),
    ("6", "turn_number",
     "OPTIONAL 1-based turn index within a session; the SDK stamps it and it advances ONLY on a "
     "user_prompt (the tool_call / agent_to_agent / llm_response events of that turn share the same "
     "number). NULL for stateless/headless calls with no session. UI: the conversation turn; "
     "HEADLESS: the workflow step index when a session/run id is supplied, else null."),
    ("7", "enterprise_id",
     "OPTIONAL tenant / enterprise id for multi-tenant deployments; orthogonal to headless-vs-UI. "
     "Set it to segregate audit and policy by customer / tenant."),
]
for num, name, txt in foot:
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(5)
    s = p.add_run(num + "  "); s.bold = True; s.font.superscript = False; s.font.color.rgb = NAVY; s.font.size = Pt(9.5)
    n = p.add_run(name + " — "); n.bold = True; n.font.name = "Consolas"; n.font.size = Pt(9.5)
    b = p.add_run(txt); b.font.size = Pt(9.5)

# ════════════════════ 4. POLICY & THRESHOLD ════════════════════
heading("4.  Policy & threshold configuration")
body("Thresholds are defined in TWO places, with a clear runtime hierarchy: named threshold "
     "profiles are primary; inline policy bands are the fallback.")
make_table(
    ["Mechanism (table)", "How thresholds are defined", "How attached to a policy", "Reusable / independent?"],
    [
        ["config_threshold_profiles", "profile_id + 6 band columns (permissive/normative/coercive lo+hi) + a runtime step-map",
         "Referenced by profile_id from config_pack_dimensions and config_dimension_overrides",
         "YES — primary model; reusable across dimensions/packs; supports step adjustments"],
        ["config_policies", "6 inline band columns on each (dimension, app_id) row",
         "Inline on the policy row itself", "NO — fallback, used only for dimensions not in any pack"],
    ],
    [Inches(1.6), Inches(2.2), Inches(1.9), Inches(1.2)])
body("Score -> verdict (band lookup is identical in both mechanisms; default bands shown):", space_after=4)
make_table(
    ["Band", "Default score range", "Verdict", "Meaning"],
    [
        ["Permissive", "1 – 4", "PERMISSIVE", "Proceed."],
        ["Normative", "5 – 6", "NORMATIVE", "Proceed with warning / revise / optional HITL pause."],
        ["Coercive", "7 – 9", "COERCIVE", "Block (most-restrictive)."],
    ],
    [Inches(1.4), Inches(1.7), Inches(1.5), Inches(2.3)])

# ════════════════════ 5. ENFORCEMENT ════════════════════
heading("5.  Enforcement configuration")
body("Enforcement is COMPOSED at request time (not a single policy column). These inputs combine "
     "into the audit row's enforcement_action.")
make_table(
    ["Setting", "Table.column", "Values", "Effect"],
    [
        ["App enforcement mode", "config_apps.enforcement_mode", "BLOCK | WARN | ALLOW", "App-wide default for how a COERCIVE verdict is acted on."],
        ["Pack enforcement", "config_policy_packs.enforcement", "(pack-defined)", "Per-pack enforcement layer applied to that pack's verdicts."],
        ["Normative action", "config_policies.normative_action", "REVISION | HITL", "REVISION = agent self-corrects (WARN); HITL = pause for human review."],
        ["HITL trigger", "config_policy_packs.hitl_trigger", "true | false", "Pack escalates to human review on a non-PERMISSIVE verdict."],
        ["Resolved action", "audit_evaluation_audit.enforcement_action", "BLOCK | WARN | ALLOW", "Final action recorded on the audit row (computed by ChainExecutor + MRA)."],
    ],
    [Inches(1.4), Inches(2.25), Inches(1.35), Inches(2.4)])
body("Verdict -> action (by app enforcement_mode):", space_after=4)
make_table(
    ["Verdict", "enforcement_mode = BLOCK", "= WARN", "= ALLOW"],
    [
        ["PERMISSIVE", "ALLOW", "ALLOW", "ALLOW"],
        ["NORMATIVE", "WARN  (HITL pause if normative_action=HITL or hitl_trigger)", "WARN", "ALLOW (logged)"],
        ["COERCIVE", "BLOCK  (SDK raises GovernanceBlock)", "WARN", "ALLOW (logged)"],
    ],
    [Inches(1.2), Inches(3.0), Inches(1.2), Inches(2.0)])

# ════════════════════ 6. PACK MODE: collect-all / abort ════════════════════
heading("6.  Pack evaluation mode  —  collect-all & abort")
make_table(
    ["Config", "Table.column", "Values (default)", "Semantics"],
    [
        ["Pack mode", "config_policy_packs.mode", "collect-all (default) | fail-fast",
         "collect-all: evaluate EVERY dimension in the pack, then combine — full visibility, all "
         "violations recorded. fail-fast: stop at the first COERCIVE."],
        ["Per-dimension abort", "config_pack_dimensions.abort", "true | false (false)",
         "If true, a COERCIVE on this dimension ABORTS the remaining dimensions in the pack even in "
         "collect-all mode — lets a critical dimension short-circuit."],
        ["Combining algorithm", "config_policy_packs.combining_algorithm", "worst_case (default)",
         "How per-dimension verdicts combine into the pack verdict — worst_case = most-restrictive-wins (MRA)."],
        ["Pack layer", "config_policy_packs.layer", "mandatory | domain",
         "mandatory packs always run first; a COERCIVE there can abort the whole chain before "
         "app-assigned (domain) packs run."],
    ],
    [Inches(1.5), Inches(2.25), Inches(1.55), Inches(2.1)])

body("Runtime order: mandatory packs -> app-assigned active packs (config_app_pack_assignments) "
     "-> per-dimension threshold profile resolved (with config_dimension_overrides + step "
     "adjustments) -> dimensions combined (worst_case) -> verdict -> enforcement_action.",
     size=9, italic=True, space_after=4)

doc.save(OUT)
print("WROTE", OUT)
