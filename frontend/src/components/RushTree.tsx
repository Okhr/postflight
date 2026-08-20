import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, FolderPlus, Palette, Pencil, Trash2 } from "lucide-react";
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
import { api, type Folder, type Sequence } from "@/lib/api";
import { RUSH_COLORS, rushColor } from "@/lib/colors";
import { formatDuration } from "@/lib/format";
import { usePersistentSet } from "@/lib/persist";
import { rushHref, selectedRushId } from "@/lib/routing";
import { cn } from "@/lib/utils";

/** What is being dragged. Kept in state rather than read from the drop event:
 *  `dataTransfer` hides its values during `dragover`, and whether a drop is legal
 *  has to be known then, which is when the target decides to light up. */
type Dragged = { kind: "rush" | "folder"; id: number } | null;

function Dot({ token, className }: { token: string; className?: string }) {
  const color = rushColor(token);
  return (
    <span
      className={cn("h-2 w-2 shrink-0 rounded-full", color?.dot ?? "bg-muted", className)}
    />
  );
}

/** The six palette tokens as swatches. Used at creation and to recolour. */
function Swatches({
  value,
  onPick,
}: {
  value: string;
  onPick: (token: string) => void;
}) {
  return (
    <span className="flex items-center gap-1.5">
      {RUSH_COLORS.map((color) => (
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

  return (
    <div
      draggable
      onDragStart={() => setDragged({ kind: "rush", id: sequence.id })}
      onDragEnd={() => setDragged(null)}
      className={cn(
        "group flex items-center gap-1.5 rounded-md pl-2 pr-1 text-sm transition-colors",
        active ? "bg-accent" : "hover:bg-accent/50",
        lifted && "opacity-40",
      )}
    >
      <button
        type="button"
        onClick={() => navigate(rushHref(pathname, sequence.id))}
        className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 text-left"
      >
        {sequence.color && <Dot token={sequence.color} />}
        <span className="truncate">{sequence.label}</span>
        <span className="tnum ml-auto shrink-0 pl-2 text-xs text-muted-foreground">
          {sequence.state === "ready" ? formatDuration(sequence.duration_ms) : sequence.state}
        </span>
      </button>
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
  onDrop,
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
  onDrop: (dragged: Dragged, into: number | null) => void;
  dragged: Dragged;
  setDragged: (dragged: Dragged) => void;
}) {
  const [over, setOver] = useState(false);
  const open = !collapsed.has(String(folder.id));
  const kids = folders.filter((f) => f.parent_id === folder.id);
  const mine = rushes.filter((s) => s.folder_id === folder.id);
  const held = mine.length + kids.length;
  const root = folder.parent_id === null;

  // Two levels is the whole rule, so a folder only ever drops into a root one, and
  // never into itself or into a folder it already sits in.
  const takes =
    dragged !== null &&
    (dragged.kind === "rush"
      ? rushes.find((s) => s.id === dragged.id)?.folder_id !== folder.id
      : root &&
        dragged.id !== folder.id &&
        folders.find((f) => f.id === dragged.id)?.parent_id !== folder.id &&
        !folders.some((f) => f.parent_id === dragged.id));

  return (
    <div>
      <div
        draggable
        onDragStart={() => setDragged({ kind: "folder", id: folder.id })}
        onDragEnd={() => setDragged(null)}
        onDragOver={(event) => {
          if (!takes) return;
          event.preventDefault();
          event.stopPropagation();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(event) => {
          if (!takes) return;
          event.preventDefault();
          event.stopPropagation();
          setOver(false);
          onDrop(dragged, folder.id);
        }}
        className={cn(
          "group flex items-center gap-1 rounded-md pr-1 transition-colors",
          over ? "bg-primary/20 ring-1 ring-primary" : "hover:bg-accent/50",
          dragged?.kind === "folder" && dragged.id === folder.id && "opacity-40",
        )}
      >
        <button
          type="button"
          onClick={() => onToggle(String(folder.id))}
          className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 pl-1 text-left text-sm"
        >
          <ChevronRight
            className={cn(
              "h-3 w-3 shrink-0 text-muted-foreground transition-transform",
              open && "rotate-90",
            )}
          />
          <Dot token={folder.color} />
          <span className="truncate font-medium">{folder.name}</span>
          <span className="tnum ml-auto shrink-0 pl-2 text-xs text-muted-foreground">
            {held || ""}
          </span>
        </button>

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
      </div>

      {open && held > 0 && (
        <div className="ml-3 border-l pl-1">
          {kids.map((child) => (
            <FolderRow
              key={child.id}
              folder={child}
              folders={folders}
              rushes={rushes}
              collapsed={collapsed}
              onToggle={onToggle}
              onRename={onRename}
              onRecolour={onRecolour}
              onDelete={onDelete}
              onAddChild={onAddChild}
              onDrop={onDrop}
              dragged={dragged}
              setDragged={setDragged}
            />
          ))}
          {mine.map((sequence) => (
            <RushRow
              key={sequence.id}
              sequence={sequence}
              dragged={dragged}
              setDragged={setDragged}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** The rush tree: folders two deep, and every rush that is not in one below them. */
export function RushTree() {
  const queryClient = useQueryClient();
  const [collapsed, toggle] = usePersistentSet("folders-collapsed");
  const [renaming, setRenaming] = useState<Folder | null>(null);
  const [creating, setCreating] = useState<{ parent: Folder | null } | null>(null);
  const [deleting, setDeleting] = useState<Folder | null>(null);
  const [name, setName] = useState("");
  const [color, setColor] = useState(RUSH_COLORS[0].token);
  const [dragged, setDragged] = useState<Dragged>(null);
  const [overRoot, setOverRoot] = useState(false);

  const { data: sequences } = useQuery({
    queryKey: ["sequences"],
    queryFn: () => api.sequences(),
    refetchInterval: 3_000,
  });
  const { data: folders } = useQuery({ queryKey: ["folders"], queryFn: api.folders });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["folders"] });
    queryClient.invalidateQueries({ queryKey: ["sequences"] });
  };
  const done = () => {
    refresh();
    setRenaming(null);
    setCreating(null);
    setDeleting(null);
    setName("");
  };
  const failed = (error: Error) => toast.error(error.message);

  const create = useMutation({
    mutationFn: () => api.createFolder(name.trim(), creating?.parent?.id ?? null, color),
    onSuccess: done,
    onError: failed,
  });
  const rename = useMutation({
    mutationFn: () => api.updateFolder(renaming?.id ?? 0, { name: name.trim() }),
    onSuccess: done,
    onError: failed,
  });
  const recolour = useMutation({
    mutationFn: ({ id, token }: { id: number; token: string }) =>
      api.updateFolder(id, { color: token }),
    onSuccess: refresh,
    onError: failed,
  });
  const remove = useMutation({
    mutationFn: () => api.deleteFolder(deleting?.id ?? 0),
    onSuccess: done,
    onError: failed,
  });
  // A rush and a folder move through different endpoints, and the result of neither is
  // read: the tree refetches, which is also what keeps the counts right.
  const move = useMutation<void, Error, { item: Dragged; into: number | null }>({
    mutationFn: async ({ item, into }) => {
      if (item === null) return;
      if (item.kind === "rush") await api.updateSequence(item.id, { folderId: into });
      else await api.updateFolder(item.id, { parentId: into });
    },
    onSuccess: refresh,
    onError: failed,
  });

  const all = folders ?? [];
  const roots = all.filter((f) => f.parent_id === null);
  const rushes = sequences ?? [];
  const loose = rushes.filter(
    (s) => s.folder_id === null || !all.some((f) => f.id === s.folder_id),
  );

  const drop = (item: Dragged, into: number | null) => {
    setDragged(null);
    if (item !== null) move.mutate({ item, into });
  };

  // The tree's own body is the way back out of a folder, for a rush and for a
  // subfolder alike. A row that takes the drop stops the event, so this only ever
  // sees what was dropped on the empty space around them.
  const rootTakes =
    dragged !== null &&
    (dragged.kind === "rush"
      ? rushes.find((s) => s.id === dragged.id)?.folder_id !== null
      : all.find((f) => f.id === dragged.id)?.parent_id !== null);

  const openCreate = (parent: Folder | null) => {
    setName("");
    // Preselected at random, so a folder made without a thought still comes out told
    // apart from its neighbours.
    setColor(RUSH_COLORS[Math.floor(Math.random() * RUSH_COLORS.length)].token);
    setCreating({ parent });
  };

  return (
    <div
      className={cn(
        "min-h-0 flex-1 overflow-y-auto rounded-md transition-colors",
        overRoot && "bg-primary/10 ring-1 ring-primary",
      )}
      onDragOver={(event) => {
        if (!rootTakes) return;
        event.preventDefault();
        setOverRoot(true);
      }}
      onDragLeave={() => setOverRoot(false)}
      onDrop={(event) => {
        if (!rootTakes) return;
        event.preventDefault();
        setOverRoot(false);
        drop(dragged, null);
      }}
    >
      <div className="mb-1 flex items-center justify-between pl-2 pr-1">
        <span className="text-sm font-medium text-muted-foreground">Rushes</span>
        <span className="flex items-center gap-1">
          <span className="tnum text-xs text-muted-foreground">{rushes.length}</span>
          <button
            type="button"
            onClick={() => openCreate(null)}
            title="New folder"
            aria-label="New folder"
            className="rounded p-0.5 text-muted-foreground hover:text-foreground"
          >
            <FolderPlus className="h-3.5 w-3.5" />
          </button>
        </span>
      </div>

      {roots.map((folder) => (
        <FolderRow
          key={folder.id}
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
          onDrop={drop}
          dragged={dragged}
          setDragged={setDragged}
        />
      ))}

      {loose.map((sequence) => (
        <RushRow
          key={sequence.id}
          sequence={sequence}
          dragged={dragged}
          setDragged={setDragged}
        />
      ))}

      {rushes.length === 0 && (
        <p className="px-2 py-3 text-sm text-muted-foreground">No rush yet.</p>
      )}

      <Dialog
        open={creating !== null || renaming !== null}
        onOpenChange={(open) => {
          if (!open) done();
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
              if (!name.trim()) return;
              (renaming ? rename : create).mutate();
            }}
          >
            <Input
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
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
            The rushes in it come back to the root. Nothing is deleted but the folder.
          </p>
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button variant="destructive" size="sm" onClick={() => remove.mutate()}>
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
