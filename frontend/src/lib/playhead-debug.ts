/**
 * Temporary recorder for the derush playhead.
 *
 * The misbehaviour shows up on one person's browser and on none of the measurements
 * made here, so the recording has to come from that browser. Every gesture and every
 * video event lands in a ring buffer; when a gesture fails to land where it asked to,
 * the buffer is posted and written to `data/debug/`.
 *
 * Remove this module, its route and its call sites once the cause is known.
 */

type Entry = Record<string, unknown>;

/** Enough to hold several seconds of 60 Hz callbacks plus the gestures around them. */
const RING = 600;
/** A page that goes wrong once tends to go wrong often: report, then stop shouting. */
const MAX_REPORTS = 20;

let entries: Entry[] = [];
let reports = 0;
const started = performance.now();

export function mark(kind: string, data: Entry = {}) {
  entries.push({ ms: Math.round((performance.now() - started) * 10) / 10, kind, ...data });
  if (entries.length > RING) entries = entries.slice(-RING);
}

/** True when the trace was sent, so the caller can say so on screen. */
export function report(reason: string, extra: Entry = {}) {
  if (reports >= MAX_REPORTS) return false;
  reports += 1;
  void fetch("/api/debug/report", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      reason,
      at: new Date().toISOString(),
      agent: navigator.userAgent,
      ...extra,
      entries,
    }),
  }).catch(() => undefined);
  return true;
}

/** What the decoder is doing, which no event reports. */
export function quality(video: HTMLVideoElement) {
  const q = (
    video as HTMLVideoElement & {
      getVideoPlaybackQuality?: () => {
        totalVideoFrames: number;
        droppedVideoFrames: number;
        corruptedVideoFrames: number;
      };
    }
  ).getVideoPlaybackQuality?.();
  return q
    ? { total: q.totalVideoFrames, dropped: q.droppedVideoFrames, corrupted: q.corruptedVideoFrames }
    : null;
}
