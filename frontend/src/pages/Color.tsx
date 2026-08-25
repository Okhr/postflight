/**
 * Colour grading of the stabilized clips, one look per clip, into a new file.
 *
 * The page is shaped like Stabilize, because it answers the same kind of question one
 * step later: the clips are grouped the way the sidebar groups them, named the way the
 * rest of the interface names things (rush, sequence, profile, never a filename), and
 * what is tuned but not yet written can be rendered in one go.
 *
 * Two gestures stay separate, which is how florian works: a look is tuned on one clip
 * by eye, then copied to the others, and rendering is a decision of its own.
 *
 * There is no save button. Every slider writes when it is released, like the derush,
 * so what is on screen is what is stored. The preview is a shader (see
 * lib/grade-shader): an approximation of the encode, 39 dB from it, and the file that
 * gets written always comes from ffmpeg.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  BarChart3,
  ChevronRight,
  Copy,
  Download,
  Droplet,
  Eye,
  Hash,
  Loader2,
  RotateCcw,
  Trash2,
  TriangleAlert,
  Wand2,
} from "lucide-react";
import { toast } from "sonner";

import { GradedVideo, type Scopes } from "@/components/GradedVideo";
import { LooksCard } from "@/components/LooksCard";
import { StateBadge } from "@/components/StateBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Progress } from "@/components/ui/progress";
import { Separator } from "@/components/ui/separator";
import { Slider } from "@/components/ui/slider";
import {
  NEUTRAL_GRADE,
  api,
  mediaUrl,
  type Folder,
  type Grade,
  type GradeParams,
  type Look,
  type Render,
} from "@/lib/api";
import { folderColor } from "@/lib/colors";
import { usePersistentState } from "@/lib/persist";
import { levelsOf } from "@/lib/grade-shader";
import { formatBytes, formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";

const CONTROLS = [
  { key: "exposure", label: "Exposure", min: -2, max: 2, step: 0.05, unit: " EV", neutral: 0 },
  // Symmetric around 1, like every other control here: a default sitting off the centre
  // of its own track reads as a mistake. It was 0.5 to 1.6, and the reach is worth
  // having even though both ends already clip well before it (measured: at 1.3, legal
  // black lands on 0 and legal white on 255).
  { key: "contrast", label: "Contrast", min: 0.3, max: 1.7, step: 0.01, unit: "", neutral: 1 },
  { key: "saturation", label: "Saturation", min: 0, max: 2, step: 0.01, unit: "", neutral: 1 },
  { key: "temperature", label: "Temperature", min: 3000, max: 10000, step: 100, unit: " K", neutral: 6500 },
  { key: "shadows", label: "Shadows", min: -1, max: 1, step: 0.02, unit: "", neutral: 0 },
  { key: "highlights", label: "Highlights", min: -1, max: 1, step: 0.02, unit: "", neutral: 0 },
] as const;

/**
 * Black and white points, kept apart from the six above.
 *
 * They are parameters like the others, with one difference that shapes the page: they
 * belong to a clip. What is unused range on this shot is picture on the next, so they
 * sit above a separator and the copy dialog leaves them where they are.
 */
const POINTS = [
  { key: "black_point", label: "Black point", min: 0, max: 0.9, step: 0.005, neutral: 0 },
  { key: "white_point", label: "White point", min: 0.1, max: 1, step: 0.005, neutral: 1 },
] as const;

/**
 * The instruments, and what each one answers.
 *
 * All four are toggles, remembered across clips: a colourist sets up their scopes once
 * and then works. Nothing is computed for one that is off.
 */
const INSTRUMENTS = [
  {
    key: "zebras" as const,
    icon: TriangleAlert,
    title: "Paint what is clipped, on the picture: red at white, blue at black",
  },
  { key: "histogram" as const, icon: BarChart3, title: "Histogram, red green and blue" },
  { key: "waveform" as const, icon: Activity, title: "Waveform: luma across the frame" },
  { key: "numbers" as const, icon: Hash, title: "What this frame measures, in numbers" },
];

