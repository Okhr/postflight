/**
 * The stabilized clip, playing, with the colour chain applied on the GPU.
 *
 * It replaced a still frame that ffmpeg produced in 0.24 s: exact, but one image at a
 * time and a round trip per slider move. The shader is an approximation of the same
 * chain (39 dB against ffmpeg, 2 levels of average error, see lib/grade-shader) and it
 * follows the sliders and the playback. The file that gets written still comes from
 * ffmpeg, so the approximation only ever costs a preview.
 *
 * The histogram reads the pixels the shader produced, so it always describes what is on
 * screen rather than what the parameters ought to give.
 */
import type React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Pause, Play } from "lucide-react";

import { Button } from "@/components/ui/button";
import { createRenderer, type GradePlan, type Renderer } from "@/lib/grade-shader";
import { cn } from "@/lib/utils";

const HIST_W = 192;
const HIST_H = 108;

export interface Mark {
  label: string;
  ms: number;
}

export function GradedVideo({
  src,
  plan,
  marks = [],
  actions,
  className,
}: {
  src: string;
  plan: GradePlan;
  marks?: Mark[];
  /** Dropped in the control row. What compares before and after belongs next to the
   *  play button, not on a line of its own under the picture. */
  actions?: React.ReactNode;
  className?: string;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const histRef = useRef<HTMLCanvasElement>(null);
  const scratch = useRef<HTMLCanvasElement | null>(null);
  const renderer = useRef<Renderer | null>(null);
  const planRef = useRef(plan);
  const [playing, setPlaying] = useState(false);
  const [at, setAt] = useState(0);
  const [length, setLength] = useState(0);
  const [broken, setBroken] = useState<string | null>(null);

  planRef.current = plan;

  /** One frame: the shader, then the histogram off the very pixels it wrote. */
  const paint = useCallback(() => {
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (!video || !canvas || !renderer.current || video.readyState < 2) return;
    if (canvas.width !== video.videoWidth || canvas.height !== video.videoHeight) {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
    }
    renderer.current.draw(video, planRef.current);

    const hist = histRef.current;
    if (!hist) return;
    if (!scratch.current) {
      scratch.current = document.createElement("canvas");
      scratch.current.width = HIST_W;
      scratch.current.height = HIST_H;
    }
    // Read a reduced copy: 20 000 pixels say the same thing as two million about
    // where the luma sits, and it costs nothing per frame. It has to happen in the
    // same task as the draw, before the drawing buffer is cleared.
    const small = scratch.current.getContext("2d", { willReadFrequently: true });
    if (!small) return;
    small.drawImage(canvas, 0, 0, HIST_W, HIST_H);
    const { data } = small.getImageData(0, 0, HIST_W, HIST_H);
    const bins = new Float32Array(64);
    for (let i = 0; i < data.length; i += 4) {
      const luma = 0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2];
      bins[Math.min(63, (luma / 4) | 0)] += 1;
    }
    const top = Math.max(...bins) || 1;
    const ctx = hist.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, hist.width, hist.height);
    ctx.fillStyle = "rgba(250, 250, 250, 0.75)";
    const step = hist.width / bins.length;
    for (let i = 0; i < bins.length; i += 1) {
      const h = (bins[i] / top) * hist.height;
      ctx.fillRect(i * step, hist.height - h, Math.max(1, step - 1), h);
    }
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

  // A slider moved: repaint the frame that is already there.
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
          className={cn("block w-full", !broken && "invisible absolute inset-0")}
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
        />
        <canvas ref={canvasRef} className={cn("block w-full", broken && "hidden")} />
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

      {/* Full width, under the controls: it replaced a card of prose about the same
          measurements, and a 48 px thumbnail in a corner would not have. */}
      <canvas ref={histRef} width={512} height={64} className="h-16 w-full rounded bg-muted/30" />
      {broken && (
        <p className="text-sm text-red-400">
          No GPU preview here ({broken}), showing the clip ungraded.
        </p>
      )}
    </div>
  );
}
