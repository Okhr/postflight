/**
 * Stabilize: one queue of everything that can be stabilized, whatever rush it is in.
 *
 * The page answers a single question, and it used to answer it badly: what is left to
 * do, and can I take all of it at once. So there is no per-rush launcher any more. The
 * tree holds every marked sequence, grouped the way the sidebar groups them, ticked on
 * arrival except where a file already exists for the chosen profile. Change the profile
 * and those rows tick themselves again, which is what makes a second format cost one
 * click and never redo work by accident.
 *
 * What a sequence has already been rendered with is written on its row, and the profile
 * is the way to the file: it leads to grading, which is the step after this one.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Download, Droplet, Loader2, Trash2, X } from "lucide-react";
import { toast } from "sonner";

import { TemplatesCard } from "@/components/TemplatesCard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api, mediaUrl, type Folder, type QueueRush, type Render, type Template } from "@/lib/api";
import { folderColor } from "@/lib/colors";
import { etaLabel, formatDuration } from "@/lib/format";
import { usePersistentState } from "@/lib/persist";
import { cn } from "@/lib/utils";

/** A folder and what hangs under it. Two levels deep, like everywhere else. */
interface Node {
  folder: Folder | null;
  children: Node[];
  rushes: QueueRush[];
}

type Mark = "on" | "off" | "mixed";

function mark(ids: number[], picked: Set<number>): Mark {
  if (ids.length === 0) return "off";
  const taken = ids.filter((id) => picked.has(id)).length;
  if (taken === 0) return "off";
  return taken === ids.length ? "on" : "mixed";
}

/** Every cut under a node, so a folder can be ticked as one thing. */
function cutsOf(node: Node): number[] {
  return [
    ...node.rushes.flatMap((rush) => rush.cuts.map((cut) => cut.id)),
    ...node.children.flatMap(cutsOf),
  ];
}

function lengthOf(node: Node): number {
  return (
    node.rushes.reduce(
      (total, rush) => total + rush.cuts.reduce((sum, cut) => sum + cut.duration_ms, 0),
      0,
    ) + node.children.reduce((total, child) => total + lengthOf(child), 0)
  );
}

/**
 * The tree, keeping only what has work in it.
 *
 * A folder holding nothing to stabilize is noise here: this page is the list of what
 * is left, not the library. Global comes first, as the folder nobody chose.
 */
function build(folders: Folder[], queue: QueueRush[]): Node[] {
  const mine = (id: number | null) => queue.filter((rush) => rush.folder_id === id);
  const node = (folder: Folder): Node => ({
    folder,
    children: folders
      .filter((child) => child.parent_id === folder.id)
      .map(node)
      .filter((child) => cutsOf(child).length > 0),
    rushes: mine(folder.id),
  });

  const roots = folders.filter((folder) => folder.parent_id === null).map(node);
  const global: Node = { folder: null, children: [], rushes: mine(null) };
  return [global, ...roots].filter((entry) => cutsOf(entry).length > 0);
}

export function Stabilize() {
  const { id } = useParams();
  const opened = Number(id);

  return (
    <div className="min-w-0 space-y-4">
      <TemplatesCard />
      <Queue highlight={Number.isFinite(opened) ? opened : undefined} />
      <Active />
    </div>
  );
}

