#!/usr/bin/env python3
"""
rai-governance-check — Claude Code hook for RAI Trust Platform governance.

Usage
-----
Invoked by Claude Code as a PreToolUse or PostToolUse hook.
Reads the hook payload from stdin (JSON), evaluates the content via the
Policy Engine, and exits with:

  0   PERMISSIVE or NORMATIVE — proceed
  2   COERCIVE                — block

Claude Code hook configuration (.claude/settings.json):

  {
    "hooks": {
      "PreToolUse": [
        {
          "matcher": ".*",
          "hooks": [
            { "type": "command", "command": "python rai_governance_check.py" }
          ]
        }
      ]
    }
  }

Environment variables
---------------------
RAI_PE_URL          Policy Engine base URL  (default: http://localhost:3000)
RAI_APP_ID          Application identifier  (default: claude-code)
RAI_SESSION_ID      Session ID — use $CLAUDE_SESSION_ID if available
RAI_TIMEOUT         HTTP timeout seconds    (default: 5)
RAI_FAIL_MODE       open | closed           (default: open)
                    open   = exit 0 on PE unreachable (don't block Claude)
                    closed = exit 2 on PE unreachable (block Claude)
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Optional


# ── Config from environment ───────────────────────────────────────────────────

PE_URL      = os.getenv("RAI_PE_URL",      "http://localhost:3000")
APP_ID      = os.getenv("RAI_APP_ID",      "claude-code")
SESSION_ID  = (
    os.getenv("RAI_SESSION_ID")
    or os.getenv("CLAUDE_SESSION_ID")   # set by Claude Code runtime
    or str(uuid.uuid4())
)
TIMEOUT     = float(os.getenv("RAI_TIMEOUT", "5"))
FAIL_MODE   = os.getenv("RAI_FAIL_MODE", "open").lower().strip()


def _read_stdin() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def _extract_content(payload: dict, hook_type: str) -> tuple[str, str]:
    """
    Extract (content_text, content_role) from the hook payload.

    PreToolUse  → tool_call  (tool_input serialised as JSON string)
    PostToolUse → tool_call  (tool_response serialised as JSON string)
    """
    if hook_type == "post":
        raw = payload.get("tool_response") or payload.get("tool_output", "")
    else:
        raw = payload.get("tool_input") or payload.get("user_prompt", "")

    # If raw is a dict (structured tool input), serialise it for the evaluator
    if isinstance(raw, dict):
        content = json.dumps(raw, ensure_ascii=False)
    else:
        content = str(raw or "").strip()

    role = "tool_call"
    # If the payload looks like a user message (not a tool call), use user_prompt
    if "user_prompt" in payload and "tool_name" not in payload:
        role = "user_prompt"

    return content, role


def _call_pe(content: str, role: str, request_id: str) -> Optional[dict]:
    """
    POST to PE /v1/app/evaluate.  Returns the response dict or None on error.
    Uses only stdlib http.client to avoid dependency on httpx/requests.
    """
    import http.client
    import urllib.parse

    parsed   = urllib.parse.urlparse(PE_URL)
    host     = parsed.hostname or "localhost"
    port     = parsed.port or (443 if parsed.scheme == "https" else 80)
    use_ssl  = parsed.scheme == "https"

    body = json.dumps({
        "request_id":   request_id,
        "content":      content,
        "role":         role,
        "app_id":       APP_ID,
        "session_id":   SESSION_ID,
        "metadata":     {"source": "claude-code-hook"},
    }, ensure_ascii=False).encode("utf-8")

    try:
        if use_ssl:
            import ssl
            conn = http.client.HTTPSConnection(
                host, port, timeout=TIMEOUT,
                context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)

        conn.request(
            "POST", "/v1/app/evaluate",
            body=body,
            headers={
                "Content-Type":   "application/json",
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()

        if resp.status == 200:
            return json.loads(data)
        else:
            _warn(f"PE returned HTTP {resp.status}: {data[:200]}")
            return None

    except OSError as exc:
        _warn(f"PE unreachable ({host}:{port}): {exc}")
        return None
    except Exception as exc:
        _warn(f"Unexpected error calling PE: {exc}")
        return None


def _warn(msg: str) -> None:
    """Write a warning to stderr (visible in Claude Code hook output)."""
    print(f"[rai-governance-check] WARNING: {msg}", file=sys.stderr)


def _block(reason: str, violations: list) -> None:
    """Print block message to stdout (shown to user by Claude Code) and exit 2."""
    lines = ["🛡️  RAI Governance Check — REQUEST BLOCKED", ""]
    lines.append(f"Reason: {reason}")
    if violations:
        lines.append("\nPolicy violations:")
        for v in violations[:5]:                  # cap at 5 for readability
            lines.append(f"  • [{v.get('policy_id', v.get('dimension', '?'))}] "
                         f"{v.get('remediation', 'Policy violation')}")
    print("\n".join(lines))
    sys.exit(2)


def main() -> None:
    # Determine hook type from argv or stdin key
    hook_type = "pre"
    if len(sys.argv) > 1 and sys.argv[1].lower() in ("post", "posttooluse"):
        hook_type = "post"

    payload    = _read_stdin()
    request_id = str(uuid.uuid4())

    # Nothing to evaluate
    if not payload:
        sys.exit(0)

    content, role = _extract_content(payload, hook_type)

    if not content:
        sys.exit(0)

    # ── Call Policy Engine ──────────────────────────────────────────────────
    result = _call_pe(content, role, request_id)

    if result is None:
        # PE unreachable
        if FAIL_MODE == "closed":
            _block("Policy Engine unreachable — blocked by fail-closed policy", [])
        else:
            _warn("Policy Engine unreachable — failing open (RAI_FAIL_MODE=open)")
            sys.exit(0)

    verdict     = result.get("verdict", "PERMISSIVE").upper()
    violations  = result.get("violations", [])
    remediations = result.get("remediations", [])

    if verdict == "COERCIVE":
        reason = remediations[0] if remediations else "Content violates policy."
        _block(reason, violations)
    else:
        # PERMISSIVE or NORMATIVE — proceed
        if verdict == "NORMATIVE" and violations:
            # Advisory: print to stderr so it appears in the Claude Code hook panel
            # but does not block the tool call.
            _warn(
                f"Advisory: prompt matched policy dimension "
                f"'{violations[0].get('dimension', '?')}' "
                f"(score {violations[0].get('score', '?')}) — proceeding."
            )
        sys.exit(0)


if __name__ == "__main__":
    main()
