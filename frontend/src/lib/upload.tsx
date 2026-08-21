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
 * Raw PUT rather than FormData: XMLHttpRequest is the only way to get upload
 * progress (fetch does not expose it), and a raw body spares the server from
 * copying 4 GB into a temporary file.
 */
function putFile(file: File, onProgress: (ratio: number) => void, signal: AbortSignal) {
  return new Promise<string>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/upload/${encodeURIComponent(file.name)}`);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        // The server may have renamed on collision: its name is the one that
        // will show up in the library, so it is the one worth keeping.
        let landedAs = file.name;
        try {
          landedAs = JSON.parse(xhr.responseText).filename ?? file.name;
        } catch {
          /* non-JSON response */
        }
        resolve(landedAs);
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
    xhr.send(file);
  });
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
        // Sequential: sending four 4 GB rushes in parallel only saturates the link
        // and scatters writes across the destination disk.
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
