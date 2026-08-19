import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Combine, Play, RotateCcw, Scissors, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { UploadZone } from "@/components/UploadZone";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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
import { ApiError, api, type Job, type Sequence } from "@/lib/api";
import { etaLabel, formatBytes, formatDateTime, formatDuration } from "@/lib/format";
import { useLiveJobs } from "@/lib/live";
import { cn } from "@/lib/utils";

/** What is left to produce before this rush can be derushed. */
function isPending(sequence: Sequence) {
  return sequence.state !== "ready";
}

/**
 * State of one step on one row: done, running (with its progress), queued, or
 * failed. Progress comes from the job, existence from the sequence itself — a job
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
          <span className="tnum text-xs text-muted-foreground">
            {Math.round(job.progress * 100)}%
          </span>
        </div>
        {eta && <p className="text-center text-[11px] text-muted-foreground">{eta}</p>}
      </div>
    );
  }
  if (done) return <Check className="mx-auto h-4 w-4 text-emerald-400" />;
  if (failed) return <span className="text-xs text-red-400">failed</span>;
  if (job?.state === "queued") return <span className="text-xs text-muted-foreground">queued</span>;
  return <span className="text-xs text-muted-foreground">—</span>;
}

export function Import() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [selection, setSelection] = useState<number[]>([]);
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
  // which is all a row needs to show — progress, queued, or nothing.
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
  // only case where the resume button means anything — showing it while a job is
  // running just makes people wonder what it would do.
  const stuck = pending.filter((sequence) => {
    const step = steps.get(sequence.id) ?? {};
    return ![step.merge, step.proxy].some(
      (job) => job?.state === "queued" || job?.state === "running",
    );
  });
  const isStuck = (sequence: Sequence) => stuck.some((s) => s.id === sequence.id);

  const toggle = (id: number) =>
    setSelection((previous) =>
      previous.includes(id) ? previous.filter((x) => x !== id) : [...previous, id],
    );

  const merge = useMutation({
    mutationFn: api.retrySequence,
    onSuccess: () => queryClient.invalidateQueries(),
    onError: (error: Error) => toast.error(error.message),
  });

  const regroup = useMutation({
    mutationFn: ({ ids, force }: { ids: number[]; force: boolean }) =>
      api.regroupSequences(ids, force),
    onSuccess: (sequence) => {
      toast.success(`${sequence.part_count} files queued for merging`);
      setSelection([]);
      queryClient.invalidateQueries();
    },
    onError: (error: Error, variables) => {
      // 409 = at least one rush is already ready; redoing it destroys its proxy
      // and its zones, so ask for confirmation.
      if (error instanceof ApiError && error.status === 409) {
        if (window.confirm(`${error.message}\n\nRedo anyway? Marked zones on those rushes will be lost.`)) {
          regroup.mutate({ ids: variables.ids, force: true });
        }
        return;
      }
      toast.error(error.message);
    },
  });

  const remove = useMutation({
    mutationFn: ({ id, purge }: { id: number; purge: boolean }) =>
      api.deleteSequence(id, !purge, !purge),
    onSuccess: (data, variables) => {
      toast.success(
        variables.purge
          ? `${data.deleted} deleted — ${data.files_removed.length} file(s) removed from disk`
          : `${data.deleted} removed — files kept on disk`,
      );
      setToDelete(null);
      setSelection([]);
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
              <CardDescription>
                Parts of the same flight are detected and joined losslessly, gyro data kept.
              </CardDescription>
            </div>
            <div className="flex items-center gap-2">
              {selection.length >= 2 && (
                <Button
                  size="sm"
                  disabled={regroup.isPending}
                  onClick={() => regroup.mutate({ ids: selection, force: false })}
                >
                  <Combine className="h-4 w-4" />
                  Join {selection.length} rushes
                </Button>
              )}
              {selection.length > 0 && (
                <Button size="sm" variant="ghost" onClick={() => setSelection([])}>
                  Clear
                </Button>
              )}
              {selection.length === 0 && stuck.length > 0 && (
                <Button
                  size="sm"
                  variant="outline"
                  title="These rushes are unfinished with nothing running — start their missing step"
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
                  <TableHead className="w-8" />
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
                  const selected = selection.includes(sequence.id);
                  const step = steps.get(sequence.id) ?? {};
                  const merged = sequence.merged_name !== null;
                  const failed = sequence.state === "failed";
                  const ready = sequence.state === "ready";
                  return (
                    <TableRow
                      key={sequence.id}
                      // A ready rush is there to be derushed: opening it is the
                      // obvious gesture, so the row carries it. Selection lives on
                      // the checkbox, which is the affordance that shows it exists.
                      onClick={ready ? () => navigate(`/derush/${sequence.id}`) : undefined}
                      title={ready ? "Open in derush" : undefined}
                      className={cn(ready && "cursor-pointer", selected && "bg-accent")}
                    >
                      <TableCell onClick={(event) => event.stopPropagation()}>
                        <button
                          type="button"
                          onClick={() => toggle(sequence.id)}
                          title="Select — to force several rushes into one sequence"
                          className={cn(
                            "flex h-4 w-4 items-center justify-center rounded border",
                            selected
                              ? "border-primary bg-primary text-primary-foreground"
                              : "border-input hover:border-primary",
                          )}
                        >
                          {selected && <Check className="h-3 w-3" />}
                        </button>
                      </TableCell>

                      <TableCell className="max-w-[24rem]">
                        <ul className="space-y-0.5">
                          {sequence.part_names.map((name) => (
                            <li
                              key={name}
                              className="truncate font-mono text-xs"
                              title={name}
                            >
                              {name}
                            </li>
                          ))}
                        </ul>
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          {sequence.width}×{sequence.height} · {sequence.fps.toFixed(2)} fps
                          {!sequence.has_gyro && (
                            <span className="text-red-400"> · no gyro data</span>
                          )}
                        </p>
                        {sequence.error && (
                          <p className="mt-1 line-clamp-2 text-xs text-red-400">{sequence.error}</p>
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
                                  : "Nothing running for this rush — start its missing step"
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

          {rows.length > 0 && (
            <p className="mt-3 text-xs text-muted-foreground">
              Click a ready rush to derush it. Tick two rows or more to force them into a single
              sequence — for a part that arrived after its flight was already processed.
            </p>
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
 * merely detached, so the next scan regroups them and the rush is back within 30 s
 * — already merged, since the artifacts carry the content hash. That makes it a
 * reset of the grouping, not a removal. And renders always go, whatever is kept:
 * they belong to the zones being deleted.
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
            {sequence?.cut_count ? ` · ${sequence.cut_count} marked zone(s)` : ""}
            {sequence?.render_count ? ` · ${sequence.render_count} stabilized clip(s)` : ""}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-sm">
          <p className="text-muted-foreground">
            <span className="font-medium text-foreground">Reset grouping</span> — drops the
            grouping and the marked zones. Source files, merge and proxy stay, so the next scan
            brings this rush back within 30 s, already merged, without re-encoding anything. Use it
            to redo a grouping that came out wrong.
          </p>
          <p className="text-muted-foreground">
            <span className="font-medium text-foreground">Delete the files</span> — masters, merge
            and proxy erased. Frees {formatBytes(sequence?.size_bytes)} or so, but re-importing
            means merging and encoding all over again.
          </p>
          {(sequence?.render_count ?? 0) > 0 && (
            <p className="rounded-md border border-amber-500/30 bg-amber-500/5 px-3 py-2 text-amber-300">
              Either way, the {sequence?.render_count} stabilized clip(s) are erased — they belong
              to the zones being dropped.
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
