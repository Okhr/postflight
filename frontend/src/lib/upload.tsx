/**
 * Uploads, held above the router.
 *
 * The transfer itself never depended on the page: an XMLHttpRequest in flight is
 * not cancelled by React unmounting the component that started it. What was lost
 * on navigation was everything around it, since the queue lived in the Import
 * page's state: the list, the percentage, the Stop button, and the fact that one
 * was running at all. So the state moved here, mounted in the layout, and the page
 * became a view of it.
 *
 * A background tab keeps uploading too, and that is not luck. Chrome throttles
 * timers and stops requestAnimationFrame in a hidden tab; it does not throttle
 * network requests in flight. Nothing in this loop waits on a timer: it is
 * promises and XHR events end to end, which is what makes it survive being out of
 * sight. Closing the tab is a different matter, hence the beforeunload guard.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { api } from "@/lib/api";

export const ACCEPTED = [".mp4", ".mov"];

export type ItemStatus = "queued" | "checking" | "skipped" | "uploading" | "done" | "error";

export interface Item {
  key: string;
  file: File;
  status: ItemStatus;
  progress: number;
  error?: string;
  /** Name this file already carries in the library, when it is a duplicate. */
  known?: string;
  /** Name the server stored it under, which is what to look for in the rush list. */
  landedAs?: string;
  /** Bytes of an interrupted upload of this same name, thrown away before resending. */
  discarded?: number;
}

interface Upload {
  items: Item[];
  busy: boolean;
  /** Bytes moved and bytes to move, skipped files excluded since they never travel. */
  moved: number;
  total: number;
  send: (files: File[]) => void;
  cancel: () => void;
  clear: () => void;
}

const UploadContext = createContext<Upload | null>(null);

export function useUpload(): Upload {
  const value = useContext(UploadContext);
  if (!value) throw new Error("useUpload outside UploadProvider");
  return value;
}

/**
 * What one request may carry.
 *
 * The public chain refuses a body over 100 MiB, measured on the real Cloudflare path:
 * 104 857 600 bytes pass, one more comes back 413 from the edge without the origin
 * seeing anything. So a 4 GB rush cannot go out in a single PUT from outside the LAN,
 * however patient we are. 64 MiB keeps margin under the ceiling and keeps a retry
 * cheap.
 */
const CHUNK = 64 * 1024 * 1024;

/**
 * Pieces in flight at once, which is the other half of what cutting a file up buys.
 * At the ~20 ms of RTT of the box VPN a single TCP flow is bounded by the window and
 * collapses on any loss, which is why the observed remote upload sat at 9 MB/s on a
 * link worth ten times that. Three, not more: the point is to fill one link, not to
 * scatter writes across the destination disk.
 */
const CONCURRENCY = 3;

/**
 * One piece, by XMLHttpRequest rather than fetch: it is the only way to get upload
 * progress, and a raw body spares the server from copying it into a temporary file.
 */
