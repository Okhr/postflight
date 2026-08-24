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

import { DeleteDialog } from "@/components/DeleteDialog";
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

  const files = cut?.files ?? 0;
  return (
    <DeleteDialog
      open={cut !== null}
      title={`Delete ${cut?.label}?`}
      note={
        files === 0
          ? undefined
          : files === 1
            ? "The stabilized file made from it is deleted with it."
            : `The ${files} files made from it are deleted with it.`
      }
      onClose={onClose}
      onConfirm={() => {
        if (onConfirm) onConfirm();
        else if (cut?.id) remove.mutate(cut.id);
      }}
    />
  );
}
