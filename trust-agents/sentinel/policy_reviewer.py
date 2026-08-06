"""
Policy Reviewer — a standing specialist that audits the governance CONFIGURATION
itself (not transactions). It reads the active dimension registry (PAP /v1/dimensions)
and the policy catalog (PE /v1/policies) and surfaces findings:

  • Coverage Gap        — an ACTIVE dimension with no governing policy/threshold.
                          (catalog + registry only → always reliable)
  • Threshold Adjustment — (best-effort) a policy whose firing behaviour suggests its
                          threshold should move. Requires a firing metric; skipped
                          cleanly if unavailable.

Findings escalate to the ADVISORY agent (Coordinator) → Tier-3 POLICY_CHANGE on
Trust Radar / Alerts, and light up the `policy-reviewer` node on the platform graph
(gate → policy-reviewer → Advisory). OFF switch: POLICY_REVIEWER=false.
"""
import asyncio
import logging
import os

import httpx

import platform_activity as _pa

logger = logging.getLogger("sentinel.policy_reviewer")

_ENABLED  = os.getenv("POLICY_REVIEWER", "true").strip().lower() in ("1", "true", "yes")
_INTERVAL = float(os.getenv("POLICY_REVIEWER_INTERVAL_S", "60"))
NODE      = "policy-reviewer"

# Dedup: a stable finding escalates to Advisory ONCE (the node still blinks every scan
# for liveness, but we don't spam the Alerts tab with identical governance alerts).
_last_escalated: dict = {}


async def _get_json(client, url):
    r = await client.get(url)
    r.raise_for_status()
    return r.json()


def coverage_gaps(policies, dimensions):
    """Active dimensions with no policy whose name matches the dimension id."""
    covered = {str(p.get("name", "")).strip().lower() for p in policies}
    gaps = []
    for d in dimensions:
        if str(d.get("status", "")).lower() != "active":
            continue
        did = str(d.get("dimension_id", "")).strip()
        if did and did.lower() not in covered:
            gaps.append({"dimension": did, "category": d.get("category", "") or "uncategorised"})
    return gaps


def threshold_adjustments(policies, min_cluster=5):
    """Policies sharing an identical (likely default / untuned) threshold band — a real
    governance smell: a severity-3 dimension carrying the same cutoffs as a severity-1
    one is mis-calibrated. Catalog-only, so it always works (no firing metric)."""
    import json as _json
    from collections import defaultdict
    by_sig = defaultdict(list)
    for p in policies:
        th = p.get("thresholds")
        if isinstance(th, dict):
            by_sig[_json.dumps(th, sort_keys=True)].append(p)
    findings = []
    for _sig, grp in by_sig.items():
        if len(grp) >= min_cluster:
            sevs = {int(p.get("severity", 0) or 0) for p in grp}
            rep  = max(grp, key=lambda p: p.get("severity", 0) or 0)
            findings.append({
                "count":     len(grp),
                "example":   rep.get("name", ""),
                "severity":  rep.get("severity"),
                "sev_spread": sorted(sevs),
            })
    findings.sort(key=lambda f: f["count"], reverse=True)
    return findings


async def _escalate(client, coordinator_url, app_id, sid, check, reason, pattern):
    """Hand a policy finding to the Advisory agent for a Tier-3 recommendation."""
    if not coordinator_url:
        return
    try:
        await client.post(
            f"{coordinator_url.rstrip('/')}/coordinate",
            json={"session_id": sid, "app_id": app_id, "sentinel_verdict": "ALERT",
                  "sentinel_checks": [check], "sentinel_reasoning": reason,
                  "pattern_type": pattern, "confidence": 0.9},
        )
        logger.warning("[policy-reviewer] %s -> Advisory (Tier-3 policy change)", check)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[policy-reviewer] Advisory escalation failed: %s", exc)


async def _scan(client, pe_url, pap_url, coordinator_url, app_id):
    try:
        policies = (await _get_json(client, f"{pe_url.rstrip('/')}/v1/policies")).get("policies", [])
        dims     = (await _get_json(client, f"{pap_url.rstrip('/')}/v1/dimensions")).get("dimensions", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[policy-reviewer] catalog/registry fetch failed: %s", exc)
        return
    gaps   = coverage_gaps(policies, dims)
    thresh = threshold_adjustments(policies)
    n_active = sum(1 for d in dims if str(d.get('status', '')).lower() == 'active')
    logger.info("[policy-reviewer] app=%s policies=%d active_dims=%d coverage_gaps=%d threshold_findings=%d",
                app_id, len(policies), n_active, len(gaps), len(thresh))

    # Pick the finding to feature: a coverage gap (higher priority) else a threshold one.
    sid = check = reason = pattern = None
    if gaps:
        g = gaps[0]
        sid, check, pattern = f"policy-review-gap-{g['dimension']}"[:90], "policy_coverage_gap", "policy_gap"
        reason = (f"Coverage gap: active dimension '{g['dimension']}' ({g['category']}) has no "
                  f"governing policy or threshold — recommend adding one.")
        edge_label = f"coverage gap '{g['dimension']}' -> recommend policy"
    elif thresh:
        t = thresh[0]
        sid, check, pattern = "policy-review-threshold-default", "policy_threshold_adjustment", "threshold_adjustment"
        reason = (f"Threshold calibration: {t['count']} policies share an identical default "
                  f"threshold band (e.g. '{t['example']}', severity {t['severity']}; severities "
                  f"{t['sev_spread']} all use the same cutoffs) — none tuned to their dimension's "
                  f"risk profile. Recommend per-dimension threshold adjustment.")
        edge_label = f"{t['count']} untuned thresholds -> recommend adjustment"

    # Platform graph: the PE gate's policy catalog is monitored by Sentinel, which runs
    # the Policy Reviewer as ONE OF ITS SURFACES. The reviewer therefore hangs off
    # Sentinel — NOT off the gate directly (the gate is the data-plane entry, not the
    # reviewer's peer). Chain: gate -> sentinel -> policy-reviewer -> coordinator.
    _scan = f"policy scan: {len(gaps)} gap(s), {len(thresh)} threshold finding(s)"
    _tier = "watch" if (gaps or thresh) else "clear"
    await _pa.emit(pe_url, _pa.GATE_NODE, _pa.SENTINEL_NODE,
                   "policy catalog monitored", outcome=_tier)
    await _pa.emit(pe_url, _pa.SENTINEL_NODE, NODE, _scan, outcome=_tier)
    if sid:
        # Always blink the reviewer → Advisory edge (liveness), but escalate to Advisory
        # only when the finding changes (dedup the governance alert).
        await _pa.emit(pe_url, NODE, "coordinator", edge_label, session_id=sid, outcome="watch")
        if _last_escalated.get(app_id) != sid:
            await _escalate(client, coordinator_url, app_id, sid, check, reason, pattern)
            _last_escalated[app_id] = sid


async def run_policy_reviewer(pe_url, pap_url, coordinator_url, apps, interval, http_timeout):
    """Background task: periodically audit the governance config and surface findings."""
    if not _ENABLED:
        logger.warning("[policy-reviewer] disabled (POLICY_REVIEWER=false)")
        return
    logger.warning("[policy-reviewer] started — interval=%ss apps=%s", interval, apps)
    while True:
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as client:
                for app_id in apps:
                    await _scan(client, pe_url, pap_url, coordinator_url, app_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[policy-reviewer] tick error: %s", exc)
        await asyncio.sleep(interval)
