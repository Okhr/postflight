import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Pencil, Plus, RotateCcw, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, type Template, type TemplateDefaults, type TemplateSettings } from "@/lib/api";

/**
 * The Gyroflow templates: what a render comes out as.
 *
 * A template is a partial Gyroflow project, and this form edits seven of its
 * settings. The file holds a good deal more (per-axis smoothing, adaptive zoom,
 * encoder options), which is why a save patches rather than rewrites.
 */
const TEMPLATES = ["templates"] as const;

/** Slider ranges, narrower than what the API accepts. Every default here is
 *  Gyroflow's own, fetched rather than written down. */
const DIALS = [
  {
    key: "smoothness",
    label: "Smoothness",
    min: 0,
    max: 1,
    step: 0.05,
    format: (v: number) => v.toFixed(2),
  },
  {
    key: "horizon_lock",
    label: "Horizon lock",
    min: 0,
    max: 100,
    step: 5,
    format: (v: number) => `${Math.round(v)} %`,
  },
  {
    key: "lens_correction",
    label: "Lens correction",
    min: 0,
    max: 1,
    step: 0.05,
    format: (v: number) => v.toFixed(2),
  },
  {
    key: "fov",
    label: "FOV",
    min: 0.5,
    max: 2,
    step: 0.05,
    format: (v: number) => v.toFixed(2),
  },
  {
    key: "frame_offset_x",
    label: "Frame offset X",
    min: -1,
    max: 1,
    step: 0.05,
    format: (v: number) => v.toFixed(2),
  },
  {
    key: "frame_offset_y",
    label: "Frame offset Y",
    min: -1,
    max: 1,
    step: 0.05,
    format: (v: number) => v.toFixed(2),
  },
] as const;

type DialKey = (typeof DIALS)[number]["key"];

