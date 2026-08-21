import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronRight,
  Droplet,
  FolderPlus,
  Palette,
  Pencil,
  Trash2,
  Zap,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { api, type Cut, type Folder, type Sequence } from "@/lib/api";
import { FOLDER_COLORS, folderColor } from "@/lib/colors";
import { formatDuration } from "@/lib/format";
import { usePersistentSet } from "@/lib/persist";
import { rushHref, selectedRushId } from "@/lib/routing";
import { cn } from "@/lib/utils";

/**
 * Global is the folder a rush is in before anyone files it, and it is not a row in
 * the database: it is what `folder_id: null` looks like. So there is nothing to
 * protect against being renamed, recoloured, deleted or dragged, because none of
 * those gestures exists for it. Always first, and grey, since it is the one folder
 * nobody chose a colour for.
 */
const GLOBAL = { id: null, name: "Global" } as const;

/** What is being dragged. Kept in state rather than read from the drop event:
 *  `dataTransfer` hides its values during `dragover`, and whether a drop is legal
 *  has to be known then, which is when the target decides to light up. */
type Dragged = { kind: "rush" | "folder"; id: number } | null;

/**
 * Is the pointer really leaving, or just moving onto a child?
 *
 * `dragleave` fires on an element the moment the pointer enters one of its children,
 * so the naive handler switches the highlight off the instant `dragover` switched it
 * on. Measured on 2026-08-20: the insertion line never lit up, while the drop itself
 * worked, because `dragover` bubbles up from the child and `dragleave` does not mean
 * what it looks like.
 */
function leaving(event: React.DragEvent): boolean {
  return !event.currentTarget.contains(event.relatedTarget as Node | null);
}

function Dot({ token }: { token: string | null }) {
  const color = token === null ? null : folderColor(token);
  return (
    <span
      className={cn(
        "h-2 w-2 shrink-0 rounded-full",
        token === null ? "bg-muted-foreground/50" : (color?.dot ?? "bg-muted"),
      )}
    />
  );
}

/** The six palette tokens as swatches. Used at creation and to recolour. */
function Swatches({ value, onPick }: { value: string; onPick: (token: string) => void }) {
  return (
    <span className="flex items-center gap-1.5">
      {FOLDER_COLORS.map((color) => (
        <button
          key={color.token}
          type="button"
          onClick={() => onPick(color.token)}
          aria-label={color.label}
          className={cn(
            "h-5 w-5 rounded-full border-2 transition-colors",
            color.dot,
            value === color.token ? "border-foreground" : "border-transparent",
          )}
        />
      ))}
    </span>
  );
}

/** A hover action on a row: an icon, and nothing else until it is hovered. */
function RowIcon({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      className="shrink-0 rounded p-0.5 text-muted-foreground opacity-0 transition-opacity hover:text-foreground focus:opacity-100 group-hover:opacity-100"
    >
      {children}
    </button>
  );
}

/**
 * The line between two folders, which is what tells reordering apart from nesting.
 *
 * It only exists while a folder is being dragged, and it claims a couple of pixels
 * of its own: dropping onto a row means "into it", dropping between rows means
 * "next to it", and a tree that only has rows cannot express the second.
 */
function Gap({
  parentId,
  index,
  dragged,
  canLand,
  onDrop,
}: {
  parentId: number | null;
  index: number;
  dragged: Dragged;
  canLand: (dragged: Dragged, parentId: number | null) => boolean;
  onDrop: (dragged: Dragged, parentId: number | null, index: number) => void;
}) {
  const [over, setOver] = useState(false);
  if (dragged?.kind !== "folder" || !canLand(dragged, parentId)) return null;

  // `dragenter` is handled as well as `dragover`, and not only for symmetry: entering
  // is what declares the element a drop zone, and measured on 2026-08-20 a pointer
  // that enters and then holds still gets the enter and no over at all, so the line
  // stayed dark underneath a drop that worked.
  const enter = (event: React.DragEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setOver(true);
  };

  return (
    <div
      onDragEnter={enter}
      onDragOver={enter}
      onDragLeave={(event) => leaving(event) && setOver(false)}
      onDrop={(event) => {
        event.preventDefault();
        event.stopPropagation();
        setOver(false);
        onDrop(dragged, parentId, index);
      }}
      className="-my-0.5 flex h-2 items-center"
      data-gap={`${parentId ?? "root"}:${index}`}
    >
      <span
        className={cn("h-0.5 w-full rounded", over ? "bg-primary" : "bg-transparent")}
      />
    </div>
  );
}

