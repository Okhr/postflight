import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, FolderPlus, MoreHorizontal } from "lucide-react";
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
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { api, type Folder, type Sequence } from "@/lib/api";
import { rushColor } from "@/lib/colors";
import { formatDuration } from "@/lib/format";
import { usePersistentSet } from "@/lib/persist";
import { rushHref, selectedRushId } from "@/lib/routing";
import { cn } from "@/lib/utils";

function Dot({ token }: { token: string }) {
  const color = rushColor(token);
  return <span className={cn("h-2 w-2 shrink-0 rounded-full", color?.dot ?? "bg-muted")} />;
}

function RushRow({ sequence, folders }: { sequence: Sequence; folders: Folder[] }) {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const active = selectedRushId(pathname) === sequence.id;

  const file = useMutation({
    mutationFn: (folderId: number | null) => api.updateSequence(sequence.id, { folderId }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["sequences"] }),
    onError: (error: Error) => toast.error(error.message),
  });

  return (
    <div
      className={cn(
        "group flex items-center gap-1.5 rounded-md pl-2 pr-1 text-sm transition-colors",
        active ? "bg-accent" : "hover:bg-accent/50",
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
          {sequence.state === "ready"
            ? formatDuration(sequence.duration_ms)
            : sequence.state}
        </span>
      </button>

      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="shrink-0 rounded p-0.5 text-muted-foreground opacity-0 transition-opacity hover:text-foreground focus:opacity-100 group-hover:opacity-100"
            aria-label={`Actions for ${sequence.label}`}
          >
            <MoreHorizontal className="h-3.5 w-3.5" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuSub>
            <DropdownMenuSubTrigger>Move to</DropdownMenuSubTrigger>
            <DropdownMenuSubContent>
              {folders.map((folder) => (
                <DropdownMenuItem
                  key={folder.id}
                  disabled={folder.id === sequence.folder_id}
                  onSelect={() => file.mutate(folder.id)}
                >
                  <Dot token={folder.color} />
                  {folder.name}
                </DropdownMenuItem>
              ))}
              {folders.length > 0 && <DropdownMenuSeparator />}
              <DropdownMenuItem
                disabled={sequence.folder_id === null}
                onSelect={() => file.mutate(null)}
              >
                No folder
              </DropdownMenuItem>
            </DropdownMenuSubContent>
          </DropdownMenuSub>
        </DropdownMenuContent>
      </DropdownMenu>
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
  onDelete,
  onAddChild,
}: {
  folder: Folder;
  /** All of them, so a row can find its own children rather than be handed them. */
  folders: Folder[];
  rushes: Sequence[];
  collapsed: Set<string>;
  onToggle: (key: string) => void;
  onRename: (folder: Folder) => void;
  onDelete: (folder: Folder) => void;
  onAddChild: (parent: Folder) => void;
}) {
  const open = !collapsed.has(String(folder.id));
  const kids = folders.filter((f) => f.parent_id === folder.id);
  const mine = rushes.filter((s) => s.folder_id === folder.id);
  const held = mine.length + kids.length;

  return (
    <div>
      <div className="group flex items-center gap-1 rounded-md pr-1 transition-colors hover:bg-accent/50">
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
              className="shrink-0 rounded p-0.5 text-muted-foreground opacity-0 transition-opacity hover:text-foreground focus:opacity-100 group-hover:opacity-100"
              aria-label={`Actions for ${folder.name}`}
            >
              <MoreHorizontal className="h-3.5 w-3.5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => onRename(folder)}>Rename</DropdownMenuItem>
            {/* Two levels is the limit, so only a root folder offers this. */}
            {folder.parent_id === null && (
              <DropdownMenuItem onSelect={() => onAddChild(folder)}>
                New folder inside
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => onDelete(folder)}>Delete</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {open && (kids.length > 0 || mine.length > 0) && (
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
              onDelete={onDelete}
              onAddChild={onAddChild}
            />
          ))}
          {mine.map((sequence) => (
            <RushRow key={sequence.id} sequence={sequence} folders={folders} />
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

  const { data: sequences } = useQuery({
    queryKey: ["sequences"],
    queryFn: () => api.sequences(),
    refetchInterval: 3_000,
  });
  const { data: folders } = useQuery({ queryKey: ["folders"], queryFn: api.folders });

  const done = () => {
    queryClient.invalidateQueries({ queryKey: ["folders"] });
    queryClient.invalidateQueries({ queryKey: ["sequences"] });
    setRenaming(null);
    setCreating(null);
    setDeleting(null);
    setName("");
  };
  const failed = (error: Error) => toast.error(error.message);

  const create = useMutation({
    mutationFn: () => api.createFolder(name.trim(), creating?.parent?.id ?? null),
    onSuccess: done,
    onError: failed,
  });
  const rename = useMutation({
    mutationFn: () => api.renameFolder(renaming?.id ?? 0, name.trim()),
    onSuccess: done,
    onError: failed,
  });
  const remove = useMutation({
    mutationFn: () => api.deleteFolder(deleting?.id ?? 0),
    onSuccess: done,
    onError: failed,
  });

  const all = folders ?? [];
  const roots = all.filter((f) => f.parent_id === null);
  const rushes = sequences ?? [];
  const loose = rushes.filter(
    (s) => s.folder_id === null || !all.some((f) => f.id === s.folder_id),
  );

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mb-1 flex items-center justify-between pl-2 pr-1">
        <span className="text-sm font-medium text-muted-foreground">Rushes</span>
        <span className="flex items-center gap-1">
          <span className="tnum text-xs text-muted-foreground">{rushes.length}</span>
          <button
            type="button"
            onClick={() => {
              setName("");
              setCreating({ parent: null });
            }}
            className="rounded p-0.5 text-muted-foreground hover:text-foreground"
            aria-label="New folder"
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
          onDelete={setDeleting}
          onAddChild={(parent) => {
            setName("");
            setCreating({ parent });
          }}
        />
      ))}

      {loose.map((sequence) => (
        <RushRow key={sequence.id} sequence={sequence} folders={all} />
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
              placeholder="Pierrevert, August 2026, ..."
            />
            <DialogFooter className="mt-4">
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
