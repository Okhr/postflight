import { useEffect, type RefObject } from "react";

/**
 * Hold a video at the rate this page asked for.
 *
 * Video-speed browser extensions remember the last rate they saw and push it onto
 * every video that turns up afterwards. Reported on 2026-08-30: ×4 set in the derush
 * player came back on the colour preview, which has no speed control at all
 * (measured there: `playbackRate` 4 with `defaultPlaybackRate` 1, so a plain write
 * from outside the page). The same write can also contradict the derush picker,
 * which would then say one thing while the player does another.
 *
 * `ratechange` is the one hook that catches it whoever wrote the value, since the
 * element fires it for every write. Putting the rate back is itself a write, so the
 * guard is what keeps the two from ping-ponging: measured, zero events after the
 * first correction.
 *
 * No dependency array on purpose. The element usually turns up on a later render
 * than the one that mounted this hook (the derush player waits for its rush), and a
 * ref filling in does not re-run an effect: with `[ref, rate]` this listened to
 * nothing at all there.
 */
export function useFixedPlaybackRate(ref: RefObject<HTMLVideoElement>, rate: number) {
  useEffect(() => {
    const video = ref.current;
    if (!video) return;
    const hold = () => {
      if (video.playbackRate !== rate) video.playbackRate = rate;
    };
    hold();
    video.addEventListener("ratechange", hold);
    return () => video.removeEventListener("ratechange", hold);
  });
}
