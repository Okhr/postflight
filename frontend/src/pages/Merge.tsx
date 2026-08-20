import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link2, Scissors } from "lucide-react";
import { toast } from "sonner";

import { StateBadge } from "@/components/StateBadge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, type Sequence } from "@/lib/api";
import { formatBytes, formatDateTime, formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Seconds between the end of one rush and the start of the next, which is what the
 *  automatic chaining reads. Below a second is a camera closing a file; above it is
 *  someone landing and taking off again. */
function gapSeconds(before: Sequence, after: Sequence): number | null {
  if (!before.recorded_at || !after.recorded_at) return null;
  const end = Date.parse(before.recorded_at) + before.duration_ms;
  return (Date.parse(after.recorded_at) - end) / 1000;
}

export function Merge() {
  const queryClient = useQueryClient();
  const [picked, setPicked] = useState<number[]>([]);
  const [splitting, setSplitting] = useState<Sequence | null>(null);

  const { data: sequences } = useQuery({
    queryKey: ["sequences"],
    queryFn: () => api.sequences(),
    refetchInterval: 3_000,
  });

  const done = (message: string) => {
    queryClient.invalidateQueries();
    setPicked([]);
    setSplitting(null);
    toast.success(message);
  };
  const failed = (error: Error) => toast.error(error.message);

  const join = useMutation({
    mutationFn: () => api.regroupSequences(picked, true),
    onSuccess: (seq) => done(`Joined into ${seq.label}`),
    onError: failed,
  });
  const split = useMutation({
    mutationFn: () => api.splitSequence(splitting?.id ?? 0, true),
    onSuccess: (parts) => done(`Split into ${parts.length} rushes`),
    onError: failed,
  });

  // Oldest first: the order parts were shot in is the order a wrong group is read in.
  const rows = [...(sequences ?? [])].sort((a, b) =>
    (a.recorded_at ?? "").localeCompare(b.recorded_at ?? ""),
  );
  const toggle = (id: number) =>
    setPicked((previous) =>
      previous.includes(id) ? previous.filter((x) => x !== id) : [...previous, id],
    );

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-4 pb-3">
          <CardTitle className="text-base">Groups</CardTitle>
          <Button
            size="sm"
            disabled={picked.length < 2 || join.isPending}
            onClick={() => join.mutate()}
          >
            <Link2 className="mr-2 h-4 w-4" />
            Join {picked.length > 1 ? `${picked.length} rushes` : ""}
          </Button>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead>Rush</TableHead>
                <TableHead>Parts</TableHead>
                <TableHead className="text-right">Start</TableHead>
                <TableHead className="text-right">Duration</TableHead>
                <TableHead className="text-right">Gap</TableHead>
                <TableHead className="text-right">Size</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((sequence, index) => {
                const gap = index > 0 ? gapSeconds(rows[index - 1], sequence) : null;
                return (
                  <TableRow key={sequence.id} className={cn(picked.includes(sequence.id) && "bg-accent/50")}>
                    <TableCell>
                      <input
                        type="checkbox"
                        checked={picked.includes(sequence.id)}
                        onChange={() => toggle(sequence.id)}
                        aria-label={`Pick ${sequence.label}`}
                        className="h-3.5 w-3.5 accent-primary"
                      />
                    </TableCell>
                    <TableCell className="font-medium">
                      {sequence.label}
                      {sequence.state !== "ready" && (
                        <span className="ml-2">
                          <StateBadge state={sequence.state} />
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {sequence.part_names.join(", ")}
                    </TableCell>
                    <TableCell className="tnum text-right text-sm">
                      {formatDateTime(sequence.recorded_at)}
                    </TableCell>
                    <TableCell className="tnum text-right text-sm">
                      {formatDuration(sequence.duration_ms)}
                    </TableCell>
                    <TableCell
                      className={cn(
                        "tnum text-right text-sm",
                        gap !== null && gap >= 0 && gap <= 1
                          ? "text-amber-400"
                          : "text-muted-foreground",
                      )}
                    >
                      {gap === null ? "-" : `${gap.toFixed(2)} s`}
                    </TableCell>
                    <TableCell className="tnum text-right text-sm">
                      {formatBytes(sequence.size_bytes)}
                    </TableCell>
                    <TableCell className="text-right">
                      {sequence.part_count > 1 && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => setSplitting(sequence)}
                          aria-label={`Split ${sequence.label}`}
                        >
                          <Scissors className="h-4 w-4" />
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
              {rows.length === 0 && (
                <TableRow>
                  <TableCell colSpan={8} className="py-8 text-center text-sm text-muted-foreground">
                    Nothing imported yet.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Dialog open={splitting !== null} onOpenChange={(open) => !open && setSplitting(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Split {splitting?.label}?</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            Its {splitting?.part_count} parts each become a rush of their own. The joined
            file and its proxy are deleted, and every zone marked on it goes with them.
            The masters are untouched.
          </p>
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setSplitting(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              size="sm"
              disabled={split.isPending}
              onClick={() => split.mutate()}
            >
              Split
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
