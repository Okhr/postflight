/**
 * Deleting a sequence, from wherever it is seen: the queue, the tree, the derush.
 *
 * One component because there is one gesture, and the whole point of it is the
 * sentence. A sequence is nothing but two frame numbers, so on its own it is the one
 * thing here that costs nothing to lose; what it made is a file, and deleting a
 * parent deletes its children. That asymmetry is exactly what a dialog is for.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api } from "@/lib/api";

export interface Doomed {
  id?: number;
  label: string;
  /** Files made from it, stabilized and graded together. Zero means no sentence. */
  files: number;
}

export function DeleteCutDialog({
  cut,
  onClose,
  onConfirm,
}: {
  cut: Doomed | null;
  onClose: () => void;
  /** The derush deletes by rewriting the list it is editing, so it brings its own. */
  onConfirm?: () => void;
}) {
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteCut(id),
    onSuccess: () => {
      for (const key of ["stabilize-queue", "sequence", "sequences", "renders", "grades"]) {
        queryClient.invalidateQueries({ queryKey: [key] });
      }
    },
    onError: (error: Error) => toast.error(error.message),
  });

  return (
    <Dialog open={cut !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Delete {cut?.label}?</DialogTitle>
        </DialogHeader>
        {cut !== null && cut.files > 0 && (
          <p className="text-sm text-muted-foreground">
            {cut.files === 1
              ? "The stabilized file made from it is deleted with it."
              : `The ${cut.files} files made from it are deleted with it.`}
          </p>
        )}
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            size="sm"
            onClick={() => {
              if (onConfirm) onConfirm();
              else if (cut?.id) remove.mutate(cut.id);
              onClose();
            }}
          >
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
