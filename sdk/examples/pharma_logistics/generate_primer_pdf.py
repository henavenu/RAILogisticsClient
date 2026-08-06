"""
Generate the RAI Trust SDK / LangGraph Integration Primer as a PDF.
Run: python generate_primer_pdf.py
Output: sdk_langgraph_primer.pdf
"""
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, Preformatted
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os

OUTPUT = os.path.join(os.path.dirname(__file__), "sdk_langgraph_primer.pdf")

# ── Styles ─────────────────────────────────────────────────────────────────────

def build_styles():
    base = getSampleStyleSheet()

    styles = {}

    styles["cover_title"] = ParagraphStyle(
        "cover_title",
        parent=base["Title"],
        fontSize=26,
        leading=32,
        textColor=colors.HexColor("#1a2b45"),
        spaceAfter=8,
        alignment=TA_CENTER,
    )
    styles["cover_subtitle"] = ParagraphStyle(
        "cover_subtitle",
        parent=base["Normal"],
        fontSize=13,
        leading=18,
        textColor=colors.HexColor("#4a6080"),
        spaceAfter=4,
        alignment=TA_CENTER,
    )
    styles["cover_meta"] = ParagraphStyle(
        "cover_meta",
        parent=base["Normal"],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#7a8fa6"),
        alignment=TA_CENTER,
    )
    styles["h1"] = ParagraphStyle(
        "h1",
        parent=base["Heading1"],
        fontSize=16,
        leading=20,
        textColor=colors.HexColor("#1a2b45"),
        spaceBefore=18,
        spaceAfter=8,
        borderPad=0,
    )
    styles["h2"] = ParagraphStyle(
        "h2",
        parent=base["Heading2"],
        fontSize=13,
        leading=17,
        textColor=colors.HexColor("#2c4a6e"),
        spaceBefore=14,
        spaceAfter=6,
    )
    styles["h3"] = ParagraphStyle(
        "h3",
        parent=base["Heading3"],
        fontSize=11,
        leading=15,
        textColor=colors.HexColor("#3a5f85"),
        spaceBefore=10,
        spaceAfter=4,
    )
    styles["body"] = ParagraphStyle(
        "body",
        parent=base["Normal"],
        fontSize=10,
        leading=15,
        textColor=colors.HexColor("#2a2a2a"),
        spaceAfter=6,
        alignment=TA_JUSTIFY,
    )
    styles["bullet"] = ParagraphStyle(
        "bullet",
        parent=base["Normal"],
        fontSize=10,
        leading=15,
        textColor=colors.HexColor("#2a2a2a"),
        spaceAfter=4,
        leftIndent=18,
        bulletIndent=6,
    )
    styles["code"] = ParagraphStyle(
        "code",
        parent=base["Code"],
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#1a1a2e"),
        backColor=colors.HexColor("#f0f4f8"),
        fontName="Courier",
        spaceAfter=8,
        spaceBefore=4,
        leftIndent=12,
        rightIndent=12,
        borderPad=6,
    )
    styles["callout"] = ParagraphStyle(
        "callout",
        parent=base["Normal"],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#1a3a5c"),
        backColor=colors.HexColor("#e8f0f8"),
        spaceAfter=10,
        spaceBefore=6,
        leftIndent=12,
        rightIndent=12,
        borderPad=8,
        borderColor=colors.HexColor("#4a7ab5"),
        borderWidth=1,
    )
    styles["warning"] = ParagraphStyle(
        "warning",
        parent=base["Normal"],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#5c2a00"),
        backColor=colors.HexColor("#fff3e0"),
        spaceAfter=10,
        spaceBefore=6,
        leftIndent=12,
        rightIndent=12,
        borderPad=8,
    )
    styles["link"] = ParagraphStyle(
        "link",
        parent=base["Normal"],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#1a5fb4"),
        spaceAfter=4,
        leftIndent=18,
    )
    styles["toc_entry"] = ParagraphStyle(
        "toc_entry",
        parent=base["Normal"],
        fontSize=10,
        leading=16,
        textColor=colors.HexColor("#2a2a2a"),
        spaceAfter=2,
        leftIndent=0,
    )

    return styles

