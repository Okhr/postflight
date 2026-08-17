/**
 * Frame ↔ time conversions, client side.
 *
 * The fps arrives as an exact rational (60000/1001), not as a rounded 59.94: over
 * a four-minute rush, rounding drifts by several frames. So the whole derush
 * timeline works in frame numbers, never in seconds.
 */

export function frameToSeconds(frame: number, fpsNum: number, fpsDen: number): number {
  if (!fpsNum) return 0;
  return (frame * fpsDen) / fpsNum;
}

export function secondsToFrame(seconds: number, fpsNum: number, fpsDen: number): number {
  if (!fpsDen) return 0;
  return Math.floor((seconds * fpsNum) / fpsDen + 1e-6);
}

export function formatTimecode(frame: number, fpsNum: number, fpsDen: number): string {
  const totalMs = Math.round(frameToSeconds(frame, fpsNum, fpsDen) * 1000);
  const ms = totalMs % 1000;
  const totalS = Math.floor(totalMs / 1000);
  const s = totalS % 60;
  const m = Math.floor(totalS / 60) % 60;
  const h = Math.floor(totalS / 3600);
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  const base = `${pad(m)}:${pad(s)}.${pad(ms, 3)}`;
  return h > 0 ? `${h}:${base}` : base;
}

export function formatDuration(ms: number): string {
  const totalS = Math.round(ms / 1000);
  const s = totalS % 60;
  const m = Math.floor(totalS / 60) % 60;
  const h = Math.floor(totalS / 3600);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes) return "—";
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("fr-FR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Rough ETA from progress and elapsed time — a plain linear extrapolation.
 *
 * Good enough because the two long steps advance almost linearly: ffmpeg walks
 * the frames at a steady rate, and so does Gyroflow. Below 2% it stays quiet,
 * where the first seconds of startup would predict nonsense.
 */
export function etaLabel(progress: number, startedAt: string | null): string | null {
  if (!startedAt || progress <= 0.02 || progress >= 1) return null;
  const elapsed = (Date.now() - new Date(startedAt).getTime()) / 1000;
  if (!Number.isFinite(elapsed) || elapsed <= 0) return null;
  const remaining = (elapsed * (1 - progress)) / progress;
  if (remaining < 10) return "a few seconds left";
  if (remaining < 90) return `~${Math.round(remaining / 5) * 5}s left`;
  const minutes = Math.round(remaining / 60);
  if (minutes < 60) return `~${minutes} min left`;
  const hours = Math.floor(minutes / 60);
  return `~${hours}h${String(minutes % 60).padStart(2, "0")} left`;
}
