import { cn } from "@/lib/cn";

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "inline-block w-3.5 h-3.5 border-2 border-text-muted/30 border-t-text-secondary rounded-full animate-spin",
        className,
      )}
    />
  );
}
