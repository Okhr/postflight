import { useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowUp, FolderInput } from "lucide-react";

import { JobsBar } from "@/components/JobsBar";
import { Logo } from "@/components/Logo";
import { RushTree } from "@/components/RushTree";
import { WorkersDialog } from "@/components/WorkersDialog";
import { Progress } from "@/components/ui/progress";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { LiveJobsProvider } from "@/lib/live";
import { UploadProvider, useUpload } from "@/lib/upload";
import { usePersistentState } from "@/lib/persist";
import { selectedRushId } from "@/lib/routing";
import { cn } from "@/lib/utils";

// The four pages, in the order the work walks through them.
const PAGES = [
  { to: "/", label: "Import", end: true, carriesRush: false },
  { to: "/derush", label: "Derush", end: false, carriesRush: true },
  { to: "/stabilisation", label: "Stabilize", end: false, carriesRush: true },
  { to: "/color", label: "Color", end: false, carriesRush: false },
];

/**
 * What is being uploaded, wherever you happen to be.
 *
 * The transfer outlives the Import page, so something has to say so from the
 * outside; without it, leaving the page looks exactly like stopping.
 */
function UploadBar() {
  const { busy, items, moved, total } = useUpload();
  if (!busy || total === 0) return null;

  const done = items.filter((it) => it.status === "done").length;
  const moving = items.filter((it) => it.status !== "skipped").length;

  return (
    <NavLink to="/" className="space-y-1 px-2 py-1">
      <span className="flex items-center gap-2 text-sm text-muted-foreground">
        <ArrowUp className="h-3.5 w-3.5" />
        <span className="tnum">
          {done}/{moving}
        </span>
        <span className="tnum ml-auto text-xs">
          {formatBytes(moved)} / {formatBytes(total)}
        </span>
      </span>
      <Progress value={(moved / total) * 100} className="h-1" />
    </NavLink>
  );
}

const MIN_WIDTH = 200;
const MAX_WIDTH = 560;

function clamp(value: number, low: number, high: number) {
  return Math.max(low, Math.min(high, value));
}

export function Layout() {
  const { pathname } = useLocation();
  const [width, setWidth] = usePersistentState("sidebar.width", 320);
  const [dragging, setDragging] = useState(false);
  const rushId = selectedRushId(pathname);
  const { data: status } = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
    // Files copied straight into the network folder show up here without any job
    // moving, so this one keeps a tick of its own.
    refetchInterval: 10_000,
  });

  return (
    <TooltipProvider>
      <LiveJobsProvider>
        <UploadProvider>
        <div className="relative flex min-h-screen">
          {/* Wide enough for a rush name, and draggable from its edge: how much room a
              name needs depends on the names, which are the camera's business. */}
          <aside
            className="sticky top-0 flex h-screen shrink-0 flex-col gap-2 border-r px-2 py-3"
            style={{ width }}
          >
            <div
              role="separator"
              aria-orientation="vertical"
              title="Drag to resize"
              className="absolute inset-y-0 right-0 w-1 cursor-col-resize hover:bg-primary/40"
              onPointerDown={(event) => {
                event.currentTarget.setPointerCapture(event.pointerId);
                setDragging(true);
              }}
              onPointerMove={(event) => {
                if (dragging) setWidth(clamp(event.clientX, MIN_WIDTH, MAX_WIDTH));
              }}
              onPointerUp={() => setDragging(false)}
              onPointerCancel={() => setDragging(false)}
            />
            <div className="flex items-center gap-2 px-2">
              <Logo />
              <span className="text-sm font-semibold tracking-tight">PostFlight</span>
            </div>

            <WorkersDialog workers={status?.workers ?? []} />

            {status && status.inbox_pending > 0 && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="flex items-center gap-2 px-2 text-sm text-muted-foreground">
                    <FolderInput className="h-3.5 w-3.5" />
                    {status.inbox_pending} in inbox
                  </span>
                </TooltipTrigger>
                <TooltipContent>
                  <p>Picked up on the next scan, which runs every 30 s.</p>
                </TooltipContent>
              </Tooltip>
            )}

            <UploadBar />

            <RushTree />

            <nav className="shrink-0 space-y-0.5 border-t pt-2">
              {PAGES.map(({ to, label, end, carriesRush }) => (
                <NavLink
                  key={to}
                  // A rush stays selected when moving between the steps that act on
                  // one. Import is about the collection, colour is about a stabilized
                  // clip: neither takes a rush id.
                  to={carriesRush && rushId ? `${to}/${rushId}` : to}
                  end={end}
                >
                  {({ isActive }) => (
                    <span
                      className={cn(
                        "block rounded-md px-2 py-1.5 text-sm transition-colors",
                        isActive
                          ? "bg-secondary text-secondary-foreground"
                          : "text-muted-foreground hover:text-foreground",
                      )}
                    >
                      {label}
                    </span>
                  )}
                </NavLink>
              ))}
            </nav>
          </aside>

          <div className="min-w-0 flex-1">
            <JobsBar />
            <main className="px-6 py-6">
              <Outlet />
            </main>
          </div>
        </div>
        </UploadProvider>
      </LiveJobsProvider>
    </TooltipProvider>
  );
}
