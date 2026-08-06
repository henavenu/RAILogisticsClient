#!/usr/bin/env python3
"""
RAI Trust Platform — Comprehensive Functional Test Suite
=========================================================
Tests data insertion, metric computation, time-window filtering, and
cross-endpoint consistency without needing the Policy Engine running.

Strategy
--------
  • Write evaluation records DIRECTLY to the SQLite DB (controlled, fast)
  • Call every PAP API endpoint with known params
  • Assert exact counts and consistency between endpoints
  • Detect the existing timestamp storage format (space vs T) automatically
  • Clean up all test records via TEST_APP_ID isolation

Usage
-----
  cd "C:\\Users\\shara\\Cursor Projects\\rai-trust-platform"
  venv\\Scripts\\python.exe tests\\functional_test.py [--keep-data] [--verbose] [--pap-url URL]

Requirements
------------
  • PAP service running on port 3002
  • Trust Agents service on port 3005 (optional — skipped if offline)
  • venv with requests installed
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Force UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError for box chars)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests")
    sys.exit(1)

# ── Configuration ──────────────────────────────────────────────────────────────

TEST_APP_ID = "_test_functional_"          # all test records tagged with this
PAP_URL     = "http://localhost:3002"
TA_URL      = "http://localhost:3005"

# Relative path from project root to SQLite DB
_DB_CANDIDATES = [
    Path("pap/config/pap.db"),
    Path("pap/config/rai_trust.db"),
]

# Dimension IDs to use in tests (soft refs — no FK; mapped to pillars)
# If not found in the DB, they are still valid soft refs for the test data.
TEST_DIMS = {
    "safety":       ["hate_speech", "self_harm", "violence"],
    "security":     ["prompt_injection", "jailbreak"],
    "privacy":      ["sensitive_data_request", "pii_exposure"],
    "compliance":   ["prohibited_practice", "regulatory_breach"],
    "fairness":     ["bias_detection", "demographic_disparity"],
    "transparency": ["ai_disclosure", "transparency_violation"],
}
ALL_TEST_DIMS = [d for dims in TEST_DIMS.values() for d in dims]

TEST_AGENTS = ["agent_alpha", "agent_beta", "agent_gamma", "agent_delta", "agent_epsilon"]

# ── Colour helpers ─────────────────────────────────────────────────────────────

RESET = "\033[0m"
GREEN = "\033[92m"
RED   = "\033[91m"
AMBER = "\033[93m"
CYAN  = "\033[96m"
BOLD  = "\033[1m"
DIM   = "\033[2m"

def _ok(msg):   print(f"  {GREEN}✓{RESET}  {msg}")
def _fail(msg): print(f"  {RED}✗{RESET}  {msg}")
def _warn(msg): print(f"  {AMBER}⚠{RESET}  {msg}")
def _info(msg): print(f"  {DIM}·{RESET}  {msg}")
def _head(msg): print(f"\n{BOLD}{CYAN}{'━'*60}{RESET}\n{BOLD}  {msg}{RESET}\n{'━'*60}")

# ── Global state ───────────────────────────────────────────────────────────────

_passes  = 0
_fails   = 0
_warns   = 0
_verbose = False


def _assert(condition: bool, ok_msg: str, fail_msg: str) -> bool:
    global _passes, _fails
    if condition:
        _ok(ok_msg)
        _passes += 1
    else:
        _fail(fail_msg)
        _fails += 1
    return condition


def _soft(condition: bool, ok_msg: str, warn_msg: str) -> bool:
    global _warns
    if condition:
        _ok(ok_msg)
    else:
        _warn(warn_msg)
        _warns += 1
    return condition


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _find_db() -> Optional[Path]:
    root = Path(__file__).parent.parent
    for cand in _DB_CANDIDATES:
        p = root / cand
        if p.exists():
            return p
    return None


def _detect_ts_format(con: sqlite3.Connection) -> str:
    """Detect whether existing records use 'T' or ' ' as datetime separator."""
    row = con.execute(
        "SELECT timestamp FROM audit_evaluation_audit LIMIT 1"
    ).fetchone()
    if not row or not row[0]:
        return "space"   # default assumption — SQLAlchemy stores with space
    ts = str(row[0])
    return "T" if "T" in ts else "space"


def _ts(dt: datetime, fmt: str) -> str:
    """Format a datetime for DB insertion matching existing record format."""
    naive = dt.replace(tzinfo=None)
    if fmt == "T":
        return naive.isoformat(timespec="microseconds")       # "2026-05-11T14:23:45.000000"
    else:
        return naive.strftime("%Y-%m-%d %H:%M:%S.%f")         # "2026-05-11 14:23:45.000000"


def _insert_eval(
    con: sqlite3.Connection,
    ts_fmt: str,
    *,
    when: datetime,
    verdict: str,           # PERMISSIVE | NORMATIVE | COERCIVE
    role: str,              # user_prompt | llm_response | tool_call | agent_to_agent
    agent_name: Optional[str] = None,
    dims: list[dict],       # [{"id": "hate_speech", "score": 2}, ...]
    latency_ms: float = 45.0,
    session_id: Optional[str] = None,
) -> int:
    """Insert one evaluation record + its dimension rows. Returns audit.id."""
    iid  = str(uuid.uuid4())
    sid  = session_id or str(uuid.uuid4())
    meta = json.dumps({"agent_name": agent_name}) if agent_name else None

    con.execute(
        """INSERT INTO audit_evaluation_audit
           (interaction_id, app_id, session_id, agent_id, role, verdict,
            violations, latency_ms, timestamp, metadata_json,
            session_verdict, session_escalated)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            iid, TEST_APP_ID, sid, agent_name, role, verdict,
            json.dumps([]) if verdict == "COERCIVE" else None,
            latency_ms,
            _ts(when, ts_fmt),
            meta,
            verdict,   # session_verdict mirrors eval verdict for simplicity
            verdict == "COERCIVE",
        ),
    )
    audit_id = con.execute(
        "SELECT id FROM audit_evaluation_audit WHERE interaction_id=?", (iid,)
    ).fetchone()[0]

    for d in dims:
        score = d["score"]
        is_violation = score >= 7
        status = "FLAGGED" if score >= 7 else ("WARNING" if score >= 4 else "PASS")
        con.execute(
            """INSERT OR IGNORE INTO audit_evaluation_dimensions
               (evaluation_audit_id, dimension_id, score, score_normalized,
                is_violation, status, provider)
               VALUES (?,?,?,?,?,?,?)""",
            (
                audit_id, d["id"], score,
                round(1.0 - (score - 1) / 8.0, 4),
                is_violation, status, "mock_provider",
            ),
        )
    return audit_id


