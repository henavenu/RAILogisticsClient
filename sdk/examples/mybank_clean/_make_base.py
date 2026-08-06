# -*- coding: utf-8 -*-
"""Regenerate server_base.py from server.py with the RAI Trust SDK stubbed out.

Run from sdk/examples/mybank_clean/:  python _make_base.py
Re-run this whenever server.py changes so the ungoverned baseline stays in lockstep.
"""
import io

src = io.open("server.py", encoding="utf-8").read()

# 1) Label the module as the ungoverned baseline.
src = src.replace(
    "MyBank — FastAPI web server\n",
    "MyBank — FastAPI web server (UNGOVERNED BASELINE — no RAI Trust SDK)\n\n"
    "Auto-generated from server.py by _make_base.py. Identical to server.py except\n"
    "the RAI Trust SDK is stubbed with no-ops, so the banking graph runs WITHOUT any\n"
    "governance. Edit server.py, then re-run `python _make_base.py` to refresh this.\n",
    1,
)

# 2) Replace the SDK import block with no-op shims. Everything downstream
#    (_proxy = PrivacyProxy(...), banking_graph = instrument(...), the
#    except GovernanceBlock/HITLInterrupt handlers) stays byte-identical to
#    server.py and simply becomes inert.
start_marker = "# ── RAI Trust SDK"
end_marker = "raise SystemExit(1) from _exc"
start = src.index(start_marker)
end = src.index(end_marker) + len(end_marker)

shim = (
    "# ── RAI Trust SDK — STUBBED OUT (ungoverned baseline) ─────────────────────────\n"
    "# No SDK import. These no-op shims keep the rest of this file identical to\n"
    "# server.py: instrument() returns the graph unchanged (no governance layer),\n"
    "# the Privacy Proxy passes content through, and GovernanceBlock / HITLInterrupt\n"
    "# are never raised. Run server.py for the governed app; run this for the\n"
    "# ungoverned 'before' demo.\n"
    "from types import SimpleNamespace\n"
    "\n"
    "def instrument(graph, *args, **kwargs):\n"
    "    return graph                                   # pass-through: no governance\n"
    "\n"
    "class EnforcementMode(str):\n"
    "    RAISE = \"RAISE\"\n"
    "    NONE  = \"NONE\"\n"
    "\n"
    "class GovernanceBlock(Exception):                  # never raised (graph not instrumented)\n"
    "    pass\n"
    "\n"
    "class HITLInterrupt(Exception):                    # never raised\n"
    "    pass\n"
    "\n"
    "class ProxyConfig:\n"
    "    def __init__(self, *args, **kwargs):\n"
    "        pass\n"
    "\n"
    "class _NoOpPrivacyProxy:\n"
    "    def process(self, text):\n"
    "        # Mirrors the SDK proxy result shape; no detections, no redaction.\n"
    "        return SimpleNamespace(detections=[], sanitized_text=text, redacted_text=text)\n"
    "\n"
    "def PrivacyProxy(config=None):\n"
    "    return _NoOpPrivacyProxy()\n"
)

out = src[:start] + shim + src[end:]
io.open("server_base.py", "w", encoding="utf-8", newline="\n").write(out)
print("wrote server_base.py")
