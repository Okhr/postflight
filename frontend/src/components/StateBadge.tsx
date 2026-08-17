import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const LABELS: Record<string, string> = {
  new: "to merge",
  merging: "merging…",
  merged: "merged",
  proxying: "proxying…",
  ready: "ready",
  failed: "failed",
  queued: "queued",
  running: "running",
  done: "done",
  cancelled: "cancelled",
  seen: "seen",
  ingested: "ingested",
  draft: "draft",
};

const TONES: Record<string, string> = {
  ready: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  done: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  failed: "bg-red-500/15 text-red-400 border-red-500/30",
  running: "bg-sky-500/15 text-sky-400 border-sky-500/30",
  merging: "bg-sky-500/15 text-sky-400 border-sky-500/30",
  proxying: "bg-sky-500/15 text-sky-400 border-sky-500/30",
  queued: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  new: "bg-amber-500/15 text-amber-400 border-amber-500/30",
};

export function StateBadge({ state, className }: { state: string; className?: string }) {
  return (
    <Badge
      variant="outline"
      className={cn("font-normal", TONES[state] ?? "text-muted-foreground", className)}
    >
      {LABELS[state] ?? state}
    </Badge>
  );
}