/** A look that would change nothing, so there is nothing to encode. */
function isNeutral(params: GradeParams): boolean {
  return [...CONTROLS, ...POINTS].every((control) => params[control.key] === control.neutral);
}

/** The six that travel. Copying a look must not carry one clip's measurement. */
function travelling(look: GradeParams, keep: GradeParams): GradeParams {
  return { ...look, black_point: keep.black_point, white_point: keep.white_point };
}

// --------------------------------------------------------------------------- //
// The clips, grouped
// --------------------------------------------------------------------------- //

interface Rush {
  id: number;
  label: string;
  clips: Render[];
}

interface Node {
  folder: Folder | null;
  children: Node[];
  rushes: Rush[];
}

/** What a clip is called: its sequence and the profile it was rendered with. */
function clipName(clip: Render, profile: (id: string) => string): string {
  return [clip.cut_label, profile(clip.template)].filter(Boolean).join(" · ");
}

function rushesIn(clips: Render[], folderId: number | null): Rush[] {
  const mine = clips.filter((clip) => clip.folder_id === folderId);
  const order: number[] = [];
  const byRush = new Map<number, Rush>();
  for (const clip of mine) {
    let rush = byRush.get(clip.sequence_id);
    if (!rush) {
      rush = { id: clip.sequence_id, label: clip.sequence_label || clip.sequence_key, clips: [] };
      byRush.set(clip.sequence_id, rush);
      order.push(clip.sequence_id);
    }
    rush.clips.push(clip);
  }
  return order.map((id) => byRush.get(id) as Rush);
}

function clipsOf(node: Node): Render[] {
  return [...node.rushes.flatMap((rush) => rush.clips), ...node.children.flatMap(clipsOf)];
}

/** Only what has a clip in it: this is the list of what can be graded, not the library. */
function build(folders: Folder[], clips: Render[]): Node[] {
  const node = (folder: Folder): Node => ({
    folder,
    children: folders
      .filter((child) => child.parent_id === folder.id)
      .map(node)
      .filter((child) => clipsOf(child).length > 0),
    rushes: rushesIn(clips, folder.id),
  });

  const roots = folders.filter((folder) => folder.parent_id === null).map(node);
  const global: Node = { folder: null, children: [], rushes: rushesIn(clips, null) };
  return [global, ...roots].filter((entry) => clipsOf(entry).length > 0);
}

