import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { etaLabel, jobKindLabel } from "@/lib/format";
import { useLiveJobs } from "@/lib/live";

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
      <div className="space-y-2 px-6 py-3">
        {running.map((job) => (
          <div key={job.id} className="space-y-1">
            <div className="flex items-center justify-between gap-3 text-sm">
              <span className="flex min-w-0 items-center gap-2">
                <span>Running task</span>
                <Badge variant="secondary" className="font-normal">
                  {jobKindLabel(job.kind)}
                </Badge>
                {/* The names the rest of the interface uses. A merge or a proxy is
                    about the whole rush and says only that; a render or a grade adds
                    the sequence it is working on. The key is the file, kept on hover. */}
                {(job.sequence_label || job.sequence_key) && (
                  <span className="truncate text-muted-foreground" title={job.sequence_key ?? ""}>
                    {[job.sequence_label || job.sequence_key, job.cut_label]
                      .filter(Boolean)
                      .join(" · ")}
                  </span>
                )}
                {/* Which machine is on it (florian, 2026-08-25). One word, and the only
                    thing that tells two identical-looking rows apart. */}
                {job.worker_name && (
                  <span className="shrink-0 text-muted-foreground">on {job.worker_name}</span>
                )}
              </span>
              <span className="tnum shrink-0 text-muted-foreground">
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
