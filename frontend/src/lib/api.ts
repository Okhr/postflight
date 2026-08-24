export type SequenceState = "new" | "merging" | "merged" | "proxying" | "ready" | "failed";
export type JobState = "queued" | "running" | "done" | "failed" | "cancelled";
export type RenderState = "queued" | "running" | "done" | "failed";

export interface Clip {
  id: number;
  filename: string;
  part_index: number;
  size_bytes: number;
  duration_ms: number;
  width: number;
  height: number;
  codec: string;
  has_gyro: boolean;
  recorded_at: string | null;
  camera_index: number | null;
  state: string;
}

/** One file made from a sequence: named by its profile, reached by its id. */
export interface QueueRender {
  id: number;
  template: string;
  /** Its graded version, when one exists. Two files, so two ways to download. */
  grade_id: number | null;
}

/** A sequence as the stabilize queue lists it, with what has been made from it. */
export interface QueueCut {
  id: number;
  label: string;
  frames: number;
  duration_ms: number;
  start_tc: string;
  end_tc: string;
  /** Profiles a finished file exists for, and profiles a job is working on. */
  done: QueueRender[];
  busy: QueueRender[];
}

export interface QueueRush {
  id: number;
  label: string;
  folder_id: number | null;
  recorded_at: string | null;
  cuts: QueueCut[];
}

export interface Cut {
  id: number;
  order_index: number;
  label: string;
  start_frame: number;
  end_frame: number;
  frames: number;
  duration_ms: number;
  start_tc: string;
  end_tc: string;
  /** A stabilized file exists for it, and a graded one on top of that. */
  rendered: boolean;
  graded: boolean;
  /** How many files exist because of it. Deleting it deletes them. */
  files: number;
}

