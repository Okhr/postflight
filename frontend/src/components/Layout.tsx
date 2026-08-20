import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { FolderInput } from "lucide-react";

import { JobsBar } from "@/components/JobsBar";
import { RushTree } from "@/components/RushTree";
import { WorkersDialog } from "@/components/WorkersDialog";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { LiveJobsProvider } from "@/lib/live";
import { selectedRushId } from "@/lib/routing";
import { cn } from "@/lib/utils";

// The five pages, in the order the work walks through them. Merge sits between
// import and derush because that is when a wrong group has to be caught: after the
// parts are in, before anything has been marked on them.
const PAGES = [
  { to: "/", label: "Import", end: true, carriesRush: false },
  { to: "/merge", label: "Merge", end: false, carriesRush: false },
  { to: "/derush", label: "Derush", end: false, carriesRush: true },
  { to: "/stabilisation", label: "Stabilize", end: false, carriesRush: true },
  { to: "/color", label: "Color", end: false, carriesRush: false },
];

export function Layout() {
  const { pathname } = useLocation();
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
        <div className="flex min-h-screen">
          <aside className="sticky top-0 flex h-screen w-64 shrink-0 flex-col gap-2 border-r px-2 py-3">
            <span className="px-2 text-sm font-semibold tracking-tight">video-stab</span>

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

            <RushTree />

            <nav className="shrink-0 space-y-0.5 border-t pt-2">
              {PAGES.map(({ to, label, end, carriesRush }) => (
                <NavLink
                  key={to}
                  // A rush stays selected when moving between the steps that act on
                  // one. Import and merge are about the collection, colour is about a
                  // stabilized clip: none of the three takes a rush id.
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
      </LiveJobsProvider>
    </TooltipProvider>
  );
}