/**
 * One marked sequence of a rush, and how far it has been taken.
 *
 * Two icons, lit or not: a stabilized file exists, and a graded one on top of it.
 * That is the whole state of an evening's work, readable without opening a page.
 */
function CutRow({ cut, onOpen }: { cut: Cut; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex w-full min-w-0 items-center gap-1.5 rounded-md py-1 pl-2 pr-1 text-left text-xs text-muted-foreground transition-colors hover:bg-accent/50 hover:text-foreground"
    >
      <span className="truncate" title={cut.label}>
        {cut.label}
      </span>
      <span className="ml-auto flex shrink-0 items-center gap-1 pl-2">
        <span title={cut.rendered ? "Stabilized" : "Not stabilized"}>
          <Zap
            className={cn("h-3 w-3", cut.rendered ? "text-foreground" : "text-muted-foreground/30")}
          />
        </span>
        <span title={cut.graded ? "Graded" : "Not graded"}>
          <Droplet
            className={cn("h-3 w-3", cut.graded ? "text-foreground" : "text-muted-foreground/30")}
          />
        </span>
      </span>
    </button>
  );
}

function RushRow({
  sequence,
  dragged,
  setDragged,
}: {
  sequence: Sequence;
  dragged: Dragged;
  setDragged: (dragged: Dragged) => void;
}) {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const active = selectedRushId(pathname) === sequence.id;
  const lifted = dragged?.kind === "rush" && dragged.id === sequence.id;
  const [open, setOpen] = useState(false);
  const marked = sequence.cut_count > 0;

  // Asked for only once unfolded: the tree holds every rush, and the sequences of
  // one are a request of their own. The key is the derush page's, so opening a rush
  // there and unfolding it here cost one fetch between them.
  const { data: detail } = useQuery({
    queryKey: ["sequence", sequence.id],
    queryFn: () => api.sequence(sequence.id),
    enabled: open && marked,
  });

  return (
    <div>
      <div
        draggable
        onDragStart={() => setDragged({ kind: "rush", id: sequence.id })}
        onDragEnd={() => setDragged(null)}
        className={cn(
          "group flex items-center rounded-md pr-1 text-sm transition-colors",
          active ? "bg-accent" : "hover:bg-accent/50",
          lifted && "opacity-40",
        )}
      >
        {marked ? (
          <button
            type="button"
            onClick={() => setOpen(!open)}
            className="shrink-0 px-1 py-1.5 text-muted-foreground hover:text-foreground"
            title={open ? "Fold" : "Unfold"}
          >
            <ChevronRight className={cn("h-3 w-3 transition-transform", open && "rotate-90")} />
          </button>
        ) : (
          <span className="w-5 shrink-0" />
        )}
        <button
          type="button"
          onClick={() => navigate(rushHref(pathname, sequence.id))}
          className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 text-left"
        >
          <span
            className={cn("truncate", sequence.derushed && "text-muted-foreground")}
            title={sequence.label}
          >
            {sequence.label}
          </span>
          <span className="tnum ml-auto shrink-0 pl-2 text-xs text-muted-foreground">
            {sequence.state === "ready" ? formatDuration(sequence.duration_ms) : sequence.state}
          </span>
          {/* Marked done: the tree is where one looks to see what is left. */}
          {sequence.derushed && (
            <span title="Derushed" className="shrink-0">
              <Check className="h-3 w-3 text-muted-foreground" />
            </span>
          )}
        </button>
      </div>
      {open && detail && (
        <div className="ml-5 border-l pl-1">
          {detail.cuts.map((cut) => (
            <CutRow
              key={cut.id}
              cut={cut}
              onOpen={() => navigate(rushHref(pathname, sequence.id))}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** Shared by Global and by a named folder: the drop target, the twisty, the count. */
function FolderShell({
  id,
  name,
  color,
  open,
  count,
  onToggle,
  takes,
  onDrop,
  actions,
  handle,
  children,
}: {
  id: number | null;
  name: string;
  color: string | null;
  open: boolean;
  /** How much it holds. Not shown: what it decides is whether there is anything to
   *  unfold at all. */
  count: number;
  onToggle: () => void;
  takes: boolean;
  onDrop: () => void;
  actions?: React.ReactNode;
  handle?: {
    lifted: boolean;
    onDragStart: () => void;
    onDragEnd: () => void;
  };
  children: React.ReactNode;
}) {
  const [over, setOver] = useState(false);

  const enter = (event: React.DragEvent) => {
    if (!takes) return;
    event.preventDefault();
    event.stopPropagation();
    setOver(true);
  };

  return (
    <div>
      <div
        draggable={handle !== undefined}
        onDragStart={handle?.onDragStart}
        onDragEnd={handle?.onDragEnd}
        onDragEnter={enter}
        onDragOver={enter}
        onDragLeave={(event) => leaving(event) && setOver(false)}
        onDrop={(event) => {
          if (!takes) return;
          event.preventDefault();
          event.stopPropagation();
          setOver(false);
          onDrop();
        }}
        className={cn(
          "group flex items-center gap-1 rounded-md pr-1 transition-colors",
          over ? "bg-primary/20 ring-1 ring-primary" : "hover:bg-accent/50",
          handle?.lifted && "opacity-40",
        )}
        data-folder={id ?? "global"}
      >
        <button
          type="button"
          onClick={onToggle}
          className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 pl-1 text-left text-sm"
        >
          <ChevronRight
            className={cn(
              "h-3 w-3 shrink-0 text-muted-foreground transition-transform",
              open && "rotate-90",
            )}
          />
          <Dot token={color} />
          <span className="truncate font-medium" title={name}>
          {name}
        </span>
        </button>
        {actions}
      </div>
      {open && count > 0 && <div className="ml-3 border-l pl-1">{children}</div>}
    </div>
  );
}

function FolderRow({
  folder,
  folders,
  rushes,
  collapsed,
  onToggle,
  onRename,
  onRecolour,
  onDelete,
  onAddChild,
  onFile,
  onOrder,
  canLand,
  dragged,
  setDragged,
}: {
  folder: Folder;
  /** All of them, so a row can find its own children rather than be handed them. */
  folders: Folder[];
  rushes: Sequence[];
  collapsed: Set<string>;
  onToggle: (key: string) => void;
  onRename: (folder: Folder) => void;
  onRecolour: (folder: Folder, token: string) => void;
  onDelete: (folder: Folder) => void;
  onAddChild: (parent: Folder) => void;
  onFile: (dragged: Dragged, into: number | null) => void;
  onOrder: (dragged: Dragged, parentId: number | null, index: number) => void;
  canLand: (dragged: Dragged, parentId: number | null) => boolean;
  dragged: Dragged;
  setDragged: (dragged: Dragged) => void;
}) {
  const open = !collapsed.has(String(folder.id));
  const kids = folders.filter((f) => f.parent_id === folder.id);
  const mine = rushes.filter((s) => s.folder_id === folder.id);
  const root = folder.parent_id === null;

  // Dropping onto the row means "into this folder". A rush always may, unless it is
  // already here. A folder may only nest inside a root one, which is the two-level
  // rule, and never inside itself or where it already is.
  const takes =
    dragged !== null &&
    (dragged.kind === "rush"
      ? rushes.find((s) => s.id === dragged.id)?.folder_id !== folder.id
      : root &&
        dragged.id !== folder.id &&
        folders.find((f) => f.id === dragged.id)?.parent_id !== folder.id &&
        !folders.some((f) => f.parent_id === dragged.id));

  return (
    <FolderShell
      id={folder.id}
      name={folder.name}
      color={folder.color}
      open={open}
      count={mine.length + kids.length}
      onToggle={() => onToggle(String(folder.id))}
      takes={takes}
      onDrop={() => onFile(dragged, folder.id)}
      handle={{
        lifted: dragged?.kind === "folder" && dragged.id === folder.id,
        onDragStart: () => setDragged({ kind: "folder", id: folder.id }),
        onDragEnd: () => setDragged(null),
      }}
      actions={
        <>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                title="Colour"
                aria-label={`Colour of ${folder.name}`}
                onClick={(event) => event.stopPropagation()}
                className="shrink-0 rounded p-0.5 text-muted-foreground opacity-0 transition-opacity hover:text-foreground focus:opacity-100 group-hover:opacity-100"
              >
                <Palette className="h-3.5 w-3.5" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="p-2">
              <Swatches value={folder.color} onPick={(token) => onRecolour(folder, token)} />
            </DropdownMenuContent>
          </DropdownMenu>
          <RowIcon label="Rename" onClick={() => onRename(folder)}>
            <Pencil className="h-3.5 w-3.5" />
          </RowIcon>
          {/* Two levels is the limit, so only a root folder offers this. */}
          {root && (
            <RowIcon label="New folder inside" onClick={() => onAddChild(folder)}>
              <FolderPlus className="h-3.5 w-3.5" />
            </RowIcon>
          )}
          <RowIcon label="Delete" onClick={() => onDelete(folder)}>
            <Trash2 className="h-3.5 w-3.5" />
          </RowIcon>
        </>
      }
    >
      {kids.map((child, index) => (
        <div key={child.id}>
          <Gap
            parentId={folder.id}
            index={index}
            dragged={dragged}
            canLand={canLand}
            onDrop={onOrder}
          />
          <FolderRow
            folder={child}
            folders={folders}
            rushes={rushes}
            collapsed={collapsed}
            onToggle={onToggle}
            onRename={onRename}
            onRecolour={onRecolour}
            onDelete={onDelete}
            onAddChild={onAddChild}
            onFile={onFile}
            onOrder={onOrder}
            canLand={canLand}
            dragged={dragged}
            setDragged={setDragged}
          />
        </div>
      ))}
      {kids.length > 0 && (
        <Gap
          parentId={folder.id}
          index={kids.length}
          dragged={dragged}
          canLand={canLand}
          onDrop={onOrder}
        />
      )}
      {/* Rushes keep the order they were shot in, and are not reordered by hand. */}
      {mine.map((sequence) => (
        <RushRow
          key={sequence.id}
          sequence={sequence}
          dragged={dragged}
          setDragged={setDragged}
        />
      ))}
    </FolderShell>
  );
}

const FOLDERS = ["folders"] as const;
const SEQUENCES = ["sequences"] as const;

interface Tree {
  folders: Folder[];
  rushes: Sequence[];
}

/**
 * A write to the tree that shows before it is confirmed.
 *
 * Every one of these is a click or a drop, and waiting for the round trip to move
 * anything makes the tree feel like it is thinking. So the cache is written as the
 * answer will look, the request goes out behind it, and a refusal puts the old tree
 * back with the error. `onSettled` refetches either way: it is what corrects a
 * prediction that turned out slightly wrong, without anyone having to notice.
 */
function useTreeWrite<TVars>(
  send: (vars: TVars) => Promise<unknown>,
  predict: (vars: TVars, tree: Tree) => Tree,
) {
  const queryClient = useQueryClient();
  return useMutation<unknown, Error, TVars, Tree>({
    mutationFn: send,
    onMutate: async (vars) => {
      // A refetch already in the air would land on top of the prediction.
      await Promise.all([
        queryClient.cancelQueries({ queryKey: FOLDERS }),
        queryClient.cancelQueries({ queryKey: SEQUENCES }),
      ]);
      const before: Tree = {
        folders: queryClient.getQueryData<Folder[]>(FOLDERS) ?? [],
        rushes: queryClient.getQueryData<Sequence[]>(SEQUENCES) ?? [],
      };
      const after = predict(vars, before);
      queryClient.setQueryData(FOLDERS, after.folders);
      queryClient.setQueryData(SEQUENCES, after.rushes);
      return before;
    },
    onError: (error, _vars, before) => {
      if (before) {
        queryClient.setQueryData(FOLDERS, before.folders);
        queryClient.setQueryData(SEQUENCES, before.rushes);
      }
      toast.error(error.message);
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: FOLDERS });
      queryClient.invalidateQueries({ queryKey: SEQUENCES });
    },
  });
}

const bySeat = (a: Folder, b: Folder) => a.position - b.position || a.id - b.id;

/**
 * The server's placement rule, repeated on this side so a drop lands at once.
 *
 * Duplicated logic is a cost paid on purpose: the alternative is a tree that jumps
 * a beat after the drop. The two can only disagree until the refetch on settle, and
 * that is what makes the duplication affordable rather than dangerous.
 */
function place(
  folders: Folder[],
  moved: Folder,
  parentId: number | null,
  index: number,
): Folder[] {
  const rank = new Map<number, number>();

  const landing = folders
    .filter((f) => f.parent_id === parentId && f.id !== moved.id)
    .sort(bySeat);
  landing.splice(Math.max(0, Math.min(index, landing.length)), 0, moved);
  landing.forEach((f, seat) => rank.set(f.id, seat));

  // The list it came from closes up, or the next drop there lands on the hole.
  if (moved.parent_id !== parentId) {
    folders
      .filter((f) => f.parent_id === moved.parent_id && f.id !== moved.id)
      .sort(bySeat)
      .forEach((f, seat) => rank.set(f.id, seat));
  }

  return folders.map((f) => {
    const seat = rank.get(f.id);
    if (seat === undefined) return f;
    return f.id === moved.id
      ? { ...f, parent_id: parentId, position: seat }
      : { ...f, position: seat };
  });
}

/** The rush tree: Global, then the folders two deep, and the rushes inside them. */
export function RushTree() {
  const [collapsed, toggle] = usePersistentSet("folders-collapsed");
  const [renaming, setRenaming] = useState<Folder | null>(null);
  const [creating, setCreating] = useState<{ parent: Folder | null } | null>(null);
  const [deleting, setDeleting] = useState<Folder | null>(null);
  const [name, setName] = useState("");
  const [color, setColor] = useState(FOLDER_COLORS[0].token);
  const [dragged, setDragged] = useState<Dragged>(null);

  const { data: sequences } = useQuery({
    queryKey: ["sequences"],
    queryFn: () => api.sequences(),
    refetchInterval: 3_000,
  });
  const { data: folders } = useQuery({ queryKey: ["folders"], queryFn: api.folders });

  const shut = () => {
    setRenaming(null);
    setCreating(null);
    setDeleting(null);
    setName("");
  };

  const create = useTreeWrite<{ name: string; parentId: number | null; color: string }>(
    (vars) => api.createFolder(vars.name, vars.parentId, vars.color),
    (vars, tree) => ({
      ...tree,
      // A made-up id until the real one arrives. Negative so it cannot collide, and
      // short-lived: the refetch on settle replaces the row wholesale.
      folders: [
        ...tree.folders,
        {
          id: -Date.now(),
          name: vars.name,
          color: vars.color,
          parent_id: vars.parentId,
          position: tree.folders.filter((f) => f.parent_id === vars.parentId).length,
          sequence_count: 0,
        },
      ],
    }),
  );

  const rename = useTreeWrite<{ id: number; name: string }>(
    (vars) => api.updateFolder(vars.id, { name: vars.name }),
    (vars, tree) => ({
      ...tree,
      folders: tree.folders.map((f) => (f.id === vars.id ? { ...f, name: vars.name } : f)),
    }),
  );

  const recolour = useTreeWrite<{ id: number; token: string }>(
    (vars) => api.updateFolder(vars.id, { color: vars.token }),
    (vars, tree) => ({
      ...tree,
      folders: tree.folders.map((f) => (f.id === vars.id ? { ...f, color: vars.token } : f)),
    }),
  );

  const remove = useTreeWrite<{ id: number }>(
    (vars) => api.deleteFolder(vars.id),
    (vars, tree) => ({
      // What it held is not lost: its rushes fall back to Global, its children to the
      // root, which is what the API does with them.
      folders: tree.folders
        .filter((f) => f.id !== vars.id)
        .map((f) => (f.parent_id === vars.id ? { ...f, parent_id: null } : f)),
      rushes: tree.rushes.map((s) =>
        s.folder_id === vars.id ? { ...s, folder_id: null } : s,
      ),
    }),
  );

  const move = useTreeWrite<{ item: Dragged; into: number | null; index?: number }>(
    async ({ item, into, index }) => {
      if (item === null) return;
      // A rush and a folder move through different endpoints, and the result of
      // neither is read: the prediction already put them where they belong.
      if (item.kind === "rush") return api.updateSequence(item.id, { folderId: into });
      return api.updateFolder(item.id, { parentId: into, position: index });
    },
    ({ item, into, index }, tree) => {
      if (item === null) return tree;
      if (item.kind === "rush") {
        return {
          ...tree,
          rushes: tree.rushes.map((s) =>
            s.id === item.id ? { ...s, folder_id: into } : s,
          ),
        };
      }
      const moved = tree.folders.find((f) => f.id === item.id);
      if (moved === undefined) return tree;
      return { ...tree, folders: place(tree.folders, moved, into, index ?? 10 ** 6) };
    },
  );

  const all = folders ?? [];
  const roots = all.filter((f) => f.parent_id === null);
  const rushes = sequences ?? [];
  // Global holds every rush nobody filed, and any whose folder went away under it.
  const global = rushes.filter(
    (s) => s.folder_id === null || !all.some((f) => f.id === s.folder_id),
  );

  /** May this folder become a child of `parentId`? Two levels is the whole rule. */
  const canLand = (item: Dragged, parentId: number | null) => {
    if (item?.kind !== "folder") return false;
    if (parentId === null) return true;
    if (parentId === item.id) return false;
    return !all.some((f) => f.parent_id === item.id);
  };

  const file = (item: Dragged, into: number | null) => {
    setDragged(null);
    if (item !== null) move.mutate({ item, into });
  };
  const order = (item: Dragged, parentId: number | null, index: number) => {
    setDragged(null);
    if (item !== null) move.mutate({ item, into: parentId, index });
  };

  const openCreate = (parent: Folder | null) => {
    setName("");
    // Preselected at random, so a folder made without a thought still comes out told
    // apart from its neighbours.
    setColor(FOLDER_COLORS[Math.floor(Math.random() * FOLDER_COLORS.length)].token);
    setCreating({ parent });
  };

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mb-1 flex items-center justify-between pl-2 pr-1">
        <span className="text-sm font-medium text-muted-foreground">Rushes</span>
        <button
          type="button"
          onClick={() => openCreate(null)}
          title="New folder"
          aria-label="New folder"
          className="rounded p-0.5 text-muted-foreground hover:text-foreground"
        >
          <FolderPlus className="h-3.5 w-3.5" />
        </button>
      </div>

      <FolderShell
        id={GLOBAL.id}
        name={GLOBAL.name}
        color={null}
        open={!collapsed.has("global")}
        count={global.length}
        onToggle={() => toggle("global")}
        takes={
          dragged?.kind === "rush" &&
          rushes.find((s) => s.id === dragged.id)?.folder_id !== null
        }
        onDrop={() => file(dragged, null)}
      >
        {global.map((sequence) => (
          <RushRow
            key={sequence.id}
            sequence={sequence}
            dragged={dragged}
            setDragged={setDragged}
          />
        ))}
      </FolderShell>

      {roots.map((folder, index) => (
        <div key={folder.id}>
          <Gap parentId={null} index={index} dragged={dragged} canLand={canLand} onDrop={order} />
          <FolderRow
            folder={folder}
            folders={all}
            rushes={rushes}
            collapsed={collapsed}
            onToggle={toggle}
            onRename={(f) => {
              setName(f.name);
              setRenaming(f);
            }}
            onRecolour={(f, token) => recolour.mutate({ id: f.id, token })}
            onDelete={setDeleting}
            onAddChild={openCreate}
            onFile={file}
            onOrder={order}
            canLand={canLand}
            dragged={dragged}
            setDragged={setDragged}
          />
        </div>
      ))}
      <Gap parentId={null} index={roots.length} dragged={dragged} canLand={canLand} onDrop={order} />

      <Dialog
        open={creating !== null || renaming !== null}
        onOpenChange={(open) => {
          if (!open) shut();
        }}
      >
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>
              {renaming
                ? "Rename folder"
                : creating?.parent
                  ? `New folder in ${creating.parent.name}`
                  : "New folder"}
            </DialogTitle>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={(event) => {
              event.preventDefault();
              const wanted = name.trim();
              if (!wanted) return;
              if (renaming) rename.mutate({ id: renaming.id, name: wanted });
              else create.mutate({ name: wanted, parentId: creating?.parent?.id ?? null, color });
              // Closed on the gesture, not on the answer: the tree already shows it.
              shut();
            }}
          >
            <Input autoFocus value={name} onChange={(event) => setName(event.target.value)} />
            {!renaming && <Swatches value={color} onPick={setColor} />}
            <DialogFooter>
              <Button type="submit" size="sm" disabled={!name.trim()}>
                {renaming ? "Rename" : "Create"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={deleting !== null} onOpenChange={(open) => !open && setDeleting(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Delete {deleting?.name}?</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            The rushes in it go back to Global. Nothing is deleted but the folder.
          </p>
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              size="sm"
              onClick={() => {
                if (deleting) remove.mutate({ id: deleting.id });
                shut();
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
