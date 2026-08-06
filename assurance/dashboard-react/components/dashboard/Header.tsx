"use client";

import { RefreshCw } from "lucide-react";
import Link from "next/link";
import { Spinner } from "@/components/ui/Spinner";
import type { AppFilter } from "@/lib/metricApps";

export const WINDOW_OPTIONS = [1, 4, 12, 24] as const;
export type WindowHours = typeof WINDOW_OPTIONS[number];

export function Header({
  window,
  onWindowChange,
  onRefresh,
  loading,
  asOf,
  app,
  onAppChange,
}: {
  window: WindowHours;
  onWindowChange: (w: WindowHours) => void;
  onRefresh: () => void;
  loading: boolean;
  asOf?: string | null;
  app: AppFilter;
  onAppChange: (a: AppFilter) => void;
}) {
  const updated = asOf ? new Date(asOf).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
  return (
    <header className="border-b border-border bg-surface/60 backdrop-blur-sm">
      {/* Top sub-tabs strip (mimics Sharad's "App Stack | Rules | Live Tail | Privacy") */}
      <div className="border-b border-border/60 px-5 py-1.5 flex items-center gap-5 text-[12px] text-text-muted">
        <span className="hover:text-text-primary cursor-pointer">App Stack</span>
        <span className="hover:text-text-primary cursor-pointer">Rules</span>
        <span className="hover:text-text-primary cursor-pointer">Live Tail</span>
        <span className="hover:text-text-primary cursor-pointer">Privacy</span>
      </div>
      <div className="px-5 py-3 flex items-center gap-4">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-green animate-pulse" />
          <h1 className="text-[13px] font-bold tracking-[0.18em] uppercase">
            Responsible AI Dashboard
          </h1>
        </div>
        <span className="text-[11px] text-text-muted tabular">Updated {updated}</span>

        <nav className="ml-6 flex items-center gap-1 text-[11px]">
          <Link
            href="/"
            className="px-3 py-1 rounded border border-border bg-surface-elevated text-text-primary"
          >
            Overview
          </Link>
          <Link
            href="/llm-evals"
            className="px-3 py-1 rounded border border-transparent text-text-secondary hover:text-text-primary hover:border-border"
          >
            LLM Evals
          </Link>
        </nav>

        <div className="ml-auto flex items-center gap-3">
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-text-muted uppercase tracking-wider">App</span>
            <select
              value={app}
              onChange={(e) => onAppChange(e.target.value as AppFilter)}
              className="bg-surface-elevated border border-border rounded px-2.5 py-1 text-[12px] text-text-primary outline-none focus:border-border-strong"
            >
              <option value="all">All Apps</option>
              <option value="loan_decision">loan_decision</option>
              <option value="support_chat">support_chat</option>
            </select>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-text-muted uppercase tracking-wider">Window</span>
            <select
              value={window}
              onChange={(e) => onWindowChange(Number(e.target.value) as WindowHours)}
              className="bg-surface-elevated border border-border rounded px-2.5 py-1 text-[12px] text-text-primary outline-none focus:border-border-strong tabular"
            >
              {WINDOW_OPTIONS.map((w) => (
                <option key={w} value={w}>{w}h</option>
              ))}
            </select>
          </div>
          <button
            onClick={onRefresh}
            disabled={loading}
            className="inline-flex items-center gap-1.5 px-3 py-1 rounded border border-border bg-surface-elevated hover:bg-border text-[12px] disabled:opacity-50"
          >
            {loading ? <Spinner /> : <RefreshCw size={13} />}
            Refresh
          </button>
        </div>
      </div>
    </header>
  );
}
