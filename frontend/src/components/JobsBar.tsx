import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { X } from "lucide-react";
import { toast } from "sonner";

import { DeleteDialog } from "@/components/DeleteDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { api, type Job } from "@/lib/api";
import { etaLabel, jobKindLabel } from "@/lib/format";
import { useLiveJobs } from "@/lib/live";

/** What stopping this job costs, said in the dialog because the two cases differ.
 *
 *  A merge or a proxy is not offered at all: nobody asked for it, and the next scan
 *  would start it again. The dead button says so on hover rather than in a paragraph.
 */
const STOPPABLE: Record<string, string> = {
  render: "The stabilized file is not written, and the sequence goes back to the queue, so it can be launched again.",
  grade: "The look stays exactly as it is, so it can be encoded again. Only the minutes already spent are lost.",
};

/**
 * Persistent progress bar, fed by the SSE stream the layout holds open.
 *
 * The API runs nothing: it reads back the `job` table the worker updates.
 */
export function JobsBar() {
  const jobs = useLiveJobs();
  const [doomed, setDoomed] = useState<Job | null>(null);

  const stop = useMutation({
    mutationFn: (job: Job) => api.cancelJob(job.id),
    onError: (error: Error) => toast.error(error.message),
  });

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
              <span className="tnum flex shrink-0 items-center gap-2 text-muted-foreground">
                {etaLabel(job.progress, job.started_at) ?? ""}
                <span>{Math.round(job.progress * 100)} %</span>
                {/* Stopping a job in flight throws away minutes, so it asks first. */}
                <span
                  title={
                    STOPPABLE[job.kind]
                      ? "Stop this job"
                      : "A merge or a proxy is not cancelled: the next scan would start it again"
                  }
                >
                  <Button
                    size="icon"
                    variant="ghost"
                    className="h-6 w-6"
                    disabled={!STOPPABLE[job.kind] || stop.isPending}
                    onClick={() => setDoomed(job)}
                  >
                    <X className="h-3.5 w-3.5" />
                  </Button>
                </span>
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

      <DeleteDialog
        open={doomed !== null}
        action="Stop"
        title={`Stop this ${doomed ? jobKindLabel(doomed.kind) : ""} job?`}
        note={doomed ? STOPPABLE[doomed.kind] : undefined}
        onClose={() => setDoomed(null)}
        onConfirm={() => doomed && stop.mutate(doomed)}
      />
    </div>
  );
}
