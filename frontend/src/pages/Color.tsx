import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Download, Eye, RotateCcw, Save, Trash2, Wand2 } from "lucide-react";
import { toast } from "sonner";

import { StateBadge } from "@/components/StateBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Slider } from "@/components/ui/slider";
import {
  NEUTRAL_GRADE,
  api,
  mediaUrl,
  type GradeParams,
  type Render,
} from "@/lib/api";
import { formatBytes, formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Colour grading of the stabilized clips, one look per clip, into a new file.
 *
 * The preview is a real ffmpeg still frame (measured at 0.32 s) rather than a
 * shader copy of the same maths: what is on screen goes through exactly the
 * filters of the final encode, so there is nothing to keep in sync.
 */

const CONTROLS = [
  { key: "exposure", label: "Exposure", min: -2, max: 2, step: 0.05, unit: " EV", neutral: 0 },
  { key: "contrast", label: "Contrast", min: 0.5, max: 1.6, step: 0.01, unit: "", neutral: 1 },
  { key: "saturation", label: "Saturation", min: 0, max: 2, step: 0.01, unit: "", neutral: 1 },
  { key: "temperature", label: "Temperature", min: 3000, max: 10000, step: 100, unit: " K", neutral: 6500 },
  { key: "shadows", label: "Shadows", min: -1, max: 1, step: 0.02, unit: "", neutral: 0 },
  { key: "highlights", label: "Highlights", min: -1, max: 1, step: 0.02, unit: "", neutral: 0 },
] as const;

/** Waits for the slider to settle before asking the server for a new frame. */
function useSettled<T>(value: T, delay = 220): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return settled;
}