function putChunk(
  partial: string,
  blob: Blob,
  offset: number,
  onLoaded: (bytes: number) => void,
  signal: AbortSignal,
) {
  return new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/upload/${encodeURIComponent(partial)}/chunk?offset=${offset}`);
    xhr.upload.onprogress = (event) => onLoaded(event.loaded);
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onLoaded(blob.size); // the last progress event can lag the response
        resolve();
        return;
      }
      let detail = `HTTP ${xhr.status}`;
      try {
        detail = JSON.parse(xhr.responseText).detail ?? detail;
      } catch {
        /* non-JSON response */
      }
      reject(new Error(detail));
    };
    xhr.onerror = () => reject(new Error("connection lost"));
    xhr.onabort = () => reject(new Error("aborted"));
    signal.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(blob);
  });
}

/**
 * Send one file in pieces and give back the name it landed under.
 *
 * The server decides completeness, not this: it counts the ranges it received and
 * refuses to rename a file with a hole in it. So a piece lost on a flaky link fails
 * here, loudly, instead of producing a 4 GB rush that only breaks later in a merge.
 */
async function putFile(file: File, onProgress: (ratio: number) => void, signal: AbortSignal) {
  const { partial } = await api.uploadBegin(file);

  const offsets: number[] = [];
  for (let at = 0; at < file.size; at += CHUNK) offsets.push(at);
  const loaded = new Array<number>(offsets.length).fill(0);
  const report = () => onProgress(loaded.reduce((sum, n) => sum + n, 0) / file.size);

  // Chained to the batch signal, so a piece that fails cancels this file's other
  // pieces without touching the files queued behind it. Aborting the batch
  // controller here would stop the whole drop.
  const local = new AbortController();
  const relay = () => local.abort();
  signal.addEventListener("abort", relay, { once: true });

  let next = 0;
  const worker = async () => {
    for (let index = next++; index < offsets.length; index = next++) {
      const offset = offsets[index];
      const blob = file.slice(offset, Math.min(offset + CHUNK, file.size));
      const track = (bytes: number) => {
        loaded[index] = bytes;
        report();
      };
      try {
        await putChunk(partial, blob, offset, track, local.signal);
      } catch (error) {
        // One retry. On a tunnel that drops, losing a 64 MiB piece must not cost
        // the whole file, and the server takes the same range twice as one write.
        if (local.signal.aborted) throw error;
        loaded[index] = 0;
        await putChunk(partial, blob, offset, track, local.signal);
      }
    }
  };

  try {
    await Promise.all(
      Array.from({ length: Math.min(CONCURRENCY, offsets.length) }, () => worker()),
    );
    const done = await api.uploadFinish(partial);
    return done.filename;
  } catch (error) {
    local.abort();
    // Leave nothing behind: a partial nobody points at would sit in inbox/ forever,
    // invisible to the scanner and to us.
    void api.uploadAbort(partial).catch(() => {});
    throw error;
  } finally {
    signal.removeEventListener("abort", relay);
  }
}

export function accepted(file: File) {
  return ACCEPTED.some((ext) => file.name.toLowerCase().endsWith(ext));
}

export function UploadProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [items, setItems] = useState<Item[]>([]);
  const [running, setRunning] = useState(0);
  /** One per batch. Stop has to reach every batch in flight, not just the last. */
  const controllers = useRef(new Set<AbortController>());

  const patch = useCallback((key: string, changes: Partial<Item>) => {
    setItems((previous) => previous.map((it) => (it.key === key ? { ...it, ...changes } : it)));
  }, []);

  const send = useCallback(
    (files: File[]) => {
      const usable = files.filter(accepted);
      const rejected = files.length - usable.length;
      if (rejected > 0) {
        toast.error(`${rejected} file(s) skipped: only ${ACCEPTED.join(" and ")} are accepted`);
      }
      if (usable.length === 0) return;

      const queued: Item[] = usable.map((file, index) => ({
        key: `${file.name}-${file.size}-${Date.now()}-${index}`,
        file,
        status: "queued",
        progress: 0,
      }));
      setItems((previous) => [...previous, ...queued]);

      const controller = new AbortController();
      controllers.current.add(controller);
      setRunning((n) => n + 1);

      void (async () => {
        let sent = 0;
        let skipped = 0;
        // One file at a time. The concurrency lives *inside* a file instead (see
        // CONCURRENCY): several pieces of one rush fill the link just as well, and
        // they land in one preallocated file rather than scattering writes across
        // four of them.
        for (const item of queued) {
          if (controller.signal.aborted) {
            patch(item.key, { status: "error", error: "aborted" });
            continue;
          }

          // Identify the file before sending it: 2 MiB of probe bytes settle
          // whether the library already has it, sparing a pointless 4 GB upload.
          patch(item.key, { status: "checking", progress: 0 });
          try {
            const verdict = await api.uploadCheck(item.file);
            // Before the duplicate branch, not after: a file that is already
            // imported can still have left a `.partial` behind, and skipping out of
            // here would leave it on the volume forever, preallocated to its full
            // size and holding the name hostage.
            if (verdict.partial_bytes != null) {
              // Throw it away rather than resume it, and above all before sending
              // again: the server steps over an existing `.partial` when it reserves
              // a name, so leaving it there is what turns the retry into
              // `name__1.MP4`, a name `parse_filename` reads as having no timestamp
              // and no camera index. That is how one flight ended up as two rushes
              // that could not be grouped.
              await api.uploadAbort(`${item.file.name}.partial`).catch(() => {});
              patch(item.key, { discarded: verdict.partial_bytes });
            }
            if (verdict.known) {
              patch(item.key, {
                status: "skipped",
                progress: 1,
                known: verdict.filename ?? undefined,
              });
              skipped += 1;
              continue;
            }
          } catch {
            // A failed check must not block the import: fall through and upload,
            // the scanner still catches duplicates on ingestion.
          }

          patch(item.key, { status: "uploading", progress: 0 });
          try {
            const landedAs = await putFile(
              item.file,
              (ratio) => patch(item.key, { progress: ratio }),
              controller.signal,
            );
            patch(item.key, { status: "done", progress: 1, landedAs });
            sent += 1;
          } catch (error) {
            patch(item.key, {
              status: "error",
              error: error instanceof Error ? error.message : "failed",
            });
          }
        }

        controllers.current.delete(controller);
        setRunning((n) => Math.max(0, n - 1));

        if (sent === 0) {
          if (skipped > 0) {
            toast.info(skipped === 1 ? "Already imported" : `${skipped} files already imported`);
          }
          return;
        }

        // The files are in inbox/: trigger the scan right away rather than waiting
        // for the worker's next tick.
        try {
          const scan = await api.scan();
          const parts: string[] = [];
          if (scan.ingested.length) parts.push(`${scan.ingested.length} ingested`);
          if (scan.sequences.length) parts.push(`${scan.sequences.length} sequence(s)`);
          if (scan.duplicates.length) parts.push(`${scan.duplicates.length} duplicate(s) skipped`);
          // A Gyroflow output dropped back in, recognized by its name. Worth saying out
          // loud, since the file is set aside and nothing else will happen to it.
          if (scan.rejected.length)
            parts.push(`${scan.rejected.length} already stabilized, set aside`);
          if (scan.failed.length) parts.push(`${scan.failed.length} unreadable`);
          if (skipped) parts.push(`${skipped} already imported`);
          toast.success(`${sent} file(s) uploaded: ${parts.join(", ") || "nothing new"}`);
        } catch (error) {
          toast.error(
            `Upload done but the scan failed: ${
              error instanceof Error ? error.message : "error"
            }`,
          );
        }
        queryClient.invalidateQueries();
      })();
    },
    [patch, queryClient],
  );

  const cancel = useCallback(() => {
    for (const controller of controllers.current) controller.abort();
  }, []);

  const clear = useCallback(() => setItems([]), []);

  // Every source file the library knows about. Same query key as the rush table,
  // so this rides its cache rather than adding a request.
  const { data: sequences } = useQuery({
    queryKey: ["sequences"],
    queryFn: () => api.sequences(),
    refetchInterval: 3_000,
  });
  const landed = useMemo(
    () => new Set((sequences ?? []).flatMap((sequence) => sequence.part_names)),
    [sequences],
  );

  // A finished transfer whose file now shows up in the rush list below has nothing
  // left to say: drop its line. Skipped files stay, since their line *is* the reason
  // nothing was sent, and pruning it would leave that unexplained.
  useEffect(() => {
    setItems((previous) => {
      const kept = previous.filter(
        (it) => !(it.status === "done" && landed.has(it.landedAs ?? it.file.name)),
      );
      return kept.length === previous.length ? previous : kept;
    });
  }, [landed]);

  // Navigating away is safe, closing the tab is not: the transfer dies with the
  // page. Chrome shows its own wording, we only get to ask.
  const busy = running > 0;
  useEffect(() => {
    if (!busy) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [busy]);

  const value = useMemo<Upload>(() => {
    const moving = items.filter((it) => it.status !== "skipped");
    return {
      items,
      busy,
      moved: moving.reduce((sum, it) => sum + it.file.size * it.progress, 0),
      total: moving.reduce((sum, it) => sum + it.file.size, 0),
      send,
      cancel,
      clear,
    };
  }, [items, busy, send, cancel, clear]);

  return <UploadContext.Provider value={value}>{children}</UploadContext.Provider>;
}
