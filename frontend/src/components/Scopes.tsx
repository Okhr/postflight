/**
 * The colour instruments: histogram, waveform, and what the frame measures.
 *
 * They sit at the top of the settings column, not under the picture. Measured before
 * the move, on a 1600 wide window: the page has four columns (sidebar, clips, picture,
 * settings), so a scope under the picture got 267 x 80 px, and the hand was on a slider
 * at x=1424 while the eye had to read a scope at x=835. Here they are as wide as the
 * column and directly above the sliders they answer for.
 *
 * The frames come from GradedVideo, which reads the pixels its own shader wrote, so a
 * scope always describes what is on screen rather than what the parameters ought to
 * give. They arrive through an imperative handle rather than a prop: one lands per
 * presented frame, and it must not re-render the page around it.
 */
import type React from "react";
import { forwardRef, useImperativeHandle, useRef, useState } from "react";

import { cn } from "@/lib/utils";

/** The sample the scopes are computed from: 256 columns so the waveform has one per
 *  drawn pixel, 144 rows because that is enough of the picture to describe it. */
export const SAMPLE_W = 256;
export const SAMPLE_H = 144;
export const BINS = 128;

/** What the frame measures, for the line of numbers under the scopes. */
export interface FrameStats {
  clippedHigh: number;
  clippedLow: number;
  min: number;
  avg: number;
  max: number;
}

/** Which instruments are on. They are remembered by the page, not by this component. */
export interface Scopes {
  zebras: boolean;
  histogram: boolean;
  waveform: boolean;
  numbers: boolean;
}

/** One painted frame, as the scopes need it. */
export interface Frame {
  /** Red, green and blue counts per bin. */
  channels: Float32Array[];
  /** Luma counts, one column per sampled column, `BINS` rows deep. */
  wave: Float32Array;
  stats: FrameStats;
}

export interface ScopeSink {
  push(frame: Frame): void;
}

/**
 * Three channels on one plot, with the ends marked.
 *
 * The scale is absolute against the tallest bin of the frame, but the marks are what
 * make it readable: a line at each end says where clipping is, which the old version
 * had no way of showing. Additive drawing, so where the three agree it goes white,
 * which is what a neutral frame looks like.
 */
function drawHistogram(target: HTMLCanvasElement | null, channels: Float32Array[]) {
  const ctx = target?.getContext("2d");
  if (!target || !ctx) return;
  const { width, height } = target;
  ctx.clearRect(0, 0, width, height);
  let top = 1;
  for (const channel of channels) for (const value of channel) if (value > top) top = value;

  ctx.globalCompositeOperation = "lighter";
  const colours = ["rgba(255,80,80,0.75)", "rgba(80,220,120,0.75)", "rgba(90,140,255,0.75)"];
  const step = width / BINS;
  channels.forEach((channel, index) => {
    ctx.fillStyle = colours[index];
    for (let i = 0; i < BINS; i += 1) {
      const h = (channel[i] / top) * height;
      if (h > 0) ctx.fillRect(i * step, height - h, Math.max(1, step), h);
    }
  });
  ctx.globalCompositeOperation = "source-over";
  ctx.fillStyle = "rgba(250,250,250,0.25)";
  ctx.fillRect(0, 0, 1, height);
  ctx.fillRect(width - 1, 0, 1, height);
}

/**
 * Luma against horizontal position: where in the frame the darks and brights are.
 *
 * The half a histogram cannot say. On this footage it separates sky from ground at a
 * glance, which is exactly the decision the highlights slider is for. Counts are
 * mapped to alpha with a square root, since one flat area otherwise saturates
 * everything else out of view.
 */
function drawWaveform(target: HTMLCanvasElement | null, wave: Float32Array) {
  const ctx = target?.getContext("2d");
  if (!target || !ctx) return;
  const { width, height } = target;
  const image = ctx.createImageData(SAMPLE_W, BINS);
  let top = 1;
  for (const value of wave) if (value > top) top = value;
  for (let i = 0; i < wave.length; i += 1) {
    const alpha = Math.sqrt(wave[i] / top);
    image.data[i * 4] = 210;
    image.data[i * 4 + 1] = 240;
    image.data[i * 4 + 2] = 210;
    image.data[i * 4 + 3] = Math.min(255, alpha * 320);
  }
  ctx.clearRect(0, 0, width, height);
  // Through a bitmap of its own, because putImageData ignores any scaling.
  const buffer = new OffscreenCanvas(SAMPLE_W, BINS);
  buffer.getContext("2d")?.putImageData(image, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(buffer, 0, 0, width, height);
  ctx.fillStyle = "rgba(250,250,250,0.25)";
  ctx.fillRect(0, 0, width, 1);
  ctx.fillRect(0, height - 1, width, 1);
}

/**
 * The instruments, stacked, with their own switches on top.
 *
 * No tabs: reading the histogram and the waveform at once is the whole point of having
 * both, and hiding one behind the other turns a glance into a round trip. Whichever are
 * on take the full width of the column, so the waveform keeps one drawn pixel per
 * sampled column.
 */
export const ScopePanel = forwardRef<
  ScopeSink,
  { scopes: Scopes; instruments: React.ReactNode; className?: string }
>(function ScopePanel({ scopes, instruments, className }, ref) {
  const histRef = useRef<HTMLCanvasElement>(null);
  const waveRef = useRef<HTMLCanvasElement>(null);
  const [stats, setStats] = useState<FrameStats | null>(null);

  useImperativeHandle(ref, () => ({
    push({ channels, wave, stats: measured }) {
      drawHistogram(histRef.current, channels);
      drawWaveform(waveRef.current, wave);
      setStats(measured);
    },
  }), []);

  return (
    <div className={cn("space-y-2", className)}>
      <div className="flex items-center gap-0.5">{instruments}</div>
      {scopes.histogram && (
        <canvas
          ref={histRef}
          width={512}
          height={80}
          title="Red, green and blue against level. The ends are marked: anything against them is clipped."
          className="h-20 w-full rounded bg-muted/30"
        />
      )}
      {scopes.waveform && (
        <canvas
          ref={waveRef}
          width={512}
          height={80}
          title="Luma against horizontal position: where in the frame the darks and the brights are."
          className="h-20 w-full rounded bg-muted/30"
        />
      )}
      {scopes.numbers && stats && (
        <p className="tnum flex flex-wrap gap-x-3 text-xs text-muted-foreground">
          <span className={cn(stats.clippedHigh > 0.02 && "text-amber-400")}>
            clipped {(stats.clippedHigh * 100).toFixed(1)} % high
          </span>
          <span className={cn(stats.clippedLow > 0.02 && "text-amber-400")}>
            {(stats.clippedLow * 100).toFixed(1)} % low
          </span>
          <span>min {stats.min}</span>
          <span>avg {stats.avg}</span>
          <span>max {stats.max}</span>
        </p>
      )}
    </div>
  );
});
