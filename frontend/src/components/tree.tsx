/**
 * What a tree row is made of, shared by the pages that draw one.
 *
 * Stabilize and Color both list the same hierarchy, and had drifted apart on every
 * detail of it: 16 px of indent against 12, `gap-2` against `gap-1.5`, hover on every
 * row against hover on leaves only, a rush that was a link with a chevron against a
 * rush that was muted text. It showed, because it is the same folders and the same
 * rushes on both.
 *
 * Primitives rather than one configurable tree: the two pages have different jobs (a
 * queue of what is missing, an editor of what exists), so what differs is which level
 * is the leaf and what sits on the right of a row. Sharing the anatomy fixes the look
 * without pretending the two interactions are one.
 */
import { ChevronRight } from "lucide-react";

import { folderColor } from "@/lib/colors";
import { cn } from "@/lib/utils";

/** One level of nesting, in pixels. */
export const INDENT = 16;

/** The row itself: same height, same gap, same hover, wherever it is drawn. */
export const rowClass = "flex items-center gap-2 rounded-md py-1 text-sm hover:bg-accent/40";

export function Indent({ depth }: { depth: number }) {
  return <span className="shrink-0" style={{ width: depth * INDENT }} />;
}

/** The chevron, which is also the whole hit area for opening a node. */
export function Twisty({ open, onClick }: { open: boolean; onClick?: () => void }) {
  const icon = (
    <ChevronRight className={cn("h-3 w-3 shrink-0 transition-transform", open && "rotate-90")} />
  );
  if (!onClick) return icon;
  return (
    <button
      type="button"
      onClick={onClick}
      className="shrink-0 text-muted-foreground hover:text-foreground"
    >
      {icon}
    </button>
  );
}

/** A folder's colour, and the one grey dot of the folder nobody chose. */
export function Dot({ color }: { color?: string | null }) {
  return (
    <span
      className={cn(
        "h-2 w-2 shrink-0 rounded-full",
        color ? (folderColor(color)?.dot ?? "bg-muted") : "bg-muted-foreground/50",
      )}
    />
  );
}

/** A number on the right: tabular, small, and always the same width per column. */
export function Meta({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span className={cn("tnum shrink-0 text-xs text-muted-foreground", className)}>
      {children}
    </span>
  );
}
