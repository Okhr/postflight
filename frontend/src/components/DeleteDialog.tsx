/**
 * The one shape a destructive confirmation takes here.
 *
 * Prose belongs in exactly this place: a delete dialog is where two outcomes differ
 * in a way the buttons cannot carry. Everywhere else the interface says it without
 * words. So this component holds the layout and lets each gesture bring its sentence,
 * or bring none when there is nothing to warn about.
 */
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function DeleteDialog({
  open,
  title,
  note,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  /** What goes with it, said only when something does. */
  note?: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        {note && <p className="text-sm text-muted-foreground">{note}</p>}
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            size="sm"
            onClick={() => {
              onConfirm();
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
