/**
 * Derush: marking the stretches of a rush worth keeping.
 *
 * A word of warning on names. What the interface calls a **sequence** is a `Cut`
 * in the code, the API and the database, because `Sequence` there is the merged
 * rush this page is editing. The two never meet in the same sentence on screen,
 * and renaming the table would be a rename onto a name already taken, so the code
 * keeps saying `cut` and only what is displayed says "sequence".
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronLeft,
  ChevronRight,
  Pause,
  Pencil,
  Play,
  SkipBack,
  SkipForward,
  Trash2,
  Zap,
} from "lucide-react";
import { toast } from "sonner";

import { GyroChart, PLOT_HEIGHT } from "@/components/GyroChart";
import { StateBadge } from "@/components/StateBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { api, mediaUrl, type Cut, type SequenceDetail } from "@/lib/api";
import { usePersistentState } from "@/lib/persist";
import { formatDuration, formatTimecode, frameToSeconds, secondsToFrame } from "@/lib/format";
import { cn } from "@/lib/utils";

const SPEEDS = [0.25, 0.5, 1, 2, 4];

// The trim bar sits above the curves and carries the handles, as in Gyroflow.
// 24px so the handles are a comfortable target and the grip clears the rounded
// corner of the track at frame 0.
const BAR_HEIGHT = 24;
// Ruler steps, in seconds. The one picked is the smallest that keeps the labels
// down to a dozen across the width, so there is no need to measure the box.
const TICK_STEPS_S = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
// Height the overlays cover: bar plus plot, stopping above the axis legend the
// chart draws under itself.
const TRACK_HEIGHT = BAR_HEIGHT + PLOT_HEIGHT;

/** What the pointer is doing on the track. */
type Drag =
  | { kind: "scrub" }
  | { kind: "start"; key: string }
  | { kind: "end"; key: string }
  /** `grab` keeps the cut from jumping under the cursor: it is the offset
   *  between where the pointer went down and the start of the cut. */
  | { kind: "move"; key: string; grab: number };

function clamp(value: number, low: number, high: number): number {
  return Math.max(low, Math.min(high, value));
}
interface LocalCut {
  key: string;
  id?: number;
  label: string;
  start_frame: number;
  end_frame: number;
}

function toLocal(cuts: Cut[]): LocalCut[] {
  return cuts.map((c) => ({
    key: `srv-${c.id}`,
    id: c.id,
    label: c.label,
    start_frame: c.start_frame,
    end_frame: c.end_frame,
  }));
}

export function Derush() {
  const { id } = useParams();
  const sequenceId = Number(id);
  const { data: sequences } = useQuery({
    queryKey: ["sequences"],
    queryFn: () => api.sequences(),
    refetchInterval: 10_000,
  });

  // Derushing happens on the proxy: a rush without one has nothing to show.
  //
  // Sorted by the date the rush was *filmed*, oldest first, which is the order one
  // walks a session in. The API returns newest first, which is right for the import list,
  // but there it reads as upload order. A rush whose date could not be read goes
  // last rather than to 1970.
  const usable = (sequences ?? [])
    .filter((sequence) => sequence.has_proxy)
    .sort((a, b) => {
      const left = a.recorded_at ? Date.parse(a.recorded_at) : Infinity;
      const right = b.recorded_at ? Date.parse(b.recorded_at) : Infinity;
      return left - right;
    });

  return (
    <div>
      {Number.isFinite(sequenceId) ? (
        <Editor key={sequenceId} sequenceId={sequenceId} />
      ) : (
        <p className="text-sm text-muted-foreground">
          {usable.length === 0 ? (
            <>
              No proxy yet. Import rushes from <Link to="/" className="underline">Import</Link>.
            </>
          ) : (
            "Pick a rush."
          )}
        </p>
      )}
    </div>
  );
}

