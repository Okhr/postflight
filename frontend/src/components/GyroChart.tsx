import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { usePersistentSet, usePersistentState } from "@/lib/persist";
import { cn } from "@/lib/utils";

/**
 * Telemetry under the derush timeline, the way Gyroflow shows it.
 *
 * Two views, because Gyroflow has two that matter and they answer different
 * questions:
 *
 * - **Quaternions**: the raw orientation components x/y/z/w. This is Gyroflow's
 *   view mode 3, and on a DJI file it is the *only* thing it can draw: its
 *   gyroscope view reads `raw_imu`, which DJI leaves empty.
 * - **Gyroscope**: angular velocity in deg/s, derived here by differentiating
 *   those quaternions. Not something Gyroflow plots for these files, but the spikes
 *   are what mark a shaky pass, which is what one derushes on.
 *
 * The two are drawn differently on purpose. Angular velocity is an **envelope**
 * (min and max per bucket): at ~2000 Hz over four minutes there are 477 000 samples
 * for about 900 pixels, and decimating would drop the very spikes one is looking
 * for. Orientation is smooth at that rate, so it is a plain line, and two bounds would
 * sit on top of each other and double the payload.
 *
 * Drawn in data space (`viewBox` of `points` by 100): the vertical scale is an SVG
 * transform on the group rather than baked into the coordinates, so changing it does
 * not rebuild paths of 6000 points.
 */

// Gyroflow's own colours for the raw axes, read off its TimelineGyroChart
// (`colors[0..3]`, identical in its light and dark themes). Muted on purpose
// there, and it happens to sit better next to shadcn than saturated RGB did.
const AXIS_COLORS: Record<string, string> = {
  x: "#8f4c4c",
  y: "#4c8f4d",
  z: "#4c7c8f",
  w: "#8f4c8f",
};

/** Views in the order they are offered, whichever of them the payload carries. */
const VIEW_ORDER = ["quaternion", "rate"];

type Bounds = { min: number[]; max: number[] };

interface ChartView {
  label: string;
  unit: string;
  axes: string[];
  /** Real names for the axes when the camera's frame was identified: Pitch,
   *  Roll, Yaw. Empty when it was not, and then the axes stay X/Y/Z. */
  axis_labels?: Record<string, string>;
  kind: "envelope" | "line";
  peak: number;
  /** An envelope keeps both bounds per bucket; a line keeps one value. */
  series: Record<string, Bounds | number[]>;
}

interface GyroData {
  format: number;
  source: string;
  origin: string;
  sample_count: number;
  sample_rate_hz: number;
  duration_ms: number;
  imu_duration_ms: number;
  points: number;
  dropped: number;
  default_view: string;
  views: Record<string, ChartView>;
}

/** On-screen height of the plot, in pixels. Exported because the derush timeline
 * draws its trim bar and its cut bands over the plot, and has to stop short of
 * the axis legend below. Taller than it used to be: the curves are the timeline
 * now, there is no filmstrip sharing the room. */
export const PLOT_HEIGHT = 128;

const HEIGHT = 100;
const MID = HEIGHT / 2;
const MARGIN = 2;

/**
 * Path in data units: y is the value itself, straight up.
 *
 * The vertical scale is applied as an SVG transform on the group, not baked into
 * the coordinates. That is what lets the scale follow the visible window without
 * rebuilding paths of 6000 points on every wheel tick.
 */
function linePath(values: number[]): string {
  let out = "";
  for (let i = 0; i < values.length; i += 1) {
    out += `${i === 0 ? "M" : "L"}${i} ${values[i]}`;
  }
  return out;
}

// Rounded steps so the scale does not wobble on every pan, and so the printed
// bound stays a number one can read. Two ladders: deg/s runs to thousands,
// quaternion components never leave [-1, 1].
const RATE_LADDER = [5, 10, 20, 50, 100, 200, 500, 1000, 2000];
const UNIT_LADDER = [0.05, 0.1, 0.25, 0.5, 1];

