/**
 * Colour grading of the stabilized clips, into new files.
 *
 * A grade is a level of the hierarchy, not a property of a clip: rush, sequence,
 * profile, grade (florian, 2026-08-25). So a clip carries as many looks as one wants,
 * side by side, each named, each with its own file and its own state, and two can be
 * encoding at once. Before this, `grade.render_id` was unique and a clip had exactly
 * one look, overwritten in place.
 *
 * The tree is drawn from the same primitives as the one on Stabilize (see
 * components/tree): same indent, same rows, same folder dots. What differs below the
 * sequence is each page's job, and only that: Stabilize stops there and summarises the
 * profiles as badges, because it is a queue of what is missing; here one navigates down
 * to a grade, because it is an editor of what exists.
 *
 * There is no save button. Every slider writes when it is released, like the derush,
 * so what is on screen is what is stored. The preview is a shader (see
 * lib/grade-shader): an approximation of the encode, 39 dB from it, and the file that
 * gets written always comes from ffmpeg.
 */
import type React from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  BarChart3,
  Copy,
  Download,
  Droplet,
  Eye,
  Hash,
  Loader2,
  Pencil,
  Plus,
  RotateCcw,
  Trash2,
  TriangleAlert,
  Wand2,
} from "lucide-react";
import { toast } from "sonner";

import { DeleteDialog } from "@/components/DeleteDialog";
import { GradedVideo } from "@/components/GradedVideo";
import { LooksCard } from "@/components/LooksCard";
import { RenameDialog } from "@/components/RenameDialog";
import { ScopePanel, type ScopeSink, type Scopes } from "@/components/Scopes";
import { StateBadge } from "@/components/StateBadge";
import { Dot, Indent, Meta, Twisty, rowClass } from "@/components/tree";
import { Badge, badgeVariants } from "@/components/ui/badge";
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

/** The clip's own range, measured on its own picture, which is why these two sit above
 *  the separator and never travel: unused range here is picture on the next shot. */
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

/** Where a group of controls sits when nobody has touched it. */
function home(group: readonly { key: keyof GradeParams; neutral: number }[]) {
  return Object.fromEntries(group.map((control) => [control.key, control.neutral]));
}

/** The six that travel. Copying a look must not carry one clip's measurement. */
function travelling(look: GradeParams, keep: GradeParams): GradeParams {
  return { ...look, black_point: keep.black_point, white_point: keep.white_point };
}

// --------------------------------------------------------------------------- //
// The clips, grouped
// --------------------------------------------------------------------------- //

/** A sequence, and the stabilized files made from it. */
interface CutNode {
  key: number;
  label: string;
  duration_ms: number;
  clips: Render[];
}

interface Rush {
  id: number;
  label: string;
  cuts: CutNode[];
}

interface Node {
  folder: Folder | null;
  children: Node[];
  rushes: Rush[];
}

function rushesIn(clips: Render[], folderId: number | null): Rush[] {
  const mine = clips.filter((clip) => clip.folder_id === folderId);
  const order: number[] = [];
  const byRush = new Map<number, Rush>();
  for (const clip of mine) {
    let rush = byRush.get(clip.sequence_id);
    if (!rush) {
      rush = { id: clip.sequence_id, label: clip.sequence_label || clip.sequence_key, cuts: [] };
      byRush.set(clip.sequence_id, rush);
      order.push(clip.sequence_id);
    }
    // Renders with no cut exist in the database, from before the derush was the only way
    // in, and they are whole rushes. They get a node of their own rather than being
    // hidden or folded into a sequence they are not part of.
    const key = clip.cut_id ?? 0;
    let cut = rush.cuts.find((entry) => entry.key === key);
    if (!cut) {
      cut = {
        key,
        label: clip.cut_label || "whole rush",
        duration_ms: clip.duration_ms,
        clips: [],
      };
      rush.cuts.push(cut);
    }
    cut.clips.push(clip);
  }
  return order.map((id) => byRush.get(id) as Rush);
}

