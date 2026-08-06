"use client";

import { ShieldAlert, Coins, Zap, ShieldCheck, AlertOctagon, ShieldOff } from "lucide-react";
import { cn } from "@/lib/cn";

function Kpi({
  label,
  value,
  sublabel,
  tone = "default",
  icon,
}: {
  label: string;
  value: React.ReactNode;
  sublabel?: string;
  tone?: "default" | "good" | "warn" | "bad";
  icon?: React.ReactNode;
}) {
  const valueColor =
    tone === "good" ? "text-green-bright" :
    tone === "warn" ? "text-amber" :
    tone === "bad"  ? "text-red" :
    "text-text-primary";
  return (
    <div className="flex-1 min-w-0 px-4 py-3 border-r border-border last:border-r-0">
      <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-text-muted">
        {icon && <span className={cn("opacity-70", valueColor)}>{icon}</span>}
        {label}
      </div>
      <div className={cn("mt-1 text-[22px] font-bold tabular leading-tight", valueColor)}>
        {value}
      </div>
      {sublabel && (
        <div className="text-[10.5px] text-text-muted mt-0.5">{sublabel}</div>
      )}
    </div>
  );
}

export function KpiStrip() {
  return (
    <div className="bg-surface border-b border-border">
      <div className="flex">
        <Kpi
          label="App Trust"
          value={<><span>31.0</span><span className="text-[14px] text-text-muted">%</span></>}
          sublabel="99 evals"
          tone="bad"
          icon={<ShieldCheck size={11} />}
        />
        <Kpi
          label="Violations"
          value={
            <span className="flex items-baseline gap-1.5">
              <span className="text-red">64</span>
              <span className="text-amber text-[18px]">5</span>
              <span className="text-red text-[18px]">30</span>
            </span>
          }
          sublabel="pass / normative / coercive"
          icon={<ShieldAlert size={11} />}
        />
        <Kpi
          label="App Tokens"
          value="11.5K"
          sublabel="total cost $0.0069"
          icon={<Coins size={11} />}
        />
        <Kpi
          label="Evals / Min"
          value="0.07"
          sublabel="99 evals · 1d window"
          icon={<Zap size={11} />}
        />
        <Kpi
          label="Block Rate"
          value={<><span>30.3</span><span className="text-[14px] text-text-muted">%</span></>}
          sublabel="coercive %"
          tone="bad"
          icon={<ShieldOff size={11} />}
        />
        <Kpi
          label="Open Alerts"
          value="20"
          sublabel="20 high"
          tone="bad"
          icon={<AlertOctagon size={11} />}
        />
      </div>
      <div className="px-4 py-1 text-[10px] text-text-muted/70 italic border-t border-border/40">
        Sharad&apos;s integrated chrome (placeholder values for KPI strip, Policy Evaluations, Verdict Distribution, and Tier 1 cards).
        Our Assurance Layer (right rail) shows live API values.
      </div>
    </div>
  );
}
