// Response shapes of the BFF (/api/*), which passes the producer and control API bodies through.

export interface SourceAccess {
  source: string;
  tenant_id: string;
  access: "write" | "read";
}

export interface SourcesResponse {
  items: SourceAccess[];
}

export interface Process {
  process_id: string;
  name: string;
  source: string;
  status: "OPEN" | "CLOSED";
  expected_count: number | null;
  registered_count: number;
  rejected_count: number;
  superseded_count?: number;
  expired_count?: number;
  tickets_issued: number;
  pending_uploads: number;
  metadata?: Record<string, unknown> | null;
  created_by: string;
  created_at: string;
  updated_at?: string | null;
  closed_at?: string | null;
  settled: boolean;
}

export interface ProcessPage {
  items: Process[];
  next_token: string | null;
}

export interface UploadTicket {
  upload_id: string;
  article_id: string;
  process_id: string;
  method: "PUT";
  url: string;
  headers: Record<string, string>;
  expires_at: string;
}

export interface UploadRow {
  upload_id: string;
  article_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  status: "PENDING" | "REGISTERING" | "REGISTERED" | "SUPERSEDED" | "REJECTED" | "EXPIRED";
  reason?: string | null;
  title?: string | null;
  external_id?: string | null;
  created_at: string;
  finished_at?: string | null;
}

export interface UploadPage {
  items: UploadRow[];
  next_token: string | null;
}

export type SummaryStatus = "IN_PROGRESS" | "COMPLETE" | "COMPLETE_WITH_ERRORS";

export interface ProcessSummary {
  process_id: string;
  status: SummaryStatus;
  counts: { indexed: number; skipped: number; failed: number };
  producer: {
    status: string;
    expected_count: number | null;
    registered_count: number;
    rejected_count: number;
  };
  failures_by_error_code: Record<string, number>;
}

export interface ArticleRow {
  article_id: string;
  status: string;
  stage?: string | null;
  doc_version?: number | null;
  chunk_count?: number | null;
  error_code?: string | null;
  error_msg?: string | null;
  updated_at?: string | null;
  indexed_at?: string | null;
  tenant_id?: string | null;
}

export interface ProcessArticles {
  process_id: string;
  items: ArticleRow[];
}

export interface ArticleLatest {
  status: string;
  stage?: string | null;
  doc_version?: number | null;
  chunk_count?: number | null;
  run_id?: string | null;
  error_code?: string | null;
  error_msg?: string | null;
  error_class?: string | null;
  indexed_at?: string | null;
  updated_at?: string | null;
  embedding_model?: string | null;
  model?: string | null;
  [key: string]: unknown;
}

export interface ArticleRun {
  run_id: string;
  trigger?: string | null;
  claim?: string | null;
  outcome?: string | null;
  execution_arn?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  embedding_model?: string | null;
}

export interface ArticleStep {
  step: string;
  step_status: string;
  attempt?: number | null;
  duration_ms?: number | null;
  metrics?: Record<string, unknown> | null;
  error_class?: string | null;
  error_code?: string | null;
  error_msg?: string | null;
  sk: string;
}

export interface ArticleStatus {
  latest: ArticleLatest;
  runs: ArticleRun[];
  steps: ArticleStep[];
  execution: string | null;
}

// --- Operations (/api/ops/*) ---

export interface OpsStatusInfo {
  count: number;
  oldest_at: string | null;
  oldest_article_id: string | null;
}

export interface OpsQueueDepth {
  visible: number;
  in_flight: number;
}

export type OpsQueue = OpsQueueDepth | { error: string };

export interface OpsOverview {
  statuses: Record<string, OpsStatusInfo>;
  queues: Record<string, OpsQueue>;
  generated_at: string;
}

export interface OpsArticle {
  article_id: string;
  status: string;
  stage?: string | null;
  process_id?: string | null;
  tenant_id?: string | null;
  doc_version?: number | null;
  error_code?: string | null;
  error_msg?: string | null;
  updated_at?: string | null;
}

export interface OpsArticlePage {
  items: OpsArticle[];
  next_token: string | null;
}

export interface RegistryState {
  live?: string | null;
  writes?: string[] | null;
  epoch?: number | null;
  tenant_id?: string | null;
}

export interface TableStats {
  rows?: number | null;
  active_rows?: number | null;
  active_articles?: number | null;
}

export interface EmbeddingProfile {
  table?: string;
  provider?: string;
  model?: string;
  dimension?: number;
}

/** GET /api/ops/tables (the control API's GET /v1/tables). Every field is optional: render defensively. */
export interface TablesResponse {
  active_table?: string | null;
  tables?: Record<string, TableStats | null> | null;
  tenant_id?: string | null;
  registry?: RegistryState | null;
  consistent?: boolean | null;
  tenants?: Record<string, RegistryState> | null;
  profiles?: Record<string, EmbeddingProfile> | null;
}

export interface SearchFilters {
  source?: string;
  published_after?: string;
  process_id?: string;
}

export interface SearchRequest {
  tenant_id: string;
  query: string;
  k: number;
  filters?: SearchFilters;
}

export interface SearchResult {
  chunk_id: string;
  article_id: string;
  doc_version?: number | null;
  title?: string | null;
  text?: string | null;
  context?: string | null;
  heading_path?: string[] | null;
  page_start?: number | null;
  page_end?: number | null;
  char_start?: number | null;
  char_end?: number | null;
  source?: string | null;
  published_at?: string | null;
  score?: number | null;
  vector_rank?: number | null;
  keyword_rank?: number | null;
}

export interface SearchResponse {
  tenant_id: string;
  query: string;
  k: number;
  table?: string | null;
  reranked?: boolean;
  results: SearchResult[];
}

/** One ingest message of a dry run (the control API's IngestMessage). */
export interface IngestMessagePreview {
  article_id: string;
  process_id?: string | null;
  event?: string;
  trigger?: string;
  force?: boolean;
  [key: string]: unknown;
}

export interface DlqMove {
  messages: number;
  moved: boolean;
  task_handle?: string;
}

/**
 * POST /api/ops/ingest, /api/ops/processes/{id}/ingest and /api/ops/dlq/redrive.
 * A dry run lists `messages`; a real call reports `queued`. Ids with no ingestion record are in
 * `not_found`.
 */
export interface ActionResponse {
  dry_run: boolean;
  count: number;
  messages?: IngestMessagePreview[];
  queued?: number;
  not_found?: string[];
  process_id?: string;
  error_code?: string | null;
  dlq?: DlqMove | null;
}
