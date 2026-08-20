import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleAlert, Loader2, SkipForward, Upload, X } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

const ACCEPTED = [".mp4", ".mov"];

type ItemStatus = "queued" | "checking" | "skipped" | "uploading" | "done" | "error";

interface Item {
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

function accepted(file: File) {
  return ACCEPTED.some((ext) => file.name.toLowerCase().endsWith(ext));
}

export function UploadZone() {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);

  const patch = useCallback((key: string, changes: Partial<Item>) => {
    setItems((previous) => previous.map((it) => (it.key === key ? { ...it, ...changes } : it)));
  }, []);

  const send = useCallback(
    async (files: File[]) => {
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
      abortRef.current = controller;
      setBusy(true);

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

      setBusy(false);
      abortRef.current = null;

      if (sent === 0) {
        if (skipped > 0) {
          toast.info(
            skipped === 1
              ? "Already imported"
              : `${skipped} files already imported`,
          );
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
        if (scan.rejected.length) parts.push(`${scan.rejected.length} already stabilized, set aside`);
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
    },
    [patch, queryClient],
  );

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

  const pending = items.filter(
    (it) => it.status === "uploading" || it.status === "queued" || it.status === "checking",
  );
  // Skipped files never travel, so they must not count towards the transfer.
  const moving = items.filter((it) => it.status !== "skipped");
  const totalBytes = moving.reduce((sum, it) => sum + it.file.size, 0);
  const doneBytes = moving.reduce((sum, it) => sum + it.file.size * it.progress, 0);
  const skippedCount = items.length - moving.length;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="text-base">Drop rushes</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div
          role="button"
          tabIndex={0}
          onClick={() => inputRef.current?.click()}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") inputRef.current?.click();
          }}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            void send(Array.from(event.dataTransfer.files));
          }}
          className={cn(
            "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors",
            dragging
              ? "border-primary bg-accent"
              : "border-border hover:border-primary/50 hover:bg-accent/40",
          )}
        >
          <Upload className="h-6 w-6 text-muted-foreground" />
          <p className="text-sm">
            Drop files here, or <span className="underline">pick them</span>
          </p>
          <p className="text-sm text-muted-foreground">{ACCEPTED.join(" · ")}</p>
          <input
            ref={inputRef}
            type="file"
            multiple
            accept={ACCEPTED.join(",")}
            className="hidden"
            onChange={(event) => {
              void send(Array.from(event.target.files ?? []));
              event.target.value = "";
            }}
          />
        </div>

        {items.length > 0 && (
          <div className="space-y-2">
            <div className="flex items-center justify-between text-sm text-muted-foreground">
              <span>
                {items.filter((it) => it.status === "done").length}/{moving.length} uploaded ·{" "}
                {formatBytes(doneBytes)} / {formatBytes(totalBytes)}
                {skippedCount > 0 && ` · ${skippedCount} already imported`}
              </span>
              <span className="flex items-center gap-2">
                {busy && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => abortRef.current?.abort()}
                  >
                    Stop
                  </Button>
                )}
                {!busy && (
                  <Button size="sm" variant="ghost" onClick={() => setItems([])}>
                    Clear list
                  </Button>
                )}
              </span>
            </div>

            <ul className="divide-y rounded-md border">
              {items.map((item) => (
                <li key={item.key} className="flex items-center gap-3 px-3 py-2">
                  <span className="shrink-0">
                    {item.status === "done" && (
                      <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                    )}
                    {item.status === "skipped" && (
                      <SkipForward className="h-4 w-4 text-muted-foreground" />
                    )}
                    {item.status === "error" && <CircleAlert className="h-4 w-4 text-red-400" />}
                    {(item.status === "uploading" || item.status === "checking") && (
                      <Loader2 className="h-4 w-4 animate-spin text-sky-400" />
                    )}
                    {item.status === "queued" && <X className="h-4 w-4 text-muted-foreground" />}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span
                      className={cn(
                        "block truncate text-sm",
                        item.status === "skipped" && "text-muted-foreground line-through",
                      )}
                    >
                      {item.file.name}
                    </span>
                    {item.status === "uploading" && (
                      <Progress value={item.progress * 100} className="mt-1 h-1" />
                    )}
                    {item.status === "skipped" && (
                      <span className="text-sm text-muted-foreground">
                        already imported
                        {item.known && item.known !== item.file.name && ` as ${item.known}`}
                      </span>
                    )}
                    {item.error && <span className="text-sm text-red-400">{item.error}</span>}
                  </span>
                  <span className="tnum shrink-0 text-sm text-muted-foreground">
                    {item.status === "uploading" && `${Math.round(item.progress * 100)} %`}
                    {item.status === "checking" && "checking…"}
                    {item.status !== "uploading" &&
                      item.status !== "checking" &&
                      formatBytes(item.file.size)}
                  </span>
                </li>
              ))}
            </ul>

            {pending.length > 0 && (
              <p className="text-sm text-muted-foreground">
                Keep this tab open while uploading.
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
