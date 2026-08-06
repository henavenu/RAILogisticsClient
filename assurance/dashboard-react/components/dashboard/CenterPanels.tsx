"use client";

import { PieChart, Pie, Cell, ResponsiveContainer } from "recharts";
import { Card, CardHeader } from "@/components/ui/Card";

const VERDICT_DATA = [
  { name: "Pass", value: 65, color: "var(--green)" },
  { name: "Norm", value: 5,  color: "var(--amber)" },
  { name: "Block", value: 30, color: "var(--red)" },
];

const EVAL_BARS = [
  { name: "PII exposure f…", value: 99, tone: "bad" as const, ev: "99 ev" },
  { name: "A2A Risk Sco…", value: 6, tone: "bad" as const, ev: "6 ev" },
  { name: "A2A violation…", value: 6, tone: "bad" as const, ev: "6 ev" },
  { name: "Sensitive dat…", value: 40, tone: "good" as const, ev: "40 ev" },
  { name: "Prompt safet…", value: 34, tone: "good" as const, ev: "34 ev" },
  { name: "Response tox…", value: 29, tone: "good" as const, ev: "29 ev" },
  { name: "Agent step s…", value: 24, tone: "good" as const, ev: "24 ev" },
  { name: "Unsanctione…", value: 6, tone: "amber" as const, ev: "6 ev" },
];

const TIER1_CARDS = [
  { label: "PII Exposure Flag", value: "20.2%", evals: "99 evals", tone: "bad" },
  { label: "A2A Risk Score (0–1)", value: "37.5%", evals: "6 evals", tone: "bad" },
  { label: "A2A Violation Rate", value: "100.0%", evals: "6 evals", tone: "bad" },
  { label: "Prompt Safety Score (0–1)", value: "100.0%", evals: "34 evals", tone: "good" },
  { label: "Response Toxicity Score (0–1)", value: "0.0%", evals: "29 evals", tone: "good" },
  { label: "Sensitive Data Request Flag", value: "0.0%", evals: "40 evals", tone: "good" },
  { label: "Agent Step Success Flag", value: "100.0%", evals: "24 evals", tone: "good" },
  { label: "Unsanctioned A2A Flag", value: "0.0%", evals: "6 evals", tone: "good" },
] as const;

function VerdictDonut() {
  return (
    <Card className="p-3">
      <div className="text-[10.5px] font-semibold tracking-[0.12em] uppercase text-text-muted text-center">
        Verdict Distribution
      </div>
      <div className="relative h-[140px] mt-1">
        <ResponsiveContainer>
          <PieChart>
            <Pie
              data={VERDICT_DATA}
              dataKey="value"
              cx="50%" cy="50%"
              innerRadius={42} outerRadius={62}
              startAngle={90} endAngle={-270}
              stroke="none"
              isAnimationActive={false}
            >
              {VERDICT_DATA.map((d, i) => (
                <Cell key={i} fill={d.color} />
              ))}
            </Pie>
          </PieChart>
        </ResponsiveContainer>
        <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
          <div className="text-[24px] font-bold text-green-bright tabular">65%</div>
        </div>
      </div>
      <div className="text-[10.5px] flex flex-col gap-0.5 mt-1">
        <div className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-green" /><span className="text-text-secondary">Pass 65%</span></div>
        <div className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-amber" /><span className="text-text-secondary">Norm 5%</span></div>
        <div className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-red" /><span className="text-text-secondary">Block 30%</span></div>
      </div>
    </Card>
  );
}

function EvalBars() {
  const max = Math.max(...EVAL_BARS.map((b) => b.value));
  return (
    <Card className="px-3 py-3">
      <div className="text-[10.5px] font-semibold tracking-[0.12em] uppercase text-text-muted mb-2">
        Evaluation Distribution
      </div>
      <div className="space-y-1">
        {EVAL_BARS.map((b) => {
          const color = b.tone === "good" ? "bg-green/70" : b.tone === "amber" ? "bg-amber/70" : "bg-red/70";
          return (
            <div key={b.name} className="flex items-center gap-2 text-[11px]">
              <span className="w-24 truncate text-text-secondary">{b.name}</span>
              <div className="flex-1 h-2.5 bg-surface-elevated rounded overflow-hidden">
                <div className={`h-full ${color}`} style={{ width: `${(b.value / max) * 100}%` }} />
              </div>
              <span className="text-text-muted tabular w-10 text-right">{b.ev}</span>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function Tier1Cards() {
  return (
    <Card>
      <CardHeader title="Tier 1 · All Governance Dimensions" />
      <div className="grid grid-cols-3 gap-px bg-border">
        {TIER1_CARDS.map((c) => {
          const color = c.tone === "good" ? "text-green-bright" : "text-red";
          return (
            <div key={c.label} className="bg-surface px-3 py-3">
              <div className="text-[9.5px] font-semibold uppercase tracking-wider text-text-muted">
                {c.label}
              </div>
              <div className={`mt-1 text-[20px] font-bold tabular ${color}`}>{c.value}</div>
              <div className="text-[10px] text-text-muted">{c.evals}</div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

export function CenterPanels() {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-[200px_1fr] gap-3">
        <VerdictDonut />
        <EvalBars />
      </div>
      <Tier1Cards />
    </div>
  );
}
