Subject: Next step — closing the loop from Governance to NEUROAI Trust (ACAP-gated agent activation)

Hi all,

I want to propose our next milestone, which converges the two halves of the platform: governance approval becomes the precondition for an agent to earn trust and go live. This mirrors the World Economic Forum's new AI Agent Capability and Authorization Profile (ACAP), and the good news is that we already have most of the pieces built.

Where we are today. Every app onboarded to the platform auto-registers its agents into the Agent Registry, and each agent gets a governance Agent Card, which is effectively its ACAP: it defines the agent's purpose, authority, allowed and prohibited actions, data and tool access, and human-oversight mode. Think of it as the employment contract for the agent. What's missing is the gate between an agent being listed and an agent being live.

What we're adding. We introduce a single Governance Gate that plugs in right after onboarding. The flow becomes: an app is onboarded, its agents are listed in the registry, the agents are sent through the Governance Gate for review and approval, and only then are they activated on the Trust platform. Every gate decision carries a named human reviewer, and on approval the agent card is stamped with an "Approved" seal showing who signed off and when, so activation is always tied to an accountable person rather than an automatic flag. Before the gate, an agent is ungoverned: it shows as "Pending Governance" and cannot act, because the Policy Engine will not route to it. After approval, it becomes a governed agent: activated in the live topology, running a trust score, and monitored across turns by Sentinel. If its authority later changes (a new tool, new data access, higher privilege) or it breaches policy, it is automatically suspended and sent back through the gate.

Ongoing cadence. Governance is not one-and-done. Re-review runs on a cadence set by each agent's live risk score, weekly for low-risk agents and daily for high-risk or money-movement agents, so authorization stays current as behavior drifts.

Why this makes a strong leadership demo. It is a single, visual story on one screen. An agent appears greyed out as ungoverned, a reviewer approves its card, and it lights up live in the topology and begins transacting. We can then trip a breach and watch it get suspended and re-queued. It turns an abstract governance promise into something concrete and auditable in under two minutes.

The full design and build steps are captured in GOVERNANCE_TO_TRUST.md. I'd like to scope this as our next sprint. Happy to walk anyone through it.

Thanks,
[name]
