/**
 * Backend response/request shapes.
 *
 * These mirror the FastAPI handlers exactly. Where a field is optional in the
 * backend it is optional here; where the backend returns `null` for "not
 * applicable" the type says `| null` rather than `| undefined`, so callers are
 * forced to handle it.
 */

// ---------------------------------------------------------------------------
// System
// ---------------------------------------------------------------------------

export type InstanceStatus =
  | 'idle'
  | 'starting'
  | 'running'
  | 'stopping'
  | 'stopped'
  | 'failed';

export interface DiagnosisEntry {
  severity: 'error' | 'warning' | 'info' | string;
  code: string;
  message: string;
  hint?: string;
}

export interface LogLine {
  timestamp: string;
  source: 'stdout' | 'stderr' | 'system' | string;
  line: string;
}

export interface Instance {
  id: string;
  name: string;
  preset_id: string | null;
  preset_name: string | null;
  restarted_from: string | null;
  runtime_id: string | null;
  runtime_label: string | null;
  model_key: string | null;
  variant_key: string | null;
  status: InstanceStatus;
  pid: number | null;
  port: number | null;
  host: string;
  started_at: string | null;
  stopped_at: string | null;
  duration_seconds: number;
  /** False when readiness never succeeded — the UI hides the uptime row then. */
  ever_ready: boolean;
  exit_code: number | null;
  error_message: string | null;
  warnings: string[];
  model_path: string;
  /** False for embedding/rerank instances: they reject chat completions. */
  serves_chat: boolean;
  context_size: number;
  command: string | null;
  supports_vision: boolean;
  declared_tools: string[];
  diagnosis?: DiagnosisEntry[];
  logs?: LogLine[];
}

/**
 * One GPU row.
 *
 * **The names are the route's, not invented ones.** `gpu_monitor.snapshot()`
 * emits `utilization` and `temperature`; this interface used to say
 * `utilization_percent` / `temperature_c`, so the resource-monitor table
 * rendered `—` in both columns forever with no error (a §2.41-class silent
 * lie — `apiGet<T>` casts, so nothing complains).
 */
export interface GpuInfo {
  index: number;
  name: string;
  memory_total_mb: number;
  memory_used_mb: number;
  utilization: number | null;
  temperature: number | null;
}

/** GPU-owning process, as reported by `/api/system/overview`. */
export interface GpuProcess {
  pid: number;
  name: string;
  memory_used_mb: number;
  /** True when the pid belongs to an instance this launcher started. */
  managed?: boolean;
}

/**
 * `/api/system/overview`.
 *
 * Units are in the names the route sends: **GB** for the memory pair (not MB;
 * reading `memory_used_mb` off a GB field is how the status bar showed `—`
 * permanently) and **MB** for GPU memory.
 */
export interface SystemOverview {
  timestamp?: number;
  cpu_percent: number;
  memory_percent?: number;
  memory_used_gb: number;
  memory_total_gb: number;
  gpus: GpuInfo[];
  gpu_processes?: GpuProcess[];
}

/**
 * `/api/system/paths` — runtime locations the Developer tab lists so they can be
 * copied, plus version facts.
 *
 * **These names are the route's, not invented ones.** The previous version of this
 * interface declared `python` / `log_dir` / `launcher_port`, none of which the route
 * sends (`python_version` / `logs_dir` / `listening_port`). Because every read goes
 * through `String(value ?? '')`, nothing threw — the Developer tab showed blank rows
 * and `PendingPage` rendered `(listening )`. `test_frontend_contracts.py::
 * test_the_system_paths_type_names_the_fields_the_route_returns` compares this list
 * against the route in both directions.
 */
export interface SystemPaths {
  data_dir: string;
  logs_dir: string;
  backend_log: string;
  instances_log_dir: string;
  config_file: string;
  presets_dir: string;
  chats_db: string;
  history_db: string;
  knowledge_db: string;
  app_version: string;
  python_version: string;
  platform: string;
  frozen: boolean;
  /** Port the backend is actually listening on; null until the server is up. */
  listening_port: number | null;
  configured_port: number;
}

