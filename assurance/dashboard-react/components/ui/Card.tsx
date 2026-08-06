import { cn } from "@/lib/cn";

export function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <div
      className={cn(
        "bg-surface border border-border rounded-md",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  icon,
  right,
  accent,
}: {
  title: string;
  icon?: React.ReactNode;
  right?: React.ReactNode;
  accent?: "green" | "violet" | "cyan" | "default";
}) {
  const accentColor =
    accent === "green" ? "text-green-bright" :
    accent === "violet" ? "text-violet" :
    accent === "cyan"  ? "text-cyan" :
    "text-text-secondary";
  return (
    <div className="flex items-center justify-between px-4 py-2.5 border-b border-border">
      <div className="flex items-center gap-2">
        {icon && <span className={accentColor}>{icon}</span>}
        <span className="text-[11px] font-semibold tracking-[0.12em] uppercase text-text-secondary">
          {title}
        </span>
      </div>
      {right}
    </div>
  );
}