# ── Page template callbacks ────────────────────────────────────────────────────

def add_header_footer(canvas, doc):
    canvas.saveState()
    w, h = letter
    # Header rule
    if doc.page > 1:
        canvas.setStrokeColor(colors.HexColor("#c0cfe0"))
        canvas.setLineWidth(0.5)
        canvas.line(inch, h - 0.6 * inch, w - inch, h - 0.6 * inch)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#7a8fa6"))
        canvas.drawString(inch, h - 0.52 * inch, "RAI Trust SDK  ·  LangGraph Integration Primer")
        canvas.drawRightString(w - inch, h - 0.52 * inch, "Confidential — Internal Use Only")

    # Footer
    canvas.setStrokeColor(colors.HexColor("#c0cfe0"))
    canvas.setLineWidth(0.5)
    canvas.line(inch, 0.6 * inch, w - inch, 0.6 * inch)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#7a8fa6"))
    canvas.drawCentredString(w / 2, 0.42 * inch, f"Page {doc.page}")
    canvas.restoreState()

# ── Build document ─────────────────────────────────────────────────────────────

def build_pdf():
    doc = SimpleDocTemplate(
        OUTPUT,
        pagesize=letter,
        leftMargin=inch,
        rightMargin=inch,
        topMargin=0.85 * inch,
        bottomMargin=0.85 * inch,
        title="RAI Trust SDK / LangGraph Integration Primer",
        author="RAI Trust Platform Engineering",
        subject="SDK Integration Challenges and Solutions",
    )

    S = build_styles()
    story = []

    # ── Cover page ─────────────────────────────────────────────────────────────
    story.append(Spacer(1, 1.4 * inch))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#1a2b45")))
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("RAI Trust SDK", S["cover_title"]))
    story.append(Paragraph("LangGraph Integration Primer", S["cover_subtitle"]))
    story.append(Spacer(1, 0.1 * inch))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#4a6080")))
    story.append(Spacer(1, 0.35 * inch))
    story.append(Paragraph(
        "Challenges, Solutions, and Architectural Patterns for<br/>"
        "Governance SDK Integration with LangGraph Multi-Agent Systems",
        S["cover_meta"]
    ))
    story.append(Spacer(1, 0.35 * inch))
    story.append(Paragraph("Version 1.0 · April 2026", S["cover_meta"]))
    story.append(Paragraph("RAI Trust Platform Engineering", S["cover_meta"]))
    story.append(Spacer(1, 1.6 * inch))
    story.append(Paragraph(
        "<i>This document captures lessons learned during the instrumentation of the myBank "
        "reference application with the RAI Trust SDK. It is intended for engineers integrating "
        "the SDK with LangGraph-based multi-agent systems.</i>",
        S["cover_meta"]
    ))
    story.append(PageBreak())

    # ── Table of Contents ──────────────────────────────────────────────────────
    story.append(Paragraph("Table of Contents", S["h1"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#c0cfe0")))
    story.append(Spacer(1, 0.1 * inch))

    toc = [
        ("1.", "The Mission: Three Reliable Interception Points", "3"),
        ("2.", "Challenge 1 — The Execution Engine Is Not a Chain", "4"),
        ("3.", "Challenge 2 — Token Callbacks Don't Propagate by Default", "5"),
        ("4.", "Challenge 3 — Node Names Are Unpredictable", "6"),
        ("5.", "Challenge 4 — Agent Nodes Look Identical to Tool Nodes", "7"),
        ("6.", "What Works Reliably and Why", "8"),
        ("7.", "Background Reading", "9"),
    ]
    for num, title, pg in toc:
        story.append(Paragraph(
            f'{num}&nbsp;&nbsp;&nbsp;{title} <font color="#aaaaaa">{"." * (52 - len(title))} {pg}</font>',
            S["toc_entry"]
        ))
        story.append(Spacer(1, 2))

    story.append(PageBreak())

    # ── Section 1: Mission ─────────────────────────────────────────────────────
    story.append(Paragraph("1.  The Mission: Three Reliable Interception Points", S["h1"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#c0cfe0")))

    story.append(Paragraph(
        "The RAI Trust SDK must intercept LangGraph execution at three well-defined points to provide "
        "comprehensive governance coverage. These interception points correspond to the three evaluation "
        "roles the SDK can assign to content it sends to the Policy Enforcement (PE) engine:",
        S["body"]
    ))

    layers = [
        ("Layer 1 — User Input", "USER_MESSAGE",
         "Fires when the user's raw message enters the system. Evaluated before any LLM "
         "processing occurs. This catches prompt injection, policy violations, and adversarial "
         "inputs at the boundary."),
        ("Layer 2 — Tool Calls", "TOOL_CALL",
         "Fires immediately before any tool execution. The tool name, arguments, and calling "
         "agent are evaluated. This catches data exfiltration attempts, unauthorized tool "
         "invocations, and policy breaches in structured action parameters."),
        ("Layer A2A — Agent-to-Agent", "AGENT_TO_AGENT",
         "Fires when one agent hands off a task to another agent. The working context "
         "(structured inputs, beneficiary details, transfer parameters, etc.) is evaluated "
         "before the receiving agent executes. This enables sanctions screening, AML detection, "
         "and inter-agent trust verification on data flows that never surface in the user's "
         "original message."),
    ]
    for label, role, desc in layers:
        story.append(Paragraph(f"<b>{label}</b> <font color='#4a7ab5'>({role})</font>", S["h3"]))
        story.append(Paragraph(desc, S["body"]))

    story.append(Paragraph(
        "The central engineering challenge is that LangGraph does not use the standard LangChain "
        "callback architecture in the way the SDK initially assumed. Each interception point required "
        "a different mechanism to achieve reliability.",
        S["callout"]
    ))

    story.append(PageBreak())

    # ── Section 2: Challenge 1 ─────────────────────────────────────────────────
    story.append(Paragraph("2.  Challenge 1 — The Execution Engine Is Not a Chain", S["h1"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#c0cfe0")))

    story.append(Paragraph("<b>What we assumed</b>", S["h2"]))
    story.append(Paragraph(
        "LangGraph graphs are built on top of LangChain's Runnable abstraction. We expected "
        "that each node in the graph would fire <font face='Courier'>on_chain_start</font> / "
        "<font face='Courier'>on_chain_end</font> callbacks on our registered "
        "<font face='Courier'>BaseCallbackHandler</font>, just like any other LangChain chain. "
        "This would have given us a reliable per-node hook for agent-name discovery, token "
        "attribution, and agent-to-agent evaluation.",
        S["body"]
    ))

    story.append(Paragraph("<b>What actually happens</b>", S["h2"]))
    story.append(Paragraph(
        "LangGraph 0.2.x replaced the chain-based node execution model with a "
        "<b>Pregel-based scheduler</b>. In this model, nodes are executed as tasks inside an "
        "async/sync scheduler loop — not as LangChain Runnable invocations. The scheduler does "
        "not emit per-node <font face='Courier'>on_chain_start</font> events in the way a "
        "standard chain does.",
        S["body"]
    ))
    story.append(Paragraph(
        "The SDK's <font face='Courier'>_ToolGuardCallback</font> (a "
        "<font face='Courier'>BaseCallbackHandler</font>) does receive one "
        "<font face='Courier'>on_chain_start</font> event — at the graph boundary, when the "
        "entire StateGraph is invoked. But per-node callbacks are unreliable and version-dependent.",
        S["body"]
    ))

    story.append(Paragraph("<b>Evidence of the problem</b>", S["h2"]))
    story.append(Paragraph(
        "When we added <font face='Courier'>agent_nodes=[\"intl_wire_agent\"]</font> to the "
        "<font face='Courier'>instrument()</font> call and relied on "
        "<font face='Courier'>on_chain_start</font> to trigger A2A evaluation, no A2A records "
        "appeared in the evaluation audit trail — even though the "
        "<font face='Courier'>intl_wire_agent_node</font> function was executing and calling tools.",
        S["body"]
    ))

    story.append(Paragraph("<b>The solution</b>", S["h2"]))
    story.append(Paragraph(
        "We kept the <font face='Courier'>agent_nodes</font> + "
        "<font face='Courier'>on_chain_start</font> path as a best-effort mechanism (it may "
        "work in future LangGraph versions or when callbacks are configured differently). "
        "The primary A2A trigger was moved to <font face='Courier'>on_tool_start</font> via "
        "the new <font face='Courier'>a2a_tools</font> parameter:",
        S["body"]
    ))
    story.append(Preformatted(
        'banking_graph = instrument(\n'
        '    banking_graph,\n'
        '    agent_nodes=["intl_wire_agent"],        # best-effort: on_chain_start\n'
        '    a2a_tools={"international_transfer":    # guaranteed: on_tool_start\n'
        '               "intl_wire_agent"},\n'
        ')',
        S["code"]
    ))
    story.append(Paragraph(
        "The key insight: <font face='Courier'>on_tool_start</font> fires reliably because "
        "it is triggered by our own explicit call to "
        "<font face='Courier'>tool_map[tool_name].invoke(tool_args, config=config)</font> "
        "inside <font face='Courier'>_execute_tools()</font>. We control this call site, "
        "so the callback always fires regardless of LangGraph's internal scheduler.",
        S["callout"]
    ))

    story.append(PageBreak())

    # ── Section 3: Challenge 2 ─────────────────────────────────────────────────
    story.append(Paragraph("3.  Challenge 2 — Token Callbacks Don't Propagate by Default", S["h1"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#c0cfe0")))

    story.append(Paragraph("<b>What we assumed</b>", S["h2"]))
    story.append(Paragraph(
        "Once a <font face='Courier'>BaseCallbackHandler</font> is attached to the LangGraph "
        "invocation via <font face='Courier'>RunnableConfig</font>, the "
        "<font face='Courier'>on_llm_end</font> callback would automatically fire for every "
        "LLM call made anywhere inside the graph — giving us token counts per node.",
        S["body"]
    ))

    story.append(Paragraph("<b>What actually happens</b>", S["h2"]))
    story.append(Paragraph(
        "LangChain's callback propagation is <b>explicit</b>, not automatic. A callback "
        "registered in a <font face='Courier'>RunnableConfig</font> only reaches an LLM call "
        "if that config object is passed all the way down the call stack to every "
        "<font face='Courier'>llm.invoke()</font> call. If any intermediate function omits "
        "the <font face='Courier'>config</font> argument, the callback chain is broken "
        "at that point.",
        S["body"]
    ))

    story.append(Paragraph("<b>The problem in myBank</b>", S["h2"]))
    story.append(Paragraph(
        "The original helper functions in <font face='Courier'>graph.py</font> did not accept "
        "or forward <font face='Courier'>config</font>:",
        S["body"]
    ))
    story.append(Preformatted(
        '# BROKEN — config not forwarded\n'
        'def _run_agent(messages, system_prompt, tools, llm):\n'
        '    bound = llm.bind_tools(tools)\n'
        '    return bound.invoke([SystemMessage(...)] + messages)  # no config!\n\n'
        'def account_node(state, config):          # config received...\n'
        '    ai_msg = _run_agent(state["messages"], prompt, ACCOUNT_TOOLS, _llm)\n'
        '    # ...but never passed to _run_agent',
        S["code"]
    ))

    story.append(Paragraph("<b>The fix</b>", S["h2"]))
    story.append(Paragraph(
        "Thread <font face='Courier'>config</font> through every helper function and every "
        "<font face='Courier'>llm.invoke()</font> call site in the graph:",
        S["body"]
    ))
    story.append(Preformatted(
        '# FIXED — config threaded through all the way\n'
        'def _run_agent(messages, system_prompt, tools, llm, config=None):\n'
        '    bound = llm.bind_tools(tools)\n'
        '    full = [SystemMessage(content=system_prompt)] + messages\n'
        '    return (\n'
        '        bound.invoke(full, config=config)\n'
        '        if config is not None\n'
        '        else bound.invoke(full)\n'
        '    )\n\n'
        'def account_node(state, config):          # config received...\n'
        '    ai_msg = _run_agent(..., config=config)  # ...and passed down',
        S["code"]
    ))
    story.append(Paragraph(
        "This applies to every LLM call in the graph: intent classification in "
        "<font face='Courier'>router_node</font>, transfer classification in "
        "<font face='Courier'>transfer_router_node</font>, the primary tool-calling LLM call "
        "in each agent node, and the final response generation call. Missing even one breaks "
        "token attribution for that node.",
        S["warning"]
    ))

    story.append(PageBreak())

    # ── Section 4: Challenge 3 ─────────────────────────────────────────────────
    story.append(Paragraph("4.  Challenge 3 — Node Names Are Unpredictable", S["h1"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#c0cfe0")))

    story.append(Paragraph("<b>The problem</b>", S["h2"]))
    story.append(Paragraph(
        "When registering a node with LangGraph, developers provide a string label and a "
        "Python function:",
        S["body"]
    ))
    story.append(Preformatted(
        'g.add_node("intl_wire_agent", intl_wire_agent_node)',
        S["code"]
    ))
    story.append(Paragraph(
        "If <font face='Courier'>on_chain_start</font> fires at all for this node, the "
        "<font face='Courier'>serialized[\"name\"]</font> field may contain either the "
        "label (<font face='Courier'>\"intl_wire_agent\"</font>) or the Python function name "
        "(<font face='Courier'>\"intl_wire_agent_node\"</font>), depending on the LangGraph "
        "version and how callbacks are wired. There is no API contract guaranteeing which "
        "form will appear.",
        S["body"]
    ))

    story.append(Paragraph("<b>The fix</b>", S["h2"]))
    story.append(Paragraph(
        "At <font face='Courier'>instrument()</font> time, expand every entry in "
        "<font face='Courier'>agent_nodes</font> into both forms and store the full set:",
        S["body"]
    ))
    story.append(Preformatted(
        'expanded_nodes: Set[str] = set()\n'
        'for n in agent_nodes:\n'
        '    expanded_nodes.add(n)           # "intl_wire_agent"\n'
        '    if n.endswith("_node"):\n'
        '        expanded_nodes.add(n[:-5])  # strip _node\n'
        '    else:\n'
        '        expanded_nodes.add(f"{n}_node")  # add _node\n\n'
        '# Match in on_chain_start:\n'
        'if node_name in self._agent_nodes:  # hits either form',
        S["code"]
    ))
    story.append(Paragraph(
        "This makes the SDK resilient to LangGraph version differences in how node names "
        "are serialized into callback events.",
        S["body"]
    ))

    story.append(PageBreak())

    # ── Section 5: Challenge 4 ─────────────────────────────────────────────────
    story.append(Paragraph("5.  Challenge 4 — Agent Nodes Look Identical to Tool Nodes", S["h1"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#c0cfe0")))

    story.append(Paragraph("<b>The problem</b>", S["h2"]))
    story.append(Paragraph(
        "From the outside, it is impossible to automatically distinguish a node that "
        "implements a sub-agent (an LLM-powered specialist with its own tool set) from a "
        "node that is simply a function dispatching a tool call. Both appear as Python "
        "functions registered with <font face='Courier'>g.add_node()</font>. "
        "There is no introspectable marker in LangGraph's graph structure.",
        S["body"]
    ))
    story.append(Paragraph(
        "This matters because AGENT_TO_AGENT evaluation has a different semantic purpose "
        "than TOOL_CALL evaluation: it is checking the <i>context and intent</i> of a "
        "multi-agent handoff — not just the parameters of a single tool invocation. "
        "Triggering A2A evaluation on every node would produce meaningless audit noise.",
        S["body"]
    ))

    story.append(Paragraph("<b>Why automated detection doesn't work</b>", S["h2"]))
    bullets_ch4 = [
        "Graph introspection reveals node labels and edges — not what the function does internally.",
        "A node that calls an LLM looks identical to one that doesn't until the function is executed.",
        "The tool set bound to a node's LLM is determined at runtime, not at graph construction time.",
        "Multi-agent patterns vary: some use sub-graphs, some use tool-calling LLMs, some use direct function calls.",
    ]
    for b in bullets_ch4:
        story.append(Paragraph(f"• {b}", S["bullet"]))

    story.append(Paragraph("<b>The solution: explicit SDK configuration</b>", S["h2"]))
    story.append(Paragraph(
        "The developer must explicitly declare which nodes or which tool invocations "
        "represent agent-to-agent handoffs. The SDK provides two complementary mechanisms:",
        S["body"]
    ))

    mech_data = [
        ["Parameter", "Trigger", "Reliability", "Best for"],
        ["agent_nodes=[...]", "on_chain_start\nfor matching node name", "Version-dependent;\nmay not fire in LG 0.2.x", "Forward compatibility;\nfuture LangGraph versions"],
        ["a2a_tools={...}", "on_tool_start\nfor matching tool name", "Guaranteed — fired by\nexplicit tool.invoke() call", "Current LangGraph 0.2.x;\nproduction use"],
    ]
    mech_table = Table(mech_data, colWidths=[1.3 * inch, 1.8 * inch, 1.8 * inch, 1.7 * inch])
    mech_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2b45")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LEADING", (0, 0), (-1, -1), 11),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#f5f8fc"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c0cfe0")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(mech_table)
    story.append(Spacer(1, 0.15 * inch))

    story.append(Paragraph(
        "The <font face='Courier'>a2a_tools</font> approach also provides a richer evaluation "
        "payload than the <font face='Courier'>agent_nodes</font> approach: when "
        "<font face='Courier'>international_transfer</font> fires, the tool's structured "
        "arguments (beneficiary name, IBAN, SWIFT code, country, amount) are passed as the "
        "evaluation content. This is exactly the data that AML and sanctions policies need — "
        "far more precise than the raw message history.",
        S["callout"]
    ))

    story.append(PageBreak())

    # ── Section 6: Reliability table ──────────────────────────────────────────
    story.append(Paragraph("6.  What Works Reliably and Why", S["h1"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#c0cfe0")))
    story.append(Spacer(1, 0.1 * inch))

    table_data = [
        ["Capability", "Mechanism", "Reliability", "Notes"],
        ["Layer 1\nUser input\nevaluation",
         "GovernedGraph.invoke()\ncalls client.evaluate()\nbefore graph.invoke()",
         "Guaranteed",
         "Controlled by SDK wrapper;\nnot dependent on LangGraph\ncallbacks at all."],
        ["Layer 2\nTool call\nevaluation",
         "on_tool_start callback;\nfired by explicit\ntool.invoke(config=config)",
         "Guaranteed",
         "Config passed in\n_execute_tools().\nAlways fires."],
        ["Layer A2A\nAgent-to-agent\nevaluation",
         "on_tool_start via\na2a_tools={tool: agent}\nmapping",
         "Guaranteed",
         "Fires before Layer 2 eval\nfor listed tools. Tool args\nbecome A2A content."],
        ["Token counting\nper node",
         "on_llm_end callback;\nrequires config passed\nto every llm.invoke()",
         "Guaranteed\n(after config fix)",
         "Breaks silently if any\nllm.invoke() omits config.\nCheck all call sites."],
        ["Cost attribution\nper node",
         "litellm.cost_per_token()\ncalled in on_llm_end",
         "Best-effort",
         "Falls back gracefully\nif litellm not installed\nor model not in table."],
        ["Agent name\ndiscovery",
         "on_chain_start sets\n_current_node;\non_tool_start fallback",
         "Best-effort\n(on_chain_start);\nGuaranteed\n(on_tool_start)",
         "Agent name in metadata\nfor all eval records\nthat pass through\non_tool_start."],
        ["Per-node A2A via\nagent_nodes param",
         "on_chain_start matches\nnode name to agent_nodes\nexpanded set",
         "Version-dependent;\nunreliable in LG 0.2.x",
         "Keep for forward compat.\nDo not rely on alone.\nUse a2a_tools instead."],
    ]

    col_widths = [1.15 * inch, 1.6 * inch, 1.2 * inch, 2.35 * inch]
    rel_table = Table(table_data, colWidths=col_widths)
    rel_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a2b45")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LEADING", (0, 0), (-1, -1), 11),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#f5f8fc"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c0cfe0")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        # Highlight "Guaranteed" rows
        ("TEXTCOLOR", (2, 1), (2, 1), colors.HexColor("#1a6b2a")),
        ("TEXTCOLOR", (2, 2), (2, 2), colors.HexColor("#1a6b2a")),
        ("TEXTCOLOR", (2, 3), (2, 3), colors.HexColor("#1a6b2a")),
        ("TEXTCOLOR", (2, 4), (2, 4), colors.HexColor("#1a6b2a")),
        # Highlight version-dependent row
        ("TEXTCOLOR", (2, 7), (2, 7), colors.HexColor("#b05a00")),
    ]))
    story.append(rel_table)
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph(
        "<b>Design principle:</b> Any interception point that depends on LangGraph's internal "
        "scheduler calling our callbacks is unreliable. Any interception point that we trigger "
        "ourselves — by passing <font face='Courier'>config</font> to an explicit "
        "<font face='Courier'>invoke()</font> call — is guaranteed.",
        S["callout"]
    ))

    story.append(PageBreak())

    # ── Section 7: Background reading ─────────────────────────────────────────
    story.append(Paragraph("7.  Background Reading", S["h1"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#c0cfe0")))
    story.append(Spacer(1, 0.1 * inch))

    story.append(Paragraph(
        "The following resources provide the foundational knowledge needed to understand "
        "the integration patterns described in this primer.",
        S["body"]
    ))

    sections_reading = [
        ("LangGraph Architecture", [
            ("LangGraph Conceptual Guides — How LangGraph works under the hood",
             "https://langchain-ai.github.io/langgraph/concepts/"),
            ("Pregel-based execution model — background on the distributed graph scheduler",
             "https://langchain-ai.github.io/langgraph/concepts/low_level/"),
            ("LangGraph Persistence and Checkpointing",
             "https://langchain-ai.github.io/langgraph/concepts/persistence/"),
            ("Multi-agent systems in LangGraph",
             "https://langchain-ai.github.io/langgraph/concepts/multi_agent/"),
        ]),
        ("LangChain Callbacks", [
            ("LangChain Callbacks documentation — how on_llm_end, on_tool_start, on_chain_start work",
             "https://python.langchain.com/docs/concepts/callbacks/"),
            ("Custom Callback Handlers",
             "https://python.langchain.com/docs/how_to/custom_callbacks/"),
            ("RunnableConfig and callback propagation",
             "https://python.langchain.com/docs/how_to/callbacks_runtime/"),
        ]),
        ("Token Counting and Cost", [
            ("LiteLLM — unified pricing library used for cost_per_token()",
             "https://github.com/BerriAI/litellm"),
            ("LiteLLM Cost Tracking documentation",
             "https://docs.litellm.ai/docs/completion/token_usage"),
            ("Groq API Documentation",
             "https://console.groq.com/docs/openai"),
        ]),
        ("RAI Trust Platform", [
            ("RAI Trust Platform SRS v2.1 — internal architecture specification",
             "(internal document — see Notion)"),
            ("RAI Trust SDK source — rai_trust_sdk/langgraph/middleware.py",
             "(this repository)"),
        ]),
    ]

    for section_title, links in sections_reading:
        story.append(Paragraph(section_title, S["h2"]))
        for link_text, url in links:
            if url.startswith("http"):
                story.append(Paragraph(
                    f'• {link_text}<br/>&nbsp;&nbsp;&nbsp;&nbsp;'
                    f'<font color="#1a5fb4">{url}</font>',
                    S["bullet"]
                ))
            else:
                story.append(Paragraph(
                    f'• {link_text}<br/>&nbsp;&nbsp;&nbsp;&nbsp;'
                    f'<font color="#7a8fa6">{url}</font>',
                    S["bullet"]
                ))
        story.append(Spacer(1, 0.05 * inch))

    story.append(Spacer(1, 0.3 * inch))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#c0cfe0")))
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph(
        "End of Primer · RAI Trust Platform Engineering · April 2026",
        S["cover_meta"]
    ))

    # ── Build ──────────────────────────────────────────────────────────────────
    doc.build(story, onFirstPage=add_header_footer, onLaterPages=add_header_footer)
    print(f"PDF written to: {OUTPUT}")


if __name__ == "__main__":
    build_pdf()