export function TemplatesCard() {
  const queryClient = useQueryClient();
  const { data: templates } = useQuery({ queryKey: TEMPLATES, queryFn: api.templates });
  const { data: defaults } = useQuery({
    queryKey: ["template-defaults"],
    queryFn: api.templateDefaults,
    staleTime: Infinity,
  });

  const [editing, setEditing] = useState<Template | null>(null);
  const [naming, setNaming] = useState<{ copyOf?: string } | null>(null);
  const [dropping, setDropping] = useState<Template | null>(null);

  const done = () => queryClient.invalidateQueries({ queryKey: TEMPLATES });

  const create = useMutation({
    mutationFn: ({ label, copyOf }: { label: string; copyOf?: string }) =>
      api.createTemplate(label, copyOf),
    onSuccess: (created) => {
      done();
      setNaming(null);
      setEditing(created);
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const drop = useMutation({
    mutationFn: (id: string) => api.deleteTemplate(id),
    onSuccess: (result) => {
      done();
      setDropping(null);
      toast.success(result.outcome === "reset" ? `${result.template} reset` : `${result.template} deleted`);
    },
    onError: (error: Error) => toast.error(error.message),
  });

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2 pb-2">
        <CardTitle className="text-sm">Templates</CardTitle>
        <Button size="sm" variant="outline" onClick={() => setNaming({})}>
          <Plus className="h-4 w-4" />
          New
        </Button>
      </CardHeader>
      <CardContent className="px-0 pb-2">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="pl-6">Name</TableHead>
              <TableHead>Size</TableHead>
              <TableHead>Codec</TableHead>
              <TableHead className="text-right">Smoothing</TableHead>
              <TableHead className="text-right">Horizon</TableHead>
              <TableHead className="w-28" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {(templates ?? []).map((template) => (
              <TableRow key={template.id}>
                <TableCell className="pl-6 text-sm">{template.label}</TableCell>
                <TableCell className="tnum whitespace-nowrap text-sm">
                  {template.width}×{template.height}
                  <span className="pl-2 text-muted-foreground">{template.aspect}</span>
                </TableCell>
                <TableCell className="whitespace-nowrap text-sm">
                  {template.codec}
                  <span className="tnum pl-2 text-muted-foreground">
                    {Math.round(template.bitrate)} Mb/s
                  </span>
                </TableCell>
                <TableCell className="tnum text-right text-sm">
                  {template.smoothness.toFixed(2)}
                </TableCell>
                <TableCell className="tnum text-right text-sm">
                  {Math.round(template.horizon_lock)} %
                </TableCell>
                <TableCell className="pr-3">
                  <div className="flex justify-end gap-0.5">
                    <Button
                      size="icon"
                      variant="ghost"
                      title="Edit"
                      className="h-7 w-7 text-muted-foreground hover:text-foreground"
                      onClick={() => setEditing(template)}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      size="icon"
                      variant="ghost"
                      title="Duplicate"
                      className="h-7 w-7 text-muted-foreground hover:text-foreground"
                      onClick={() => setNaming({ copyOf: template.id })}
                    >
                      <Copy className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      size="icon"
                      variant="ghost"
                      // A bundled template's file is inside the image, so it cannot go:
                      // dropping the edited copy is the only thing delete can mean here.
                      title={template.bundled ? "Reset to the shipped version" : "Delete"}
                      className="h-7 w-7 text-muted-foreground hover:text-foreground"
                      onClick={() => setDropping(template)}
                    >
                      {template.bundled ? (
                        <RotateCcw className="h-3.5 w-3.5" />
                      ) : (
                        <Trash2 className="h-3.5 w-3.5" />
                      )}
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>

      <EditDialog
        template={editing}
        defaults={defaults}
        onClose={() => setEditing(null)}
        onSaved={done}
      />

      <NameDialog
        open={naming !== null}
        copying={templates?.find((t) => t.id === naming?.copyOf)?.label}
        pending={create.isPending}
        onClose={() => setNaming(null)}
        onSubmit={(label) => create.mutate({ label, copyOf: naming?.copyOf })}
      />

      <Dialog open={dropping !== null} onOpenChange={(open) => !open && setDropping(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>
              {dropping?.bundled ? `Reset ${dropping?.label}?` : `Delete ${dropping?.label}?`}
            </DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            {dropping?.bundled
              ? "It goes back to the version shipped with the image. Your changes to it are lost."
              : "Renders already made from it are untouched."}
          </p>
          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setDropping(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              size="sm"
              disabled={drop.isPending}
              onClick={() => dropping && drop.mutate(dropping.id)}
            >
              {dropping?.bundled ? "Reset" : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

/** Naming a new template, or a copy of one. */
function NameDialog({
  open,
  copying,
  pending,
  onClose,
  onSubmit,
}: {
  open: boolean;
  copying?: string;
  pending: boolean;
  onClose: () => void;
  onSubmit: (label: string) => void;
}) {
  const [draft, setDraft] = useState("");

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
        setDraft("");
      }}
    >
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{copying ? `Copy of ${copying}` : "New template"}</DialogTitle>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (draft.trim()) onSubmit(draft.trim());
          }}
        >
          <Input
            autoFocus
            placeholder="Vertical 4K"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
          <DialogFooter>
            <Button type="submit" size="sm" disabled={!draft.trim() || pending}>
              Create
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function EditDialog({
  template,
  defaults,
  onClose,
  onSaved,
}: {
  template: Template | null;
  defaults?: TemplateDefaults;
  onClose: () => void;
  onSaved: () => void;
}) {
  return (
    <Dialog open={template !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{template?.label}</DialogTitle>
        </DialogHeader>
        {template && (
          <EditForm
            key={template.id}
            template={template}
            defaults={defaults}
            onClose={onClose}
            onSaved={onSaved}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

/** Mounted with the dialog, so it always starts from what is stored. */
function EditForm({
  template,
  defaults,
  onClose,
  onSaved,
}: {
  template: Template;
  defaults?: TemplateDefaults;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState({
    label: template.label,
    description: template.description,
    width: String(template.width),
    height: String(template.height),
    codec: template.codec,
    bitrate: String(Math.round(template.bitrate)),
    smoothness: template.smoothness,
    horizon_lock: template.horizon_lock,
    lens_correction: template.lens_correction,
    fov: template.fov,
    frame_offset_x: template.frame_offset_x,
    frame_offset_y: template.frame_offset_y,
  });

  const save = useMutation({
    mutationFn: (settings: TemplateSettings) => api.updateTemplate(template.id, settings),
    onSuccess: () => {
      onSaved();
      onClose();
    },
    onError: (error: Error) => toast.error(error.message),
  });

  const dial = (key: DialKey, value: number) => setForm((f) => ({ ...f, [key]: value }));

  /** Back to what Gyroflow starts from. Name, dimensions and bitrate are left alone:
   *  Gyroflow derives those from the source file, so they have no default here. */
  const reset = () => {
    if (!defaults) return;
    setForm((f) => ({
      ...f,
      codec: defaults.codec,
      smoothness: defaults.smoothness,
      horizon_lock: defaults.horizon_lock,
      lens_correction: defaults.lens_correction,
      fov: defaults.fov,
      frame_offset_x: defaults.frame_offset_x,
      frame_offset_y: defaults.frame_offset_y,
    }));
  };

  return (
    <form
      className="space-y-5"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate({
          label: form.label.trim() || template.label,
          description: form.description,
          width: Number(form.width),
          height: Number(form.height),
          codec: form.codec,
          bitrate: Number(form.bitrate),
          smoothness: form.smoothness,
          horizon_lock: form.horizon_lock,
          lens_correction: form.lens_correction,
          fov: form.fov,
          frame_offset_x: form.frame_offset_x,
          frame_offset_y: form.frame_offset_y,
        });
      }}
    >
      <div className="space-y-2">
        <Input
          value={form.label}
          onChange={(event) => setForm((f) => ({ ...f, label: event.target.value }))}
        />
        <Input
          placeholder="What it is for"
          value={form.description}
          onChange={(event) => setForm((f) => ({ ...f, description: event.target.value }))}
        />
      </div>

      <div className="space-y-3">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Output</p>
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1.5">
            <Label className="text-xs">Width</Label>
            <Input
              className="tnum w-20"
              value={form.width}
              onChange={(event) => setForm((f) => ({ ...f, width: event.target.value }))}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">Height</Label>
            <Input
              className="tnum w-20"
              value={form.height}
              onChange={(event) => setForm((f) => ({ ...f, height: event.target.value }))}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">Codec</Label>
            <Select
              value={form.codec}
              onValueChange={(value) => setForm((f) => ({ ...f, codec: value }))}
            >
              <SelectTrigger className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(defaults?.codecs ?? [form.codec]).map((codec) => (
                  <SelectItem key={codec} value={codec}>
                    {codec}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">Mb/s</Label>
            <Input
              className="tnum w-20"
              value={form.bitrate}
              onChange={(event) => setForm((f) => ({ ...f, bitrate: event.target.value }))}
            />
          </div>
        </div>
      </div>

      <div className="space-y-3">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Image</p>
        {DIALS.map((entry) => (
          <div key={entry.key} className="flex items-center gap-3">
            <Label className="w-32 shrink-0 text-xs">{entry.label}</Label>
            <Slider
              className="flex-1"
              min={entry.min}
              max={entry.max}
              step={entry.step}
              value={[form[entry.key]]}
              onValueChange={([value]) => dial(entry.key, value)}
            />
            <span className="tnum w-14 shrink-0 text-right text-sm text-muted-foreground">
              {entry.format(form[entry.key])}
            </span>
          </div>
        ))}
      </div>

      <DialogFooter className="sm:justify-between">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={!defaults}
          title="Name, dimensions and bitrate are left alone: Gyroflow derives those from the source"
          onClick={reset}
        >
          Gyroflow defaults
        </Button>
        <Button type="submit" size="sm" disabled={save.isPending}>
          Save
        </Button>
      </DialogFooter>
    </form>
  );
}