function Queue({ highlight }: { highlight?: number }) {
  const queryClient = useQueryClient();
  const [template, setTemplate] = usePersistentState("stabilize.template", "");
  /** `null` means nobody has touched a box, so the default stands. */
  const [picked, setPicked] = useState<Set<number> | null>(null);
  const [shut, setShut] = useState<Set<string>>(new Set());

  const { data: queue } = useQuery({
    queryKey: ["stabilize-queue"],
    queryFn: api.stabilizeQueue,
    refetchInterval: 5_000,
  });
  const { data: folders } = useQuery({ queryKey: ["folders"], queryFn: api.folders });
  const { data: templates } = useQuery({ queryKey: ["templates"], queryFn: api.templates });

  // A profile has to be chosen for the page to mean anything, and the last one used is
  // the right guess: the profile was settled in Gyroflow long before this page.
  useEffect(() => {
    if (!templates?.length) return;
    if (!templates.some((option) => option.id === template)) setTemplate(templates[0].id);
  }, [templates, template, setTemplate]);

  const tree = useMemo(() => build(folders ?? [], queue ?? []), [folders, queue]);

  // What this profile has already produced, and therefore what cannot be asked for
  // again: rendering it twice would leave two files of the same look.
  const locked = useMemo(() => {
    const out = new Set<number>();
    for (const rush of queue ?? []) {
      for (const cut of rush.cuts) {
        if ([...cut.done, ...cut.busy].some((file) => file.template === template)) {
          out.add(cut.id);
        }
      }
    }
    return out;
  }, [queue, template]);

  // Untouched, the queue offers exactly the work that is missing for this profile.
  const fresh = useMemo(() => {
    const out = new Set<number>();
    for (const rush of queue ?? []) {
      for (const cut of rush.cuts) if (!locked.has(cut.id)) out.add(cut.id);
    }
    return out;
  }, [queue, locked]);
  const selection = picked ?? fresh;

  const flip = useCallback(
    (ids: number[], on: boolean) => {
      setPicked((previous) => {
        const next = new Set(previous ?? fresh);
        for (const id of ids) {
          if (on) {
            if (!locked.has(id)) next.add(id);
          } else next.delete(id);
        }
        return next;
      });
    },
    [fresh, locked],
  );

  const chosen = useMemo(
    () =>
      (queue ?? []).flatMap((rush) =>
        rush.cuts.filter((cut) => selection.has(cut.id)).map((cut) => ({ rush, cut })),
      ),
    [queue, selection],
  );
  const total = chosen.reduce((sum, entry) => sum + entry.cut.duration_ms, 0);

  const launch = useMutation({
    // One request per rush, since a render is created against the rush that owns the
    // cuts. Reported together: what matters is how many jobs the click produced.
    mutationFn: async () => {
      const byRush = new Map<number, number[]>();
      for (const { rush, cut } of chosen) {
        byRush.set(rush.id, [...(byRush.get(rush.id) ?? []), cut.id]);
      }
      const results = await Promise.allSettled(
        [...byRush].map(([rushId, cutIds]) =>
          api.createRenders(rushId, { template, cut_ids: cutIds }),
        ),
      );
      const made = results
        .filter((r): r is PromiseFulfilledResult<Render[]> => r.status === "fulfilled")
        .flatMap((r) => r.value);
      const failed = results.filter((r) => r.status === "rejected").length;
      return { made: made.length, failed };
    },
    onSuccess: ({ made, failed }) => {
      if (made) toast.success(`${made} render${made > 1 ? "s" : ""} queued`);
      if (failed) toast.error(`${failed} rush${failed > 1 ? "es" : ""} refused`);
      setPicked(null);
      queryClient.invalidateQueries({ queryKey: ["stabilize-queue"] });
      queryClient.invalidateQueries({ queryKey: ["renders"] });
      queryClient.invalidateQueries({ queryKey: ["sequences"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const everything = tree.flatMap(cutsOf).filter((id) => !locked.has(id));
  const label = (id: string) => templates?.find((t) => t.id === id)?.label ?? id;

  return (
    <Card>
      <CardHeader className="gap-3 pb-3">
        <div className="flex flex-wrap items-center gap-3">
          <Select value={template} onValueChange={setTemplate}>
            <SelectTrigger className="w-64">
              <SelectValue placeholder="Profile…" />
            </SelectTrigger>
            <SelectContent>
              {(templates ?? []).map((option: Template) => (
                <SelectItem key={option.id} value={option.id}>
                  {option.label} · {option.width}×{option.height}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span className="text-sm text-muted-foreground">
            {chosen.length} of {everything.length} sequence{everything.length === 1 ? "" : "s"}
            {chosen.length > 0 && ` · ${formatDuration(total)}`}
          </span>
          <Button
            className="ml-auto"
            disabled={!template || chosen.length === 0 || launch.isPending}
            onClick={() => launch.mutate()}
          >
            Stabilize {chosen.length}
          </Button>
        </div>
      </CardHeader>

      <CardContent>
        {everything.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Nothing marked yet. See{" "}
            <Link to="/derush" className="underline">
              Derush
            </Link>
            .
          </p>
        ) : (
          <div className="space-y-0.5">
            <div className="flex items-center gap-2 border-b pb-2">
              <Box
                state={mark(everything, selection)}
                onChange={(on) => flip(everything, on)}
              />
              <span className="text-sm text-muted-foreground">Everything waiting</span>
            </div>
            {tree.map((node) => (
              <FolderRow
                key={node.folder?.id ?? "global"}
                node={node}
                depth={0}
                selection={selection}
                locked={locked}
                flip={flip}
                shut={shut}
                toggle={(key) =>
                  setShut((previous) => {
                    const next = new Set(previous);
                    if (next.has(key)) next.delete(key);
                    else next.add(key);
                    return next;
                  })
                }
                highlight={highlight}
                name={label}
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** A checkbox that can also be neither on nor off, which is what a folder often is. */
function Box({
  state,
  onChange,
  disabled,
  title,
}: {
  state: Mark;
  onChange: (on: boolean) => void;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <span title={title}>
      <Checkbox
        checked={state === "mixed" ? "indeterminate" : state === "on"}
        disabled={disabled}
        onCheckedChange={() => onChange(state !== "on")}
      />
    </span>
  );
}

interface RowProps {
  selection: Set<number>;
  /** Sequences that already have a file, or a job, for the chosen profile. Rendering
   *  one again would write a second file of the same look, so the box says no. */
  locked: Set<number>;
  flip: (ids: number[], on: boolean) => void;
  shut: Set<string>;
  toggle: (key: string) => void;
  highlight?: number;
  name: (id: string) => string;
}

function FolderRow({ node, depth, ...rest }: { node: Node; depth: number } & RowProps) {
  const key = `folder-${node.folder?.id ?? "global"}`;
  const open = !rest.shut.has(key);
  const all = cutsOf(node);
  const ids = all.filter((id) => !rest.locked.has(id));

  return (
    <div>
      <div className="flex items-center gap-2 rounded-md py-1 hover:bg-accent/40">
        <span style={{ width: depth * 16 }} />
        <Box
          state={mark(ids, rest.selection)}
          onChange={(on) => rest.flip(ids, on)}
          disabled={ids.length === 0}
          title={ids.length === 0 ? "Everything here is already rendered" : undefined}
        />
        <button
          type="button"
          onClick={() => rest.toggle(key)}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left text-sm"
        >
          <ChevronRight className={cn("h-3 w-3 shrink-0 transition-transform", open && "rotate-90")} />
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
        <span className="tnum shrink-0 pr-1 text-xs text-muted-foreground">
          {formatDuration(lengthOf(node))}
        </span>
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

function RushRow({ rush, depth, ...rest }: { rush: QueueRush; depth: number } & RowProps) {
  const key = `rush-${rush.id}`;
  const open = !rest.shut.has(key) || rush.id === rest.highlight;
  const ids = rush.cuts.map((cut) => cut.id).filter((id) => !rest.locked.has(id));
  const length = rush.cuts.reduce((sum, cut) => sum + cut.duration_ms, 0);

  return (
    <div>
      <div
        className={cn(
          "flex items-center gap-2 rounded-md py-1 hover:bg-accent/40",
          rush.id === rest.highlight && "bg-accent/40",
        )}
      >
        <span style={{ width: depth * 16 }} />
        <Box
          state={mark(ids, rest.selection)}
          onChange={(on) => rest.flip(ids, on)}
          disabled={ids.length === 0}
          title={ids.length === 0 ? "Every sequence here is already rendered" : undefined}
        />
        <button
          type="button"
          onClick={() => rest.toggle(key)}
          className="shrink-0 text-muted-foreground hover:text-foreground"
        >
          <ChevronRight className={cn("h-3 w-3 transition-transform", open && "rotate-90")} />
        </button>
        <Link
          to={`/derush/${rush.id}`}
          className="min-w-0 flex-1 truncate text-sm hover:underline"
          title={rush.label}
        >
          {rush.label}
        </Link>
        <span className="tnum shrink-0 pr-1 text-xs text-muted-foreground">
          {formatDuration(length)}
        </span>
      </div>
      {open &&
        rush.cuts.map((cut) => (
          <CutRow key={cut.id} cut={cut} depth={depth + 1} {...rest} />
        ))}
    </div>
  );
}

function CutRow({
  cut,
  depth,
  selection,
  locked,
  flip,
  name,
}: { cut: QueueRush["cuts"][number]; depth: number } & RowProps) {
  const done = locked.has(cut.id);
  return (
    <div className="flex items-center gap-2 rounded-md py-1 hover:bg-accent/40">
      <span style={{ width: depth * 16 }} />
      <Box
        state={selection.has(cut.id) ? "on" : "off"}
        onChange={(on) => flip([cut.id], on)}
        disabled={done}
        title={done ? "Already rendered with this profile" : undefined}
      />
      <span className="min-w-0 flex-1 truncate text-sm" title={cut.label}>
        {cut.label}
      </span>
      <span className="tnum hidden shrink-0 text-xs text-muted-foreground sm:inline">
        {cut.start_tc} → {cut.end_tc}
      </span>
      <span className="flex shrink-0 items-center gap-1">
        {cut.busy.map((file) => (
          <Badge key={file.id} variant="outline" className="gap-1 font-normal">
            <Loader2 className="h-3 w-3 animate-spin" />
            {name(file.template)}
          </Badge>
        ))}
        {cut.done.map((file) => (
          <Made key={file.id} id={file.id} label={name(file.template)} />
        ))}
      </span>
      <span className="tnum w-12 shrink-0 pr-1 text-right text-xs text-muted-foreground">
        {formatDuration(cut.duration_ms)}
      </span>
    </div>
  );
}

/** A file that exists. Named by its profile, because that is what tells two apart. */
function Made({ id, label }: { id: number; label: string }) {
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => api.deleteRender(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["stabilize-queue"] });
      queryClient.invalidateQueries({ queryKey: ["renders"] });
      queryClient.invalidateQueries({ queryKey: ["sequences"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Badge variant="secondary" className="cursor-pointer font-normal">
          {label}
        </Badge>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem asChild>
          <Link to={`/color/${id}`}>
            <Droplet className="h-3.5 w-3.5" />
            Grade
          </Link>
        </DropdownMenuItem>
        <DropdownMenuItem asChild>
          <a href={mediaUrl.download(id)}>
            <Download className="h-3.5 w-3.5" />
            Download
          </a>
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => remove.mutate()}>
          <Trash2 className="h-3.5 w-3.5" />
          Delete
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * What is running, and what failed.
 *
 * Finished files are not here: they are the material of the next step, and the queue
 * above already says which sequence has one. A failure stays until it is cleared,
 * since its sequence is back in the queue and the reason is the only thing left.
 *
 * Lines are named the way the queue names things, rush then sequence, and the queue is
 * what gets asked: a render only knows the key of its rush, and printing
 * `DJI_20260711191722_0025_D` under a tree that says "Rush 1" is two vocabularies for
 * one thing on one screen.
 */
function Active() {
  const queryClient = useQueryClient();
  const { data: renders } = useQuery({
    queryKey: ["renders"],
    queryFn: () => api.renders(),
    refetchInterval: 3_000,
  });
  const { data: templates } = useQuery({ queryKey: ["templates"], queryFn: api.templates });
  const drop = useMutation({
    mutationFn: api.deleteRender,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["renders"] });
      queryClient.invalidateQueries({ queryKey: ["stabilize-queue"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const busy = (renders ?? []).filter((r) => r.state === "running" || r.state === "queued");
  const failed = (renders ?? []).filter((r) => r.state === "failed");
  if (busy.length === 0 && failed.length === 0) return null;

  const name = (id: string) => templates?.find((t) => t.id === id)?.label ?? id;
  /** Rush then sequence, the way the tree above says them. */
  const who = (render: Render) =>
    [render.sequence_label || render.sequence_key, render.cut_label].filter(Boolean).join(" · ");

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-sm">
          {busy.length > 0 ? `Running · ${busy.length}` : "Failed"}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {busy.map((render) => (
          <div key={render.id} className="space-y-1">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="truncate" title={who(render)}>
                {who(render)}
              </span>
              <Badge variant="secondary" className="font-normal">
                {name(render.template)}
              </Badge>
              {render.state === "queued" && (
                <span className="text-sm text-muted-foreground">waiting</span>
              )}
              <span className="ml-auto flex items-center gap-2">
                {render.state === "running" && (
                  <span className="tnum text-sm text-muted-foreground">
                    {etaLabel(render.progress, render.started_at) ?? ""}
                    <span className="ml-2">{Math.round(render.progress * 100)} %</span>
                  </span>
                )}
                <Button
                  size="icon"
                  variant="ghost"
                  title="Cancel"
                  onClick={() => drop.mutate(render.id)}
                >
                  <X className="h-4 w-4" />
                </Button>
              </span>
            </div>
            {render.state === "running" && (
              <Progress value={render.progress * 100} className="h-1.5" />
            )}
          </div>
        ))}
        {failed.map((render) => (
          <div key={render.id} className="flex flex-wrap items-center gap-2 text-sm">
            <span className="truncate" title={who(render)}>
              {who(render)}
            </span>
            <Badge variant="outline" className="font-normal">
              {name(render.template)}
            </Badge>
            {/* The first line, and the whole thing on hover: gyroflow hands back its
                last thirty lines of progress, which is a wall of red over one fact. */}
            <p className="min-w-0 flex-1 truncate text-red-400" title={render.error ?? ""}>
              {(render.error ?? "failed").split("\n")[0]}
            </p>
            <Button
              size="icon"
              variant="ghost"
              title="Clear"
              onClick={() => drop.mutate(render.id)}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
