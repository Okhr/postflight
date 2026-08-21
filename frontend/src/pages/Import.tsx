import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Play, RotateCcw, Scissors, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { UploadZone } from "@/components/UploadZone";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Progress } from "@/components/ui/progress";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, type Job, type Sequence } from "@/lib/api";
import { etaLabel, formatBytes, formatDateTime, formatDuration } from "@/lib/format";
import { useLiveJobs } from "@/lib/live";
import { cn } from "@/lib/utils";

/** What is left to produce before this rush can be derushed. */
function isPending(sequence: Sequence) {
  return sequence.state !== "ready";
}

/**
 * State of one step on one row: done, running (with its progress), queued, or
 * failed. Progress comes from the job, existence from the sequence itself: a job
 * that is gone does not un-produce what it made.
 */
function StepCell({
  done,
  job,
  failed,
}: {
  done: boolean;
  job?: Job;
  failed: boolean;
}) {
  if (job?.state === "running") {
    const eta = etaLabel(job.progress, job.started_at);
    return (
      <div className="space-y-1">
        <div className="flex items-center justify-center gap-2">
          <Progress value={job.progress * 100} className="h-1.5 w-16" />
          <span className="tnum text-sm text-muted-foreground">
            {Math.round(job.progress * 100)}%
          </span>
        </div>
        {eta && <p className="text-center text-xs text-muted-foreground">{eta}</p>}
      </div>
    );
  }
  if (done) return <Check className="mx-auto h-4 w-4 text-emerald-400" />;
  if (failed) return <span className="text-sm text-red-400">failed</span>;
  if (job?.state === "queued") return <span className="text-sm text-muted-foreground">queued</span>;
  return <span className="text-sm text-muted-foreground">-</span>;
}

