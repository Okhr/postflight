import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Cpu, FolderInput } from "lucide-react";

import { JobsBar } from "@/components/JobsBar";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { api } from "@/lib/api";
import { LiveJobsProvider } from "@/lib/live";
import { cn } from "@/lib/utils";

// The four processing steps, in the order they are walked through.
const STEPS = [
  { to: "/", step: 1, label: "Import", end: true },
  { to: "/derush", step: 2, label: "Derush", end: false },
  { to: "/stabilisation", step: 3, label: "Stabilize", end: false },
  { to: "/color", step: 4, label: "Color", end: false },
];

export function Layout() {
  const { data: status } = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
    // Files copied straight into the network folder show up here without any job
    // moving, so this one keeps a tick of its own.
    refetchInterval: 10_000,
  });

  const caps = status?.capabilities;
  // Two independent paths: VAAPI for proxy decoding, OpenCL for Gyroflow's
  // warping. One can be on the GPU while the other is not, so say which is which
  // rather than showing a single "CPU" that reads as "nothing is accelerated".
  const decode = caps?.hwaccel === "vaapi" ? "GPU" : "CPU";
  const opencl = (caps?.opencl_icds?.length ?? 0) > 0 && (caps?.dri_devices?.length ?? 0) > 0;
  const stabilize = opencl ? "GPU" : "CPU";

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
                    <span className="inline-flex items-center gap-1.5">
                      <Cpu className="h-3.5 w-3.5" />
                      decode {decode} · stabilize {stabilize}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-xs">
                    <p>
                      Proxy decoding:{" "}
                      {decode === "GPU"
                        ? "VAAPI"
                        : "CPU (about 1.6x slower — VAAPI can wedge AMD iGPU drivers)"}
                    </p>
                    <p>
                      Stabilization:{" "}
                      {opencl
                        ? "OpenCL (about 3x faster than CPU)"
                        : "CPU only, no OpenCL device available"}
                    </p>
                    {caps?.notes?.map((note) => (
                      <p key={note} className="mt-1 text-amber-400">
                        {note}
                      </p>
                    ))}
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
                      <p>Picked up on the next scan — the worker looks every 30 s.</p>
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
