import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Download, Trash2, Zap } from "lucide-react";
import { toast } from "sonner";

import { SequenceList, SequenceWork } from "@/components/SequenceList";
import { StateBadge } from "@/components/StateBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, mediaUrl, type Render } from "@/lib/api";
import { etaLabel, formatBytes, formatDateTime, formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";

export function Stabilize() {
  const { id } = useParams();
  const sequenceId = Number(id);
  const selected = Number.isFinite(sequenceId) ? sequenceId : undefined;

  const { data: sequences } = useQuery({
    queryKey: ["sequences"],
    queryFn: () => api.sequences(),
    refetchInterval: 10_000,
  });
  const { data: renders } = useQuery({
    queryKey: ["renders"],
    queryFn: () => api.renders(),
    refetchInterval: 3_000,
  });

  // Only rushes with something to render: a marked zone. A rush nobody derushed
  // has nothing to offer here, and listing it just makes the step look busier than
  // it is. Chronological like the derush list, oldest first, so the two read the
  // same way.
  //
  // "ready" is still required: a render needs the merged file, and that state is the
  // one that guarantees it.
  const withZones = (sequences ?? [])
    .filter((sequence) => sequence.state === "ready" && sequence.cut_count > 0)
    .sort((a, b) => {
      const left = a.recorded_at ? Date.parse(a.recorded_at) : Infinity;
      const right = b.recorded_at ? Date.parse(b.recorded_at) : Infinity;
      return left - right;
    });

  return (
    <div className="grid gap-6 lg:grid-cols-[17rem_minmax(0,1fr)]">
      <SequenceList
        title="Derushed rushes"
        sequences={withZones}
        activeId={selected}
        hrefFor={(sequence) => `/stabilisation/${sequence.id}`}
        meta={(sequence) => <SequenceWork sequence={sequence} />}
        empty={
          <>
            No zone marked yet. Mark some in{" "}
            <Link to="/derush" className="underline">
              Derush
            </Link>
            .
          </>
        }
      />

      <div className="min-w-0 space-y-4">
        {selected ? (
          <Launcher key={selected} sequenceId={selected} />
        ) : (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Pick a rush</CardTitle>
              <CardDescription>
                Gyroflow cuts and stabilizes in a single pass, on the kept zones only: one output
                file per zone, no multi-gigabyte intermediate.
              </CardDescription>
            </CardHeader>
          </Card>
        )}

        <RendersTable renders={renders ?? []} highlight={selected} />
      </div>
    </div>
  );
}

function Launcher({ sequenceId }: { sequenceId: number }) {
  const queryClient = useQueryClient();
  const [template, setTemplate] = useState("");
  const [picked, setPicked] = useState<number[] | null>(null);

  const { data: sequence } = useQuery({
    queryKey: ["sequence", sequenceId],
    queryFn: () => api.sequence(sequenceId),
    refetchInterval: (query) =>
      query.state.data?.renders.some((r) => r.state === "running" || r.state === "queued")
        ? 3_000
        : false,
  });
  const { data: templates } = useQuery({ queryKey: ["templates"], queryFn: api.templates });

  useEffect(() => {
    if (templates?.length && !template) setTemplate(templates[0].id);
  }, [templates, template]);

  const cuts = sequence?.cuts ?? [];
  // `null` = nothing unchecked yet, so everything is taken.
  const selection = picked ?? cuts.map((cut) => cut.id);

  const launch = useMutation({
    mutationFn: (payload: { whole: boolean }) =>
      api.createRenders(sequenceId, {
        template,
        whole_sequence: payload.whole,
        cut_ids: payload.whole ? undefined : selection,
      }),
    onSuccess: (created) => {
      toast.success(`${created.length} render${created.length > 1 ? "s" : ""} queued`);
      queryClient.invalidateQueries({ queryKey: ["sequence", sequenceId] });
      queryClient.invalidateQueries({ queryKey: ["renders"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  if (!sequence) return <p className="text-sm text-muted-foreground">Loading…</p>;

  const chosen = templates?.find((option) => option.id === template);

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">{sequence.label}</CardTitle>
            <CardDescription>
              {sequence.width}×{sequence.height} · {formatDuration(sequence.duration_ms)} ·{" "}
              {cuts.length} zone{cuts.length > 1 ? "s" : ""} marked
              {!sequence.has_gyro && (
                <span className="text-red-400"> · no gyro data, stabilization will fail</span>
              )}
            </CardDescription>
          </div>
          <Button asChild size="sm" variant="ghost">
            <Link to={`/derush/${sequence.id}`}>Edit zones</Link>
          </Button>
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1.5">
            <Label className="text-xs">Template</Label>
            <Select value={template} onValueChange={setTemplate}>
              <SelectTrigger className="w-64">
                <SelectValue placeholder="Choose…" />
              </SelectTrigger>
              <SelectContent>
                {(templates ?? []).map((option) => (
                  <SelectItem key={option.id} value={option.id}>
                    {option.label} · {option.width}×{option.height}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {chosen?.description && (
            <p className="max-w-md pb-2 text-xs text-muted-foreground">{chosen.description}</p>
          )}
        </div>

        {cuts.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No zone marked on this rush. Go through{" "}
            <Link to={`/derush/${sequence.id}`} className="underline">
              derush
            </Link>
            , or stabilize the whole sequence.
          </p>
        ) : (
          <div className="overflow-hidden rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>Zone</TableHead>
                  <TableHead>Start</TableHead>
                  <TableHead>End</TableHead>
                  <TableHead className="text-right">Length</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {cuts.map((cut) => {
                  const on = selection.includes(cut.id);
                  return (
                    <TableRow
                      key={cut.id}
                      className={cn("cursor-pointer", on && "bg-accent/60")}
                      onClick={() =>
                        setPicked(
                          on
                            ? selection.filter((x) => x !== cut.id)
                            : [...selection, cut.id],
                        )
                      }
                    >
                      <TableCell>
                        <span
                          className={cn(
                            "flex h-4 w-4 items-center justify-center rounded border",
                            on
                              ? "border-primary bg-primary text-primary-foreground"
                              : "border-input",
                          )}
                        >
                          {on && <Check className="h-3 w-3" />}
                        </span>
                      </TableCell>
                      <TableCell className="text-sm">{cut.label}</TableCell>
                      <TableCell className="tnum text-xs">{cut.start_tc}</TableCell>
                      <TableCell className="tnum text-xs">{cut.end_tc}</TableCell>
                      <TableCell className="tnum text-right text-xs">
                        {formatDuration(cut.duration_ms)}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <Button
            disabled={!template || selection.length === 0 || launch.isPending}
            onClick={() => launch.mutate({ whole: false })}
          >
            <Zap className="h-4 w-4" />
            Stabilize {selection.length} zone{selection.length > 1 ? "s" : ""}
          </Button>
          <Button
            variant="outline"
            disabled={!template || launch.isPending}
            onClick={() => launch.mutate({ whole: true })}
          >
            Stabilize the whole sequence
          </Button>
          <span className="text-xs text-muted-foreground">
            {formatDuration(
              cuts
                .filter((cut) => selection.includes(cut.id))
                .reduce((total, cut) => total + cut.duration_ms, 0),
            )}{" "}
            to render
          </span>
        </div>
      </CardContent>
    </Card>
  );
}

function RendersTable({ renders, highlight }: { renders: Render[]; highlight?: number }) {
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: api.deleteRender,
    onSuccess: () => {
      toast.success("Render deleted");
      queryClient.invalidateQueries({ queryKey: ["renders"] });
      queryClient.invalidateQueries({ queryKey: ["sequences"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Renders ({renders.length})</CardTitle>
        <CardDescription>
          Output goes to <code>out/</code>. The matching Gyroflow project is kept in{" "}
          <code>projects/</code>, so any render can be replayed exactly.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {renders.length === 0 ? (
          <p className="text-sm text-muted-foreground">No render started yet.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>File</TableHead>
                <TableHead>Rush</TableHead>
                <TableHead>Template</TableHead>
                <TableHead>State</TableHead>
                <TableHead className="w-36">Progress</TableHead>
                <TableHead>Device</TableHead>
                <TableHead className="text-right">Size</TableHead>
                <TableHead>Finished</TableHead>
                <TableHead className="text-right" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {renders.map((render) => (
                <TableRow
                  key={render.id}
                  className={cn(render.sequence_id === highlight && "bg-accent/40")}
                >
                  <TableCell className="max-w-[18rem] truncate text-xs">
                    {render.out_name ?? "—"}
                    {render.error && (
                      <p className="mt-1 line-clamp-2 text-xs text-red-400">{render.error}</p>
                    )}
                  </TableCell>
                  <TableCell>
                    <Link
                      to={`/stabilisation/${render.sequence_id}`}
                      className="text-xs hover:underline"
                    >
                      {render.sequence_key}
                    </Link>
                  </TableCell>
                  <TableCell className="text-xs">{render.template}</TableCell>
                  <TableCell>
                    <StateBadge state={render.state} />
                  </TableCell>
                  <TableCell>
                    {render.state === "running" ? (
                      <div className="space-y-1">
                        <div className="flex items-center gap-2">
                          <Progress value={render.progress * 100} className="h-1.5" />
                          <span className="tnum text-xs text-muted-foreground">
                            {Math.round(render.progress * 100)} %
                          </span>
                        </div>
                        <p className="text-[11px] text-muted-foreground">
                          {etaLabel(render.progress, render.started_at)}
                        </p>
                      </div>
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {render.processing_device ?? "—"}
                  </TableCell>
                  <TableCell className="tnum text-right text-xs">
                    {formatBytes(render.size_bytes)}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {formatDateTime(render.finished_at)}
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-1">
                      {render.state === "done" && (
                        <Button asChild size="icon" variant="outline" title="Download">
                          <a href={mediaUrl.download(render.id)}>
                            <Download className="h-4 w-4" />
                          </a>
                        </Button>
                      )}
                      <Button
                        size="icon"
                        variant="ghost"
                        title="Delete render"
                        onClick={() => remove.mutate(render.id)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