export function Color() {
  const { id } = useParams();
  const renderId = Number(id);
  const selected = Number.isFinite(renderId) ? renderId : undefined;

  const { data: renders } = useQuery({
    queryKey: ["renders"],
    queryFn: () => api.renders(),
    refetchInterval: 5_000,
  });
  const { data: grades } = useQuery({
    queryKey: ["grades"],
    queryFn: api.grades,
    refetchInterval: 3_000,
  });

  const stabilized = (renders ?? []).filter((render) => render.state === "done");
  const gradeOf = new Map((grades ?? []).map((grade) => [grade.render_id, grade]));

  return (
    <div className="grid gap-6 lg:grid-cols-[17rem_minmax(0,1fr)]">
      <aside className="lg:sticky lg:top-4 lg:self-start">
        <div className="mb-2 flex items-baseline justify-between px-1">
          <h2 className="text-sm font-medium">Stabilized clips</h2>
          <span className="tnum text-sm text-muted-foreground">{stabilized.length}</span>
        </div>
        {stabilized.length === 0 ? (
          <p className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
            Nothing stabilized yet. See <Link to="/stabilisation" className="underline">Stabilize</Link>.
          </p>
        ) : (
          <ul className="max-h-[calc(100vh-10rem)] space-y-1 overflow-y-auto pr-1">
            {stabilized.map((render) => {
              const grade = gradeOf.get(render.id);
              return (
                <li key={render.id}>
                  <Link
                    to={`/color/${render.id}`}
                    className={cn(
                      "block rounded-md border p-2 transition-colors",
                      render.id === selected
                        ? "border-primary bg-accent"
                        : "border-transparent hover:bg-accent/50",
                    )}
                  >
                    <span className="block truncate text-sm font-medium">
                      {render.out_name ?? `render ${render.id}`}
                    </span>
                    <span className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
                      {render.template}
                      {grade && grade.state !== "draft" && <StateBadge state={grade.state} />}
                      {grade?.state === "done" && "graded"}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </aside>

      {selected ? (
        <Editor key={selected} renderId={selected} render={stabilized.find((r) => r.id === selected)} />
      ) : (
        <p className="pt-2 text-sm text-muted-foreground">Pick a clip.</p>
      )}
    </div>
  );
}

function Editor({ renderId, render }: { renderId: number; render?: Render }) {
  const queryClient = useQueryClient();
  const [params, setParams] = useState<GradeParams>(NEUTRAL_GRADE);
  const [at, setAt] = useState(0);
  const [showBefore, setShowBefore] = useState(false);
  const [dirty, setDirty] = useState(false);

  const { data: grade, isLoading } = useQuery({
    queryKey: ["grade", renderId],
    queryFn: () => api.grade(renderId),
    refetchInterval: (query) =>
      ["queued", "running"].includes(query.state.data?.state ?? "") ? 2_000 : false,
  });

  useEffect(() => {
    if (!grade) return;
    setParams(grade.params);
    setDirty(false);
    if (at === 0 && grade.analysis.median_ms) setAt(grade.analysis.median_ms);
  }, [grade?.id, grade?.params]);

  const settled = useSettled(params);
  const previewUrl = mediaUrl.gradePreview(renderId, showBefore ? NEUTRAL_GRADE : settled, at);

  const save = useMutation({
    mutationFn: () => api.saveGrade(renderId, params),
    onSuccess: () => {
      setDirty(false);
      toast.success("Look saved");
      queryClient.invalidateQueries({ queryKey: ["grade", renderId] });
      queryClient.invalidateQueries({ queryKey: ["grades"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const apply = useMutation({
    mutationFn: async () => {
      await api.saveGrade(renderId, params);
      return api.applyGrade(renderId);
    },
    onSuccess: () => {
      setDirty(false);
      toast.success("Grading queued");
      queryClient.invalidateQueries();
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const remove = useMutation({
    mutationFn: (gradeId: number) => api.deleteGrade(gradeId),
    onSuccess: () => {
      toast.success("Graded file deleted");
      queryClient.invalidateQueries();
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const analysis = grade?.analysis ?? {};
  const neutral = useMemo(
    () => CONTROLS.every((c) => params[c.key] === c.neutral) && !params.auto_levels,
    [params],
  );

  const set = <K extends keyof GradeParams>(key: K, value: GradeParams[K]) => {
    setParams((previous) => ({ ...previous, [key]: value }));
    setDirty(true);
  };

  if (isLoading) return <p className="text-sm text-muted-foreground">Analysing the clip…</p>;

  const references = [
    { label: "Darkest", ms: analysis.darkest_ms },
    { label: "Median", ms: analysis.median_ms },
    { label: "Brightest", ms: analysis.brightest_ms },
  ].filter((r) => r.ms !== undefined);

  return (
    <div className="min-w-0 space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="truncate text-sm font-semibold">{render?.out_name ?? `render ${renderId}`}</h1>
        {grade && grade.state !== "draft" && <StateBadge state={grade.state} />}
        <span className="text-sm text-muted-foreground">
          {render && formatDuration(((render.end_frame - render.start_frame + 1) / 60) * 1000)} ·{" "}
          {render?.template}
          {analysis.frames ? ` · analysed on ${analysis.frames} frames` : ""}
        </span>
        <Button asChild size="sm" variant="ghost" className="ml-auto">
          <Link to={`/stabilisation/${render?.sequence_id ?? ""}`}>Back to stabilize</Link>
        </Button>
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_19rem]">
        <Card className="overflow-hidden">
          <div className="relative bg-black">
            <img
              src={previewUrl}
              alt=""
              className="mx-auto block w-full"
              style={{ aspectRatio: "16 / 9" }}
            />
            {showBefore && (
              <span className="absolute left-2 top-2 rounded bg-black/70 px-2 py-0.5 text-sm text-white">
                before
              </span>
            )}
          </div>
          <CardContent className="flex flex-wrap items-center gap-2 py-3">
            <Button
              size="sm"
              variant={showBefore ? "default" : "outline"}
              onMouseDown={() => setShowBefore(true)}
              onMouseUp={() => setShowBefore(false)}
              onMouseLeave={() => setShowBefore(false)}
              onTouchStart={() => setShowBefore(true)}
              onTouchEnd={() => setShowBefore(false)}
            >
              <Eye className="h-4 w-4" />
              Hold to compare
            </Button>
            {/* Three frames rather than one: grading on a single lucky frame is the
                surest way to be wrong about a whole clip. */}
            {references.map((reference) => (
              <Button
                key={reference.label}
                size="sm"
                variant={Math.abs(at - (reference.ms ?? -1)) < 1 ? "secondary" : "ghost"}
                onClick={() => setAt(reference.ms ?? 0)}
              >
                {reference.label}
              </Button>
            ))}
            <span className="tnum ml-auto text-sm text-muted-foreground">
              frame at {(at / 1000).toFixed(1)}s
            </span>
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Look</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <Button
                size="sm"
                variant={params.auto_levels ? "default" : "outline"}
                className="w-full"
                onClick={() => set("auto_levels", !params.auto_levels)}
                title="Stretches the unused luma range of this clip. A side that already clips is left alone."
              >
                <Wand2 className="h-4 w-4" />
                Auto levels
              </Button>

              {CONTROLS.map((control) => (
                <div key={control.key} className="space-y-1">
                  <div className="flex items-baseline justify-between text-sm">
                    <span>{control.label}</span>
                    <span className="tnum text-muted-foreground">
                      {typeof params[control.key] === "number"
                        ? (params[control.key] as number).toFixed(control.step < 1 ? 2 : 0)
                        : ""}
                      {control.unit}
                    </span>
                  </div>
                  <Slider
                    value={[params[control.key] as number]}
                    min={control.min}
                    max={control.max}
                    step={control.step}
                    onValueChange={([value]) => set(control.key, value as never)}
                  />
                </div>
              ))}

              <div className="flex items-center gap-2 pt-1">
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={neutral}
                  onClick={() => {
                    setParams(NEUTRAL_GRADE);
                    setDirty(true);
                  }}
                >
                  <RotateCcw className="h-4 w-4" />
                  Reset
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="ml-auto"
                  disabled={!dirty || save.isPending}
                  onClick={() => save.mutate()}
                >
                  <Save className="h-4 w-4" />
                  {dirty ? "Save" : "Saved"}
                </Button>
              </div>

              <Button
                className="w-full"
                disabled={neutral || apply.isPending || grade?.state === "running"}
                onClick={() => apply.mutate()}
              >
                Render graded file
              </Button>
              {neutral && (
                <p className="text-xs text-muted-foreground">
                  Neutral look, nothing to apply.
                </p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Measured</CardTitle>
            </CardHeader>
            <CardContent className="space-y-1 text-xs text-muted-foreground">
              <p>
                Blacks at {analysis.y_low ?? "-"}, whites at {analysis.y_high ?? "-"} on a 64-940
                scale
              </p>
              <p>
                Unused range: {Math.round((analysis.headroom_low ?? 0) * 100)}% low,{" "}
                {Math.round((analysis.headroom_high ?? 0) * 100)}% high
              </p>
              {(analysis.clipped_white ?? 0) > 0.05 && (
                <p className="flex items-center gap-1 text-amber-400">
                  <AlertTriangle className="h-3 w-3" />
                  {Math.round((analysis.clipped_white ?? 0) * 100)}% of frames clip the highlights
                </p>
              )}
              {(analysis.clipped_black ?? 0) > 0.05 && (
                <p className="flex items-center gap-1 text-amber-400">
                  <AlertTriangle className="h-3 w-3" />
                  {Math.round((analysis.clipped_black ?? 0) * 100)}% of frames crush the blacks
                </p>
              )}
              {analysis.looks_log && (
                <p className="text-amber-400">Log profile</p>
              )}
            </CardContent>
          </Card>

          {grade && grade.state !== "draft" && (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Graded file</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                {grade.state === "running" && (
                  <Progress value={grade.progress * 100} className="h-1.5" />
                )}
                {grade.error && <p className="text-red-400">{grade.error}</p>}
                {grade.state === "done" && (
                  <>
                    <p className="truncate text-muted-foreground">{grade.out_name}</p>
                    <p className="tnum text-muted-foreground">{formatBytes(grade.size_bytes)}</p>
                    <div className="flex gap-1">
                      <Button asChild size="sm" variant="outline">
                        <a href={mediaUrl.gradedDownload(grade.id)}>
                          <Download className="h-4 w-4" />
                          Download
                        </a>
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        title="Delete the graded file"
                        onClick={() => remove.mutate(grade.id)}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </>
                )}
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
