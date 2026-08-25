import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

/**
 * Renaming one thing, in a dialog.
 *
 * `value` is both the current name and the open state: null is closed. The field
 * lives in a child so that it is mounted with the dialog and starts from `value`
 * every time, rather than keeping what was typed and abandoned last time.
 */
export function RenameDialog({
  title,
  value,
  onClose,
  onRename,
  action = "Rename",
}: {
  title: string;
  value: string | null;
  onClose: () => void;
  onRename: (name: string) => void;
  /** The button's word, since the same dialog also names a thing into existence. */
  action?: string;
}) {
  return (
    <Dialog open={value !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        {value !== null && (
          <Field initial={value} onClose={onClose} onRename={onRename} action={action} />
        )}
      </DialogContent>
    </Dialog>
  );
}

function Field({
  initial,
  onClose,
  onRename,
  action,
}: {
  initial: string;
  onClose: () => void;
  onRename: (name: string) => void;
  action: string;
}) {
  const [draft, setDraft] = useState(initial);

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        const wanted = draft.trim();
        if (wanted && wanted !== initial) onRename(wanted);
        onClose();
      }}
    >
      <Input autoFocus value={draft} onChange={(event) => setDraft(event.target.value)} />
      <DialogFooter>
        <Button type="submit" size="sm" disabled={!draft.trim()}>
          {action}
        </Button>
      </DialogFooter>
    </form>
  );
}
