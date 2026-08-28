import { useRef, useState } from "react";
import { CheckCircle2, CircleAlert, Loader2, SkipForward, Upload, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { formatBytes } from "@/lib/format";
import { ACCEPTED, useUpload } from "@/lib/upload";
import { cn } from "@/lib/utils";

/** The drop target and the list of what is moving. The work is in `lib/upload`. */
export function UploadZone() {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const { items, busy, moved, total, send, cancel, clear } = useUpload();

  const moving = items.filter((it) => it.status !== "skipped");
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
            send(Array.from(event.dataTransfer.files));
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
              send(Array.from(event.target.files ?? []));
              event.target.value = "";
            }}
          />
        </div>

        {items.length > 0 && (
          <div className="space-y-2">
            <div className="flex items-center justify-between text-sm text-muted-foreground">
              <span>
                {items.filter((it) => it.status === "done").length}/{moving.length} uploaded ·{" "}
                {formatBytes(moved)} / {formatBytes(total)}
                {skippedCount > 0 && ` · ${skippedCount} already imported`}
              </span>
              <span className="flex items-center gap-2">
                {busy ? (
                  <Button size="sm" variant="ghost" onClick={cancel}>
                    Stop
                  </Button>
                ) : (
                  <Button size="sm" variant="ghost" onClick={clear}>
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
                    {item.discarded != null && item.status !== "skipped" && (
                      <span className="text-sm text-muted-foreground">
                        an interrupted upload of {formatBytes(item.discarded)} was
                        discarded, sending again
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
          </div>
        )}
      </CardContent>
    </Card>
  );
}
