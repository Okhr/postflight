import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { mediaUrl, type Sequence } from "@/lib/api";
import { rushColor } from "@/lib/colors";
import { formatDateTime, formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * What a rush already has to show for itself: marked zones, and clips produced
 * from them. Chips rather than a grey sentence: the whole point of the side list
 * is telling at a glance which rushes have been dealt with.
 */
export function SequenceWork({ sequence }: { sequence: Sequence }) {
  const { cut_count: zones, render_count: clips } = sequence;
  if (!zones && !clips) {
    return <span className="text-[11px] text-muted-foreground">nothing marked yet</span>;
  }
  return (
    <span className="flex flex-wrap items-center gap-1">
      {zones > 0 && (
        <span className="rounded border px-1 py-px text-[10px] leading-tight">
          {zones} zone{zones > 1 ? "s" : ""}
        </span>
      )}
      {clips > 0 && (
        <span className="rounded border border-emerald-500/40 bg-emerald-500/10 px-1 py-px text-[10px] leading-tight text-emerald-300">
          {clips} clip{clips > 1 ? "s" : ""}
        </span>
      )}
    </span>
  );
}

/**
 * Side list of rushes, shared by derush and stabilize: the same visual anchor
 * from one step to the next, rather than two different inventories of the same
 * content.
 */
export function SequenceList({
  sequences,
  activeId,
  hrefFor,
  meta,
  empty,
  title,
}: {
  sequences: Sequence[];
  activeId?: number;
  hrefFor: (sequence: Sequence) => string;
  meta?: (sequence: Sequence) => ReactNode;
  empty: ReactNode;
  title: string;
}) {
  return (
    <aside className="lg:sticky lg:top-4 lg:self-start">
      <div className="mb-2 flex items-baseline justify-between px-1">
        <h2 className="text-sm font-medium">{title}</h2>
        <span className="tnum text-xs text-muted-foreground">{sequences.length}</span>
      </div>

      {sequences.length === 0 ? (
        <p className="rounded-md border border-dashed px-3 py-6 text-center text-xs text-muted-foreground">
          {empty}
        </p>
      ) : (
        <ul className="max-h-[calc(100vh-10rem)] space-y-1 overflow-y-auto pr-1">
          {sequences.map((sequence) => (
            <li key={sequence.id}>
              <Link
                to={hrefFor(sequence)}
                className={cn(
                  "relative flex items-center gap-2 overflow-hidden rounded-md border p-2 pl-3 transition-colors",
                  sequence.id === activeId
                    ? "border-primary bg-accent"
                    : "border-transparent hover:bg-accent/50",
                )}
              >
                {/* The colour tag as a full-height bar rather than a dot: at this
                    size it is the only thing findable while scrolling fast. */}
                {rushColor(sequence.color) && (
                  <span
                    className={cn(
                      "absolute inset-y-0 left-0 w-1",
                      rushColor(sequence.color)?.bar,
                    )}
                  />
                )}
                {sequence.has_proxy ? (
                  <img
                    src={mediaUrl.poster(sequence.id)}
                    alt=""
                    className="h-9 w-12 shrink-0 rounded object-cover"
                    loading="lazy"
                  />
                ) : (
                  <div className="h-9 w-12 shrink-0 rounded bg-muted" />
                )}
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-xs font-medium">{sequence.label}</span>
                  <span className="block truncate text-[11px] text-muted-foreground">
                    {formatDuration(sequence.duration_ms)} · {formatDateTime(sequence.recorded_at)}
                  </span>
                  {meta && (
                    <span className="mt-0.5 block text-[11px] text-muted-foreground">
                      {meta(sequence)}
                    </span>
                  )}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
