export function DashboardFooter() {
  return (
    <footer className="px-5 py-2 border-t border-border text-[10px] text-text-muted flex items-center gap-3">
      <span className="flex items-center gap-1.5">
        <span className="w-1.5 h-1.5 rounded-full bg-green animate-pulse" />
        Streaming T1
      </span>
      <span>·</span>
      <span>Lagging T2/T3</span>
      <span>·</span>
      <span>Config driven</span>
      <span className="ml-auto text-text-muted/60">RAI Assurance · Cognizant</span>
    </footer>
  );
}
