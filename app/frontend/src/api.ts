export interface Rollup {
  documented: number;
  total: number;
  coverage: number;
  open_changes: number;
  needs_attention?: number;
}

export interface CatalogNode extends Rollup {
  name: string;
  schema_count: number;
  table_count: number;
  error?: string;
}

export interface SchemaNode extends Rollup {
  name: string;
  comment: string | null;
  table_count: number;
  column_count: number;
}

export interface TableNode extends Rollup {
  name: string;
  table_type: string;
  comment: string | null;
  column_count: number;
  documented_columns: number;
  has_comment: boolean;
}

export interface OpenRequest {
  request_id: string;
  status: "PENDING" | "APPROVED";
  proposed_value: string | null;
  requested_by: string;
  requested_at: string;
  apply_message: string | null;
}

export interface EntityState {
  open_request: OpenRequest | null;
  drifted: boolean;
  last_applied_value: string | null;
}

export interface ColumnDetail extends EntityState {
  column_name: string;
  data_type: string;
  comment: string | null;
  ordinal_position: number;
}

export interface TableDetail extends EntityState {
  catalog: string;
  schema: string;
  name: string;
  table_type: string;
  comment: string | null;
  table_owner: string | null;
  created: string | null;
  last_altered: string | null;
  data_source_format: string | null;
  columns: ColumnDetail[];
}

export type Status =
  | "PENDING"
  | "APPROVED"
  | "REJECTED"
  | "APPLIED"
  | "FAILED"
  | "WITHDRAWN"
  | "SUPERSEDED";

export interface ChangeRequest {
  request_id: string;
  batch_id: string;
  entity_type: "TABLE" | "COLUMN";
  catalog_name: string;
  schema_name: string;
  table_name: string;
  column_name: string | null;
  table_type: string;
  previous_value: string | null;
  proposed_value: string | null;
  note: string | null;
  status: Status;
  requested_by: string;
  requested_at: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  force_apply: boolean | null;
  apply_attempts: number | null;
  deferrals: number | null;
  last_attempt_at: string | null;
  apply_message: string | null;
  applied_at: string | null;
  updated_at: string | null;
}

export interface Me {
  email: string;
  name: string;
  is_approver: boolean;
}

export interface AppConfig {
  title: string;
  scope: string[];
  staging_table: string;
  coverage_target: number;
  allow_self_approval: boolean;
  apply_job_id: string | null;
  apply_mode: "defer" | "window";
  workspace_host: string;
}

export interface ApplySchedule {
  mode: "defer" | "window";
  next_run: string | null;
  cron?: string | null;
  timezone?: string | null;
  paused?: boolean | null;
}

export interface Batch {
  batch_id: string;
  requested_by: string;
  note: string | null;
  requests: number;
  tables: number;
  columns: number;
  table_descriptions: number;
  first_requested_at: string;
}

export interface ApplyRun {
  run_id: number;
  start_time: number;
  end_time: number;
  life_cycle_state: string | null;
  result_state: string | null;
  url: string;
}

export interface Edit {
  catalog: string;
  schema: string;
  table: string;
  column: string | null;
  proposed_value: string;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not json */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

const enc = encodeURIComponent;
const post = <T>(path: string, body?: unknown) =>
  call<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });

export const api = {
  me: () => call<Me>("/api/me"),
  config: () => call<AppConfig>("/api/config"),
  catalogs: () => call<CatalogNode[]>("/api/graph/catalogs"),
  schemas: (c: string) => call<SchemaNode[]>(`/api/graph/catalogs/${enc(c)}/schemas`),
  tables: (c: string, s: string) =>
    call<TableNode[]>(`/api/graph/catalogs/${enc(c)}/schemas/${enc(s)}/tables`),
  table: (c: string, s: string, t: string) =>
    call<TableDetail>(`/api/tables/${enc(c)}/${enc(s)}/${enc(t)}`),
  search: (q: string) =>
    call<{ kind: string; catalog: string; schema: string; table: string; comment: string | null }[]>(
      `/api/search?q=${enc(q)}`,
    ),
  refresh: () => post<{ ok: boolean }>("/api/refresh"),
  submit: (changes: Edit[], note: string) =>
    post<{ submitted: number }>("/api/changes", { changes, note }),
  changes: (status: string, mine = false, batchId?: string) =>
    call<ChangeRequest[]>(
      `/api/changes?status=${enc(status)}&mine=${mine}` + (batchId ? `&batch_id=${enc(batchId)}` : ""),
    ),
  batches: () => call<Batch[]>("/api/changes/batches"),
  reviewBatch: (batchId: string, decision: "approve" | "reject", note: string) =>
    post<{ updated: number; skipped_self: number }>("/api/changes/review_batch", {
      batch_id: batchId,
      decision,
      note,
    }),
  summary: () => call<Record<string, number>>("/api/changes/summary"),
  history: (c: string, s: string, t: string, column: string | null) =>
    call<ChangeRequest[]>(
      `/api/history?catalog=${enc(c)}&schema=${enc(s)}&table=${enc(t)}` +
        (column ? `&column=${enc(column)}` : ""),
    ),
  review: (ids: string[], decision: "approve" | "reject", note: string) =>
    post<{ updated: number; skipped_self: number }>("/api/changes/review", { ids, decision, note }),
  withdraw: (id: string) => post<{ updated: number }>(`/api/changes/${enc(id)}/withdraw`),
  force: (id: string) => post<{ updated: number }>(`/api/changes/${enc(id)}/force`),
  retry: (id: string) => post<{ updated: number }>(`/api/changes/${enc(id)}/retry`),
  applyRuns: () => call<ApplyRun[]>("/api/apply/runs"),
  applySchedule: () => call<ApplySchedule>("/api/apply/schedule"),
  applyNow: () => post<{ run_id: number }>("/api/apply/run"),
};