/**
 * `/api/system/storage` — whether the data directory and each SQLite database can
 * actually be written.
 *
 * `databases` is a **map keyed by label**, not an array: the route builds
 * `{chats: {...}, history: {...}, knowledge: {...}}`. Declaring it as a list is
 * what crashed the Developer tab in 2.0.0 (`s.data.databases.map is not a
 * function`) — `apiGet<T>` does not validate at runtime, so the lie survived to
 * render time.
 */
export interface StorageReport {
  writable: boolean;
  data_dir: string;
  data_dir_writable: boolean | null;
  reasons: string[];
  databases: Record<
    string,
    {
      path: string;
      writable: boolean;
      /** Empty string when the probe succeeded. */
      reason: string;
    }
  >;
  /**
   * Size + free space, added so the System tab can answer "is my knowledge base
   * going to fill the system drive" without three more probes. Optional because
   * an older backend (or a route stub in a test) may not send it.
   */
  usage?: DataUsage;
}

/** Directory size and the free space on its volume. */
export interface DataUsage {
  data_dir_bytes: number;
  data_dir_files: number;
  /** True when the walk hit its entry/time budget; the size is a lower bound. */
  truncated: boolean;
  volume_free_bytes: number | null;
  volume_total_bytes: number | null;
  database_bytes: Record<string, number>;
}

// ---------------------------------------------------------------------------
// External engines (registry only — the launcher never installs or spawns one)
// ---------------------------------------------------------------------------

export type EngineKind = 'llama_cpp' | 'openai_compat';

export interface EngineRecord {
  id: string;
  kind: EngineKind | string;
  label: string;
  base_url: string;
  /** Model id to request; empty = let the engine pick its default. */
  model: string;
  /** **Name** of an environment variable, never the key itself. */
  api_key_env: string;
  created_at?: number;
  updated_at?: number;
  last_probe_at?: number | null;
  last_probe_ok?: boolean | null;
  last_error?: string | null;
  models_seen?: string[];
  capabilities?: string[];
  /** True when the launcher could resolve and run this kind itself. */
  launch?: boolean;
}

export interface EngineList {
  engines: EngineRecord[];
  /** Whether a non-loopback base_url is permitted on this machine. */
  allow_remote: boolean;
}

// ---------------------------------------------------------------------------
// Usage statistics (aggregated from chats.db message stats)
// ---------------------------------------------------------------------------

export interface UsageTotals {
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  /** Average predicted tokens/second across messages that reported it. */
  avg_tokens_per_second: number | null;
}

export interface UsageDay {
  /** `YYYY-MM-DD` in local time. */
  date: string;
  requests: number;
  tokens: number;
}

export interface UsageModelRow {
  model: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  avg_tokens_per_second: number | null;
}