export function Import() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [toDelete, setToDelete] = useState<Sequence | null>(null);

  const { data: status } = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
    refetchInterval: 10_000,
  });
  const { data: sequences, isLoading } = useQuery({
    queryKey: ["sequences"],
    queryFn: () => api.sequences(),
    // The live stream invalidates this the moment a job moves; this interval is
    // only insurance against a dead stream.
    refetchInterval: 30_000,
  });

  // Pushed, not polled: the stream carries exactly the queued and running jobs,
  // which is all a row needs to show: progress, queued, or nothing.
  const jobs = useLiveJobs();

  const rows = sequences ?? [];
  const pending = rows.filter(isPending);

  const steps = new Map<number, { merge?: Job; proxy?: Job }>();
  for (const job of jobs) {
    if (job.sequence_id == null || job.kind === "render") continue;
    const entry = steps.get(job.sequence_id) ?? {};
    const slot = job.kind === "merge" ? "merge" : "proxy";
    if (!entry[slot]) entry[slot] = job;
    steps.set(job.sequence_id, entry);
  }

  // A rush that is unfinished *and* has nothing in flight: the pipeline enqueues
  // on its own, so this only happens after a failure or a dropped job. That is the
  // only case where the resume button means anything. Showing it while a job is
  // running just makes people wonder what it would do.
  const stuck = pending.filter((sequence) => {
    const step = steps.get(sequence.id) ?? {};
    return ![step.merge, step.proxy].some(
      (job) => job?.state === "queued" || job?.state === "running",
    );
  });
  const isStuck = (sequence: Sequence) => stuck.some((s) => s.id === sequence.id);

  const merge = useMutation({
    mutationFn: api.retrySequence,
    onSuccess: () => queryClient.invalidateQueries(),
    onError: (error: Error) => toast.error(error.message),
  });

  const remove = useMutation({
    mutationFn: ({ id, purge }: { id: number; purge: boolean }) =>
      api.deleteSequence(id, !purge, !purge),
    onSuccess: (data, variables) => {
      toast.success(
        variables.purge
          ? `${data.deleted} deleted, ${data.files_removed.length} file(s) removed from disk`
          : `${data.deleted} removed, files kept on disk`,
      );
      setToDelete(null);
      queryClient.invalidateQueries();
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const hardwareNotes = (status?.workers ?? []).flatMap((worker) =>
    (worker.capabilities.notes ?? []).map((note) => ({ worker: worker.name, note })),
  );

  return (
    <div className="space-y-6">
      <UploadZone />

      {hardwareNotes.length > 0 && (
        <Card className="border-amber-500/30 bg-amber-500/5">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <AlertTriangle className="h-4 w-4 text-amber-400" />
              Hardware notes
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-sm text-muted-foreground">
            {hardwareNotes.map(({ worker, note }) => (
              <p key={`${worker}:${note}`}>
                <span className="font-medium">{worker}</span>: {note}
              </p>
            ))}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="text-base">Rushes</CardTitle>
            </div>
            <div className="flex items-center gap-2">
              {stuck.length > 0 && (
                <Button
                  size="sm"
                  variant="outline"
                  title="Start the missing step on every unfinished rush"
                  disabled={merge.isPending}
                  onClick={() => stuck.forEach((sequence) => merge.mutate(sequence.id))}
                >
                  <Play className="h-4 w-4" />
                  Resume {stuck.length} stalled
                </Button>
              )}
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No rush yet. Drop files above, or copy them into <code>inbox/</code> and hit Scan.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Source files</TableHead>
                  <TableHead className="text-center">Filmed</TableHead>
                  <TableHead className="text-center">Length</TableHead>
                  <TableHead className="text-center">Size</TableHead>
                  <TableHead className="w-32 text-center">Merged</TableHead>
                  <TableHead className="w-32 text-center">Proxy</TableHead>
                  <TableHead className="text-right" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((sequence) => {
                  const step = steps.get(sequence.id) ?? {};
                  const merged = sequence.merged_name !== null;
                  const failed = sequence.state === "failed";
                  const ready = sequence.state === "ready";
                  return (
                    <TableRow
                      key={sequence.id}
                      // A ready rush is there to be derushed, so the row carries it.
                      onClick={ready ? () => navigate(`/derush/${sequence.id}`) : undefined}
                      title={ready ? "Open in derush" : undefined}
                      className={cn(ready && "cursor-pointer")}
                    >
                      <TableCell className="max-w-[24rem]">
                        <ul className="space-y-0.5">
                          {sequence.part_names.map((name) => (
                            <li
                              key={name}
                              className="truncate font-mono text-sm"
                              title={name}
                            >
                              {name}
                            </li>
                          ))}
                        </ul>
                        <p className="mt-0.5 text-sm text-muted-foreground">
                          {sequence.width}×{sequence.height} · {sequence.fps.toFixed(2)} fps
                          {!sequence.has_gyro && (
                            <span className="text-red-400"> · no gyro data</span>
                          )}
                        </p>
                        {sequence.error && (
                          <p className="mt-1 line-clamp-2 text-sm text-red-400">{sequence.error}</p>
                        )}
                      </TableCell>

                      <TableCell className="tnum whitespace-nowrap text-center text-sm">
                        {formatDateTime(sequence.recorded_at)}
                      </TableCell>
                      <TableCell className="tnum text-center text-sm">
                        {formatDuration(sequence.duration_ms)}
                      </TableCell>
                      <TableCell className="tnum text-center text-sm">
                        {formatBytes(sequence.size_bytes)}
                      </TableCell>

                      <TableCell className="text-center">
                        <StepCell done={merged} job={step.merge} failed={failed && !merged} />
                      </TableCell>
                      <TableCell className="text-center">
                        <StepCell
                          done={sequence.has_proxy}
                          job={step.proxy}
                          failed={failed && merged}
                        />
                      </TableCell>

                      <TableCell onClick={(event) => event.stopPropagation()}>
                        <div className="flex justify-end gap-1">
                          {ready && (
                            <Button
                              size="icon"
                              variant="outline"
                              title="Open in derush"
                              onClick={() => navigate(`/derush/${sequence.id}`)}
                            >
                              <Scissors className="h-4 w-4" />
                            </Button>
                          )}
                          {isStuck(sequence) && (
                            <Button
                              size="icon"
                              variant="outline"
                              title={
                                failed
                                  ? "Retry the step that failed"
                                  : "Start the missing step"
                              }
                              disabled={merge.isPending}
                              onClick={() => merge.mutate(sequence.id)}
                            >
                              {failed ? (
                                <RotateCcw className="h-4 w-4" />
                              ) : (
                                <Play className="h-4 w-4" />
                              )}
                            </Button>
                          )}
                          <Button
                            size="icon"
                            variant="ghost"
                            title="Remove, or delete for good"
                            onClick={() => setToDelete(sequence)}
                          >
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}

        </CardContent>
      </Card>

      <DeleteDialog
        sequence={toDelete}
        pending={remove.isPending}
        onCancel={() => setToDelete(null)}
        onConfirm={(purge) => toDelete && remove.mutate({ id: toDelete.id, purge })}
      />
    </div>
  );
}

/**
 * Two ways out, because they are not the same act.
 *
 * Measured, not assumed: keeping the raw files leaves the clips in the database,
 * merely detached, so the next scan regroups them and the rush is back within 30 s,
 * already merged, since the artifacts carry the content hash. That makes it a
 * reset of the grouping, not a removal. And renders always go, whatever is kept:
 * they belong to the cuts being deleted.
 */
function DeleteDialog({
  sequence,
  pending,
  onCancel,
  onConfirm,
}: {
  sequence: Sequence | null;
  pending: boolean;
  onCancel: () => void;
  onConfirm: (purge: boolean) => void;
}) {
  return (
    <Dialog open={sequence !== null} onOpenChange={(open) => !open && onCancel()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{sequence?.label}</DialogTitle>
          <DialogDescription>
            {sequence?.part_count === 1
              ? "1 source file"
              : `${sequence?.part_count} source files`}{" "}
            · {formatBytes(sequence?.size_bytes)}
            {sequence?.cut_count ? ` · ${sequence.cut_count} marked sequence(s)` : ""}
            {sequence?.render_count ? ` · ${sequence.render_count} stabilized clip(s)` : ""}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-sm">
          <p className="text-muted-foreground">
            <span className="font-medium text-foreground">Reset grouping</span> keeps the files.
            The next scan brings this rush back, already merged.
          </p>
          <p className="text-muted-foreground">
            <span className="font-medium text-foreground">Delete the files</span> frees{" "}
            {formatBytes(sequence?.size_bytes)}. Re-importing re-encodes everything.
          </p>
          {(sequence?.render_count ?? 0) > 0 && (
            <p className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-amber-300">
              Either way, {sequence?.render_count} stabilized clip(s) are erased.
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={onCancel} disabled={pending}>
            Cancel
          </Button>
          <Button variant="outline" onClick={() => onConfirm(false)} disabled={pending}>
            <RotateCcw className="h-4 w-4" />
            Reset grouping
          </Button>
          <Button
            className="bg-red-600 text-white hover:bg-red-600/90"
            onClick={() => onConfirm(true)}
            disabled={pending}
          >
            <Trash2 className="h-4 w-4" />
            Delete the files
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
