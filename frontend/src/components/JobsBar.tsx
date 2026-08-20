import { Progress } from "@/components/ui/progress";
import { StateBadge } from "@/components/StateBadge";
import { etaLabel } from "@/lib/format";
import { useLiveJobs } from "@/lib/live";

const KIND_LABELS: Record<string, string> = {
  merge: "merge",
  proxy: "proxy",
  render: "stabilize",
  grade: "color",
};

/**
 * Persistent progress bar, fed by the SSE stream the layout holds open.
 *
 * The API runs nothing: it reads back the `job` table the worker updates.
 */
export function JobsBar() {
  const jobs = useLiveJobs();

  if (jobs.length === 0) return null;

  const running = jobs.filter((j) => j.state === "running");
  const queued = jobs.length - running.length;

  return (
    <div className="border-b bg-card/60">
      <div className="mx-auto max-w-7xl space-y-2 px-4 py-3">
        {running.map((job) => (
          <div key={job.id} className="space-y-1">
            <div className="flex items-center justify-between gap-3 text-sm">
              <span className="flex items-center gap-2">
                <StateBadge state={job.state} />
                <span className="font-medium">{KIND_LABELS[job.kind] ?? job.kind}</span>
                {job.sequence_key && (
                  <span className="text-muted-foreground">{job.sequence_key}</span>
                )}
              </span>
              <span className="tnum text-muted-foreground">
                {etaLabel(job.progress, job.started_at) ?? ""}
                <span className="ml-2">{Math.round(job.progress * 100)} %</span>
              </span>
            </div>
            <Progress value={job.progress * 100} className="h-1.5" />
          </div>
        ))}
        {queued > 0 && (
          <p className="text-sm text-muted-foreground">
            {queued} job{queued > 1 ? "s" : ""} queued
          </p>
        )}
      </div>
    </div>
  );
}