function ladderFor(view: ChartView): number[] {
  return view.unit === "deg/s" ? RATE_LADDER : UNIT_LADDER;
}

function boundsOf(view: ChartView, axis: string): Bounds {
  const raw = view.series[axis];
  return Array.isArray(raw) ? { min: raw, max: raw } : raw;
}

function format(value: number, view: ChartView): string {
  return view.unit === "deg/s" ? String(Math.round(value)) : value.toFixed(3);
}

export function GyroChart({
  sequenceId,
  lastFrame,
  frame,
  onSeek,
  showPlayhead = true,
}: {
  sequenceId: number;
  lastFrame: number;
  frame: number;
  /** Left out when the caller draws its own: the derush timeline owns one
   *  playhead across the whole track rather than one per row. */
  onSeek?: (frame: number) => void;
  showPlayhead?: boolean;
}) {
  // Kept across reloads: which curve one reads and which axes are muted is a
  // habit, not a per-visit decision.
  const [hidden, toggleHidden] = usePersistentSet("gyro.hidden");
  const [picked, setPicked] = usePersistentState<string | null>("gyro.view", null);
  const [hover, setHover] = useState<number | null>(null);

  const { data, isLoading, error } = useQuery<GyroData>({
    queryKey: ["gyro", sequenceId],
    queryFn: async () => {
      const response = await fetch(`/api/media/gyro/${sequenceId}`);
      if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try {
          detail = (await response.json()).detail ?? detail;
        } catch {
          /* non-JSON response */
        }
        throw new Error(detail);
      }
      return response.json();
    },
    // Built on first request when the sequence predates the feature: a few
    // seconds, and pointless to retry on failure (a rush with no telemetry will
    // never have any).
    retry: false,
    staleTime: Infinity,
  });

  const offered = data ? VIEW_ORDER.filter((key) => data.views[key]) : [];
  const activeKey = (picked && data?.views[picked] ? picked : data?.default_view) ?? "";
  const active = data?.views[activeKey];

  const paths = useMemo(() => {
    if (!active) return [];
    return active.axes.map((axis) => {
      const raw = active.series[axis];
      return {
        key: axis,
        color: AXIS_COLORS[axis] ?? "currentColor",
        // One stroke for a line, two for an envelope: quiet stretches show a
        // single trace, shaky ones open into a band.
        d: Array.isArray(raw) ? [linePath(raw)] : [linePath(raw.max), linePath(raw.min)],
      };
    });
  }, [active]);

  // Hidden axes are left out of the scale, so hiding the loud one reveals the quiet
  // ones instead of leaving them flat on the zero line.
  const bound = useMemo(() => {
    if (!active) return 1;
    const ladder = ladderFor(active);
    let peak = 0;
    for (const axis of active.axes) {
      if (hidden.has(axis)) continue;
      const { min, max } = boundsOf(active, axis);
      for (let i = 0; i < max.length; i += 1) {
        const local = Math.max(Math.abs(min[i]), Math.abs(max[i]));
        if (local > peak) peak = local;
      }
    }
    return ladder.find((step) => step >= peak) ?? ladder[ladder.length - 1];
  }, [active, hidden]);

  if (isLoading) {
    return (
      <p
        className="flex items-center justify-center text-sm text-muted-foreground"
        style={{ height: PLOT_HEIGHT }}
      >
        Reading telemetry…
      </p>
    );
  }
  if (error || !data || !active) {
    return (
      <p
        className="flex items-center justify-center text-sm text-muted-foreground"
        style={{ height: PLOT_HEIGHT }}
      >
        No gyro chart: {error instanceof Error ? error.message : "unavailable"}
      </p>
    );
  }

  const points = data.points;
  const position = lastFrame ? frame / lastFrame : 0;
  const readout = hover ?? Math.round(position * (points - 1));

  return (
    <div className="space-y-1">
      <div
        className="relative w-full cursor-pointer overflow-hidden"
        style={{ height: PLOT_HEIGHT }}
        onClick={
          onSeek &&
          ((event) => {
            const rect = event.currentTarget.getBoundingClientRect();
            const x = (event.clientX - rect.left) / rect.width;
            onSeek(x * lastFrame);
          })
        }
        onMouseMove={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          const x = (event.clientX - rect.left) / rect.width;
          const bucket = x * (points - 1);
          setHover(Math.max(0, Math.min(points - 1, Math.round(bucket))));
        }}
        onMouseLeave={() => setHover(null)}
      >
        <svg
          viewBox={`0 0 ${Math.max(points - 1, 1)} ${HEIGHT}`}
          preserveAspectRatio="none"
          className="h-full w-full"
        >
          <line
            x1={0}
            y1={MID}
            x2={points}
            y2={MID}
            stroke="currentColor"
            strokeOpacity={0.2}
            strokeWidth={1}
            vectorEffect="non-scaling-stroke"
            className="text-foreground"
          />
          <g transform={`translate(0 ${MID}) scale(1 ${-(MID - MARGIN) / bound})`}>
            {paths.map((axis) =>
              hidden.has(axis.key) ? null : (
                <g key={axis.key} fill="none" stroke={axis.color} strokeWidth={1}>
                  {/* non-scaling-stroke: the viewBox is stretched horizontally and
                      the group is scaled vertically; without it the lines would
                      thicken and squash with the zoom. */}
                  {axis.d.map((d, index) => (
                    <path key={index} d={d} vectorEffect="non-scaling-stroke" />
                  ))}
                </g>
              ),
            )}
          </g>
        </svg>

        {showPlayhead && (
          <div
            className="pointer-events-none absolute inset-y-0 w-0.5 bg-red-500"
            style={{ left: `${position * 100}%` }}
          />
        )}
        {hover !== null && (
          <div
            className="pointer-events-none absolute inset-y-0 w-px bg-foreground/40"
            style={{ left: `${(hover / (points - 1)) * 100}%` }}
          />
        )}

        <span className="pointer-events-none absolute left-1 top-0.5 text-xs text-muted-foreground">
          ±{bound} {active.unit}
        </span>
      </div>

      {/* Chrome, not timeline. The derush track wraps this whole component and turns
          a press into a scrub, so without stopping here, clicking a view switch or
          an axis toggle would also drag the playhead to the button's x position. */}
      <div
        className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
        onPointerDown={(event) => event.stopPropagation()}
      >
        {offered.length > 1 && (
          <span className="mr-1 flex items-center rounded-md border p-0.5">
            {offered.map((key) => (
              <button
                key={key}
                type="button"
                onClick={() => setPicked(key)}
                className={cn(
                  "rounded px-1.5 py-0.5 text-xs transition-colors",
                  key === activeKey
                    ? "bg-secondary text-secondary-foreground"
                    : "hover:text-foreground",
                )}
              >
                {data.views[key].label}
              </button>
            ))}
          </span>
        )}

        {active.axes.map((axis) => {
          const off = hidden.has(axis);
          const { min, max } = boundsOf(active, axis);
          return (
            <Button
              key={axis}
              size="sm"
              variant="ghost"
              className={cn("h-6 gap-1.5 px-1.5", off && "opacity-40")}
              onClick={() => toggleHidden(axis)}
            >
              <span
                className="inline-block h-2 w-2 rounded-sm"
                style={{ background: AXIS_COLORS[axis] ?? "currentColor" }}
              />
              <span className="tnum">
                {active.axis_labels?.[axis] ?? axis.toUpperCase()}{" "}
                {format(max[readout] ?? 0, active)}
                {active.kind === "envelope" && `/${format(min[readout] ?? 0, active)}`}
              </span>
            </Button>
          );
        })}
      </div>
    </div>
  );
}
