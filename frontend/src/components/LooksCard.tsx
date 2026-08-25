/**
 * The named looks, managed the way the Gyroflow profiles are.
 *
 * Same shape as TemplatesCard on the stabilize page, for the same reason: a thing you
 * name, reuse and throw away belongs in a list at the top of the page that uses it,
 * not behind a dialog. A look is six numbers, so the row can show them all and there
 * is nothing to open.
 *
 * What a look never holds is the black and white points: they are measured on one
 * clip's own range. The server drops them on the way in, so nothing here has to
 * remember that rule.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Save, Stamp, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { DeleteDialog } from "@/components/DeleteDialog";
import { RenameDialog } from "@/components/RenameDialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, type GradeParams, type Look } from "@/lib/api";

/** What the row shows of a look: only what it actually changes. */
function summary(params: Look["params"]): string {
  const parts: string[] = [];
  if (params.exposure) parts.push(`${params.exposure > 0 ? "+" : ""}${params.exposure.toFixed(2)} EV`);
  if (params.contrast !== 1) parts.push(`contrast ${params.contrast.toFixed(2)}`);
  if (params.saturation !== 1) parts.push(`sat ${params.saturation.toFixed(2)}`);
  if (params.temperature !== 6500) parts.push(`${Math.round(params.temperature)} K`);
  if (params.shadows) parts.push(`shadows ${params.shadows.toFixed(2)}`);
  if (params.highlights) parts.push(`highlights ${params.highlights.toFixed(2)}`);
  return parts.join(" · ") || "neutral";
}

export function LooksCard({
  current,
  onApply,
}: {
  /** The look being tuned on the open clip, or null when no clip is open. */
  current: GradeParams | null;
  onApply: (look: Look) => void;
}) {
  const queryClient = useQueryClient();
  const [naming, setNaming] = useState(false);
  const [renaming, setRenaming] = useState<Look | null>(null);
  const [deleting, setDeleting] = useState<Look | null>(null);

  const { data: looks } = useQuery({ queryKey: ["looks"], queryFn: api.looks });
  const done = () => queryClient.invalidateQueries({ queryKey: ["looks"] });

  const create = useMutation({
    mutationFn: (label: string) => api.createLook(label, current as GradeParams),
    onSuccess: done,
    onError: (error: Error) => toast.error(error.message),
  });
  const rename = useMutation({
    mutationFn: ({ id, label }: { id: number; label: string }) => api.updateLook(id, { label }),
    onSuccess: done,
    onError: (error: Error) => toast.error(error.message),
  });
  const overwrite = useMutation({
    mutationFn: (id: number) => api.updateLook(id, { params: current as GradeParams }),
    onSuccess: done,
    onError: (error: Error) => toast.error(error.message),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteLook(id),
    onSuccess: done,
    onError: (error: Error) => toast.error(error.message),
  });

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between pb-3">
        <CardTitle className="text-sm">Looks</CardTitle>
        {/* The quick way in: a look starts as a clip that has been tuned by eye. */}
        <span title={current ? undefined : "Open a clip and tune it first"}>
          <Button size="sm" variant="outline" disabled={!current} onClick={() => setNaming(true)}>
            <Plus className="h-4 w-4" />
            From this clip
          </Button>
        </span>
      </CardHeader>
      <CardContent>
        {(looks ?? []).length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No look yet. Tune a clip and save it here to reuse it on the others.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Settings</TableHead>
                <TableHead className="w-32" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {(looks ?? []).map((look) => (
                <TableRow key={look.id}>
                  <TableCell className="font-medium">{look.label}</TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {summary(look.params)}
                  </TableCell>
                  <TableCell className="pr-3">
                    <div className="flex justify-end gap-0.5">
                      <span title={current ? "Apply to this clip" : "Open a clip first"}>
                        <Button
                          size="icon"
                          variant="ghost"
                          disabled={!current}
                          className="h-7 w-7 text-muted-foreground hover:text-foreground"
                          onClick={() => onApply(look)}
                        >
                          {/* A stamp, not a brush: a brush and the pencil next to it
                              are the same fourteen pixels of scribble. */}
                          <Stamp className="h-3.5 w-3.5" />
                        </Button>
                      </span>
                      <span title={current ? "Store this clip's look under this name" : "Open a clip first"}>
                        <Button
                          size="icon"
                          variant="ghost"
                          disabled={!current}
                          className="h-7 w-7 text-muted-foreground hover:text-foreground"
                          onClick={() => overwrite.mutate(look.id)}
                        >
                          <Save className="h-3.5 w-3.5" />
                        </Button>
                      </span>
                      <Button
                        size="icon"
                        variant="ghost"
                        title="Rename"
                        className="h-7 w-7 text-muted-foreground hover:text-foreground"
                        onClick={() => setRenaming(look)}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        title="Delete"
                        className="h-7 w-7 text-muted-foreground hover:text-foreground"
                        onClick={() => setDeleting(look)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>

      <RenameDialog
        title="Name this look"
        action="Save"
        value={naming ? "" : null}
        onClose={() => setNaming(false)}
        onRename={(label) => create.mutate(label)}
      />
      <RenameDialog
        title="Rename look"
        value={renaming?.label ?? null}
        onClose={() => setRenaming(null)}
        onRename={(label) => renaming && rename.mutate({ id: renaming.id, label })}
      />
      <DeleteDialog
        open={deleting !== null}
        title={`Delete ${deleting?.label}?`}
        note="The clips already wearing it keep their settings: applying a look copies its numbers."
        onClose={() => setDeleting(null)}
        onConfirm={() => deleting && remove.mutate(deleting.id)}
      />
    </Card>
  );
}
