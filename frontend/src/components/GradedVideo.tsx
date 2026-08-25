/**
 * The stabilized clip, playing, with the colour chain applied on the GPU.
 *
 * It replaced a still frame that ffmpeg produced in 0.24 s: exact, but one image at a
 * time and a round trip per slider move. The shader is an approximation of the same
 * chain (39 dB against ffmpeg, 2 levels of average error, see lib/grade-shader) and it
 * follows the sliders and the playback. The file that gets written still comes from
 * ffmpeg, so the approximation only ever costs a preview.
 *
 * The instruments are drawn elsewhere (components/Scopes, in the settings column): what
 * happens here is the readback, from the very pixels the shader wrote, so a scope always
 * describes what is on screen rather than what the parameters ought to give.
 */
import type React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Pause, Play } from "lucide-react";

import { BINS, SAMPLE_H, SAMPLE_W, type ScopeSink, type Scopes } from "@/components/Scopes";
import { Button } from "@/components/ui/button";
import { createRenderer, type GradePlan, type Renderer } from "@/lib/grade-shader";
import { cn } from "@/lib/utils";

export interface Mark {
  label: string;
  ms: number;
}

export function GradedVideo({
  src,
  plan,
  marks = [],
  scopes,
  sink,
  actions,
  className,
}: {
  src: string;
  plan: GradePlan;
  marks?: Mark[];
  /** Which instruments are on. Nothing is computed for one that is off. */
  scopes: Scopes;
  /** Where each painted frame goes to be drawn. Its own handle, not a prop callback:
   *  one frame lands per presentation, and the page must not re-render around it. */
  sink?: React.RefObject<ScopeSink | null>;
  /** Dropped in the control row. What compares before and after belongs next to the
   *  play button, not on a line of its own under the picture. */
  actions?: React.ReactNode;
  className?: string;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const scratch = useRef<HTMLCanvasElement | null>(null);
  const renderer = useRef<Renderer | null>(null);
  const planRef = useRef(plan);
  const [playing, setPlaying] = useState(false);
  const [at, setAt] = useState(0);
  const [length, setLength] = useState(0);
  const [broken, setBroken] = useState<string | null>(null);
  const [undecodable, setUndecodable] = useState(false);
  const scopesRef = useRef(scopes);
  const sinkRef = useRef(sink);

  planRef.current = plan;
  scopesRef.current = scopes;
  sinkRef.current = sink;

  /**
   * One frame: the shader, then every instrument off the very pixels it wrote.
   *
   * All of them share a single readback of a reduced copy. 36 000 pixels describe a
   * two megapixel frame well enough for a scope, and the read has to happen in the
   * same task as the draw, before the drawing buffer is cleared.
   *
   * Which is why the clipping overlay is not derived from these numbers: it runs per
   * pixel in the shader, at full resolution. Seen on a real frame, the sample reported
   * a maximum of 234 while the overlay still found pixels at the ceiling, because
   * scaling the frame down averages a lone blown pixel away. The overlay is the exact
   * instrument, the numbers are a sample of the same frame.
   */
  const paint = useCallback(() => {
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (!video || !canvas || !renderer.current || video.readyState < 2) return;
    if (canvas.width !== video.videoWidth || canvas.height !== video.videoHeight) {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
    }
    renderer.current.draw(video, planRef.current);

    const want = scopesRef.current;
    if (!want.histogram && !want.waveform && !want.numbers) return;
    if (!scratch.current) {
      scratch.current = document.createElement("canvas");
      scratch.current.width = SAMPLE_W;
      scratch.current.height = SAMPLE_H;
    }
    const small = scratch.current.getContext("2d", { willReadFrequently: true });
    if (!small) return;
    small.drawImage(canvas, 0, 0, SAMPLE_W, SAMPLE_H);
    const { data } = small.getImageData(0, 0, SAMPLE_W, SAMPLE_H);

    const red = new Float32Array(BINS);
    const green = new Float32Array(BINS);
    const blue = new Float32Array(BINS);
    // The waveform is a density: one column per sampled column, counts down the rows.
    const wave = new Float32Array(SAMPLE_W * BINS);
    let high = 0;
    let low = 0;
    let min = 255;
    let max = 0;
    let sum = 0;
    for (let i = 0, p = 0; i < data.length; i += 4, p += 1) {
      const r = data[i];
      const g = data[i + 1];
      const b = data[i + 2];
      red[(r * (BINS - 1)) / 255 | 0] += 1;
      green[(g * (BINS - 1)) / 255 | 0] += 1;
      blue[(b * (BINS - 1)) / 255 | 0] += 1;
      const luma = 0.2126 * r + 0.7152 * g + 0.0722 * b;
      if (r >= 254 || g >= 254 || b >= 254) high += 1;
      if (r <= 1 && g <= 1 && b <= 1) low += 1;
      if (luma < min) min = luma;
      if (luma > max) max = luma;
      sum += luma;
      const bin = (luma * (BINS - 1)) / 255 | 0;
      wave[(BINS - 1 - bin) * SAMPLE_W + (p % SAMPLE_W)] += 1;
    }
    const pixels = data.length / 4;
    sinkRef.current?.current?.push({
      channels: [red, green, blue],
      wave,
      stats: {
        clippedHigh: high / pixels,
        clippedLow: low / pixels,
        min: Math.round(min),
        avg: Math.round(sum / pixels),
        max: Math.round(max),
      },
    });
  }, []);

  // The pipeline lives as long as the canvas does.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    try {
      renderer.current = createRenderer(canvas);
    } catch (error) {
      setBroken(error instanceof Error ? error.message : "no GPU preview");
      return;
    }
    return () => {
      renderer.current?.destroy();
      renderer.current = null;
    };
  }, []);

  // While playing, every presented frame. Paused, whenever the plan changes.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || broken) return;
    let handle = 0;
    let live = true;
    type WithRvfc = HTMLVideoElement & {
      requestVideoFrameCallback?: (cb: () => void) => number;
      cancelVideoFrameCallback?: (h: number) => void;
    };
    const target = video as WithRvfc;
    if (!target.requestVideoFrameCallback) {
      const loop = () => {
        if (!live) return;
        paint();
        handle = requestAnimationFrame(loop);
      };
      handle = requestAnimationFrame(loop);
      return () => {
        live = false;
        cancelAnimationFrame(handle);
      };
    }
    const step = () => {
      if (!live) return;
      paint();
      handle = target.requestVideoFrameCallback!(step);
    };
    handle = target.requestVideoFrameCallback(step);
    return () => {
      live = false;
      target.cancelVideoFrameCallback?.(handle);
    };
  }, [paint, broken, src]);

  // A slider moved: repaint the frame that is already there. An instrument switched on
  // lands here too, since the page rebuilds `plan` on every render (measured: 7963 lit
  // pixels in the histogram either way, so a `scopes` dependency would be noise).
  useEffect(() => {
    paint();
  }, [plan, paint]);

  const seek = (ms: number) => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = ms / 1000;
    setAt(ms);
  };

  return (
    <div className={cn("space-y-2", className)}>
      <div className="relative overflow-hidden rounded-md bg-black">
        <video
          ref={videoRef}
          src={src}
          className={cn(
            "mx-auto block max-h-[65vh] max-w-full",
            !broken && "invisible absolute inset-0",
          )}
          playsInline
          muted
          onLoadedMetadata={(event) => setLength(event.currentTarget.duration * 1000)}
          // A paused video presents one frame and no more, so the frame callback
          // fires once, often before there is anything to draw. These are the events
          // that promise data, and they are what actually gets the first frame up.
          onLoadedData={paint}
          onCanPlay={paint}
          onSeeked={paint}
          onTimeUpdate={(event) => setAt(event.currentTarget.currentTime * 1000)}
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          // A decode that dies mid-seek leaves the picture frozen and every control
          // dead, which reads as the page being broken. Measured on a 10-bit H.264
          // render: the first seek raises MEDIA_ERR_DECODE and nothing recovers it,
          // not even reloading the element. So it gets said rather than suffered.
          onError={() => setUndecodable(true)}
        />
        {/* Capped by height as well as width: a 9:16 clip at full width pushed the
            controls and the histogram off the screen. */}
        <canvas
          ref={canvasRef}
          className={cn("mx-auto block max-h-[65vh] max-w-full", broken && "hidden")}
        />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="icon"
          variant="outline"
          onClick={() => {
            const video = videoRef.current;
            if (!video) return;
            if (video.paused) void video.play();
            else video.pause();
          }}
          title="Play / pause"
        >
          {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
        </Button>
        <input
          type="range"
          min={0}
          max={Math.max(length, 1)}
          value={at}
          onChange={(event) => seek(Number(event.target.value))}
          className="h-1 min-w-24 flex-1 accent-primary"
        />
        <span className="tnum text-sm text-muted-foreground">
          {(at / 1000).toFixed(1)}s
        </span>
        {marks.map((point) => (
          <Button
            key={point.label}
            size="sm"
            variant={Math.abs(at - point.ms) < 200 ? "secondary" : "ghost"}
            onClick={() => seek(point.ms)}
          >
            {point.label}
          </Button>
        ))}
        {actions && <span className="ml-auto flex items-center gap-2">{actions}</span>}
      </div>

      {broken && (
        <p className="text-sm text-red-400">
          No GPU preview here ({broken}), showing the clip ungraded.
        </p>
      )}
      {undecodable && (
        <p className="text-sm text-red-400">
          The browser cannot decode this clip. Render it again to get one it can play.
        </p>
      )}
    </div>
  );
}
