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
}

export interface Render {
  id: number;
  sequence_id: number;
  sequence_key: string;
  cut_id: number | null;
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

export interface Sequence {
  id: number;
  key: string;
  label: string;
  /** Palette token from `lib/colors`, empty when untagged. */
  color: string;
  state: SequenceState;
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
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Template {
  id: string;
  label: string;
  description: string;
  aspect: string;
  width: number;
  height: number;
}

export interface Status {
  capabilities: {
    hwaccel: string;
    ffmpeg_version: string;
    gyroflow_version: string;
    mp4_merge_available: boolean;
    dri_devices: string[];
    opencl_icds: string[];
    vaapi_decode: boolean;
    notes: string[];
  };
  counts: Record<string, number>;
  inbox_pending: number;
  settings: Record<string, unknown>;
}

export interface ScanResult {
  ingested: string[];
  duplicates: string[];
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

  sequences: (state?: string) =>
    request<Sequence[]>(`/sequences${state ? `?state=${state}` : ""}`),
  sequence: (id: number) => request<SequenceDetail>(`/sequences/${id}`),
  /** Rename a rush, tag it with a colour, or both. Omitted fields stay as they are. */
  updateSequence: (id: number, changes: { label?: string; color?: string }) => {
    const query = new URLSearchParams();
    if (changes.label !== undefined) query.set("label", changes.label);
    if (changes.color !== undefined) query.set("color", changes.color);
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
  regroupSequences: (sequenceIds: number[], force = false) =>
    request<Sequence>("/sequences/regroup", {
      method: "POST",
      body: JSON.stringify({ sequence_ids: sequenceIds, force }),
    }),

  saveCuts: (sequenceId: number, cuts: Array<{ label: string; start_frame: number; end_frame: number }>) =>
    request<Cut[]>(`/sequences/${sequenceId}/cuts`, {
      method: "PUT",
      body: JSON.stringify({ cuts }),
    }),

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