export interface UsageSummary {
  range_days: number;
  totals: UsageTotals;
  daily: UsageDay[];
  /** Per-model rows, largest token total first. At most 20 are sent. */
  models: UsageModelRow[];
  /** How many models appeared in the window; may exceed `models.length`. */
  models_total: number;
  active_days: number;
  longest_streak: number;
  top_model: { model: string; tokens: number } | null;
  peak_day: { date: string; tokens: number } | null;
  /** False when the database has no message stats yet. */
  has_data: boolean;
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

export type ChatRole = 'system' | 'user' | 'assistant' | 'tool';

export interface ChatAttachment {
  id?: number;
  name: string;
  size: number;
  /**
   * Raw text, or a `data:image/...;base64,` URL for images.
   *
   * There is no persisted `kind` column: the backend decides by content shape
   * (`_normalize_attachments`), because a client-declared `kind` can lie. The UI
   * must therefore derive the kind with `classifyAttachmentContent()` on load —
   * 1.0 relied on a client-only `kind` field that was dropped on reload, so a
   * reloaded image was re-sent as a text block containing its base64.
   */
  content: string;
  created_at?: string;
}

export interface ChatToolCall {
  id: string;
  type: 'function';
  function: {
    name: string;
    arguments: string;
  };
}

export interface ChatMessageStats {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  prompt_n?: number;
  predicted_n?: number;
  timings?: Record<string, number>;
  tool_calls?: ChatToolCall[];
  sources?: Citation[];
  stopped?: boolean;
  [key: string]: unknown;
}

export interface ChatMessage {
  id: number | string;
  conversation_id: string;
  role: ChatRole;
  content: string;
  /** First-class column in `messages`; empty string when absent. */
  reasoning: string;
  /** Only `tool` messages carry this; it pairs the result with its call. */
  tool_call_id: string | null;
  stats: ChatMessageStats;
  /** Unix epoch seconds. */
  created_at: number;
  attachments: ChatAttachment[];
}

export interface Conversation {
  id: string;
  title: string;
  instance_id: string | null;
  model: string | null;
  model_key: string | null;
  params: Record<string, unknown>;
  knowledge_base_id: string | null;
  pinned: boolean;
  /** Session-scoped directory the model may read through tools. */
  tool_root: string | null;
  /** Project this conversation belongs to, or null when ungrouped. */
  project_id: string | null;
  /**
   * Unix epoch **seconds** as a float, straight from SQLite — not an ISO string.
   * Formatting for display goes through `formatTime`, which accepts both.
   */
  created_at: number;
  updated_at: number;
  message_count: number | null;
}

export interface ConversationDetail extends Conversation {
  messages: ChatMessage[];
}

export interface ChatSearchHit {
  conversation_id: string;
  title: string;
  match_type: 'title' | 'message' | 'attachment' | 'attachment_name' | string;
  snippet: string;
  attachment_name?: string | null;
  message_count: number | null;
}

export interface Citation {
  index: number;
  title: string;
  page?: number | null;
  heading?: string | null;
  snippet: string;
  /** Present when the chunk came from OCR rather than a text layer. */
  ocr?: boolean;
  document_id?: string;
}

// ---------------------------------------------------------------------------
// Streaming
// ---------------------------------------------------------------------------

export interface ChatStreamChunk {
  content: string;
  reasoning: string;
  toolCalls: ChatToolCall[];
  finishReason: string | null;
  usage: Record<string, number> | null;
  timings: Record<string, number> | null;
  error: string | null;
}

// ---------------------------------------------------------------------------
// Models, runtimes, params
// ---------------------------------------------------------------------------

export interface ModelCapabilitiesSummary {
  probed?: boolean;
  error?: string;
  /** Native context length read from the GGUF header. */
  n_ctx_train?: number | null;
  context_length?: number | null;
  quant?: string | null;
  architecture?: string | null;
  has_vision_encoder?: boolean;
  is_mmproj?: boolean;
  is_draft?: boolean;
  companion_count?: number;
  /** Absent in the list view and present only on the detail endpoint. */
  [key: string]: unknown;
}

/**
 * One row from `GET /api/models/scan`.
 *
 * Field names come from `core/model_scanner.scan_models`, which emits
 * `filename` / `size_bytes` / `size_human` / `quant` / `modified` — not
 * `name` / `size`. `capabilities` and `manifest` are attached by the route only
 * when the caller asked for them.
 */
export interface ModelEntry {
  filename: string;
  path: string;
  size_bytes: number;
  size_human: string;
  quant: string | null;
  modified: string | null;
  capabilities?: ModelCapabilitiesSummary | null;
  manifest?: {
    found?: boolean;
    format?: string | null;
    model_key?: string | null;
    display_name?: string | null;
    [key: string]: unknown;
  } | null;
  companions?: Array<{ path: string; filename?: string; kind: string }>;
}

export interface ModelScanResult {
  directory: string;
  models: ModelEntry[];
  /** True when capability summaries were cut off at the backend's limit. */
  capabilities_truncated?: boolean;
  manifests_truncated?: boolean;
}

/**
 * A registered llama-server build.
 *
 * Field names follow `runtime_registry._build_profile` exactly: the executable
 * path is `executable_path` (not `path`), the backend family lives in
 * `backend.family`, and probe state is nested under `probe`.
 */
export interface RuntimeProfile {
  schema_version?: number;
  id: string;
  display_name: string;
  executable_path: string;
  manifest_path: string | null;
  engine: string;
  runtime_kind: string;
  version: string | null;
  build: number | null;
  build_text: string | null;
  backend: { family: string; [key: string]: unknown };
  /** Devices this build actually enumerates on this machine (`--list-devices`). */
  devices: Array<{ name: string; description?: string; selected?: boolean; [key: string]: unknown }>;
  domains: string[];
  supported_model_formats: string[];
  target_libraries: string[];
  manifest: Record<string, unknown> | null;
  warnings: string[];
  probe: {
    probed: boolean;
    complete: boolean;
    option_count: number;
    version: string | null;
    build: number | null;
    error: string | null;
    probed_at: number | null;
  };
  status: 'ok' | 'probe_failed' | string;
  artifacts: Array<{ relative_path: string; size: number | null; mtime: number | null; executable: boolean }>;
  created_at: number;
  updated_at: number;
}

export type ParamType =
  | 'text'
  | 'password'
  | 'number'
  | 'boolean'
  | 'select'
  | 'multi'
  | 'file'
  | 'dir';

export interface ParamMeta {
  key: string;
  label: string;
  label_en?: string;
  cli: string | null;
  group: string;
  type: ParamType | string;
  default?: unknown;
  min?: number | null;
  max?: number | null;
  step?: number | null;
  options?: string[] | null;
  option_labels?: Record<string, string> | null;
  option_labels_en?: Record<string, string> | null;
  placeholder?: string | null;
  /**
   * Full explanation. Shown only on request — the collapsed row shows `summary`.
   *
   * The backend has always sent this one prose blob per parameter, so the UI had
   * nothing shorter to display and printed all of it under every control (92
   * parameters → a wall of text). `summary` is the missing short line.
   */
  help?: string | null;
  help_en?: string | null;
  /** One-line description; falls back to the first sentence of `help`. */
  summary?: string | null;
  summary_en?: string | null;
  required?: boolean;
  /**
   * Conditional visibility. The backend models these and applies them when building
   * the command line, but the UI rendered every dependent parameter regardless.
   */
  depends_on?: Record<string, unknown> | null;
  depends_on_any?: Record<string, unknown[]> | null;
  depends_on_nonempty?: string | null;
  multiple?: boolean;
  cli_reverse?: string | null;
  cli_no_value?: boolean;
  unit?: string | null;
  order?: number;
  /** `true` / `false` from the selected build; `null` = not probed (never disable). */
  supported?: boolean | null;
  upstream_options?: string[] | null;
  append_raw?: boolean;
}

export interface ParamSchema {
  groups: Array<{
    key: string;
    label: string;
    label_en?: string;
    /** Whether this section starts expanded. Honoured by the 2.0.1 layout. */
    default_open?: boolean;
    order?: number;
  }>;
  params: ParamMeta[];
  probed?: boolean;
  probe_complete?: boolean;
}

export interface BuildCommandResult {
  argv: string[];
  command: string;
  /** Params the user set that this build does not accept. Must be shown. */
  suppressed: string[];
}

export interface DiagnoseResult {
  blocking: DiagnosisEntry[];
  warnings: DiagnosisEntry[];
  guardrail?: 'off' | 'warn' | 'strict';
}

// ---------------------------------------------------------------------------
// Presets / history
// ---------------------------------------------------------------------------

export interface Preset {
  id: string;
  name: string;
  description?: string;
  tags?: string[];
  params: Record<string, unknown>;
  runtime_id?: string | null;
  created_at?: string;
  updated_at?: string;
}

/**
 * A row from `launch_history`. `/api/history` returns a bare array (not an
 * envelope) and the row set is fixed by `history_store.list_history`: it selects
 * `id, instance_id, preset_id, preset_name, model_path, port, started_at,
 * stopped_at, duration_seconds, status, exit_code, error_message, runtime_id,
 * runtime_label, model_key, variant_key`.
 */
export interface HistoryRecord {
  id: number;
  instance_id: string | null;
  preset_id: string | null;
  preset_name: string | null;
  model_path: string | null;
  port: number | null;
  status: string;
  started_at: string | null;
  stopped_at: string | null;
  duration_seconds: number | null;
  exit_code: number | null;
  error_message: string | null;
  runtime_id: string | null;
  runtime_label: string | null;
  model_key: string | null;
  variant_key: string | null;
  /** Present only on the detail endpoint. */
  command_text?: string | null;
  params?: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// Knowledge
//
// Column names follow `knowledge_bases` / `documents` in
// `backend/core/knowledge_store.py` verbatim. Two differences from a naive guess:
// the chunk size column is `chunk_chars` (not `chunk_size`), and documents carry
// `display_name` + `size_bytes` + `chunk_count` rather than `name`/`size`/`chunks`.
// ---------------------------------------------------------------------------

export type KnowledgeStatus = 'idle' | 'indexing' | 'ready' | 'error' | 'cancelled' | string;

export interface KnowledgeDocument {
  id: string;
  base_id: string;
  path: string;
  display_name: string;
  kind: string;
  size_bytes: number;
  mtime: number;
  sha256: string;
  status: string;
  error: string;
  chunk_count: number;
  indexed_at: number | null;
  created_at: number;
  updated_at: number;
}

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  embedding_model: string;
  embedding_dim: number;
  ocr_model: string;
  ocr_mmproj: string;
  ocr_prompt: string;
  chunk_chars: number;
  chunk_overlap: number;
  top_k: number;
  min_score: number;
  rerank_enabled: boolean;
  rerank_model: string;
  status: KnowledgeStatus;
  /** 0–100. */
  progress: number;
  error: string;
  created_at: number;
  updated_at: number;
  document_count: number;
  chunk_count: number;
  /** Whether the SQLite build provided FTS5 for this base. */
  _fts?: boolean;
}

export interface KnowledgeDetail {
  base: KnowledgeBase;
  documents: KnowledgeDocument[];
}

export interface KnowledgeChunk {
  id: number;
  chunk_index?: number;
  text: string;
  page?: number | null;
  heading?: string | null;
  ocr?: boolean;
}

export interface KnowledgeSearchHit {
  chunk_id?: number;
  document_id?: string;
  document_name?: string;
  text: string;
  score: number;
  page?: number | null;
  heading?: string | null;
  ocr?: boolean;
  /** Present when reranking reordered the fused result. */
  rerank_score?: number | null;
}

export interface KnowledgeServicesStatus {
  services: Array<{
    instance_id?: string;
    kind: 'embedding' | 'rerank' | 'ocr' | string;
    name?: string;
    status?: string;
    port?: number | null;
    model?: string | null;
  }>;
  count?: number;
}

// ---------------------------------------------------------------------------
// Local API server (OpenAI-compatible gateway)
// ---------------------------------------------------------------------------

export interface ApiServerStatus {
  enabled: boolean;
  running: boolean;
  error: string | null;
  port: number;
  base_url: string;
  cors_origins: string[];
  key_configured: boolean;
  log_requests: boolean;
  logs_capacity: number;
  models: Array<{ id: string; name: string; instance_id: string; port: number }>;
}

export interface ApiServerLogEntry {
  at: string;
  method: string;
  path: string;
  status: number;
  model: string | null;
  stream: boolean;
  ms: number;
  error: string | null;
}

export interface ApiServerRecipe {
  label: string;
  language: string;
  code: string;
}

// ---------------------------------------------------------------------------
// Config / backups / prompts
// ---------------------------------------------------------------------------

/**
 * `GET/PUT /api/system/config` — the whole persisted settings file.
 *
 * **Every field name here is a key of `backend/config.DEFAULT_CONFIG`, spelled
 * identically.** `update_config()` rejects unknown keys with
 * `400 unsupported config key(s): …`, so a typo does not degrade — it makes the
 * whole settings form unsaveable. That is exactly what shipped in 2.0.0: the
 * model directory was sent as `model_directory` while the backend key is
 * `default_model_dir`, so "save" always failed with 不支持配置项 and the field
 * also rendered empty (the GET never returns the invented name).
 *
 * There is deliberately **no index signature**: it would let a typo typecheck.
 * `test_frontend_contracts.py` reconciles this interface against
 * `DEFAULT_CONFIG` in both directions, so adding a backend key without adding a
 * field (or vice versa) is a red test rather than a silent mismatch.
 */
export interface AppConfig {
  llama_server_path: string;
  default_model_dir: string;
  launcher_host: string;
  launcher_port: number;
  instance_port_start: number;
  instance_port_end: number;
  startup_timeout: number;
  health_check_interval_ms: number;
  log_buffer_lines: number;
  monitor_interval_seconds: number;
  monitor_history_seconds: number;
  history_max_records: number;
  persist_instance_logs: boolean;
  launch_guardrail: 'off' | 'warn' | 'strict';
  theme: string;
  close_to_tray: boolean;
  chat_reasoning: 'auto' | 'expand' | 'collapse';
  /** Which key combo sends: Enter, or Ctrl+Enter (Enter inserts a newline). */
  chat_send_key: 'enter' | 'ctrl-enter';
  /** Whether the per-message stats row is shown at all. */
  chat_show_stats: 'always' | 'never';
  chat_show_timestamps: boolean;
  /** Closed set; each preset is a hand-checked (fill, text) pair per theme. */
  accent_preset: 'blue' | 'violet' | 'teal' | 'amber' | 'rose' | 'green';
  chat_assistant_name: string;
  chat_user_name: string;
  chat_assistant_avatar: string;
  chat_user_avatar: string;
  chat_sidebar_width: number;
  chat_panel_width: number;
  chat_background_image: string;
  chat_background_opacity: number;
  chat_background_blur: number;
  api_server_enabled: boolean;
  api_server_port: number;
  api_server_cors_origins: string;
  api_server_key: string;
  api_server_log_requests: boolean;
  web_search_enabled: boolean;
  web_search_endpoint: string;
  web_search_max_results: number;
  web_search_timeout_seconds: number;
}

export interface BackupEntry {
  name: string;
  created_at: string;
  size: number;
  includes_logs?: boolean;
  verified?: boolean;
}

export interface Prompt {
  id: string;
  title: string;
  content: string;
  builtin?: boolean;
  created_at?: string;
  updated_at?: string;
}

// ---------------------------------------------------------------------------
// Messages pushed over WebSocket
// ---------------------------------------------------------------------------

export interface InstancesMessage {
  type: 'instances';
  instances: Instance[];
}

export interface PingMessage {
  type: 'ping';
}

export interface LogsMessage {
  type: 'logs';
  line: LogLine;
}

export interface LogsDroppedMessage {
  type: 'logs_dropped';
  dropped: number;
}

export type InstanceSocketMessage =
  | InstancesMessage
  | PingMessage
  | LogsMessage
  | LogsDroppedMessage
  | { type: string; [key: string]: unknown };
