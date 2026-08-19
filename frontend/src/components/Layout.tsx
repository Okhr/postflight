import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Cpu, FolderInput, ServerOff } from "lucide-react";

import { JobsBar } from "@/components/JobsBar";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { api, type WorkerInfo } from "@/lib/api";
import { LiveJobsProvider } from "@/lib/live";
import { cn } from "@/lib/utils";

// The four processing steps, in the order they are walked through.
const STEPS = [
  { to: "/", step: 1, label: "Import", end: true },
  { to: "/derush", step: 2, label: "Derush", end: false },
  { to: "/stabilisation", step: 3, label: "Stabilize", end: false },
  { to: "/color", step: 4, label: "Color", end: false },
];

function decodeLabel(worker: WorkerInfo): string {
  const backend = worker.capabilities.decode_backend || "cpu";
  return backend === "cpu" ? "CPU" : backend.toUpperCase();
}

/** One line per worker, which is where the hardware actually lives. */
function WorkerLine({ worker }: { worker: WorkerInfo }) {
  const caps = worker.capabilities;
  // Backends that were tried and refused, so a CPU fallback can be explained rather
  // than merely announced.
  const refused = Object.entries(caps.decode_probes ?? {}).filter(([, why]) => why);

  return (
    <div className="mt-1.5 first:mt-0">
      <p className="font-medium">
        {worker.name}
        {!worker.online && <span className="text-muted-foreground"> (offline)</span>}
        {worker.running > 0 && <span className="text-muted-foreground"> · busy</span>}
      </p>
      <p>decode: {decodeLabel(worker)}{caps.decode_device ? ` on ${caps.decode_device}` : ""}</p>
      <p>stabilize: {caps.stabilize_device || "CPU"}</p>
      {refused.length > 0 && (
        <p className="text-muted-foreground">
          refused:{" "}
          {refused.map(([name, why]) => `${name} (${why.split("\n")[0].slice(0, 70)})`).join("; ")}
        </p>
      )}
      {caps.notes?.map((note) => (
        <p key={note} className="text-amber-400">
          {note}
        </p>
      ))}
    </div>
  );
}

export function Layout() {
  const { data: status } = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
    // Files copied straight into the network folder show up here without any job
    // moving, so this one keeps a tick of its own.
    refetchInterval: 10_000,
  });

  // Hardware belongs to the workers, and each one measured its own by running the
  // thing. The API has none worth reporting: it never decodes, warps or merges.
  const workers = status?.workers ?? [];
  const online = workers.filter((w) => w.online);
  const lone = online.length === 1 ? online[0] : null;

  return (
    <TooltipProvider>
      <LiveJobsProvider>
        <div className="min-h-screen">
          <header className="border-b">
            <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3">
              <span className="text-sm font-semibold tracking-tight">video-stab</span>

              <nav className="flex items-center gap-1 rounded-lg border p-1">
                {STEPS.map(({ to, step, label, end }) => (
                  <NavLink key={to} to={to} end={end}>
                    {({ isActive }) => (
                      <span
                        className={cn(
                          "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm transition-colors",
                          isActive
                            ? "bg-secondary text-secondary-foreground"
                            : "text-muted-foreground hover:text-foreground",
                        )}
                      >
                        <span
                          className={cn(
                            "tnum flex h-5 w-5 items-center justify-center rounded-full text-[11px]",
                            isActive
                              ? "bg-primary text-primary-foreground"
                              : "bg-muted text-muted-foreground",
                          )}
                        >
                          {step}
                        </span>
                        {label}
                      </span>
                    )}
                  </NavLink>
                ))}
              </nav>

              <div className="ml-auto flex items-center gap-3 text-xs text-muted-foreground">
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span
                      className={cn(
                        "inline-flex items-center gap-1.5",
                        online.length === 0 && "text-amber-400",
                      )}
                    >
                      {online.length === 0 ? (
                        <>
                          <ServerOff className="h-3.5 w-3.5" />
                          no worker
                        </>
                      ) : (
                        <>
                          <Cpu className="h-3.5 w-3.5" />
                          {lone
                            ? `decode ${decodeLabel(lone)} · stabilize ${
                                lone.capabilities.stabilize_on_gpu ? "GPU" : "CPU"
                              }`
                            : `${online.length} workers`}
                        </>
                      )}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-sm">
                    {workers.length === 0 ? (
                      <p>
                        Nothing has registered yet. A worker announces itself to this API
                        on startup, so check that one is running and that VS_API_URL
                        points here.
                      </p>
                    ) : (
                      workers.map((worker) => <WorkerLine key={worker.id} worker={worker} />)
                    )}
                  </TooltipContent>
                </Tooltip>

                {status && status.inbox_pending > 0 && (
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="inline-flex items-center gap-1.5">
                        <FolderInput className="h-3.5 w-3.5" />
                        {status.inbox_pending} in inbox
                      </span>
                    </TooltipTrigger>
                    <TooltipContent>
                      <p>Picked up on the next scan, which runs every 30 s.</p>
                    </TooltipContent>
                  </Tooltip>
                )}
              </div>
            </div>
          </header>

          <JobsBar />

          <main className="mx-auto max-w-7xl px-4 py-6">
            <Outlet />
          </main>
        </div>
      </LiveJobsProvider>
    </TooltipProvider>
  );
}