function Editor({ sequenceId }: { sequenceId: number }) {
  const queryClient = useQueryClient();
  const videoRef = useRef<HTMLVideoElement>(null);

  const { data: sequence, isLoading } = useQuery({
    queryKey: ["sequence", sequenceId],
    queryFn: () => api.sequence(sequenceId),
  });

  const [frame, setFrame] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = usePersistentState("derush.speed", 1);
  const [markIn, setMarkIn] = useState<number | null>(null);
  const [cuts, setCuts] = useState<LocalCut[]>([]);
  const [drag, setDrag] = useState<Drag | null>(null);
  const [renaming, setRenaming] = useState<LocalCut | null>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  /** What a drag has changed and not written yet. A drag fires a move per pointer
   *  event; only the release is worth a request. */
  const pending = useRef<LocalCut[] | null>(null);

  const fpsNum = sequence?.fps_num ?? 0;
  const fpsDen = sequence?.fps_den ?? 1;
  const lastFrame = Math.max((sequence?.frame_count ?? 1) - 1, 0);

  useEffect(() => {
    if (sequence) setCuts(toLocal(sequence.cuts));
  }, [sequence?.id, sequence?.cuts.length]);

  /** Land on an exact frame: pin the time to the middle of the target frame, or
   * the decoder's rounding may fall back onto the previous one. */
  const seek = useCallback(
    (target: number) => {
      const clamped = Math.max(0, Math.min(Math.round(target), lastFrame));
      const video = videoRef.current;
      if (video && fpsNum) {
        video.currentTime = frameToSeconds(clamped + 0.5, fpsNum, fpsDen);
      }
      setFrame(clamped);
    },
    [fpsNum, fpsDen, lastFrame],
  );

  // Tracking the current frame. requestVideoFrameCallback gives the frame
  // actually displayed; timeupdate only fires about 4 times a second.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !fpsNum) return;
    let handle = 0;
    let cancelled = false;

    type WithRvfc = HTMLVideoElement & {
      requestVideoFrameCallback?: (cb: (now: number, meta: { mediaTime: number }) => void) => number;
      cancelVideoFrameCallback?: (handle: number) => void;
    };
    const target = video as WithRvfc;

    if (target.requestVideoFrameCallback) {
      const step = (_now: number, meta: { mediaTime: number }) => {
        if (cancelled) return;
        setFrame(secondsToFrame(meta.mediaTime, fpsNum, fpsDen));
        handle = target.requestVideoFrameCallback!(step);
      };
      handle = target.requestVideoFrameCallback(step);
      return () => {
        cancelled = true;
        target.cancelVideoFrameCallback?.(handle);
      };
    }

    const onTime = () => setFrame(secondsToFrame(video.currentTime, fpsNum, fpsDen));
    video.addEventListener("timeupdate", onTime);
    return () => video.removeEventListener("timeupdate", onTime);
  }, [fpsNum, fpsDen, sequence?.id]);

  useEffect(() => {
    const video = videoRef.current;
    if (video) video.playbackRate = speed;
  }, [speed]);

  const togglePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) {
      void video.play();
      setPlaying(true);
    } else {
      video.pause();
      setPlaying(false);
    }
  }, []);

  /**
   * Write the list. There is no save button: every gesture that changes a sequence
   * ends in here, so what is on screen is what is stored.
   *
   * The ids go back out with it. That is what makes an edit an edit rather than a
   * delete and an insert, and it is what keeps a stabilized clip attached to the
   * sequence it came from.
   */
  const save = useMutation({
    mutationFn: (next: LocalCut[]) =>
      api.saveCuts(
        sequenceId,
        next.map((c) => ({
          id: c.id,
          label: c.label,
          start_frame: c.start_frame,
          end_frame: c.end_frame,
        })),
      ),
    onSuccess: (saved) => {
      setCuts(toLocal(saved));
      queryClient.invalidateQueries({ queryKey: ["sequence", sequenceId] });
      queryClient.invalidateQueries({ queryKey: ["sequences"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  /** Show it and write it, in that order. */
  const commit = useCallback(
    (next: LocalCut[]) => {
      setCuts(next);
      save.mutate(next);
    },
    [save],
  );

  const addCut = useCallback(
    (start: number, end: number) => {
      if (end <= start) {
        toast.error("The end must come after the start");
        return;
      }
      commit(
        [
          ...cuts,
          {
            key: `new-${Date.now()}`,
            label: `sequence ${cuts.length + 1}`,
            start_frame: start,
            end_frame: end,
          },
        ].sort((a, b) => a.start_frame - b.start_frame),
      );
      setMarkIn(null);
    },
    [commit, cuts],
  );

  // Keyboard shortcuts: space, arrows, I/O, Ctrl+S.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA") return;

      const secondInFrames = Math.round(fpsNum / fpsDen) || 30;
      switch (event.key) {
        case " ":
          event.preventDefault();
          togglePlay();
          break;
        case "ArrowLeft":
          event.preventDefault();
          seek(frame - (event.shiftKey ? secondInFrames : 1));
          break;
        case "ArrowRight":
          event.preventDefault();
          seek(frame + (event.shiftKey ? secondInFrames : 1));
          break;
        case "i":
        case "I":
          event.preventDefault();
          if (markIn === null) setMarkIn(frame);
          break;
        case "o":
        case "O":
          event.preventDefault();
          if (markIn === null) {
            toast.info("Set a start first, with I");
          } else {
            addCut(Math.min(markIn, frame), Math.max(markIn, frame));
          }
          break;
        case "Escape":
          setMarkIn(null);
          break;
        default:
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [frame, markIn, fpsNum, fpsDen, seek, togglePlay, addCut]);

  const keptFrames = useMemo(
    () => cuts.reduce((total, cut) => total + (cut.end_frame - cut.start_frame + 1), 0),
    [cuts],
  );
  // Falls back to the master's shape, then to 4:3, so the box is never zero-sized.
  const playerRatio =
    (sequence?.proxy_width || sequence?.width || 4) / (sequence?.proxy_height || sequence?.height || 3);

  if (isLoading) return <p className="text-sm text-muted-foreground">Loading…</p>;
  if (!sequence) return <p className="text-sm text-muted-foreground">Unknown sequence.</p>;

  if (!sequence.has_proxy) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{sequence.label}</CardTitle>
          <CardDescription>The proxy is not ready yet.</CardDescription>
        </CardHeader>
        <CardContent className="flex items-center gap-3">
          <StateBadge state={sequence.state} />
          {sequence.error && <p className="text-sm text-red-400">{sequence.error}</p>}
        </CardContent>
      </Card>
    );
  }

  // The track spans the whole rush: a frame's position is just its fraction of it.
  const percent = (value: number) => (lastFrame ? (value / lastFrame) * 100 : 0);
  const widthPercent = (frames: number) => (lastFrame ? (frames / lastFrame) * 100 : 0);

  /**
   * The stretches no cut keeps, as frame pairs. This is what gets dimmed.
   *
   * Gyroflow greys out what its single trim range excludes; with several cuts the
   * equivalent is the complement of their union. Empty while no cut exists: the
   * whole rush is still a candidate, dimming it all would read as "nothing usable".
   */
  const excluded: Array<[number, number]> = [];
  if (cuts.length > 0) {
    const ordered = [...cuts].sort((a, b) => a.start_frame - b.start_frame);
    let edge = 0;
    for (const cut of ordered) {
      if (cut.start_frame > edge) excluded.push([edge, cut.start_frame]);
      edge = Math.max(edge, cut.end_frame);
    }
    if (edge < lastFrame) excluded.push([edge, lastFrame]);
  }

  // Ruler marks along the trim bar. The last one is dropped when it would collide
  // with the total duration printed at the right edge.
  const durationS = frameToSeconds(lastFrame, fpsNum, fpsDen);
  const tickStep =
    TICK_STEPS_S.find((step) => durationS / step <= 12) ?? TICK_STEPS_S[TICK_STEPS_S.length - 1];
  const ticks: number[] = [];
  for (let at = tickStep; at < durationS - tickStep * 0.5; at += tickStep) ticks.push(at);

  /** Frame under a pointer. */
  const frameAt = (clientX: number) => {
    const node = trackRef.current;
    if (!node) return 0;
    const rect = node.getBoundingClientRect();
    return Math.round(clamp((clientX - rect.left) / rect.width, 0, 1) * lastFrame);
  };

  /**
   * Apply the current drag at `at`.
   *
   * Cuts stay in order and never overlap: an edge stops at its neighbour
   * rather than crossing it, which would produce two ranges Gyroflow renders
   * twice over the same frames. The video follows the edge being dragged,
   * that is the whole point of having handles rather than typing timecodes.
   */
  /**
   * Start a drag, whatever was grabbed.
   *
   * The track captures the pointer even when the press landed on a handle, so
   * moves and the release keep arriving here after the cursor has left the box,
   * otherwise a drag would freeze at the edge and, worse, never end.
   */
  const beginDrag = (event: ReactPointerEvent, next: Drag) => {
    if (event.button !== 0) return;
    event.stopPropagation();
    trackRef.current?.setPointerCapture(event.pointerId);
    setDrag(next);
  };

  const dragTo = (at: number) => {
    if (!drag) return;
    if (drag.kind === "scrub") {
      seek(at);
      return;
    }
    // Computed from the current list rather than inside an updater: every position
    // is absolute, derived from `at`, so two moves batched into one render give the
    // same answer as two renders, and the result can be kept for the release.
    const ordered = [...cuts].sort((a, b) => a.start_frame - b.start_frame);
    const index = ordered.findIndex((cut) => cut.key === drag.key);
    if (index >= 0) {
      const cut = ordered[index];
      const floor = index > 0 ? ordered[index - 1].end_frame + 1 : 0;
      const ceiling = index < ordered.length - 1 ? ordered[index + 1].start_frame - 1 : lastFrame;

      if (drag.kind === "start") {
        ordered[index] = { ...cut, start_frame: clamp(at, floor, cut.end_frame - 1) };
      } else if (drag.kind === "end") {
        ordered[index] = { ...cut, end_frame: clamp(at, cut.start_frame + 1, ceiling) };
      } else {
        const length = cut.end_frame - cut.start_frame;
        const start = clamp(at - drag.grab, floor, ceiling - length);
        ordered[index] = { ...cut, start_frame: start, end_frame: start + length };
      }
      pending.current = ordered;
      setCuts(ordered);
    }
    if (at !== frame) seek(at);
  };

  /** End of a drag: write what it changed, once. */
  const endDrag = () => {
    setDrag(null);
    if (pending.current) {
      save.mutate(pending.current);
      pending.current = null;
    }
  };

  return (
    <div className="min-w-0 space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <RushIdentity sequence={sequence} />
        <StateBadge state={sequence.state} />
        <span className="text-sm text-muted-foreground">
          {sequence.width}×{sequence.height} · {sequence.fps.toFixed(3)} fps ·{" "}
          {formatDuration(sequence.duration_ms)} · {sequence.frame_count} frames ·{" "}
          <span title={sequence.clips.map((clip) => clip.filename).join("\n")}>
            {sequence.part_count} source{sequence.part_count > 1 ? "s" : ""}
          </span>
        </span>
        {cuts.length > 0 && (
          <Button asChild size="sm" variant="outline" className="ml-auto">
            <Link to={`/stabilisation/${sequence.id}`}>
              <Zap className="h-4 w-4" />
              Stabilize
            </Link>
          </Button>
        )}
      </div>

      {/* Player and sequences side by side while there is room for both, stacked
          under each other when there is not. */}
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_18rem]">
      <Card className="overflow-hidden">
        {/* The box is sized from the proxy dimensions the API already reports, so
            the player is at its final size on first paint. Left to `w-auto`, the
            video has no intrinsic size until its metadata arrives: it renders in
            the browser's default 300x150 box, then jumps. */}
        <div
          className="relative mx-auto overflow-hidden bg-black"
          style={{
            aspectRatio: `${playerRatio}`,
            width: `min(100%, calc(60vh * ${playerRatio}))`,
          }}
        >
          <video
            ref={videoRef}
            src={mediaUrl.proxy(sequence.id)}
            poster={mediaUrl.poster(sequence.id)}
            className="h-full w-full object-contain"
            preload="auto"
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onClick={togglePlay}
          />
        </div>

        <CardContent className="space-y-3 py-4">
          <div className="space-y-2">
            {/* Gyroflow's timeline, in shadcn clothes: the gyro curves *are* the
                timeline, with a trim bar across the top carrying the handles. No
                frame thumbnails: the curves are what tells a calm pass from a
                shaky one, and the player above shows the picture. */}
            <div
              ref={trackRef}
              className="relative cursor-pointer select-none touch-none overflow-hidden rounded-md border bg-card"
              onPointerDown={(event) => {
                beginDrag(event, { kind: "scrub" });
                if (event.button === 0) seek(frameAt(event.clientX));
              }}
              onPointerMove={(event) => drag && dragTo(frameAt(event.clientX))}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
            >
              <div
                className="relative border-b bg-muted/40"
                style={{ height: BAR_HEIGHT }}
              >
                {ticks.map((seconds) => (
                  <div
                    key={seconds}
                    className="pointer-events-none absolute inset-y-0"
                    style={{ left: `${percent(secondsToFrame(seconds, fpsNum, fpsDen))}%` }}
                  >
                    <span className="absolute inset-y-0 w-px bg-foreground/20" />
                    <span className="tnum absolute left-1 top-1/2 -translate-y-1/2 whitespace-nowrap text-[9px] leading-none text-muted-foreground">
                      {formatDuration(seconds * 1000)}
                    </span>
                  </div>
                ))}
                <span className="tnum pointer-events-none absolute right-1 top-1/2 -translate-y-1/2 text-[9px] leading-none text-muted-foreground">
                  {sequence.duration_tc}
                </span>
              </div>

              <GyroChart
                sequenceId={sequence.id}
                lastFrame={lastFrame}
                frame={frame}
                showPlayhead={false}
              />

              {/* Everything outside the kept cuts is dimmed, the way Gyroflow greys
                  what its trim range leaves out. Nothing to dim until a first cut
                  exists: the whole rush is still a candidate. */}
              {excluded.map(([from, to]) => (
                <div
                  key={`gap-${from}-${to}`}
                  className="pointer-events-none absolute bg-background/60"
                  style={{
                    top: BAR_HEIGHT,
                    height: PLOT_HEIGHT,
                    left: `${percent(from)}%`,
                    width: `${widthPercent(to - from)}%`,
                  }}
                />
              ))}

              {cuts.map((cut) => {
                const bounds = `${formatTimecode(cut.start_frame, fpsNum, fpsDen)} \u2192 ${formatTimecode(cut.end_frame, fpsNum, fpsDen)}`;
                const grabbed = drag?.kind === "move" && drag.key === cut.key;
                return (
                  <div
                    key={cut.key}
                    className="pointer-events-none absolute top-0"
                    style={{
                      left: `${percent(cut.start_frame)}%`,
                      width: `${Math.max(widthPercent(cut.end_frame - cut.start_frame), 0.4)}%`,
                      height: TRACK_HEIGHT,
                    }}
                  >
                    {/* The cut over the curves: a tint and two edges, nothing that
                        hides the signal being read underneath. */}
                    <div
                      className="pointer-events-none absolute inset-x-0 bottom-0 border-x border-primary/70 bg-primary/10"
                      style={{ top: BAR_HEIGHT }}
                    />

                    {/* The same cut in the trim bar, solid. This is the part one
                        grabs, and the only place a click is not a scrub. */}
                    <div
                      className={cn(
                        "pointer-events-auto absolute inset-x-0 top-0 bg-primary/70 transition-colors hover:bg-primary",
                        grabbed ? "cursor-grabbing" : "cursor-grab",
                      )}
                      style={{ height: BAR_HEIGHT }}
                      title={`${cut.label} \u2014 ${bounds} (drag to move, edges to trim)`}
                      onPointerDown={(event) =>
                        beginDrag(event, {
                          kind: "move",
                          key: cut.key,
                          grab: frameAt(event.clientX) - cut.start_frame,
                        })
                      }
                    >
                      {widthPercent(cut.end_frame - cut.start_frame) > 7 && (
                        <span className="tnum pointer-events-none absolute inset-0 flex items-center justify-center text-[9px] leading-none text-primary-foreground">
                          {formatDuration(
                            ((cut.end_frame - cut.start_frame + 1) * 1000 * fpsDen) / (fpsNum || 1),
                          )}
                        </span>
                      )}
                    </div>

                    {(["start", "end"] as const).map((edge) => (
                      <div
                        key={edge}
                        className={cn(
                          // Inside the band, not straddling its edge: the track is
                          // clipped to a rounded box, and a handle hanging over the
                          // edge lost a corner to it at frame 0.
                          "pointer-events-auto absolute top-0 flex w-4 cursor-col-resize items-center justify-center rounded-sm bg-primary",
                          edge === "start" ? "left-0" : "right-0",
                        )}
                        style={{ height: BAR_HEIGHT }}
                        title={edge === "start" ? "Trim the start" : "Trim the end"}
                        onPointerDown={(event) => beginDrag(event, { kind: edge, key: cut.key })}
                      >
                        <span className="h-4 w-[3px] rounded-full bg-primary-foreground" />
                      </div>
                    ))}

                    <span className="pointer-events-none absolute left-1.5 truncate text-xs font-medium text-primary/90"
                      style={{ top: BAR_HEIGHT + 2 }}
                    >
                      {cut.label}
                    </span>
                  </div>
                );
              })}

              {markIn !== null && (
                <div
                  className="pointer-events-none absolute top-0 w-px bg-amber-400"
                  style={{ left: `${percent(markIn)}%`, height: TRACK_HEIGHT }}
                />
              )}

              {/* Playhead: a line across the whole track with a knob in the bar, so
                  it stays findable when cuts cover the width. */}
              <div
                className="pointer-events-none absolute top-0 w-px bg-foreground"
                style={{ left: `${percent(frame)}%`, height: TRACK_HEIGHT }}
              >
                <span className="absolute -left-1 top-0 h-2 w-[9px] rounded-b-sm bg-foreground" />
              </div>
            </div>

          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button size="icon" variant="outline" title="Start" onClick={() => seek(0)}>
              <SkipBack className="h-4 w-4" />
            </Button>
            <Button
              size="icon"
              variant="outline"
              title="Previous frame (←)"
              onClick={() => seek(frame - 1)}
            >
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <Button size="icon" onClick={togglePlay} title="Play / pause (space)">
              {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
            </Button>
            <Button
              size="icon"
              variant="outline"
              title="Next frame (→)"
              onClick={() => seek(frame + 1)}
            >
              <ChevronRight className="h-4 w-4" />
            </Button>
            <Button size="icon" variant="outline" title="End" onClick={() => seek(lastFrame)}>
              <SkipForward className="h-4 w-4" />
            </Button>

            <span className="tnum ml-2 text-sm">
              {formatTimecode(frame, fpsNum, fpsDen)}
              <span className="ml-2 text-sm text-muted-foreground">frame {frame}</span>
            </span>

            <div className="ml-auto flex items-center gap-2">
              <Select value={String(speed)} onValueChange={(value) => setSpeed(Number(value))}>
                <SelectTrigger className="h-8 w-24">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {SPEEDS.map((value) => (
                    <SelectItem key={value} value={String(value)}>
                      ×{value}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <Separator />

          {/* Marking a sequence, and the only two states it has: one button is live
              at a time, so which one is the next move needs no reading. */}
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={markIn !== null} onClick={() => setMarkIn(frame)}>
              Start sequence <kbd className="ml-1 text-xs opacity-60">I</kbd>
            </Button>
            <Button
              size="sm"
              disabled={markIn === null}
              onClick={() =>
                markIn !== null && addCut(Math.min(markIn, frame), Math.max(markIn, frame))
              }
            >
              End sequence <kbd className="ml-1 text-xs opacity-60">O</kbd>
            </Button>
            {markIn !== null && (
              <>
                <Button size="sm" variant="ghost" onClick={() => setMarkIn(null)}>
                  Cancel
                </Button>
                <span className="tnum text-sm text-amber-400">
                  from {formatTimecode(markIn, fpsNum, fpsDen)}
                </span>
              </>
            )}
          </div>

        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex-row items-baseline justify-between gap-2 pb-2">
          <CardTitle className="text-sm">Sequences</CardTitle>
          {cuts.length > 0 && (
            <span className="tnum text-xs text-muted-foreground">
              {formatDuration((keptFrames * 1000 * fpsDen) / (fpsNum || 1))} kept
            </span>
          )}
        </CardHeader>
        <CardContent className="space-y-0.5 px-2 pb-3">
          {cuts.length === 0 ? (
            <p className="px-2 text-sm text-muted-foreground">Nothing marked yet.</p>
          ) : (
            cuts.map((cut) => (
              <div
                key={cut.key}
                className="group rounded-md px-2 py-1.5 transition-colors hover:bg-accent/50"
              >
                <div className="flex items-center gap-1">
                  <span className="min-w-0 flex-1 truncate text-sm">{cut.label}</span>
                  <span className="tnum shrink-0 text-xs text-muted-foreground">
                    {formatDuration(
                      ((cut.end_frame - cut.start_frame + 1) * 1000 * fpsDen) / (fpsNum || 1),
                    )}
                  </span>
                  <Button
                    size="icon"
                    variant="ghost"
                    title="Rename"
                    className="h-6 w-6 shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
                    onClick={() => setRenaming(cut)}
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    size="icon"
                    variant="ghost"
                    title="Delete"
                    className="h-6 w-6 shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
                    onClick={() => commit(cuts.filter((c) => c.key !== cut.key))}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
                <div className="tnum flex items-center gap-1 text-xs text-muted-foreground">
                  <button
                    className="hover:text-foreground hover:underline"
                    onClick={() => seek(cut.start_frame)}
                  >
                    {formatTimecode(cut.start_frame, fpsNum, fpsDen)}
                  </button>
                  {"\u2192"}
                  <button
                    className="hover:text-foreground hover:underline"
                    onClick={() => seek(cut.end_frame)}
                  >
                    {formatTimecode(cut.end_frame, fpsNum, fpsDen)}
                  </button>
                </div>
              </div>
            ))
          )}
        </CardContent>
      </Card>
      </div>

      <RenameCut
        cut={renaming}
        onClose={() => setRenaming(null)}
        onRename={(label) =>
          commit(cuts.map((c) => (c.key === renaming?.key ? { ...c, label } : c)))
        }
      />
    </div>
  );
}

/**
 * Renaming one sequence, in a dialog.
 *
 * A field sitting in the list would be one click shorter and cost a column: the
 * name is the widest thing there, and an input around it turns a list one reads
 * into a form one fills.
 */
function RenameCut({
  cut,
  onClose,
  onRename,
}: {
  cut: LocalCut | null;
  onClose: () => void;
  onRename: (label: string) => void;
}) {
  const [draft, setDraft] = useState("");
  useEffect(() => {
    if (cut) setDraft(cut.label);
  }, [cut?.key]);

  return (
    <Dialog open={cut !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Rename sequence</DialogTitle>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            const wanted = draft.trim();
            if (wanted && wanted !== cut?.label) onRename(wanted);
            onClose();
          }}
        >
          <Input autoFocus value={draft} onChange={(event) => setDraft(event.target.value)} />
          <DialogFooter>
            <Button type="submit" size="sm" disabled={!draft.trim()}>
              Rename
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Name of the current rush, editable in place.
 *
 * The key a rush is born with (`DJI_20260811144828_0044_D`) says when it was
 * filmed and nothing about what is in it. Over a session of thirty, a name is what
 * makes one findable again.
 */
function RushIdentity({ sequence }: { sequence: SequenceDetail }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(sequence.label);

  const update = useMutation({
    mutationFn: (changes: { label?: string }) => api.updateSequence(sequence.id, changes),
    onSuccess: () => {
      setEditing(false);
      queryClient.invalidateQueries({ queryKey: ["sequence", sequence.id] });
      queryClient.invalidateQueries({ queryKey: ["sequences"] });
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const commit = () => {
    const label = draft.trim();
    if (!label || label === sequence.label) {
      setEditing(false);
      return;
    }
    update.mutate({ label });
  };

  return (
    <span className="flex items-center gap-3">
      {editing ? (
        <Input
          autoFocus
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === "Enter") commit();
            if (event.key === "Escape") {
              setDraft(sequence.label);
              setEditing(false);
            }
          }}
          className="h-7 w-72 text-sm"
        />
      ) : (
        <button
          type="button"
          title="Rename this rush"
          onClick={() => {
            setDraft(sequence.label);
            setEditing(true);
          }}
          className="group inline-flex items-center gap-1.5 text-sm font-semibold"
        >
          {sequence.label}
          <Pencil className="h-3 w-3 opacity-0 transition-opacity group-hover:opacity-60" />
        </button>
      )}
    </span>
  );
}
