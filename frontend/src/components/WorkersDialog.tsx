import { Cpu, ServerOff } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { type WorkerInfo } from "@/lib/api";
import { cn } from "@/lib/utils";

function decodeLabel(worker: WorkerInfo): string {
  const backend = worker.capabilities.decode_backend || "cpu";
  return backend === "cpu" ? "CPU" : backend.toUpperCase();
}

// The four rates the dispatcher ranks a worker on, and what each is measured in.
const RATES: [label: string, key: string, unit: string][] = [
  ["proxy", "proxy_fps", "img/s"],
  ["render", "render_fps", "img/s"],
  ["grade", "grade_fps", "img/s"],
  ["merge", "merge_mbps", "MB/s"],
];

/** One rate, preferring what real jobs measured over the startup benchmark.
 *
 *  The job count is worth showing: the benchmark runs on half a second of footage
 *  and overstates by a fixed-ish factor, so "28 img/s" and "22.7 img/s (4 jobs)"
 *  do not deserve to look alike.
 */
function rateLabel(worker: WorkerInfo, key: string, unit: string): string | null {
  const observed = worker.observed?.[key];
  const bench = (worker.rates as unknown as Record<string, number | null>)?.[key];
  const value = observed ?? bench;
  if (!value) return null;
  const samples = worker.observed?.[`${key}_n`] ?? 0;
  const shown = value < 10 ? value.toFixed(1) : Math.round(value).toString();
  return `${shown} ${unit}${samples ? ` (${samples} job${samples > 1 ? "s" : ""})` : ""}`;
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 py-0.5">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right">{value}</span>
    </div>
  );
}

/** One card per worker, which is where the hardware actually lives. */
function WorkerCard({ worker }: { worker: WorkerInfo }) {
  const caps = worker.capabilities;
  // Backends that were tried and refused, so a CPU fallback can be explained rather
  // than merely announced.
  const refused = Object.entries(caps.decode_probes ?? {}).filter(([, why]) => why);
  const speeds = RATES.map(([label, key, unit]) => {
    const shown = rateLabel(worker, key, unit);
    return shown ? `${label} ${shown}` : null;
  }).filter(Boolean) as string[];
  const link = worker.rates?.link_mbps;

  return (
    <div className="rounded-md border p-3 text-sm">
      <div className="mb-2 flex items-center gap-2">
        <span
          className={cn(
            "h-2 w-2 rounded-full",
            worker.online ? "bg-emerald-500" : "bg-muted-foreground/40",
          )}
        />
        <span className="text-sm font-medium">{worker.name}</span>
        <span className="ml-auto text-muted-foreground">
          {!worker.online ? "offline" : worker.running > 0 ? "busy" : "idle"}
        </span>
      </div>

      <Row
        label="decode"
        value={`${decodeLabel(worker)}${caps.decode_device ? ` on ${caps.decode_device}` : ""}`}
      />
      <Row label="stabilize" value={caps.stabilize_device || "CPU"} />
      <Row
        label="volume"
        value={
          worker.shares_data
            ? "shared"
            : `own${link ? `, ${Math.round(link)} MB/s link` : ""}`
        }
      />
      {speeds.length > 0 && <Row label="speed" value={speeds.join(" · ")} />}
      {caps.ffmpeg_version && (
        <Row label="ffmpeg" value={caps.ffmpeg_version.replace("ffmpeg version ", "")} />
      )}
      {caps.gyroflow_version && <Row label="gyroflow" value={caps.gyroflow_version} />}

      {refused.length > 0 && (
        <p className="mt-2 text-muted-foreground">
          refused:{" "}
          {refused.map(([name, why]) => `${name} (${why.split("\n")[0].slice(0, 70)})`).join("; ")}
        </p>
      )}
      {[...(caps.notes ?? []), ...(worker.rates?.notes ?? [])].map((note) => (
        <p key={note} className="mt-2 text-amber-400">
          {note}
        </p>
      ))}
    </div>
  );
}

/** The worker count in the sidebar, and everything behind it. */
export function WorkersDialog({ workers }: { workers: WorkerInfo[] }) {
  const online = workers.filter((w) => w.online);
  const none = online.length === 0;

  return (
    <Dialog>
      <DialogTrigger asChild>
        <button
          type="button"
          className={cn(
            "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm transition-colors hover:bg-accent",
            none ? "text-amber-400" : "text-muted-foreground",
          )}
        >
          {none ? <ServerOff className="h-3.5 w-3.5" /> : <Cpu className="h-3.5 w-3.5" />}
          {none
            ? "no worker"
            : `${online.length} worker${online.length > 1 ? "s" : ""}`}
        </button>
      </DialogTrigger>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Workers</DialogTitle>
        </DialogHeader>
        <div className="space-y-2">
          {workers.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              None has registered. Check VS_API_URL on the worker.
            </p>
          ) : (
            workers.map((worker) => <WorkerCard key={worker.id} worker={worker} />)
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
