/**
 * Everything behind the worker count in the sidebar.
 *
 * One card per machine, because that is where the hardware lives and no two are alike:
 * what it decodes with, what it warps with, whether it reads the dispatcher's volume,
 * and how fast it actually is at each of the four jobs.
 *
 * Speeds get a line each (florian, 2026-08-25). They were one run-on line, which hid
 * the thing worth reading: a rate measured on real jobs and a rate measured by the
 * startup benchmark are not the same claim, and the benchmark overstates by a
 * fixed-ish factor (measured on this project: 28.0 img/s against 24.9 on real work).
 */
import { Cpu, ServerOff } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Separator } from "@/components/ui/separator";
import { type Job, type WorkerInfo } from "@/lib/api";
import { jobKindLabel } from "@/lib/format";
import { useLiveJobs } from "@/lib/live";
import { cn } from "@/lib/utils";

function decodeLabel(worker: WorkerInfo): string {
  const backend = worker.capabilities.decode_backend || "cpu";
  return backend === "cpu" ? "CPU" : backend.toUpperCase();
}

/** The four rates the dispatcher ranks a worker on, named as the jobs are named. */
const RATES: [label: string, key: string, unit: string][] = [
  ["merge", "merge_mbps", "MB/s"],
  ["proxy", "proxy_fps", "img/s"],
  ["stabilize", "render_fps", "img/s"],
  ["color", "grade_fps", "img/s"],
];

function round(value: number): string {
  return value < 10 ? value.toFixed(1) : Math.round(value).toString();
}

/** Label on the left, value on the right, and the same widths in every card. */
function Row({
  label,
  value,
  note,
}: {
  label: string;
  value: string;
  /** What qualifies the value, in muted type: where it comes from, mostly. */
  note?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-0.5">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="min-w-0 text-right">
        <span className="tnum">{value}</span>
        {note && <span className="ml-2 text-xs text-muted-foreground">{note}</span>}
      </span>
    </div>
  );
}

/**
 * One speed, and where the number comes from.
 *
 * What real jobs measured wins over the benchmark, and both are shown when both exist:
 * the benchmark runs on half a second of footage and never leaves the page cache, so it
 * ranks machines and does not predict durations.
 */
function SpeedRow({ worker, label, field, unit }: {
  worker: WorkerInfo;
  label: string;
  field: string;
  unit: string;
}) {
  const observed = worker.observed?.[field];
  const bench = (worker.rates as unknown as Record<string, number | null>)?.[field];
  const samples = worker.observed?.[`${field}_n`] ?? 0;

  if (!observed && !bench) {
    return <Row label={label} value="not measured" />;
  }
  if (!observed) {
    return <Row label={label} value={`${round(bench as number)} ${unit}`} note="at start" />;
  }
  return (
    <Row
      label={label}
      value={`${round(observed)} ${unit}`}
      note={
        bench
          ? `${samples} job${samples > 1 ? "s" : ""} · ${round(bench)} at start`
          : `${samples} job${samples > 1 ? "s" : ""}`
      }
    />
  );
}

/** What this machine is on right now, which is the other half of "who does what". */
function Doing({ jobs }: { jobs: Job[] }) {
  return (
    <>
      {jobs.map((job) => (
        <div key={job.id} className="flex items-baseline gap-2 py-0.5">
          <Badge variant="secondary" className="font-normal">
            {jobKindLabel(job.kind)}
          </Badge>
          <span className="min-w-0 truncate text-muted-foreground">
            {[job.sequence_label || job.sequence_key, job.cut_label].filter(Boolean).join(" · ")}
          </span>
          <span className="tnum ml-auto shrink-0">{Math.round(job.progress * 100)} %</span>
        </div>
      ))}
    </>
  );
}

function WorkerCard({ worker, jobs }: { worker: WorkerInfo; jobs: Job[] }) {
  const caps = worker.capabilities;
  // Backends that were tried and refused, so a CPU fallback can be explained rather
  // than merely announced.
  const refused = Object.entries(caps.decode_probes ?? {}).filter(([, why]) => why);
  const link = worker.rates?.link_mbps;
  const mine = jobs.filter((job) => job.worker_name === worker.name);

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
        value={worker.shares_data ? "shared" : "own copy"}
        note={
          worker.shares_data
            ? "nothing travels"
            : link
              ? `${round(link)} MB/s link`
              : "link not measured"
        }
      />
      {caps.ffmpeg_version && (
        <Row label="ffmpeg" value={caps.ffmpeg_version.replace("ffmpeg version ", "")} />
      )}
      {caps.gyroflow_version && <Row label="gyroflow" value={caps.gyroflow_version} />}

      <Separator className="my-2" />
      <p className="pb-1 text-xs uppercase tracking-wide text-muted-foreground">Speed</p>
      {RATES.map(([label, field, unit]) => (
        <SpeedRow key={field} worker={worker} label={label} field={field} unit={unit} />
      ))}

      {mine.length > 0 && (
        <>
          <Separator className="my-2" />
          <p className="pb-1 text-xs uppercase tracking-wide text-muted-foreground">Now</p>
          <Doing jobs={mine} />
        </>
      )}

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
  const jobs = useLiveJobs().filter((job) => job.state === "running");
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
          {none ? "no worker" : `${online.length} worker${online.length > 1 ? "s" : ""}`}
        </button>
      </DialogTrigger>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Workers</DialogTitle>
        </DialogHeader>
        <div className="max-h-[70vh] space-y-2 overflow-y-auto">
          {workers.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              None has registered. Check VS_API_URL on the worker.
            </p>
          ) : (
            workers.map((worker) => (
              <WorkerCard key={worker.id} worker={worker} jobs={jobs} />
            ))
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
