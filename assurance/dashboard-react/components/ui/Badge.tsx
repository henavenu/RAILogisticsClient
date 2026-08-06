import { cn } from "@/lib/cn";
import type { Status } from "@/lib/types";

export function StatusDot({ status, className }: { status: Status; className?: string }) {
  const color =
    status === "ok" ? "bg-green" :
    status === "warning" ? "bg-amber" :
    "bg-red";
  return (
    <span className={cn("inline-block w-2 h-2 rounded-full", color, className)} />
  );
}

export function Badge({
  children,
  tone = "default",
  pulse = false,
  className,
}: {
  children: React.ReactNode;
  tone?: "default" | "ok" | "warning" | "critical" | "info" | "violet";
  pulse?: boolean;
  className?: string;
}) {
  const styles =
    tone === "ok"       ? "bg-green/10 text-green-bright border-green/30" :
    tone === "warning"  ? "bg-amber/10 text-amber border-amber/30" :
    tone === "critical" ? "bg-red/10 text-red border-red/30" :
    tone === "info"     ? "bg-cyan/10 text-cyan border-cyan/30" :
    tone === "violet"   ? "bg-violet/10 text-violet border-violet/30" :
                          "bg-surface-elevated text-text-secondary border-border";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold tracking-wide border uppercase",
        styles,
        pulse && "alert-pulse",
        className,
      )}
    >
      {children}
    </span>
  );
}

export function statusToTone(status: Status): "ok" | "warning" | "critical" {
  return status === "ok" ? "ok" : status === "warning" ? "warning" : "critical";
}