export function Color() {
  const queryClient = useQueryClient();
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
  const { data: folders } = useQuery({ queryKey: ["folders"], queryFn: api.folders });
  const { data: templates } = useQuery({ queryKey: ["templates"], queryFn: api.templates });

  const clips = (renders ?? []).filter((render) => render.state === "done");
  const gradeOf = new Map((grades ?? []).map((grade) => [grade.render_id, grade]));
  const open = selected ? gradeOf.get(selected) : undefined;

  /**
   * Applying a look is a write, not a piece of shared state.
   *
   * The card sits above the editor and needs nothing from inside it: what a clip
   * currently wears is in the grades already loaded here, and the editor picks the new
   * value up because its own query is invalidated. The clip's own black and white
   * points are kept, as they are everywhere else.
   */
  const apply = useMutation({
    mutationFn: (look: Look) => {
      if (!selected) throw new Error("no clip open");
      const keep = open?.params ?? NEUTRAL_GRADE;
      return api.saveGrade(selected, {
        ...NEUTRAL_GRADE,
        ...look.params,
        black_point: keep.black_point,
        white_point: keep.white_point,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["grade", selected] });
      queryClient.invalidateQueries({ queryKey: ["grades"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });
  const profile = (template: string) =>
    templates?.find((option) => option.id === template)?.label ?? template;

  return (
    <div className="space-y-4">
      <LooksCard current={open?.params ?? null} onApply={(look) => apply.mutate(look)} />
      <div className="grid items-start gap-6 lg:grid-cols-[20rem_minmax(0,1fr)]">
      <Clips
        tree={build(folders ?? [], clips)}
        grades={grades ?? []}
        gradeOf={gradeOf}
        profile={profile}
        selected={selected}
      />
      {selected ? (
        <Editor
          key={selected}
          renderId={selected}
          render={clips.find((clip) => clip.id === selected)}
          profile={profile}
          others={clips.filter((clip) => clip.id !== selected)}
          folders={folders ?? []}
          gradeOf={gradeOf}
        />
      ) : (
        <p className="pt-2 text-sm text-muted-foreground">Pick a clip.</p>
      )}
      </div>
    </div>
  );
}

/** The picker, and the one thing that can be done to all of them at once. */
function Clips({
  tree,
  grades,
  gradeOf,
  profile,
  selected,
}: {
  tree: Node[];
  grades: Grade[];
  gradeOf: Map<number, Grade>;
  profile: (id: string) => string;
  selected?: number;
}) {
  const queryClient = useQueryClient();
  const waiting = grades.filter((grade) => grade.state === "draft" && !isNeutral(grade.params));

  const renderAll = useMutation({
    mutationFn: async () => {
      const results = await Promise.allSettled(
        waiting.map((grade) => api.applyGrade(grade.render_id)),
      );
      return results.filter((result) => result.status === "rejected").length;
    },
    onSuccess: (failed) => {
      if (failed) toast.error(`${failed} refused`);
      queryClient.invalidateQueries({ queryKey: ["grades"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const count = tree.flatMap(clipsOf).length;
  return (
    <aside className="lg:sticky lg:top-4 lg:self-start">
      <div className="mb-2 flex items-baseline justify-between px-1">
        <h2 className="text-sm font-medium">Stabilized clips</h2>
        <span className="tnum text-sm text-muted-foreground">{count}</span>
      </div>

      {count === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-6 text-center text-sm text-muted-foreground">
          Nothing stabilized yet. See{" "}
          <Link to="/stabilisation" className="underline">
            Stabilize
          </Link>
          .
        </p>
      ) : (
        <div className="space-y-2">
          {waiting.length > 0 && (
            <Button
              size="sm"
              className="w-full"
              disabled={renderAll.isPending}
              onClick={() => renderAll.mutate()}
            >
              Render {waiting.length} look{waiting.length > 1 ? "s" : ""}
            </Button>
          )}
          <div className="max-h-[calc(100vh-12rem)] space-y-0.5 overflow-y-auto pr-1">
            {tree.map((node) => (
              <FolderRow
                key={node.folder?.id ?? "global"}
                node={node}
                depth={0}
                selected={selected}
                gradeOf={gradeOf}
                profile={profile}
              />
            ))}
          </div>
        </div>
      )}
    </aside>
  );
}

interface RowProps {
  selected?: number;
  gradeOf: Map<number, Grade>;
  profile: (id: string) => string;
  /** Present in the copy dialog, absent in the picker: a row is then a checkbox. */
  picked?: Set<number>;
  flip?: (ids: number[], on: boolean) => void;
}

function FolderRow({ node, depth, ...rest }: { node: Node; depth: number } & RowProps) {
  const [open, setOpen] = useState(true);
  return (
    <div>
      <div className="flex items-center gap-1.5 py-1 text-sm">
        <span style={{ width: depth * 12 }} />
        {rest.picked && rest.flip && (
          <Tick ids={clipsOf(node).map((clip) => clip.id)} {...rest} />
        )}
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
        >
          <ChevronRight
            className={cn("h-3 w-3 shrink-0 transition-transform", open && "rotate-90")}
          />
          <span
            className={cn(
              "h-2 w-2 shrink-0 rounded-full",
              node.folder
                ? (folderColor(node.folder.color)?.dot ?? "bg-muted")
                : "bg-muted-foreground/50",
            )}
          />
          <span className="truncate font-medium" title={node.folder?.name ?? "Global"}>
            {node.folder?.name ?? "Global"}
          </span>
        </button>
      </div>
      {open && (
        <div>
          {node.rushes.map((rush) => (
            <RushRow key={rush.id} rush={rush} depth={depth + 1} {...rest} />
          ))}
          {node.children.map((child) => (
            <FolderRow key={child.folder?.id} node={child} depth={depth + 1} {...rest} />
          ))}
        </div>
      )}
    </div>
  );
}

function RushRow({ rush, depth, ...rest }: { rush: Rush; depth: number } & RowProps) {
  return (
    <div>
      <div className="flex items-center gap-1.5 py-1 text-sm text-muted-foreground">
        <span style={{ width: depth * 12 }} />
        {rest.picked && rest.flip && <Tick ids={rush.clips.map((clip) => clip.id)} {...rest} />}
        <span className="truncate" title={rush.label}>
          {rush.label}
        </span>
      </div>
      {rush.clips.map((clip) => (
        <ClipRow key={clip.id} clip={clip} depth={depth + 1} {...rest} />
      ))}
    </div>
  );
}

/** A checkbox that can also be neither on nor off, which a rush often is. */
function Tick({ ids, picked, flip }: { ids: number[] } & RowProps) {
  const taken = ids.filter((id) => picked?.has(id)).length;
  return (
    <Checkbox
      checked={taken === 0 ? false : taken === ids.length ? true : "indeterminate"}
      onCheckedChange={() => flip?.(ids, taken !== ids.length)}
    />
  );
}

function ClipRow({
  clip,
  depth,
  selected,
  gradeOf,
  profile,
  picked,
  flip,
}: { clip: Render; depth: number } & RowProps) {
  const grade = gradeOf.get(clip.id);
  const name = clipName(clip, profile);
  const body = (
    <>
      <span className="truncate" title={name}>
        {name}
      </span>
      <span className="ml-auto flex shrink-0 items-center gap-1.5 pl-2">
        {(grade?.state === "queued" || grade?.state === "running") && (
          <Loader2 className="h-3 w-3 animate-spin" />
        )}
        <span title={grade?.state === "done" ? "Graded" : "Not graded"}>
          <Droplet
            className={cn(
              "h-3 w-3",
              grade?.state === "done" ? "fill-current" : "text-muted-foreground/30",
            )}
          />
        </span>
        <span className="tnum text-xs text-muted-foreground">
          {formatDuration(clip.duration_ms)}
        </span>
      </span>
    </>
  );

  if (picked && flip) {
    return (
      <label className="flex cursor-pointer items-center gap-1.5 rounded-md py-1 pr-1 text-sm hover:bg-accent/40">
        <span style={{ width: depth * 12 }} />
        <Checkbox checked={picked.has(clip.id)} onCheckedChange={() => flip([clip.id], !picked.has(clip.id))} />
        {body}
      </label>
    );
  }

  return (
    <Link
      to={`/color/${clip.id}`}
      className={cn(
        "flex items-center gap-1.5 rounded-md py-1 pr-1 text-sm transition-colors",
        clip.id === selected ? "bg-accent" : "hover:bg-accent/50",
      )}
    >
      <span style={{ width: depth * 12 }} />
      {body}
    </Link>
  );
}

// --------------------------------------------------------------------------- //
// The editor
// --------------------------------------------------------------------------- //

function Editor({
  renderId,
  render,
  profile,
  others,
  folders,
  gradeOf,
}: {
  renderId: number;
  render?: Render;
  profile: (id: string) => string;
  others: Render[];
  folders: Folder[];
  gradeOf: Map<number, Grade>;
}) {
  const queryClient = useQueryClient();
  const [params, setParams] = useState<GradeParams>(NEUTRAL_GRADE);
  const [showBefore, setShowBefore] = useState(false);
  const [copying, setCopying] = useState(false);
  const [scopes, setScopes] = usePersistentState<Scopes>("color.scopes", {
    zebras: false,
    histogram: true,
    waveform: false,
    numbers: true,
  });

  const { data: grade, isLoading } = useQuery({
    queryKey: ["grade", renderId],
    queryFn: () => api.grade(renderId),
    refetchInterval: (query) =>
      ["queued", "running"].includes(query.state.data?.state ?? "") ? 2_000 : false,
  });

  useEffect(() => {
    if (grade) setParams(grade.params);
  }, [grade?.id, grade?.params]);

  /** No save button: a gesture that changes the look writes it. */
  const save = useMutation({
    mutationFn: (next: GradeParams) => api.saveGrade(renderId, next),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["grade", renderId] });
      queryClient.invalidateQueries({ queryKey: ["grades"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const apply = useMutation({
    mutationFn: () => api.applyGrade(renderId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["grade", renderId] });
      queryClient.invalidateQueries({ queryKey: ["grades"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const remove = useMutation({
    mutationFn: (gradeId: number) => api.deleteGrade(gradeId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["grade", renderId] });
      queryClient.invalidateQueries({ queryKey: ["grades"] });
      queryClient.invalidateQueries({ queryKey: ["stabilize-queue"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  /** Show it, then write it. Called on release, not on every pointer move. */
  const commit = (next: GradeParams) => {
    setParams(next);
    save.mutate(next);
  };

  const analysis = grade?.analysis ?? {};
  const neutral = useMemo(() => isNeutral(params), [params]);

  if (isLoading) return <p className="text-sm text-muted-foreground">Analysing the clip…</p>;

  const marks = [
    { label: "Darkest", ms: analysis.darkest_ms },
    { label: "Median", ms: analysis.median_ms },
    { label: "Brightest", ms: analysis.brightest_ms },
  ].filter((mark): mark is { label: string; ms: number } => mark.ms !== undefined);

  return (
    <div className="min-w-0 space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="truncate text-sm font-semibold">
          {render?.sequence_label || `render ${renderId}`}
          {render?.cut_label && ` · ${render.cut_label}`}
        </h1>
        {render && <Badge variant="secondary" className="font-normal">{profile(render.template)}</Badge>}
        {grade && grade.state !== "draft" && <StateBadge state={grade.state} />}
        <span className="text-sm text-muted-foreground">
          {render && formatDuration(render.duration_ms)}
          {analysis.frames ? ` · analysed on ${analysis.frames} frames` : ""}
        </span>
        <Button asChild size="sm" variant="ghost" className="ml-auto">
          <Link to={`/stabilisation/${render?.sequence_id ?? ""}`}>Back to stabilize</Link>
        </Button>
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_19rem]">
        <Card className="overflow-hidden">
          <CardContent className="p-3">
            <GradedVideo
              src={mediaUrl.render(renderId)}
              plan={{
                levels: showBefore ? null : levelsOf(params.black_point, params.white_point),
                exposure: showBefore ? 0 : params.exposure,
                shadows: showBefore ? 0 : params.shadows,
                highlights: showBefore ? 0 : params.highlights,
                contrast: showBefore ? 1 : params.contrast,
                saturation: showBefore ? 1 : params.saturation,
                temperature: showBefore ? 6500 : params.temperature,
                zebras: scopes.zebras,
              }}
              marks={marks}
              scopes={scopes}
              actions={
                /* The reason a button is dead goes in a tooltip, not in a line of
                   prose under it. A disabled button takes no pointer events, so the
                   title has to sit on something around it. */
                <span title={neutral ? "Nothing to compare: the look is neutral" : "Hold to see it ungraded"}>
                  <Button
                    size="sm"
                    variant={showBefore ? "default" : "outline"}
                    disabled={neutral}
                    onMouseDown={() => setShowBefore(true)}
                    onMouseUp={() => setShowBefore(false)}
                    onMouseLeave={() => setShowBefore(false)}
                    onTouchStart={() => setShowBefore(true)}
                    onTouchEnd={() => setShowBefore(false)}
                  >
                    <Eye className="h-4 w-4" />
                    {showBefore ? "Before" : "Compare"}
                  </Button>
                </span>
              }
              instruments={INSTRUMENTS.map((instrument) => (
                <Button
                  key={instrument.key}
                  size="icon"
                  variant={scopes[instrument.key] ? "secondary" : "ghost"}
                  title={instrument.title}
                  className="h-8 w-8 text-muted-foreground data-[on=true]:text-foreground"
                  data-on={scopes[instrument.key]}
                  onClick={() =>
                    setScopes({ ...scopes, [instrument.key]: !scopes[instrument.key] })
                  }
                >
                  <instrument.icon className="h-4 w-4" />
                </Button>
              ))}
            />
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card>
            {/* No card title: the two points at the top are not part of the look, they
                are this clip's own range, so "Look" belongs below the separator with
                the settings that travel. */}
            <CardContent className="space-y-3 pt-4">
              {POINTS.map((point) => (
                <Range
                  key={point.key}
                  control={point}
                  params={params}
                  setParams={setParams}
                  commit={commit}
                  format={(value) => `${Math.round(value * 100)} %`}
                  scale={(value) => String(Math.round(value * 100))}
                />
              ))}
              <span
                className="block"
                title={
                  grade?.suggested
                    ? "Puts the points on the unused range measured in this clip. A side that already clips is left where it is."
                    : "Nothing to reclaim: this clip already uses its range"
                }
              >
                <Button
                  size="sm"
                  variant="outline"
                  className="w-full"
                  disabled={!grade?.suggested}
                  onClick={() => grade?.suggested && commit({ ...params, ...grade.suggested })}
                >
                  <Wand2 className="h-4 w-4" />
                  Auto range
                </Button>
              </span>

              <Separator />

              <CardTitle className="text-sm">Look</CardTitle>

              {CONTROLS.map((control) => (
                <Range
                  key={control.key}
                  control={control}
                  params={params}
                  setParams={setParams}
                  commit={commit}
                  format={(value) => `${value.toFixed(control.step < 1 ? 2 : 0)}${control.unit}`}
                  scale={(value) => String(Number(value.toFixed(2)))}
                />
              ))}

              <div className="flex items-center gap-2 pt-1">
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={neutral}
                  onClick={() => commit(NEUTRAL_GRADE)}
                >
                  <RotateCcw className="h-4 w-4" />
                  Reset
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="ml-auto"
                  disabled={neutral || others.length === 0}
                  onClick={() => setCopying(true)}
                  title="Give this look to other clips"
                >
                  <Copy className="h-4 w-4" />
                  Copy to
                </Button>
              </div>

              <span
                className="block"
                title={neutral ? "Nothing to encode: the look is neutral" : undefined}
              >
                <Button
                  className="w-full"
                  disabled={neutral || apply.isPending || grade?.state === "running"}
                  onClick={() => apply.mutate()}
                >
                  Render graded file
                </Button>
              </span>
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
                    <p className="tnum text-muted-foreground">{formatBytes(grade.size_bytes)}</p>
                    <div className="flex gap-1">
                      <Button asChild size="sm" variant="outline">
                        <a href={mediaUrl.gradedDownload(grade.id)}>
                          <Download className="h-4 w-4" />
                          Graded
                        </a>
                      </Button>
                      <Button asChild size="sm" variant="ghost">
                        <a href={mediaUrl.download(renderId)}>
                          <Download className="h-4 w-4" />
                          Stabilized
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

      <CopyDialog
        open={copying}
        onClose={() => setCopying(false)}
        params={params}
        clips={others}
        folders={folders}
        profile={profile}
        gradeOf={gradeOf}
      />
    </div>
  );
}

/** One slider and its readout. Written on release, never on every pointer move. */
/**
 * One slider: its reading above, and its scale below.
 *
 * The scale carries the two bounds and the default, the default sitting under its own
 * position on the track rather than in the middle of the line: half of these have a
 * default that is not the centre of their track (the two points, whose default is a
 * bound), so its place is information. It is dropped where it coincides with a bound.
 *
 * A notch on the track was tried first and abandoned: drawn on the track it sat
 * behind the filled part of the bar and disappeared for every value past the default,
 * and no single colour reads on both the white fill and the dark remainder.
 */
function Range({
  control,
  params,
  setParams,
  commit,
  format,
  scale,
}: {
  control: {
    key: keyof GradeParams;
    label: string;
    min: number;
    max: number;
    step: number;
    neutral: number;
  };
  params: GradeParams;
  setParams: (update: (previous: GradeParams) => GradeParams) => void;
  commit: (next: GradeParams) => void;
  /** The reading above the slider, with its unit. */
  format: (value: number) => string;
  /** The scale below it: bare numbers, since the unit is already stated once. */
  scale: (value: number) => string;
}) {
  const value = params[control.key] as number;
  const home = (control.neutral - control.min) / (control.max - control.min);
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between text-sm">
        <span className="flex items-center gap-1">
          {control.label}
          {/* Only on a slider that has been moved, which makes it both the way back
              and the mark that says this one is off its default. */}
          {value !== control.neutral && (
            <button
              type="button"
              title={`Back to ${scale(control.neutral)}`}
              onClick={() => commit({ ...params, [control.key]: control.neutral })}
              className="text-muted-foreground hover:text-foreground"
            >
              <RotateCcw className="h-3 w-3" />
            </button>
          )}
        </span>
        <span className="tnum text-muted-foreground">{format(value)}</span>
      </div>
      <Slider
        value={[value]}
        min={control.min}
        max={control.max}
        step={control.step}
        onValueChange={([next]) =>
          setParams((previous) => ({ ...previous, [control.key]: next }))
        }
        onValueCommit={([next]) => commit({ ...params, [control.key]: next })}
      />
      <div className="relative h-3 text-[11px] text-muted-foreground">
        <span className="tnum absolute left-0">{scale(control.min)}</span>
        {home > 0.02 && home < 0.98 && (
          <span
            className="tnum absolute -translate-x-1/2"
            style={{ left: `calc(0.5rem + (100% - 1rem) * ${home})` }}
          >
            {scale(control.neutral)}
          </span>
        )}
        <span className="tnum absolute right-0">{scale(control.max)}</span>
      </div>
    </div>
  );
}

/**
 * Give this look to other clips.
 *
 * It writes the parameters and stops there. Rendering them is the button in the left
 * column, because deciding what a clip looks like and deciding to spend minutes of
 * encoding on it are two different decisions.
 */
function CopyDialog({
  open,
  onClose,
  params,
  clips,
  folders,
  profile,
  gradeOf,
}: {
  open: boolean;
  onClose: () => void;
  params: GradeParams;
  clips: Render[];
  folders: Folder[];
  profile: (id: string) => string;
  gradeOf: Map<number, Grade>;
}) {
  const queryClient = useQueryClient();
  const [picked, setPicked] = useState<Set<number>>(new Set());

  const copy = useMutation({
    mutationFn: async () => {
      // Each target keeps its own black and white points: they were measured on its
      // own picture, and this look was measured on another.
      const results = await Promise.allSettled(
        [...picked].map((id) =>
          api.saveGrade(id, travelling(params, gradeOf.get(id)?.params ?? NEUTRAL_GRADE)),
        ),
      );
      return results.filter((result) => result.status === "rejected").length;
    },
    onSuccess: (failed) => {
      if (failed) toast.error(`${failed} refused`);
      else toast.success(`Look copied to ${picked.size} clip${picked.size > 1 ? "s" : ""}`);
      queryClient.invalidateQueries({ queryKey: ["grades"] });
      onClose();
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const flip = (ids: number[], on: boolean) =>
    setPicked((previous) => {
      const next = new Set(previous);
      for (const id of ids) {
        if (on) next.add(id);
        else next.delete(id);
      }
      return next;
    });

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Copy this look to</DialogTitle>
        </DialogHeader>
        <div className="max-h-80 overflow-y-auto pr-1">
          {build(folders, clips).map((node) => (
            <FolderRow
              key={node.folder?.id ?? "global"}
              node={node}
              depth={0}
              gradeOf={new Map()}
              profile={profile}
              picked={picked}
              flip={flip}
            />
          ))}
        </div>
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button
            size="sm"
            disabled={picked.size === 0 || copy.isPending}
            onClick={() => copy.mutate()}
          >
            Copy to {picked.size}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