export interface Render {
  id: number;
  sequence_id: number;
  sequence_key: string;
  /** What the interface says: the rush's name, and the sequence's when there is one. */
  sequence_label: string;
  cut_label: string;
  cut_id: number | null;
  /** Where its rush sits, so a list of clips groups like every other list here. */
  folder_id: number | null;
  /** From the rush's own fps, not from a frame count over an assumed 60. */
  duration_ms: number;
  template: string;
  state: RenderState;
  progress: number;
  start_frame: number;
  end_frame: number;
  out_name: string | null;
  size_bytes: number | null;
  error: string | null;
  processing_device: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Folder {
  id: number;
  name: string;
  /** Palette token from `lib/colors`, drawn when the folder is created. */
  color: string;
  parent_id: number | null;
  /** Rank among its siblings, dense from 0. */
  position: number;
  /** Rushes filed directly here, not counting a child folder's. */
  sequence_count: number;
}

export interface Sequence {
  id: number;
  key: string;
  label: string;
  /** Which folder it is filed in, null for none, which is where they start. */
  folder_id: number | null;
  state: SequenceState;
  /** Marked by hand when there is nothing left to do on it. Never deduced from the
   *  cuts: a rush worth nothing is derushed with none at all. */
  derushed: boolean;
  part_count: number;
  width: number;
  height: number;
  fps: number;
  fps_num: number;
  fps_den: number;
  duration_ms: number;
  frame_count: number;
  size_bytes: number;
  recorded_at: string | null;
  has_proxy: boolean;
  has_filmstrip: boolean;
  proxy_width: number;
  proxy_height: number;
  cut_count: number;
  render_count: number;
  has_gyro: boolean;
  part_names: string[];
  merged_name: string | null;
  error: string | null;
  duration_tc: string;
}

export interface SequenceDetail extends Sequence {
  clips: Clip[];
  cuts: Cut[];
  renders: Render[];
}

export interface GradeParams {
  exposure: number;
  contrast: number;
  saturation: number;
  temperature: number;
  shadows: number;
  highlights: number;
  auto_levels: boolean;
}

export interface GradeAnalysis {
  frames: number;
  y_low: number;
  y_high: number;
  y_avg: number;
  sat_avg: number;
  clipped_black: number;
  clipped_white: number;
  looks_log: boolean;
  darkest_ms: number;
  median_ms: number;
  brightest_ms: number;
  headroom_low: number;
  headroom_high: number;
}

export interface Grade {
  id: number;
  render_id: number;
  sequence_id: number;
  sequence_key: string;
  render_name: string | null;
  state: "draft" | "queued" | "running" | "done" | "failed";
  progress: number;
  params: GradeParams;
  analysis: Partial<GradeAnalysis>;
  /** What auto-levels resolves to, as [low, gain], or null when it does nothing.
   *  Decided on the server so the browser preview never reasons about it twice. */
  levels: [number, number] | null;
  out_name: string | null;
  size_bytes: number | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export const NEUTRAL_GRADE: GradeParams = {
  exposure: 0,
  contrast: 1,
  saturation: 1,
  temperature: 6500,
  shadows: 0,
  highlights: 0,
  auto_levels: false,
};

export interface Job {
  id: number;
  kind: string;
  state: JobState;
  progress: number;
  message: string;
  error: string | null;
  sequence_id: number | null;
  sequence_key: string | null;
  /** A merge or a proxy names the rush; a render or a grade names its sequence too. */
  sequence_label: string | null;
  cut_label: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Template {
  id: string;
  label: string;
  description: string;
  /** Derived from the dimensions by the API, never stored. */
  aspect: string;
  width: number;
  height: number;
  /** Ships inside the image: editable and resettable, never deletable. */
  bundled: boolean;
  codec: string;
  bitrate: number;
  smoothness: number;
  horizon_lock: number;
  lens_correction: number;
  frame_offset_x: number;
  frame_offset_y: number;
  fov: number;
}

/** The seven settings, as the edit form holds them. */
export type TemplateSettings = Partial<
  Pick<
    Template,
    | "label"
    | "description"
    | "width"
    | "height"
    | "codec"
    | "bitrate"
    | "smoothness"
    | "horizon_lock"
    | "lens_correction"
    | "frame_offset_x"
    | "frame_offset_y"
    | "fov"
  >
>;

/** Gyroflow's own starting values. No width, height or bitrate: it derives those
 *  from the source file, so they have no default in a template. */
export interface TemplateDefaults {
  codecs: string[];
  codec: string;
  smoothness: number;
  horizon_lock: number;
  lens_correction: number;
  frame_offset_x: number;
  frame_offset_y: number;
  fov: number;
}

export interface OpenCLDevice {
  platform: string;
  name: string;
  /** "GPU" | "CPU" | "ACCELERATOR" | "other" */
  kind: string;
}

/**
 * What a worker measured on its own machine, by running the thing rather than by
 * reading a driver list. The API has no such block of its own: it never decodes,
 * warps or merges anything, so its hardware says nothing useful.
 */
export interface WorkerCapabilities {
  /** What the probe settled on: "cuda", "vaapi" or "cpu". */
  decode_backend: string;
  decode_device: string | null;
  /** Backend → why it was refused. An empty string means it works. */
  decode_probes: Record<string, string>;
  ffmpeg_version: string;
  gyroflow_version: string;
  mp4_merge_available: boolean;
  dri_devices: string[];
  nvidia_present: boolean;
  opencl_icds: string[];
  opencl_devices: OpenCLDevice[];
  /** The OpenCL GPU Gyroflow will warp on, null when there is only a CPU one. */
  opencl_gpu: string | null;
  /** Vulkan GPUs, Gyroflow's wgpu fallback when no OpenCL GPU exists. */
  vulkan_devices: string[];
  /** What Gyroflow will most likely warp on, named. */
  stabilize_device: string;
  stabilize_on_gpu: boolean;
  notes: string[];
}

/** One rate per job kind, in the kind's own unit. Null when it could not be measured,
 *  which is not the same as slow: the dispatcher treats unknown as unknown. */
export interface WorkerRates {
  clip?: string;
  clip_frames?: number;
  merge_mbps: number | null;
  proxy_fps: number | null;
  render_fps: number | null;
  grade_fps: number | null;
  /** Megabytes per second pulled from the dispatcher. */
  link_mbps: number | null;
  elapsed_s?: number;
  notes?: string[];
  measured_at?: string;
}

export interface WorkerInfo {
  id: number;
  name: string;
  capabilities: WorkerCapabilities;
  /** The startup benchmark: real jobs on half a second of baked-in rush. */
  rates: WorkerRates;
  /** The moving average over real completed jobs, which beats the benchmark.
   *  Keys are the same, plus a `<key>_n` sample count. */
  observed: Record<string, number>;
  /** Whether this worker reads the dispatcher's own volume, so nothing has to travel. */
  shares_data: boolean;
  concurrency: number;
  last_seen_at: string;
  /** A worker that stopped asking for work is gone: that is the whole health model. */
  online: boolean;
  running: number;
}

export interface Status {
  workers: WorkerInfo[];
  counts: Record<string, number>;
  inbox_pending: number;
  settings: Record<string, unknown>;
}

export interface ScanResult {
  ingested: string[];
  duplicates: string[];
  rejected: string[];
  failed: string[];
  sequences: string[];
}

export interface UploadCheck {
  fingerprint: string;
  known: boolean;
  filename: string | null;
  sequence_id: number | null;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
    } catch {
      /* non-JSON response */
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/**
 * Bytes the server hashes to identify a file: head chunk, then tail chunk. Must
 * stay in step with FINGERPRINT_CHUNK in `services/probe.py`.
 */
const FINGERPRINT_CHUNK = 1 << 20;

function probeBytes(file: File): Blob {
  const head = file.slice(0, FINGERPRINT_CHUNK);
  if (file.size <= 2 * FINGERPRINT_CHUNK) return head;
  return new Blob([head, file.slice(file.size - FINGERPRINT_CHUNK)]);
}

export const api = {
  status: () => request<Status>("/status"),
  scan: () => request<ScanResult>("/scan", { method: "POST" }),

  /** Is this file already imported? Reads 2 MiB of it, sends no more. */
  uploadCheck: (file: File) =>
    request<UploadCheck>(`/upload/check?size=${file.size}`, {
      method: "POST",
      body: probeBytes(file),
      headers: { "Content-Type": "application/octet-stream" },
    }),
  templates: () => request<Template[]>("/templates"),
  templateDefaults: () => request<TemplateDefaults>("/templates/defaults"),
  createTemplate: (label: string, copyOf?: string) =>
    request<Template>("/templates", {
      method: "POST",
      body: JSON.stringify({ label, copy_of: copyOf ?? null }),
    }),
  /** Patch: what is left out keeps its value in the file, including the settings this
   *  form never shows. */
  updateTemplate: (id: string, settings: TemplateSettings) =>
    request<Template>(`/templates/${id}`, { method: "PATCH", body: JSON.stringify(settings) }),
  /** Deletes one made here, resets one that ships with the image. */
  deleteTemplate: (id: string) =>
    request<{ template: string; outcome: "deleted" | "reset" }>(`/templates/${id}`, {
      method: "DELETE",
    }),

  sequences: (state?: string) =>
    request<Sequence[]>(`/sequences${state ? `?state=${state}` : ""}`),
  sequence: (id: number) => request<SequenceDetail>(`/sequences/${id}`),
  /** Rename a rush, file it, mark it derushed. Omitted fields stay as they are. */
  updateSequence: (
    id: number,
    changes: { label?: string; folderId?: number | null; derushed?: boolean },
  ) => {
    const query = new URLSearchParams();
    if (changes.label !== undefined) query.set("label", changes.label);
    if (changes.derushed !== undefined) query.set("derushed", String(changes.derushed));
    // 0 is how the API hears "out of every folder": a query parameter cannot carry
    // null in a way that differs from being left out.
    if (changes.folderId !== undefined) query.set("folder_id", String(changes.folderId ?? 0));
    return request<Sequence>(`/sequences/${id}?${query}`, { method: "PATCH" });
  },
  retrySequence: (id: number) => request<Sequence>(`/sequences/${id}/retry`, { method: "POST" }),
  // keepDerived: the merge and proxy carry the content hash in their name, so
  // keeping them lets the same parts be re-added later with no reprocessing.
  deleteSequence: (id: number, keepRaw = true, keepDerived = true) =>
    request<{ deleted: string; files_removed: string[] }>(
      `/sequences/${id}?keep_raw=${keepRaw}&keep_derived=${keepDerived}`,
      { method: "DELETE" },
    ),

  folders: () => request<Folder[]>("/folders"),
  createFolder: (name: string, parentId: number | null = null, color?: string) =>
    request<Folder>("/folders", {
      method: "POST",
      body: JSON.stringify({ name, parent_id: parentId, color }),
    }),
  /** Rename, recolour, reparent. Omitted fields stay as they are, and `parentId: null`
   *  is a move to the root, which is why it has to be spelled out to happen. */
  updateFolder: (
    id: number,
    changes: {
      name?: string;
      color?: string;
      parentId?: number | null;
      /** Rank to land on among the siblings it ends up with. Past the last is last. */
      position?: number;
    },
  ) =>
    request<Folder>(`/folders/${id}`, {
      method: "PATCH",
      body: JSON.stringify({
        ...(changes.name !== undefined && { name: changes.name }),
        ...(changes.color !== undefined && { color: changes.color }),
        ...(changes.parentId !== undefined && { parent_id: changes.parentId }),
        ...(changes.position !== undefined && { position: changes.position }),
      }),
    }),
  /** Nothing is lost: what was inside comes back to the root. */
  deleteFolder: (id: number) =>
    request<{ deleted: string; rushes_freed: number; folders_freed: number }>(
      `/folders/${id}`,
      { method: "DELETE" },
    ),

  /** Replace the whole list. Sending a cut's `id` back is what keeps its identity,
   *  and with it the renders that point at it. */
  saveCuts: (
    sequenceId: number,
    cuts: Array<{ id?: number; label: string; start_frame: number; end_frame: number }>,
  ) =>
    request<Cut[]>(`/sequences/${sequenceId}/cuts`, {
      method: "PUT",
      body: JSON.stringify({ cuts }),
    }),

  /** Deleting a sequence deletes what was made from it, files included. */
  deleteCut: (id: number) =>
    request<{ deleted: number; files_removed: string[] }>(`/cuts/${id}`, { method: "DELETE" }),

  /** Everything that can be stabilized, grouped as the tree draws it. One request:
   *  the page has to say what is left to do, which it cannot do a rush at a time. */
  stabilizeQueue: () => request<QueueRush[]>("/stabilize/queue"),

  createRenders: (
    sequenceId: number,
    payload: { template: string; cut_ids?: number[]; whole_sequence?: boolean; overrides?: Record<string, unknown> },
  ) =>
    request<Render[]>(`/sequences/${sequenceId}/renders`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  renders: (state?: string) => request<Render[]>(`/renders${state ? `?state=${state}` : ""}`),
  deleteRender: (id: number) => request<{ deleted: number }>(`/renders/${id}`, { method: "DELETE" }),

  grades: () => request<Grade[]>("/grades"),
  grade: (renderId: number) => request<Grade>(`/renders/${renderId}/grade`),
  saveGrade: (renderId: number, params: GradeParams) =>
    request<Grade>(`/renders/${renderId}/grade`, {
      method: "PUT",
      body: JSON.stringify({ params }),
    }),
  applyGrade: (renderId: number) =>
    request<Grade>(`/renders/${renderId}/grade/apply`, { method: "POST" }),
  deleteGrade: (gradeId: number) =>
    request<{ deleted: number }>(`/grades/${gradeId}`, { method: "DELETE" }),

  jobs: (limit = 50) => request<Job[]>(`/jobs?limit=${limit}`),
  retryJob: (id: number) => request<Job>(`/jobs/${id}/retry`, { method: "POST" }),
};

/** Query string of a look, shared by the preview and nothing else. */
export function gradeQuery(params: GradeParams, atMs: number): string {
  return new URLSearchParams({
    at_ms: String(Math.round(atMs)),
    exposure: params.exposure.toFixed(3),
    contrast: params.contrast.toFixed(3),
    saturation: params.saturation.toFixed(3),
    temperature: String(Math.round(params.temperature)),
    shadows: params.shadows.toFixed(3),
    highlights: params.highlights.toFixed(3),
    auto_levels: String(params.auto_levels),
  }).toString();
}

export const mediaUrl = {
  proxy: (sequenceId: number) => `/api/media/proxy/${sequenceId}`,
  filmstrip: (sequenceId: number) => `/api/media/filmstrip/${sequenceId}`,
  poster: (sequenceId: number) => `/api/media/poster/${sequenceId}`,
  render: (renderId: number) => `/api/media/render/${renderId}`,
  download: (renderId: number) => `/api/media/render/${renderId}/download`,
  graded: (gradeId: number) => `/api/media/graded/${gradeId}`,
  gradedDownload: (gradeId: number) => `/api/media/graded/${gradeId}/download`,
  gradePreview: (renderId: number, params: GradeParams, atMs: number) =>
    `/api/renders/${renderId}/grade/preview?${gradeQuery(params, atMs)}`,
};