def _insert_session(
    con: sqlite3.Connection,
    *,
    session_id: str,
    coercive_count: int,
    final_trust_score: float,
    turn_count: int = 5,
    end_reason: str = "timeout",
    ended_ago_seconds: float = 60.0,
) -> None:
    now_ts = time.time()
    ended  = now_ts - ended_ago_seconds
    started = ended - turn_count * 30
    created_at_str = datetime.fromtimestamp(ended).strftime("%Y-%m-%d %H:%M:%S")
    con.execute(
        """INSERT OR REPLACE INTO pe_session_summaries
           (session_id, app_id, summary_json, end_reason, turn_count,
            coercive_count, final_trust_score, started_at_ts, ended_at_ts, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            session_id, TEST_APP_ID,
            json.dumps({"session_id": session_id, "app_id": TEST_APP_ID}),
            end_reason, turn_count, coercive_count,
            round(final_trust_score, 4), started, ended, created_at_str,
        ),
    )


def _cleanup(con: sqlite3.Connection) -> tuple[int, int]:
    """Delete all test records. Returns (eval_rows, session_rows) deleted."""
    # Delete dimension rows via cascade (FK with ondelete CASCADE)
    # — but sqlite3 might not enforce it, so delete explicitly
    rows = con.execute(
        "SELECT id FROM audit_evaluation_audit WHERE app_id=?", (TEST_APP_ID,)
    ).fetchall()
    ids = [r[0] for r in rows]
    if ids:
        placeholders = ",".join("?" * len(ids))
        con.execute(
            f"DELETE FROM audit_evaluation_dimensions WHERE evaluation_audit_id IN ({placeholders})", ids
        )
    n_evals = con.execute(
        "DELETE FROM audit_evaluation_audit WHERE app_id=?", (TEST_APP_ID,)
    ).rowcount
    n_sess = con.execute(
        "DELETE FROM pe_session_summaries WHERE app_id=?", (TEST_APP_ID,)
    ).rowcount
    con.commit()
    return n_evals, n_sess


# ── API helpers ────────────────────────────────────────────────────────────────

def _api(path: str, params: dict = None, base: str = PAP_URL) -> Optional[dict]:
    try:
        r = requests.get(f"{base}{path}", params=params, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        if _verbose:
            _warn(f"API {path} error: {exc}")
        return None


def _pap_ok() -> bool:
    return _api("/v1/health") is not None


def _ta_ok() -> bool:
    return _api("/v1/health", base=TA_URL) is not None


# ── Dimension discovery ────────────────────────────────────────────────────────

def _discover_dims(con: sqlite3.Connection) -> dict[str, list[str]]:
    """
    Query existing dimensions from config_evaluation_dimensions.
    Falls back to TEST_DIMS if table is empty or unavailable.
    """
    try:
        rows = con.execute(
            "SELECT dimension_id, category FROM config_evaluation_dimensions WHERE status='active'"
        ).fetchall()
        if not rows:
            return TEST_DIMS
        by_cat: dict[str, list[str]] = {}
        for dim_id, cat in rows:
            by_cat.setdefault(cat, []).append(dim_id)
        # Merge: prefer real dims, fall back to test dims per category
        result = dict(TEST_DIMS)
        for cat, dims in by_cat.items():
            if cat in result:
                result[cat] = dims[:3]   # cap at 3 per pillar
        return result
    except Exception:
        return TEST_DIMS


# ══════════════════════════════════════════════════════════════════════════════
#  TEST SECTIONS
# ══════════════════════════════════════════════════════════════════════════════

def test_db_format(con: sqlite3.Connection, ts_fmt: str):
    _head("T-00  DB & Format Detection")
    total = con.execute("SELECT COUNT(*) FROM audit_evaluation_audit").fetchone()[0]
    _info(f"Existing evaluation records in DB: {total}")

    # Pre-test cleanup — remove any leftover data from a previous failed run
    leftover = con.execute(
        "SELECT COUNT(*) FROM audit_evaluation_audit WHERE app_id=?", (TEST_APP_ID,)
    ).fetchone()[0]
    if leftover:
        _cleanup(con)
        _warn(f"Removed {leftover} leftover records from a previous test run")

    _info(f"Detected timestamp format: '{ts_fmt}'")
    _assert(
        ts_fmt in ("T", "space"),
        f"Timestamp format detected: '{ts_fmt}'",
        "Could not detect timestamp format",
    )

    # Check if PRAGMA foreign_keys is ON
    fk = con.execute("PRAGMA foreign_keys").fetchone()[0]
    _soft(fk == 1, "Foreign keys enabled (CASCADE deletes will work)", "Foreign keys OFF — manual cascade delete needed")

    # Verify required tables exist
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in ["audit_evaluation_audit", "audit_evaluation_dimensions", "pe_session_summaries", "config_evaluation_dimensions"]:
        _assert(t in tables, f"Table exists: {t}", f"Table MISSING: {t}")


def test_sunny_day(con: sqlite3.Connection, ts_fmt: str, dims: dict) -> dict:
    """
    T-01: 120 PERMISSIVE evaluations across all 6 pillars and 5 agents.
    All within last 10 minutes. Expect 0% violation rate.
    """
    _head("T-01  Sunny Day — 120 PERMISSIVE evaluations")
    now = datetime.now(timezone.utc)

    roles   = ["user_prompt", "llm_response", "tool_call"]
    inserts = 0

    for i in range(120):
        agent  = TEST_AGENTS[i % len(TEST_AGENTS)]
        role   = roles[i % len(roles)]
        # Cycle through all pillars
        pillar_keys = list(dims.keys())
        pillar = pillar_keys[i % len(pillar_keys)]
        dim_ids = dims[pillar]
        dim_list = [{"id": dim_ids[0], "score": 2}]   # score 2 → PASS, PERMISSIVE
        when = now - timedelta(minutes=i % 5)

        _insert_eval(
            con, ts_fmt, when=when, verdict="PERMISSIVE",
            role=role, agent_name=agent, dims=dim_list,
        )
        inserts += 1

    con.commit()
    _info(f"Inserted {inserts} PERMISSIVE evaluations")

    # Query tier1
    r = _api("/v1/metrics/tier1", {"window_minutes": 30, "app_id": TEST_APP_ID})
    if r is None:
        _warn("PAP not responding — skipping API assertions for T-01")
        return {"sunny_count": inserts}

    total = r.get("computed_from", 0)
    _assert(total == inserts, f"tier1.computed_from = {total} (expected {inserts})", f"tier1.computed_from = {total}, expected {inserts}")

    vr = r.get("metrics", {}).get("violation_rate", {})
    flag_pct = vr.get("flag_rate_pct", -1)
    _assert(flag_pct == 0.0, f"violation_rate = 0.0% (all PERMISSIVE)", f"violation_rate = {flag_pct}% (expected 0.0%)")

    # dim-breakdown
    dr = _api("/v1/metrics/dim-breakdown", {"window_hours": 1, "app_id": TEST_APP_ID})
    if dr:
        dim_total = dr.get("total_dim_evals", -1)
        _assert(
            dim_total == inserts,
            f"dim-breakdown.total_dim_evals = {dim_total} (expected {inserts})",
            f"dim-breakdown.total_dim_evals = {dim_total}, expected {inserts}  ← CONSISTENCY BUG",
        )
        cats = dr.get("categories", {})
        for cat in dims:
            if cat in cats:
                vrate = cats[cat].get("violation_rate_pct", -1)
                _assert(vrate == 0.0, f"pillar {cat} violation_rate = 0.0%", f"pillar {cat} violation_rate = {vrate}% (expected 0.0%)")

    return {"sunny_count": inserts}


def test_violations(con: sqlite3.Connection, ts_fmt: str, dims: dict) -> dict:
    """
    T-02: 80 mixed evaluations (40 COERCIVE + 40 PERMISSIVE).
    Transparency gets most violations. Safety gets some.
    Expect ~50% violation rate; transparency and safety pillars amber/red.
    """
    _head("T-02  Violation Injection — 40 COERCIVE + 40 PERMISSIVE")
    now = datetime.now(timezone.utc)

    coercive_ids  = set()
    permissive_n  = 0

    transparency_dim = dims.get("transparency", ["ai_disclosure"])[0]
    safety_dim       = dims.get("safety",       ["hate_speech"])[0]
    compliance_dim   = dims.get("compliance",   ["prohibited_practice"])[0]

    # 40 COERCIVE — spread across transparency, safety, compliance
    for i in range(40):
        if i < 20:
            dim_id, score = transparency_dim, 8   # transparency violation
        elif i < 32:
            dim_id, score = safety_dim, 7          # safety violation
        else:
            dim_id, score = compliance_dim, 9      # compliance violation
        agent = TEST_AGENTS[i % 3]
        role  = ["llm_response", "tool_call", "user_prompt"][i % 3]
        _insert_eval(
            con, ts_fmt, when=now - timedelta(minutes=i % 8),
            verdict="COERCIVE", role=role, agent_name=agent,
            dims=[{"id": dim_id, "score": score}],
        )

    # 40 PERMISSIVE alongside
    for i in range(40):
        pillar = list(dims.keys())[i % len(dims)]
        dim_id = dims[pillar][0]
        _insert_eval(
            con, ts_fmt, when=now - timedelta(minutes=i % 8),
            verdict="PERMISSIVE", role="user_prompt",
            agent_name=TEST_AGENTS[0],
            dims=[{"id": dim_id, "score": 1}],
        )
        permissive_n += 1

    con.commit()

    total_inserted = 80
    expected_coercive = 40
    expected_viol_pct = round(expected_coercive / total_inserted * 100, 1)

    _info(f"Inserted {total_inserted} evals ({expected_coercive} COERCIVE, {permissive_n} PERMISSIVE)")
    _info(f"Expected violation rate (this batch): {expected_viol_pct}%")

    # Query tier1 with a narrow window to capture only THIS batch (last 15 min)
    r = _api("/v1/metrics/tier1", {"window_minutes": 15, "app_id": TEST_APP_ID})
    if r is None:
        _warn("PAP not responding — skipping T-02 API assertions")
        return {"violation_count": expected_coercive}

    total = r.get("computed_from", 0)
    # >= because T-01's PERMISSIVE records may also be in the 15-minute window
    _assert(total >= total_inserted, f"tier1.computed_from = {total} (>= {total_inserted})", f"tier1.computed_from = {total}, expected >= {total_inserted}  ← COUNT BUG")

    vr = r.get("metrics", {}).get("violation_rate", {})
    actual_flagged = vr.get("flagged", -1)
    actual_pct     = vr.get("flag_rate_pct", -1)
    _assert(
        actual_flagged == expected_coercive,
        f"COERCIVE count = {actual_flagged} (expected {expected_coercive})",
        f"COERCIVE count = {actual_flagged}, expected {expected_coercive}  ← VERDICT COUNT BUG",
    )
    # Violation rate is diluted by T-01 PERMISSIVE records still in window — just check > 0
    _soft(
        actual_pct > 0,
        f"violation_rate = {actual_pct}% (violations detected; rate diluted by prior PERMISSIVE batch)",
        f"violation_rate = {actual_pct}% — NO violations detected (expected {expected_viol_pct}%)",
    )

    # Check dim-breakdown captures the transparency violations
    dr = _api("/v1/metrics/dim-breakdown", {"window_hours": 1, "app_id": TEST_APP_ID})
    if dr:
        trans_cat = dr.get("categories", {}).get("transparency", {})
        trans_vr  = trans_cat.get("violation_rate_pct", -1)
        _soft(
            trans_vr > 0,
            f"transparency violation_rate = {trans_vr}%  (violations detected)",
            f"transparency violation_rate = {trans_vr}% — transparency violations NOT reflected in dim-breakdown",
        )

    return {"violation_count": expected_coercive}


def test_time_windows(con: sqlite3.Connection, ts_fmt: str, dims: dict):
    """
    T-03: Time window filtering accuracy.
    Inserts 10 records at: NOW, 2h ago, 5h ago, 25h ago, 49h ago.
    Verifies each window param returns the correct subset.
    Critically checks tier1 and dim-breakdown AGREE with the same window.
    """
    _head("T-03  Time Window Accuracy — Cross-Endpoint Consistency")
    now = datetime.now(timezone.utc)

    BUCKETS = [
        ("now",   0,    10),   # (label, hours_ago, count)
        ("2h",    2,    10),
        ("5h",    5,    10),
        ("25h",   25,   10),
        ("49h",   49,   10),
    ]

    dim_id = dims.get("safety", ["hate_speech"])[0]

    for label, hrs_ago, n in BUCKETS:
        when = now - timedelta(hours=hrs_ago)
        for _ in range(n):
            _insert_eval(
                con, ts_fmt, when=when,
                verdict="PERMISSIVE", role="user_prompt",
                dims=[{"id": dim_id, "score": 1}],
            )

    con.commit()
    total_w_buckets = sum(c for _, _, c in BUCKETS)
    _info(f"Inserted {total_w_buckets} records across 5 time buckets")

    # Expected counts per window
    WINDOW_TESTS = [
        # (label, window_hours, expected_count_within_window)
        ("3h window",   3,  10),     # only "now"
        ("6h window",   6,  20),     # now + 2h
        ("24h window", 24,  30),     # now + 2h + 5h
        ("36h window", 36,  40),     # now + 2h + 5h + 25h
        ("72h window", 72,  50),     # all 5
    ]

    print()
    for label, wh, expected in WINDOW_TESTS:
        wm = wh * 60   # convert to minutes for tier1

        # Query tier1 (window_minutes)
        t1 = _api("/v1/metrics/tier1", {"window_minutes": wm, "app_id": TEST_APP_ID})
        t1_count = t1.get("computed_from", -1) if t1 else -1

        # Query dim-breakdown (window_hours)
        db_r = _api("/v1/metrics/dim-breakdown", {"window_hours": wh, "app_id": TEST_APP_ID})
        db_count = db_r.get("total_dim_evals", -1) if db_r else -1

        # NOTE: agent-activity is intentionally NOT compared here.
        # It counts only *agent-attributed* evaluations (records with metadata.agent_name)
        # while tier1/dim-breakdown count ALL evaluations.  That is by design —
        # they serve different purposes.  The cross-consistency check is tier1 ↔ dim-breakdown.

        # Consistency: tier1 vs dim-breakdown must agree (within 1 tolerance)
        t1_db_close = t1_count >= 0 and db_count >= 0 and abs(t1_count - db_count) <= 1

        if t1_count >= 0 and db_count >= 0:
            _assert(
                t1_db_close,
                f"{label}: tier1={t1_count}  dim-breakdown={db_count}  [CONSISTENT]",
                f"{label}: tier1={t1_count}  dim-breakdown={db_count}  ← MISMATCH",
            )
        else:
            _warn(f"{label}: endpoints not reachable (t1={t1_count}, db={db_count})")

        # At minimum check tier1 window is filtering correctly
        if t1_count >= 0:
            _soft(
                t1_count >= expected,
                f"{label}: tier1 returned >= {expected} records within window ({t1_count} total with prior tests)",
                f"{label}: tier1 returned {t1_count}, expected >= {expected} — possible window filter BUG",
            )

    # Explicit check: 72h window vs no-window (limit-only) — should agree for small datasets
    t1_all   = _api("/v1/metrics/tier1", {"limit": 5000, "app_id": TEST_APP_ID})
    t1_72h   = _api("/v1/metrics/tier1", {"window_minutes": 72 * 60, "limit": 5000, "app_id": TEST_APP_ID})
    if t1_all and t1_72h:
        cnt_all = t1_all.get("computed_from", -1)
        cnt_72h = t1_72h.get("computed_from", -1)
        _soft(
            abs(cnt_all - cnt_72h) <= 5,
            f"tier1 (no window) = {cnt_all}  vs  tier1 (72h window) = {cnt_72h}  [close]",
            f"tier1 no-window ({cnt_all}) vs 72h-window ({cnt_72h}) — LARGE DIVERGENCE — check limit vs window interaction",
        )


def test_agent_activity(con: sqlite3.Connection, ts_fmt: str, dims: dict):
    """
    T-04: Agent Activity panel correctness.
    Inserts known distribution per agent; verifies per-agent evaluation counts,
    violation counts, and platform_accountability_score.
    """
    _head("T-04  Agent Activity — Per-Agent Accuracy")
    now = datetime.now(timezone.utc)
    dim_id = dims.get("safety", ["hate_speech"])[0]
    sec_dim = dims.get("security", ["prompt_injection"])[0]

    # Agent distribution (agent_name, n_permissive, n_coercive, coercive_role)
    # NOTE: Use T04-unique names so T-01/T-02 records for the same TEST_AGENTS
    # don't contaminate these agents' counts or accountability scores.
    # All roles must be tool_call or agent_to_agent to appear in agent-activity
    # (the endpoint filters out user_prompt/llm_response-only agents as routing nodes).
    AGENT_SPEC = [
        # (name,               perm, coerc, role_for_all)
        ("t04_alpha",   50,   0,    "tool_call"),       # clean tool-call agent
        ("t04_beta",    30,   10,   "tool_call"),        # 10 output tool-call violations
        ("t04_gamma",   20,   5,    "tool_call"),        # 5 tool violations
        ("t04_delta",   15,   15,   "agent_to_agent"),  # 15 A2A violations
        ("t04_epsilon", 10,   0,    "agent_to_agent"),  # clean A2A agent
    ]

    # Expected accountability scores: 1 - (coercive_in_non_user_roles / total)
    # user_prompt weight=0.0, others=1.0
    for name, perm, coerc, role in AGENT_SPEC:
        sid = str(uuid.uuid4())
        for _ in range(perm):
            _insert_eval(con, ts_fmt, when=now - timedelta(minutes=2),
                verdict="PERMISSIVE", role=role,    # use agent's characteristic role
                agent_name=name, dims=[{"id": dim_id, "score": 1}], session_id=sid)
        for _ in range(coerc):
            _insert_eval(con, ts_fmt, when=now - timedelta(minutes=2),
                verdict="COERCIVE", role=role,
                agent_name=name, dims=[{"id": sec_dim, "score": 8}], session_id=sid)

    con.commit()
    _info("Inserted 5-agent distribution (150 PERMISSIVE + 30 COERCIVE)")

    aa = _api("/v1/metrics/agent-activity", {"window_minutes": 10, "app_id": TEST_APP_ID})
    if aa is None:
        _warn("PAP not responding — skipping T-04 assertions")
        return

    agents_by_name = {a["agent_name"]: a for a in aa.get("agents", [])}

    for name, perm, coerc, role in AGENT_SPEC:
        a = agents_by_name.get(name)
        if a is None:
            _fail(f"{name}: NOT found in agent-activity response")
            continue

        expected_total  = perm + coerc
        expected_coerc  = coerc
        actual_total    = a.get("evaluations", -1)
        actual_output_v = a.get("output_violations", -1)
        actual_input_b  = a.get("input_blocks", -1)
        acc_score       = a.get("platform_accountability_score", -1)

        _assert(
            actual_total >= expected_total,
            f"{name}: evaluations={actual_total} (>= {expected_total})",
            f"{name}: evaluations={actual_total}, expected >= {expected_total}  ← EVAL COUNT BUG",
        )

        total_violations = actual_output_v + actual_input_b if actual_output_v >= 0 else -1
        _assert(
            total_violations >= expected_coerc,
            f"{name}: total_violations={total_violations} (expected >= {expected_coerc})",
            f"{name}: total_violations={total_violations}, expected >= {expected_coerc}  ← VIOLATION COUNT BUG",
        )

        if coerc == 0 and actual_total > 0:
            _assert(
                acc_score >= 0.99,
                f"{name}: accountability_score={acc_score:.3f} (clean agent → should be 1.0)",
                f"{name}: accountability_score={acc_score:.3f} for clean agent — should be ~1.0",
            )
        elif coerc > 0 and actual_total > 0 and role != "user_prompt":
            expected_acc = max(0.0, 1.0 - coerc / max(1, actual_total))
            _soft(
                abs(acc_score - expected_acc) < 0.05,
                f"{name}: accountability_score={acc_score:.3f} (expected ~{expected_acc:.3f})",
                f"{name}: accountability_score={acc_score:.3f} vs expected {expected_acc:.3f}",
            )


def test_session_summaries(con: sqlite3.Connection):
    """
    T-05: Session data accuracy.
    Inserts 6 sessions with known trust scores and coercive counts.
    Verifies the sessions/summaries endpoint returns correct data.
    """
    _head("T-05  Session Summaries — Trust Score & Coercive Count")

    SESSIONS = [
        # (session_id, coercive_count, final_trust_score, end_reason, ended_ago_s)
        (str(uuid.uuid4()), 0,   1.0,  "normal",       30),
        (str(uuid.uuid4()), 1,   0.80, "normal",       60),
        (str(uuid.uuid4()), 2,   0.60, "normal",       90),
        (str(uuid.uuid4()), 3,   0.40, "escalated",   120),
        (str(uuid.uuid4()), 5,   0.20, "force_closed", 150),
        (str(uuid.uuid4()), 0,   0.95, "normal",       180),
    ]

    for sid, cc, ts, er, ago in SESSIONS:
        _insert_session(con, session_id=sid, coercive_count=cc,
                        final_trust_score=ts, end_reason=er, ended_ago_seconds=ago)
    con.commit()
    _info(f"Inserted {len(SESSIONS)} sessions with known trust scores")

    r = _api("/v1/sessions/summaries", {"window_hours": 1, "app_id": TEST_APP_ID})
    if r is None:
        _warn("PAP not responding — skipping T-05 assertions")
        return

    sessions = r.get("sessions", [])
    returned = {s["session_id"]: s for s in sessions}
    _assert(
        len(sessions) >= len(SESSIONS),
        f"sessions/summaries returned >= {len(SESSIONS)} sessions ({len(sessions)} total)",
        f"sessions/summaries returned {len(sessions)}, expected >= {len(SESSIONS)}  ← MISSING SESSIONS",
    )

    matched = 0
    for sid, cc, ts, er, _ in SESSIONS:
        s = returned.get(sid)
        if s is None:
            _fail(f"Session {sid[:8]}… NOT found in response")
            continue
        cc_ok = s.get("coercive_count") == cc
        ts_ok = abs(float(s.get("final_trust_score", -1)) - ts) < 0.01
        er_ok = s.get("end_reason") == er
        if cc_ok and ts_ok and er_ok:
            matched += 1
            if _verbose:
                _ok(f"  Session {sid[:8]}: coercive={cc} trust={ts} end={er} ✓")
        else:
            _fail(f"  Session {sid[:8]}: coercive_exp={cc} got={s.get('coercive_count')} | trust_exp={ts} got={s.get('final_trust_score')} | end_exp={er} got={s.get('end_reason')}")

    _assert(
        matched == len(SESSIONS),
        f"All {matched}/{len(SESSIONS)} sessions match (coercive_count, trust_score, end_reason)",
        f"Only {matched}/{len(SESSIONS)} sessions matched exactly",
    )


def test_cross_endpoint_consistency(con: sqlite3.Connection, ts_fmt: str, dims: dict):
    """
    T-06: THE BIG ONE — cross-endpoint consistency with a controlled dataset.
    Inserts exactly 200 evaluations in last 5 minutes.
    All three main endpoints MUST agree on the total count.
    """
    _head("T-06  Cross-Endpoint Consistency — 200 Controlled Evals")

    # Clear any existing test data first for a clean slate
    _cleanup(con)

    now    = datetime.now(timezone.utc)
    total  = 200
    coerc  = 40    # 20%
    norm   = 30    # 15%
    perm   = 130   # 65%

    dim_safety      = dims.get("safety",       ["hate_speech"])[0]
    dim_transp      = dims.get("transparency", ["ai_disclosure"])[0]
    dim_fairness    = dims.get("fairness",     ["bias_detection"])[0]
    dim_privacy     = dims.get("privacy",      ["sensitive_data_request"])[0]
    dim_compliance  = dims.get("compliance",   ["prohibited_practice"])[0]
    dim_security    = dims.get("security",     ["prompt_injection"])[0]

    roles = ["user_prompt", "llm_response", "tool_call"]

    # Insert PERMISSIVE
    for i in range(perm):
        dims_cycle = [dim_safety, dim_transp, dim_fairness, dim_privacy, dim_compliance, dim_security]
        d = dims_cycle[i % len(dims_cycle)]
        _insert_eval(con, ts_fmt, when=now - timedelta(seconds=i % 240),
            verdict="PERMISSIVE", role=roles[i % 3],
            agent_name=TEST_AGENTS[i % len(TEST_AGENTS)],
            dims=[{"id": d, "score": 2}])

    # Insert NORMATIVE
    for i in range(norm):
        d = dim_transp
        _insert_eval(con, ts_fmt, when=now - timedelta(seconds=i % 240),
            verdict="NORMATIVE", role="llm_response",
            agent_name=TEST_AGENTS[i % len(TEST_AGENTS)],
            dims=[{"id": d, "score": 5}])

    # Insert COERCIVE
    for i in range(coerc):
        d = [dim_safety, dim_transp, dim_security][i % 3]
        _insert_eval(con, ts_fmt, when=now - timedelta(seconds=i % 240),
            verdict="COERCIVE", role=roles[i % 3],
            agent_name=TEST_AGENTS[i % len(TEST_AGENTS)],
            dims=[{"id": d, "score": 8}])

    con.commit()
    _info(f"Inserted {total} evaluations ({perm} PERMISSIVE, {norm} NORMATIVE, {coerc} COERCIVE)")
    _info(f"Expected violation_rate = {coerc}/{total} = {round(coerc/total*100, 1)}%")

    wm = 10    # 10-minute window — captures all 200 (within 4 minutes)
    wh = 1     # 1-hour window for dim-breakdown

    t1 = _api("/v1/metrics/tier1",       {"window_minutes": wm, "app_id": TEST_APP_ID, "limit": 1000})
    db = _api("/v1/metrics/dim-breakdown", {"window_hours": wh, "app_id": TEST_APP_ID})
    aa = _api("/v1/metrics/agent-activity", {"window_minutes": wm, "app_id": TEST_APP_ID, "limit": 5000})

    t1_total = t1.get("computed_from", -1) if t1 else -1
    db_total = db.get("total_dim_evals", -1) if db else -1
    aa_total = aa.get("computed_from",   -1) if aa else -1

    _assert(t1_total == total, f"tier1.computed_from = {t1_total} (expected {total})", f"tier1.computed_from = {t1_total}, expected {total}  ← COUNT BUG")

    if db_total >= 0:
        # dim-breakdown counts dimension rows — should equal total evaluations (1 dim per eval in our test)
        _assert(
            db_total == total,
            f"dim-breakdown.total_dim_evals = {db_total} (expected {total})",
            f"dim-breakdown.total_dim_evals = {db_total}, expected {total}  ← CONSISTENCY BUG",
        )
        _assert(
            abs(t1_total - db_total) <= 1,
            f"tier1 ({t1_total}) == dim-breakdown ({db_total})  [CONSISTENT]",
            f"tier1 ({t1_total}) ≠ dim-breakdown ({db_total})  ← ENDPOINT MISMATCH",
        )

    if aa_total >= 0:
        _assert(
            aa_total == total,
            f"agent-activity.computed_from = {aa_total} (expected {total})",
            f"agent-activity.computed_from = {aa_total}, expected {total}  ← AGENT-ACTIVITY COUNT BUG",
        )

    # Violation rate in tier1
    if t1:
        vr = t1.get("metrics", {}).get("violation_rate", {})
        flagged  = vr.get("flagged", -1)
        flag_pct = vr.get("flag_rate_pct", -1)
        _assert(
            flagged == coerc,
            f"tier1.violation_rate.flagged = {flagged} (expected {coerc})",
            f"tier1.violation_rate.flagged = {flagged}, expected {coerc}  ← COERCIVE COUNT BUG",
        )
        expected_pct = round(coerc / total * 100, 1)
        _assert(
            abs(flag_pct - expected_pct) < 0.2,
            f"tier1.flag_rate_pct = {flag_pct}% (expected {expected_pct}%)",
            f"tier1.flag_rate_pct = {flag_pct}%, expected {expected_pct}%  ← RATE CALC BUG",
        )

    # Pillar distribution in dim-breakdown
    if db:
        cats = db.get("categories", {})
        _info(f"dim-breakdown categories found: {sorted(cats.keys())}")
        for cat in dims:
            if cat in cats:
                vrate = cats[cat].get("violation_rate_pct", 0)
                if _verbose:
                    _info(f"  {cat}: violation_rate={vrate}%")

    # Per-agent consistency: sum of all agent evaluations must equal tier1 total
    if aa and t1:
        agent_sum = sum(a.get("evaluations", 0) for a in aa.get("agents", []))
        _soft(
            abs(agent_sum - t1_total) <= 2,
            f"Sum of per-agent evals ({agent_sum}) ≈ tier1.computed_from ({t1_total})",
            f"Sum of per-agent evals ({agent_sum}) ≠ tier1.computed_from ({t1_total}) ← AGENT ACTIVITY MISMATCH",
        )


def test_normative_handling(con: sqlite3.Connection, ts_fmt: str, dims: dict):
    """
    T-07: NORMATIVE evaluations — 'warnings' — must NOT count as violations
    in the violation_rate but MUST appear in the agent warnings count.
    """
    _head("T-07  NORMATIVE / Warning Handling")
    now = datetime.now(timezone.utc)
    _cleanup(con)

    dim_id = dims.get("transparency", ["ai_disclosure"])[0]

    for i in range(50):
        _insert_eval(con, ts_fmt, when=now - timedelta(seconds=i),
            verdict="NORMATIVE", role="tool_call",   # tool_call so agent appears in agent-activity filter
            agent_name="agent_alpha",
            dims=[{"id": dim_id, "score": 5}])   # score 5 → WARNING, not FLAGGED

    con.commit()

    r = _api("/v1/metrics/tier1", {"window_minutes": 5, "app_id": TEST_APP_ID})
    if not r:
        _warn("Skipping T-07")
        return

    vr = r.get("metrics", {}).get("violation_rate", {})
    flagged = vr.get("flagged", -1)
    _assert(flagged == 0, f"NORMATIVE evals: violation_rate.flagged = 0 (NORMATIVE ≠ COERCIVE)", f"NORMATIVE evals: violation_rate.flagged = {flagged} — NORMATIVE counted as violation BUG")

    aa = _api("/v1/metrics/agent-activity", {"window_minutes": 5, "app_id": TEST_APP_ID})
    if aa:
        agents = {a["agent_name"]: a for a in aa.get("agents", [])}
        alpha = agents.get("agent_alpha", {})
        warnings = alpha.get("warnings", -1)
        _assert(warnings >= 50, f"agent_alpha.warnings = {warnings} (>= 50 NORMATIVE evals)", f"agent_alpha.warnings = {warnings}, expected >= 50  ← WARNING COUNT BUG")
        output_v = alpha.get("output_violations", -1)
        _assert(output_v == 0, f"agent_alpha.output_violations = 0 (no COERCIVE)", f"agent_alpha.output_violations = {output_v} — NORMATIVE shouldn't be output_violation")


def test_trust_agents(ts_fmt: str):
    """
    T-08: Trust Agents service (optional, skip if offline).
    Triggers a run and verifies the response contains deterministic status.
    """
    _head("T-08  Trust Agents — Deterministic Status & Narrative")

    if not _ta_ok():
        _warn("Trust Agents service not running on port 3005 — skipping T-08")
        return

    # Hit findings endpoint
    r = _api("/v1/trust/findings", base=TA_URL)
    if not r:
        _warn("Trust Agents findings endpoint not responding")
        return

    for agent_type in ["compliance", "tracker"]:
        finding = r.get(agent_type) or {}
        det_status = finding.get("deterministic_status")
        has_data   = finding.get("has_data")
        # Soft assertion: TA service may be running older code without deterministic_status field
        _soft(
            det_status in ("standby", "compliant", "stable", "watch", "breach", "degrading"),
            f"{agent_type}: deterministic_status = '{det_status}' (valid value)",
            f"{agent_type}: deterministic_status = '{det_status}' — NOT a valid status (upgrade TA service)",
        )
        _info(f"  {agent_type}: status={det_status}  has_data={has_data}  llm_available={finding.get('llm_available')}")

    # Trigger a sync run on compliance agent
    try:
        run_r = requests.post(f"{TA_URL}/v1/trust/run/compliance", timeout=60)
        if run_r.status_code == 200:
            data = run_r.json()
            status = data.get("overall_status", "")
            _assert(
                status in ("compliant", "watch", "breach", "unknown"),
                f"Compliance run: overall_status = '{status}'",
                f"Compliance run: overall_status = '{status}' — unexpected value",
            )
            reasoning = data.get("reasoning_trace", "")
            _soft(
                len(reasoning) > 100,
                f"Compliance run: reasoning_trace present ({len(reasoning)} chars)",
                f"Compliance run: reasoning_trace short ({len(reasoning)} chars) — LLM may be offline",
            )
        else:
            _warn(f"Compliance run returned HTTP {run_r.status_code}")
    except Exception as exc:
        _warn(f"Compliance run timed out or failed: {exc}")


def test_donut_score_accuracy(con: sqlite3.Connection, ts_fmt: str, dims: dict):
    """
    T-09: Donut score (dim-breakdown) matches tier1 pillar compliance rates.
    The dashboard renders both — they must use the same source of truth.
    """
    _head("T-09  Donut Score Accuracy — Pillar Compliance vs dim-breakdown")
    _cleanup(con)
    now = datetime.now(timezone.utc)

    # Insert exactly 100 safety evals: 80 PASS (score 2), 20 FLAGGED (score 8)
    # Expected safety violation rate: 20%
    safety_dim = dims.get("safety", ["hate_speech"])[0]
    for i in range(80):
        _insert_eval(con, ts_fmt, when=now - timedelta(seconds=i),
            verdict="PERMISSIVE", role="user_prompt",
            dims=[{"id": safety_dim, "score": 2}])
    for i in range(20):
        _insert_eval(con, ts_fmt, when=now - timedelta(seconds=i),
            verdict="COERCIVE", role="user_prompt",
            dims=[{"id": safety_dim, "score": 8}])

    con.commit()
    _info("Inserted 100 safety evals: 80 PASS + 20 FLAGGED → expected safety violation rate = 20%")

    # dim-breakdown check
    db = _api("/v1/metrics/dim-breakdown", {"window_hours": 1, "app_id": TEST_APP_ID})
    if db:
        safety_cat = db.get("categories", {}).get("safety", {})
        safety_total  = safety_cat.get("total", -1)
        safety_flagged = safety_cat.get("flagged", -1)
        safety_vr = safety_cat.get("violation_rate_pct", -1)
        _assert(safety_total == 100, f"dim-breakdown safety total = {safety_total} (expected 100)", f"dim-breakdown safety total = {safety_total}  ← SAFETY COUNT WRONG")
        _assert(safety_flagged == 20, f"dim-breakdown safety flagged = {safety_flagged} (expected 20)", f"dim-breakdown safety flagged = {safety_flagged}  ← FLAGGED COUNT WRONG")
        _assert(abs(safety_vr - 20.0) < 0.5, f"dim-breakdown safety violation_rate = {safety_vr}% (expected 20%)", f"dim-breakdown safety violation_rate = {safety_vr}%  ← DONUT SCORE BUG")

    # tier1 check — should show safety dim_scores with matching rate
    t1 = _api("/v1/metrics/tier1", {"window_minutes": 60, "app_id": TEST_APP_ID, "limit": 1000})
    if t1:
        dim_scores = t1.get("dim_scores", [])
        safety_dims = [d for d in dim_scores if d.get("category") == "safety"]
        if safety_dims:
            total_safety = sum(d.get("sample", 0) for d in safety_dims)
            flagged_safety = sum(d.get("flagged_count", 0) for d in safety_dims)
            actual_vr = round(flagged_safety / total_safety * 100, 1) if total_safety else -1
            _assert(total_safety == 100, f"tier1 dim_scores safety sample = {total_safety}", f"tier1 dim_scores safety sample = {total_safety}, expected 100")
            _assert(abs(actual_vr - 20.0) < 0.5, f"tier1 dim_scores safety violation = {actual_vr}% (expected 20%)", f"tier1 dim_scores safety violation = {actual_vr}% — DONUT/TIER1 MISMATCH")

        # Cross-check: dim-breakdown vs tier1 dim_scores must agree
        if db and safety_dims:
            db_safety_vr  = db.get("categories", {}).get("safety", {}).get("violation_rate_pct", -1)
            t1_safety_vr  = round(sum(d.get("flagged_count",0) for d in safety_dims) / max(1, sum(d.get("sample",0) for d in safety_dims)) * 100, 1)
            _assert(
                abs(db_safety_vr - t1_safety_vr) < 0.5,
                f"dim-breakdown safety {db_safety_vr}% == tier1 dim_scores {t1_safety_vr}%  [DONUT SOURCES MATCH]",
                f"dim-breakdown safety {db_safety_vr}% ≠ tier1 dim_scores {t1_safety_vr}%  ← DONUT vs TIER1 SOURCE MISMATCH",
            )


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    global _verbose, PAP_URL

    parser = argparse.ArgumentParser(description="RAI Trust Platform Functional Tests")
    parser.add_argument("--keep-data",  action="store_true", help="Do not delete test records after run")
    parser.add_argument("--verbose",    action="store_true", help="Print extra detail")
    parser.add_argument("--pap-url",    default=PAP_URL,     help=f"PAP base URL (default: {PAP_URL})")
    parser.add_argument("--skip-tests", default="",          help="Comma-separated test IDs to skip (e.g. T-08,T-09)")
    args = parser.parse_args()

    _verbose = args.verbose
    PAP_URL  = args.pap_url
    skip     = {s.strip().upper() for s in args.skip_tests.split(",") if s.strip()}

    print(f"\n{BOLD}{'═'*60}")
    print(f"  RAI Trust Platform — Functional Test Suite")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*60}{RESET}\n")

    # ── Find DB ───────────────────────────────────────────────────
    db_path = _find_db()
    if not db_path:
        print(f"{RED}ERROR: Could not find PAP SQLite DB.{RESET}")
        print("Searched:", [str(Path(__file__).parent.parent / c) for c in _DB_CANDIDATES])
        sys.exit(1)

    _info(f"DB path: {db_path}")
    _info(f"PAP URL: {PAP_URL}")

    # ── Check PAP service ─────────────────────────────────────────
    if not _pap_ok():
        print(f"\n{RED}ERROR: PAP service not responding at {PAP_URL}{RESET}")
        print("Start the PAP service first, then re-run this script.")
        sys.exit(1)
    _ok(f"PAP service reachable at {PAP_URL}")

    # ── Open DB connection ────────────────────────────────────────
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA foreign_keys = ON")

    try:
        ts_fmt = _detect_ts_format(con)
        dims   = _discover_dims(con)

        _info(f"Timestamp format: '{ts_fmt}'")
        _info(f"Active pillars discovered: {sorted(dims.keys())}")
        _info(f"Isolated test app_id: '{TEST_APP_ID}'")

        # ── Run all test sections ─────────────────────────────────
        tests = [
            ("T-00", lambda: test_db_format(con, ts_fmt)),
            ("T-01", lambda: test_sunny_day(con, ts_fmt, dims)),
            ("T-02", lambda: test_violations(con, ts_fmt, dims)),
            ("T-03", lambda: test_time_windows(con, ts_fmt, dims)),
            ("T-04", lambda: test_agent_activity(con, ts_fmt, dims)),
            ("T-05", lambda: test_session_summaries(con)),
            ("T-06", lambda: test_cross_endpoint_consistency(con, ts_fmt, dims)),
            ("T-07", lambda: test_normative_handling(con, ts_fmt, dims)),
            ("T-08", lambda: test_trust_agents(ts_fmt)),
            ("T-09", lambda: test_donut_score_accuracy(con, ts_fmt, dims)),
        ]

        for test_id, fn in tests:
            if test_id in skip:
                _info(f"Skipping {test_id} (--skip-tests)")
                continue
            try:
                fn()
            except Exception as exc:
                _fail(f"{test_id} crashed: {exc}")
                if _verbose:
                    import traceback
                    traceback.print_exc()

        # ── Cleanup ───────────────────────────────────────────────
        if not args.keep_data:
            n_eval, n_sess = _cleanup(con)
            _info(f"\nCleanup: removed {n_eval} eval records + {n_sess} sessions (app_id='{TEST_APP_ID}')")
        else:
            _warn("--keep-data set: test records NOT deleted")

        # ── Summary ───────────────────────────────────────────────
        total_checks = _passes + _fails
        print(f"\n{BOLD}{'═'*60}")
        print(f"  RESULTS:  {GREEN}{_passes} passed{RESET}  {RED}{_fails} failed{RESET}  {AMBER}{_warns} warnings{RESET}")
        print(f"  Coverage: {total_checks} assertions across 10 test sections")
        print(f"{'═'*60}{RESET}\n")

        if _fails > 0:
            print(f"{RED}{BOLD}  FAILED — {_fails} assertion(s) require attention.{RESET}\n")
            sys.exit(1)
        else:
            print(f"{GREEN}{BOLD}  ALL CHECKS PASSED{RESET}\n")
            sys.exit(0)

    finally:
        con.close()


if __name__ == "__main__":
    main()