function clipsOf(node: Node): Render[] {
  return [
    ...node.rushes.flatMap((rush) => rush.cuts.flatMap((cut) => cut.clips)),
    ...node.children.flatMap(clipsOf),
  ];
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
  const navigate = useNavigate();
  const { id, gradeId } = useParams();
  const renderId = Number(id) || undefined;
  const openId = Number(gradeId) || undefined;
  const [shut, setShut] = useState<Set<string>>(new Set());
  const [doomed, setDoomed] = useState<Grade | null>(null);

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
  const gradesOf = useMemo(() => {
    const out = new Map<number, Grade[]>();
    for (const grade of grades ?? []) {
      out.set(grade.render_id, [...(out.get(grade.render_id) ?? []), grade]);
    }
    for (const list of out.values()) list.sort((a, b) => a.id - b.id);
    return out;
  }, [grades]);
  const open = (grades ?? []).find((grade) => grade.id === openId);
  const openRender = clips.find((clip) => clip.id === (open?.render_id ?? renderId));

  /** A new grade on a clip, opened straight away: the "+" on a profile row. */
  const add = useMutation({
    mutationFn: (target: number) => api.putGrade(target, {}),
    onSuccess: (grade) => {
      queryClient.invalidateQueries({ queryKey: ["grades"] });
      navigate(`/color/${grade.render_id}/${grade.id}`);
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const remove = useMutation({
    mutationFn: (grade: Grade) => api.deleteGrade(grade.id),
    onSuccess: (_result, grade) => {
      queryClient.invalidateQueries({ queryKey: ["grades"] });
      queryClient.invalidateQueries({ queryKey: ["stabilize-queue"] });
      queryClient.invalidateQueries({ queryKey: ["sequences"] });
      if (grade.id === openId) navigate(`/color/${grade.render_id}`);
    },
    onError: (error: Error) => toast.error(error.message),
  });

  /**
   * A clip addressed without a grade opens one: its first, or a new one.
   *
   * Which is what the droplet on Stabilize links to, and what the URL of a clip means
   * on its own. Creating on sight is what opening a clip already did when a clip had
   * exactly one grade; the difference is that the row now shows up in the tree.
   */
  const asked = useRef<Set<number>>(new Set());
  useEffect(() => {
    if (!renderId || openId || !grades) return;
    const mine = gradesOf.get(renderId);
    if (mine?.length) {
      navigate(`/color/${renderId}/${mine[0].id}`, { replace: true });
      return;
    }
    // Once per clip, remembered in a ref: the effect runs on every render, and a
    // creation that failed would otherwise be retried for as long as the page is open.
    if (asked.current.has(renderId)) return;
    asked.current.add(renderId);
    add.mutate(renderId);
  }, [renderId, openId, grades, gradesOf, navigate, add]);

  /**
   * Applying a look is a write, not a piece of shared state.
   *
   * The card sits above the editor and needs nothing from inside it: what the open
   * grade wears is in the grades already loaded here, and the editor picks the new
   * value up because its own query is invalidated. The clip's own black and white
   * points are kept, as they are everywhere else.
   */
  const paint = useMutation({
    mutationFn: (look: Look) => {
      if (!open) throw new Error("no grade open");
      return api.saveGrade(open.id, {
        params: travelling({ ...NEUTRAL_GRADE, ...look.params }, open.params),
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["grade", openId] });
      queryClient.invalidateQueries({ queryKey: ["grades"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const profile = (template: string) =>
    templates?.find((option) => option.id === template)?.label ?? template;
  const codecOf = (template: string) =>
    templates?.find((option) => option.id === template)?.codec;

  /* The picker sits under the picture, not in a column of its own (florian, 2026-08-25).
     It costs a scroll when changing clip, which happens once per clip, and it gives the
     picture the width a whole column used to hold: measured 462 px wide before, 838
     after, on a 1600 window. */
  const picker = (
    <Clips
      tree={build(folders ?? [], clips)}
      grades={grades ?? []}
      gradesOf={gradesOf}
      profile={profile}
      selected={openId}
      shut={shut}
      toggle={(key) =>
        setShut((previous) => {
          const next = new Set(previous);
          if (next.has(key)) next.delete(key);
          else next.add(key);
          return next;
        })
      }
      onAdd={(target) => add.mutate(target)}
      onDelete={setDoomed}
    />
  );

  return (
    <div className="space-y-4">
      <LooksCard
        current={open?.params ?? null}
        currentLabel={open?.label}
        onApply={(look) => paint.mutate(look)}
      />
      {open && openRender ? (
        <Editor
          key={open.id}
          gradeId={open.id}
          render={openRender}
          profile={profile}
          codec={codecOf(openRender.template)}
          others={clips.filter((clip) => clip.id !== openRender.id)}
          folders={folders ?? []}
          gradesOf={gradesOf}
          picker={picker}
        />
      ) : (
        <>
          <p className="text-sm text-muted-foreground">Pick a clip.</p>
          {picker}
        </>
      )}

      <DeleteDialog
        open={doomed !== null}
        title={`Delete the grade "${doomed?.label ?? ""}"?`}
        note={
          doomed?.out_name
            ? "Its graded file goes with it. The stabilized clip and the other grades on it stay."
            : "The stabilized clip and the other grades on it stay."
        }
        onClose={() => setDoomed(null)}
        onConfirm={() => doomed && remove.mutate(doomed)}
      />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// The tree
// --------------------------------------------------------------------------- //

interface RowProps {
  gradesOf: Map<number, Grade[]>;
  profile: (id: string) => string;
  /** The grade being edited. */
  selected?: number;
  shut: Set<string>;
  toggle: (key: string) => void;
  onAdd: (renderId: number) => void;
  onDelete: (grade: Grade) => void;
  /** Present in the copy dialog, absent in the picker: a clip is then a checkbox and
   *  the tree stops at the profile, since a clip is what a copy targets. */
  picked?: Set<number>;
  flip?: (ids: number[], on: boolean) => void;
}

/** The picker, and the one thing that can be done to all of them at once. */
function Clips({ tree, grades, ...rest }: { tree: Node[]; grades: Grade[] } & RowProps) {
  const queryClient = useQueryClient();
  const waiting = grades.filter(
    (grade) => grade.state === "draft" && !isNeutral(grade.params),
  );

  const renderAll = useMutation({
    mutationFn: async () => {
      const results = await Promise.allSettled(
        waiting.map((grade) => api.applyGrade(grade.id)),
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
    <aside>
      {/* The batch button rides in the header row: full width, under the picture, it
          was a 900 px primary button. Same shape as the launch button on Stabilize. */}
      <div className="mb-2 flex items-center gap-2 px-1">
        <h2 className="text-sm font-medium">Stabilized clips</h2>
        <Meta>{count}</Meta>
        {waiting.length > 0 && (
          <Button
            size="sm"
            className="ml-auto"
            disabled={renderAll.isPending}
            onClick={() => renderAll.mutate()}
          >
            Render {waiting.length} look{waiting.length > 1 ? "s" : ""}
          </Button>
        )}
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
        <div className="space-y-0.5">
          {tree.map((node) => (
            <FolderRow key={node.folder?.id ?? "global"} node={node} depth={0} {...rest} />
          ))}
        </div>
      )}
    </aside>
  );
}

function FolderRow({ node, depth, ...rest }: { node: Node; depth: number } & RowProps) {
  const key = `folder-${node.folder?.id ?? "global"}`;
  const open = !rest.shut.has(key);
  const mine = clipsOf(node);
  return (
    <div>
      <div className={rowClass}>
        <Indent depth={depth} />
        {rest.picked && rest.flip && <Tick ids={mine.map((clip) => clip.id)} {...rest} />}
        <button
          type="button"
          onClick={() => rest.toggle(key)}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
        >
          <Twisty open={open} />
          <Dot color={node.folder?.color} />
          <span className="truncate font-medium" title={node.folder?.name ?? "Global"}>
            {node.folder?.name ?? "Global"}
          </span>
        </button>
        <Meta>{formatDuration(mine.reduce((sum, clip) => sum + clip.duration_ms, 0))}</Meta>
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
  const key = `rush-${rush.id}`;
  const open = !rest.shut.has(key);
  const clips = rush.cuts.flatMap((cut) => cut.clips);
  return (
    <div>
      <div className={rowClass}>
        <Indent depth={depth} />
        {rest.picked && rest.flip && <Tick ids={clips.map((clip) => clip.id)} {...rest} />}
        <Twisty open={open} onClick={() => rest.toggle(key)} />
        <Link
          to={`/derush/${rush.id}`}
          className="min-w-0 flex-1 truncate hover:underline"
          title={rush.label}
        >
          {rush.label}
        </Link>
        <Meta>{formatDuration(clips.reduce((sum, clip) => sum + clip.duration_ms, 0))}</Meta>
      </div>
      {open &&
        rush.cuts.map((cut) => <CutRow key={cut.key} cut={cut} depth={depth + 1} {...rest} />)}
    </div>
  );
}

function CutRow({ cut, depth, ...rest }: { cut: CutNode; depth: number } & RowProps) {
  const key = `cut-${cut.key}`;
  const open = !rest.shut.has(key);
  return (
    <div>
      <div className={rowClass}>
        <Indent depth={depth} />
        {rest.picked && rest.flip && <Tick ids={cut.clips.map((clip) => clip.id)} {...rest} />}
        <Twisty open={open} onClick={() => rest.toggle(key)} />
        <span className="min-w-0 flex-1 truncate" title={cut.label}>
          {cut.label}
        </span>
        <Meta>{formatDuration(cut.duration_ms)}</Meta>
      </div>
      {open &&
        cut.clips.map((clip) => (
          <ProfileRow key={clip.id} clip={clip} depth={depth + 1} {...rest} />
        ))}
    </div>
  );
}

/**
 * A stabilized file, named by its profile, with its grades under it.
 *
 * The badge is the one Stabilize draws on a sequence row: a profile looks the same
 * wherever it is seen. Clicking a profile that has no grade yet makes its first one,
 * which is what opening a clip did when a clip had exactly one.
 */
function ProfileRow({ clip, depth, ...rest }: { clip: Render; depth: number } & RowProps) {
  const key = `profile-${clip.id}`;
  const mine = rest.gradesOf.get(clip.id) ?? [];
  const open = !rest.shut.has(key);
  const label = rest.profile(clip.template);
  const badge = cn(badgeVariants({ variant: "secondary" }), "font-normal");

  if (rest.picked && rest.flip) {
    const on = rest.picked.has(clip.id);
    return (
      <label className={cn(rowClass, "cursor-pointer pr-1")}>
        <Indent depth={depth} />
        <Checkbox checked={on} onCheckedChange={() => rest.flip?.([clip.id], !on)} />
        <Twisty />
        <span className={badge}>{label}</span>
      </label>
    );
  }

  return (
    <div>
      <div className={rowClass}>
        <Indent depth={depth} />
        <Twisty open={open} onClick={mine.length ? () => rest.toggle(key) : undefined} />
        <button
          type="button"
          onClick={() => (mine.length ? rest.toggle(key) : rest.onAdd(clip.id))}
          className="min-w-0 flex-1 text-left"
        >
          <span className={badge}>{label}</span>
        </button>
        <Button
          size="icon"
          variant="ghost"
          title="Add a grade"
          className="h-6 w-6 shrink-0 text-muted-foreground hover:text-foreground"
          onClick={() => rest.onAdd(clip.id)}
        >
          <Plus className="h-3.5 w-3.5" />
        </Button>
      </div>
      {open &&
        mine.map((grade) => (
          <GradeRow key={grade.id} grade={grade} depth={depth + 1} {...rest} />
        ))}
    </div>
  );
}

function GradeRow({ grade, depth, ...rest }: { grade: Grade; depth: number } & RowProps) {
  const busy = grade.state === "queued" || grade.state === "running";
  // On the state, not on `out_name`: a look changed after an encode leaves the old file
  // on disk, reusable if the sliders come back to it exactly, and it is not this look.
  const made = grade.state === "done";
  return (
    <div className={cn(rowClass, grade.id === rest.selected && "bg-accent hover:bg-accent")}>
      <Indent depth={depth} />
      <Twisty />
      <Link
        to={`/color/${grade.render_id}/${grade.id}`}
        className="min-w-0 flex-1 truncate"
        title={grade.label}
      >
        {grade.label}
      </Link>
      {busy && <Loader2 className="h-3 w-3 shrink-0 animate-spin" />}
      {grade.state === "failed" && <Meta className="text-red-400">failed</Meta>}
      <span title={made ? "A graded file exists" : "Nothing encoded yet"}>
        <Droplet
          className={cn("h-3 w-3 shrink-0", made ? "fill-current" : "text-muted-foreground/30")}
        />
      </span>
      {made && (
        <a
          href={mediaUrl.gradedDownload(grade.id)}
          title="Download the graded file"
          className="shrink-0 text-muted-foreground hover:text-foreground"
        >
          <Download className="h-3.5 w-3.5" />
        </a>
      )}
      {/* Always drawn, like every other row of this tree: an icon that appears on
          hover is an icon nobody knows is there. */}
      <Button
        size="icon"
        variant="ghost"
        title="Delete this grade"
        className="h-6 w-6 shrink-0 text-muted-foreground hover:text-foreground"
        onClick={() => rest.onDelete(grade)}
      >
        <Trash2 className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}

/** A checkbox that can also be neither on nor off, which a folder often is. */
function Tick({ ids, picked, flip }: { ids: number[] } & RowProps) {
  const taken = ids.filter((id) => picked?.has(id)).length;
  return (
    <Checkbox
      checked={taken === 0 ? false : taken === ids.length ? true : "indeterminate"}
      onCheckedChange={() => flip?.(ids, taken !== ids.length)}
    />
  );
}

// --------------------------------------------------------------------------- //
// The editor
// --------------------------------------------------------------------------- //

function Editor({
  gradeId,
  render,
  profile,
  codec,
  others,
  folders,
  gradesOf,
  picker,
}: {
  gradeId: number;
  render: Render;
  profile: (id: string) => string;
  /** What this clip's profile renders, so an unplayable codec can be named. */
  codec?: string;
  others: Render[];
  folders: Folder[];
  gradesOf: Map<number, Grade[]>;
  /** The clip list, dropped under the picture. */
  picker: React.ReactNode;
}) {
  const queryClient = useQueryClient();
  const [params, setParams] = useState<GradeParams>(NEUTRAL_GRADE);
  const [showBefore, setShowBefore] = useState(false);
  const [copying, setCopying] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [droppingFile, setDroppingFile] = useState(false);
  const sink = useRef<ScopeSink | null>(null);
  const [scopes, setScopes] = usePersistentState<Scopes>("color.scopes", {
    zebras: false,
    histogram: true,
    waveform: false,
    numbers: true,
  });

  const { data: grade, isLoading } = useQuery({
    queryKey: ["grade", gradeId],
    queryFn: () => api.grade(gradeId),
    refetchInterval: (query) =>
      ["queued", "running"].includes(query.state.data?.state ?? "") ? 2_000 : false,
  });

  useEffect(() => {
    if (grade) setParams(grade.params);
  }, [grade?.id, grade?.params]);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["grade", gradeId] });
    queryClient.invalidateQueries({ queryKey: ["grades"] });
  };

  /** No save button: a gesture that changes the look writes it. */
  const save = useMutation({
    mutationFn: (next: GradeParams) => api.saveGrade(gradeId, { params: next }),
    onSuccess: invalidate,
    onError: (error: Error) => toast.error(error.message),
  });

  const rename = useMutation({
    mutationFn: (label: string) => api.saveGrade(gradeId, { label }),
    onSuccess: invalidate,
    onError: (error: Error) => toast.error(error.message),
  });

  const apply = useMutation({
    mutationFn: () => api.applyGrade(gradeId),
    onSuccess: invalidate,
    onError: (error: Error) => toast.error(error.message),
  });

  /** The file, not the grade: the look stays, ready to be encoded again. */
  const dropFile = useMutation({
    mutationFn: () => api.deleteGradedFile(gradeId),
    onSuccess: () => {
      invalidate();
      queryClient.invalidateQueries({ queryKey: ["stabilize-queue"] });
      queryClient.invalidateQueries({ queryKey: ["sequences"] });
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
  const pointsHome = POINTS.every((point) => params[point.key] === point.neutral);
  const lookHome = CONTROLS.every((control) => params[control.key] === control.neutral);

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
          {render.sequence_label || `render ${render.id}`}
          {render.cut_label && ` · ${render.cut_label}`}
        </h1>
        <Badge variant="secondary" className="font-normal">
          {profile(render.template)}
        </Badge>
        <span className="flex items-center gap-1 text-sm">
          {grade?.label}
          <button
            type="button"
            title="Rename this grade"
            className="text-muted-foreground hover:text-foreground"
            onClick={() => setRenaming(grade?.label ?? "")}
          >
            <Pencil className="h-3 w-3" />
          </button>
        </span>
        {grade && grade.state !== "draft" && <StateBadge state={grade.state} />}
        <span className="text-sm text-muted-foreground">
          {formatDuration(render.duration_ms)}
          {analysis.frames ? ` · analysed on ${analysis.frames} frames` : ""}
        </span>
        <Button asChild size="sm" variant="ghost" className="ml-auto">
          <Link to={`/stabilisation/${render.sequence_id}`}>Back to stabilize</Link>
        </Button>
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_clamp(21rem,22vw,26rem)]">
        <div className="min-w-0 space-y-4">
          <Card className="overflow-hidden">
            <CardContent className="p-3">
              <GradedVideo
                src={mediaUrl.render(render.id)}
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
                sink={sink}
                codec={codec}
                actions={
                  /* The reason a button is dead goes in a tooltip, not in a line of
                     prose under it. A disabled button takes no pointer events, so the
                     title has to sit on something around it. */
                  <span
                    title={
                      neutral
                        ? "Nothing to compare: the look is neutral"
                        : "Hold to see it ungraded"
                    }
                  >
                    <Button
                      size="icon"
                      variant={showBefore ? "default" : "outline"}
                      disabled={neutral}
                      onMouseDown={() => setShowBefore(true)}
                      onMouseUp={() => setShowBefore(false)}
                      onMouseLeave={() => setShowBefore(false)}
                      onTouchStart={() => setShowBefore(true)}
                      onTouchEnd={() => setShowBefore(false)}
                    >
                      <Eye className="h-4 w-4" />
                    </Button>
                  </span>
                }
              />
            </CardContent>
          </Card>
          {picker}
        </div>

        <div className="space-y-4">
          {/* The instruments, at the top of the column they are read from: the hand is
              on a slider here, and reading a scope under the picture was a diagonal
              across the screen. Pinned, because the column is taller than the window:
              the last sliders would otherwise be dragged with the scopes scrolled off. */}
          <Card className="xl:sticky xl:top-4 xl:z-10">
            <CardContent className="p-2">
              <ScopePanel
                ref={sink}
                scopes={scopes}
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
              {/* One reset per group, because the two groups are two decisions: the
                  points belong to this clip, the look travels. */}
              <div className="flex items-center gap-1">
                <span
                  className="min-w-0 flex-1"
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
                <Button
                  size="icon"
                  variant="ghost"
                  title="Back to the full range"
                  className="h-8 w-8 shrink-0 text-muted-foreground hover:text-foreground"
                  disabled={pointsHome}
                  onClick={() => commit({ ...params, ...home(POINTS) })}
                >
                  <RotateCcw className="h-4 w-4" />
                </Button>
              </div>

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
                  title="Back to a neutral look. The points above are left alone."
                  disabled={lookHome}
                  onClick={() => commit({ ...params, ...home(CONTROLS) })}
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
                {/* The title says which of the four states this is. It said "Graded
                    file" throughout, over an empty bar, while the file did not exist
                    yet (florian, 2026-08-25). */}
                <CardTitle className="text-sm">
                  {grade.state === "done"
                    ? "Graded file"
                    : grade.state === "failed"
                      ? "Failed"
                      : grade.state === "running"
                        ? "Encoding"
                        : "Waiting for a worker"}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                {grade.state === "running" && (
                  <>
                    <Progress value={grade.progress * 100} className="h-1.5" />
                    <p className="tnum text-muted-foreground">
                      {Math.round(grade.progress * 100)} %
                    </p>
                  </>
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
                        <a href={mediaUrl.download(render.id)}>
                          <Download className="h-4 w-4" />
                          Stabilized
                        </a>
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        title="Delete the graded file"
                        onClick={() => setDroppingFile(true)}
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

      <RenameDialog
        title="Rename this grade"
        value={renaming}
        onClose={() => setRenaming(null)}
        onRename={(label) => rename.mutate(label)}
      />
      <DeleteDialog
        open={droppingFile}
        title="Delete the graded file?"
        note="The look stays as it is, so the file can be encoded again from it."
        onClose={() => setDroppingFile(false)}
        onConfirm={() => dropFile.mutate()}
      />
      <CopyDialog
        open={copying}
        onClose={() => setCopying(false)}
        label={grade?.label ?? ""}
        params={params}
        clips={others}
        folders={folders}
        profile={profile}
        gradesOf={gradesOf}
      />
    </div>
  );
}

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
 * Give this look to other clips, as a grade of the same name.
 *
 * Addressed by name, so pressing it twice is harmless: each target ends up with one
 * grade called this, whatever else it already carries. What a target has tuned under
 * another name is untouched, which is the point of grades being a level of their own.
 *
 * It writes and stops there. Rendering is the button in the settings column, because
 * deciding what a clip looks like and deciding to spend minutes of encoding on it are
 * two different decisions.
 */
function CopyDialog({
  open,
  onClose,
  label,
  params,
  clips,
  folders,
  profile,
  gradesOf,
}: {
  open: boolean;
  onClose: () => void;
  label: string;
  params: GradeParams;
  clips: Render[];
  folders: Folder[];
  profile: (id: string) => string;
  gradesOf: Map<number, Grade[]>;
}) {
  const queryClient = useQueryClient();
  const [picked, setPicked] = useState<Set<number>>(new Set());

  const copy = useMutation({
    mutationFn: async () => {
      // Each target keeps its own black and white points: they were measured on its own
      // picture, and this look was measured on another. A target that already carries a
      // grade of this name keeps that grade's points.
      const results = await Promise.allSettled(
        [...picked].map((id) => {
          const mine = (gradesOf.get(id) ?? []).find((grade) => grade.label === label);
          return api.putGrade(id, {
            label,
            params: travelling(params, mine?.params ?? NEUTRAL_GRADE),
          });
        }),
      );
      return results.filter((result) => result.status === "rejected").length;
    },
    onSuccess: (failed) => {
      if (failed) toast.error(`${failed} refused`);
      else
        toast.success(`"${label}" copied to ${picked.size} clip${picked.size > 1 ? "s" : ""}`);
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
          <DialogTitle>Copy "{label}" to</DialogTitle>
        </DialogHeader>
        <div className="max-h-80 overflow-y-auto pr-1">
          {build(folders, clips).map((node) => (
            <FolderRow
              key={node.folder?.id ?? "global"}
              node={node}
              depth={0}
              gradesOf={gradesOf}
              profile={profile}
              shut={new Set()}
              toggle={() => {}}
              onAdd={() => {}}
              onDelete={() => {}}
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
