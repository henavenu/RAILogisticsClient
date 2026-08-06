import { Card, CardHeader } from "@/components/ui/Card";

function EvalRow({ name, value, tone }: { name: string; value: string; tone: "good" | "bad" }) {
  const color = tone === "good" ? "text-green-bright" : "text-red";
  const bar = tone === "good" ? "bg-green/60" : "bg-red/60";
  const pct = parseInt(value, 10) || 0;
  return (
    <div className="flex items-center gap-2 py-1.5">
      <span className={`w-1.5 h-1.5 rounded-full ${tone === "good" ? "bg-green" : "bg-red"}`} />
      <span className="flex-1 text-[12px] text-text-secondary truncate">{name}</span>
      <div className="w-12 h-1 rounded bg-surface-elevated overflow-hidden">
        <div className={bar} style={{ width: `${pct}%`, height: "100%" }} />
      </div>
      <span className={`text-[12px] tabular ${color} w-10 text-right`}>{value}</span>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="px-4 py-2 border-b border-border/50 last:border-b-0">
      <div className="text-[10.5px] font-semibold tracking-[0.15em] uppercase text-text-muted mb-1">{title}</div>
      {children}
    </div>
  );
}

export function PolicyEvalsRail() {
  return (
    <Card>
      <CardHeader title="Policy Evaluations · 24h" />
      <div className="py-1">
        <Section title="Safety">
          <EvalRow name="Prompt safety scor…" value="100%" tone="good" />
          <EvalRow name="Response toxicity …" value="100%" tone="good" />
        </Section>
        <Section title="Security">
          <EvalRow name="PII exposure flag" value="80%" tone="bad" />
        </Section>
        <Section title="Other">
          <EvalRow name="Sensitive data req…" value="100%" tone="good" />
          <EvalRow name="A2A Risk Score (0…" value="0%" tone="bad" />
          <EvalRow name="Agent step succes…" value="0%" tone="bad" />
          <EvalRow name="A2A violation rate" value="0%" tone="bad" />
          <EvalRow name="Unsanctioned A2…" value="100%" tone="good" />
        </Section>
      </div>
    </Card>
  );
}
