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
  tickets_issued: number | null;
  pending_uploads?: number | null;
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
