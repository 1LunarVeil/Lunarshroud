/**
 * The whole backend surface the UI uses, typed.
 *
 * Every method here maps to a real route in `backend/api/*`. When a route's
 * response shape is not modelled in `types.ts` the method falls back to
 * `unknown` — silently pretending a shape exists is how the 1.0 frontend read
 * fields that were never sent.
 */

import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  apiPut,
  INDEX_TIMEOUT_MS,
  LONG_TIMEOUT_MS,
  SEARCH_TIMEOUT_MS,
  openStream,
  watchSocket,
  type RequestOptions,
  type SocketHandlers,
  type SocketWatcher,
} from './client';
import type {
  ApiServerLogEntry,
  ApiServerRecipe,
  ApiServerStatus,
  AppConfig,
  BackupEntry,
  BuildCommandResult,
  ChatAttachment,
  ChatMessage,
  ChatSearchHit,
  ChatToolCall,
  Conversation,
  ConversationDetail,
  DiagnoseResult,
  EngineList,
  EngineRecord,
  HistoryRecord,
  Instance,
  KnowledgeBase,
  KnowledgeChunk,
  KnowledgeDetail,
  KnowledgeDocument,
  KnowledgeSearchHit,
  KnowledgeServicesStatus,
  ModelEntry,
  ModelScanResult,
  ParamSchema,
  Preset,
  Prompt,
  RuntimeProfile,
  StorageReport,
  SystemOverview,
  SystemPaths,
  UsageSummary,
} from './types';

/** Query-string builder; empty values are omitted, matching the 1.0 behaviour. */
function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
  }
  return parts.length ? `?${parts.join('&')}` : '';
}

const enc = encodeURIComponent;

// ---------------------------------------------------------------------------
// System
// ---------------------------------------------------------------------------

export const systemApi = {
  getConfig: () => apiGet<AppConfig>('/api/system/config'),
  updateConfig: (body: Partial<AppConfig>) => apiPut<AppConfig>('/api/system/config', body),
  overview: () => apiGet<SystemOverview>('/api/system/overview'),
  paths: () => apiGet<SystemPaths>('/api/system/paths'),
  storage: () => apiGet<StorageReport>('/api/system/storage'),
  capabilities: (model?: string, runtimeId?: string) =>
    apiGet<Record<string, unknown>>(
      `/api/system/capabilities${qs({ model, runtime_id: runtimeId })}`,
      { timeoutMs: LONG_TIMEOUT_MS },
    ),
  health: () => apiGet<{ status: string; session_id: string; pid: number }>('/api/system/health'),
  /** Copy the data directory elsewhere and point the launcher at it. */
  migrateDataDir: (target: string) =>
    apiPost<{
      source: string;
      target: string;
      files: Array<{ name: string; bytes: number; sha256: string }>;
      file_count: number;
      restart_required: boolean;
      pointer: string;
      free_bytes: number | null;
    }>('/api/system/data-dir/migrate', { target }, { timeoutMs: INDEX_TIMEOUT_MS }),
  /** Roll a migration back by deleting the pointer file. */
  clearDataDirLocation: () =>
    apiDelete<{ removed: boolean; restart_required: boolean }>('/api/system/data-dir/location'),
};

// ---------------------------------------------------------------------------
// Params
// ---------------------------------------------------------------------------

export const paramsApi = {
  schema: (runtimeId?: string) =>
    apiGet<ParamSchema>(`/api/params/schema${qs({ runtime_id: runtimeId })}`, {
      timeoutMs: LONG_TIMEOUT_MS,
    }),
  defaults: () => apiGet<Record<string, unknown>>('/api/params/defaults'),
  recipes: (runtimeId?: string) =>
    apiGet<Array<{ key: string; label: string; label_en?: string; params: Record<string, unknown>; unsupported?: string[] }>>(
      `/api/params/recipes${qs({ runtime_id: runtimeId })}`,
    ),
  buildCommand: (params: Record<string, unknown>, runtimeId?: string) =>
    apiPost<BuildCommandResult>('/api/params/build-command', {
      params,
      runtime_id: runtimeId || undefined,
    }),
};

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------

export const modelsApi = {
  scan: (directory?: string, withCapabilities = false, withManifest = false) =>
    apiGet<ModelScanResult>(
      `/api/models/scan${qs({
        directory,
        with_capabilities: withCapabilities ? 'true' : undefined,
        with_manifest: withManifest ? 'true' : undefined,
      })}`,
      { timeoutMs: LONG_TIMEOUT_MS },
    ),
  browse: (directory?: string) =>
    apiGet<{ directory: string; entries: Array<{ name: string; path: string; is_dir: boolean }> }>(
      `/api/models/browse${qs({ directory })}`,
    ),
  manifest: (modelPath: string) =>
    apiGet<Record<string, unknown>>(`/api/models/manifest${qs({ model: modelPath })}`),
  manifestRaw: (modelPath: string) =>
    apiGet<{ content: string; format: string }>(
      `/api/models/manifest/raw${qs({ model: modelPath })}`,
    ),
  validateManifest: (body: { model: string; content: string; format: string }) =>
    apiPost<{ ok: boolean; errors: string[]; warnings: string[]; model_key?: string | null }>(
      '/api/models/manifest/validate',
      body,
    ),
  saveManifest: (body: { model: string; content: string; format: string; overwrite?: boolean }) =>
    apiPut<Record<string, unknown>>('/api/models/manifest', body),
  deleteManifest: (modelPath: string, format = 'yaml') =>
    apiDelete<Record<string, unknown>>(
      `/api/models/manifest${qs({ model: modelPath, format })}`,
    ),
  defaults: (modelPath: string) => apiGet<Record<string, unknown>>(
    `/api/models/defaults${qs({ model: modelPath })}`,
  ),
  saveDefaults: (modelPath: string, params: Record<string, unknown>) =>
    apiPut<Record<string, unknown>>('/api/models/defaults', { model: modelPath, params }),
  clearDefaults: (modelPath: string) =>
    apiDelete<Record<string, unknown>>(`/api/models/defaults${qs({ model: modelPath })}`),
  vramEstimate: (query: {
    model?: string;
    ctx_size?: number | string;
    n_gpu_layers?: number | string;
    cache_type_k?: string;
    cache_type_v?: string;
    parallel?: number | string;
    mmproj?: string;
  }) =>
    apiGet<Record<string, unknown>>(
      `/api/models/vram-estimate${qs({
        model: query.model,
        ctx_size: query.ctx_size,
        n_gpu_layers: query.n_gpu_layers,
        cache_type_k: query.cache_type_k,
        cache_type_v: query.cache_type_v,
        parallel: query.parallel,
        mmproj: query.mmproj,
      })}`,
    ),
};

// ---------------------------------------------------------------------------
// Runtimes
// ---------------------------------------------------------------------------

export const runtimesApi = {
  list: () =>
    apiGet<{
      runtimes: RuntimeProfile[];
      preferences: Record<string, string | null>;
    }>('/api/runtimes'),
  register: (body: { path: string; manifest_path?: string; display_name?: string }) =>
    apiPost<RuntimeProfile>('/api/runtimes', body, { timeoutMs: LONG_TIMEOUT_MS }),
  probe: (id: string) =>
    apiPost<RuntimeProfile>(`/api/runtimes/${enc(id)}/probe`, undefined, {
      timeoutMs: LONG_TIMEOUT_MS,
    }),
  rename: (id: string, displayName: string) =>
    apiPut<RuntimeProfile>(`/api/runtimes/${enc(id)}`, { display_name: displayName }),
  remove: (id: string) => apiDelete<Record<string, unknown>>(`/api/runtimes/${enc(id)}`),
  preferences: () =>
    apiGet<{ preferences: Record<string, string | null> }>('/api/runtimes/preferences'),
  setPreferences: (body: Record<string, string | null>) =>
    apiPut<{ preferences: Record<string, string | null> }>('/api/runtimes/preferences', body),
};

// ---------------------------------------------------------------------------
// Instances
// ---------------------------------------------------------------------------

export const instancesApi = {
  list: () => apiGet<Instance[]>('/api/instances'),
  detail: (id: string) => apiGet<Instance>(`/api/instances/${enc(id)}`),
  create: (body: Record<string, unknown>) => apiPost<Instance>('/api/instances', body),
  diagnose: (body: Record<string, unknown>) =>
    apiPost<DiagnoseResult>('/api/diagnose', body, { timeoutMs: LONG_TIMEOUT_MS }),
  command: (id: string) =>
    apiGet<{ argv: string[]; command: string }>(`/api/instances/${enc(id)}/command`),
  diagnosis: (id: string) => apiGet<{ diagnosis: unknown[] }>(`/api/instances/${enc(id)}/diagnosis`),
  stop: (id: string, force = false) =>
    apiPost<Instance>(`/api/instances/${enc(id)}/stop`, { force }),
  restart: (id: string) => apiPost<Instance>(`/api/instances/${enc(id)}/restart`),
  remove: (id: string) => apiDelete<Record<string, unknown>>(`/api/instances/${enc(id)}`),
  watch: (handlers: SocketHandlers): SocketWatcher => watchSocket('/ws/instances', handlers),
  watchLogs: (id: string, handlers: SocketHandlers): SocketWatcher =>
    watchSocket(`/ws/instances/${enc(id)}/logs`, handlers),
  watchMetrics: (handlers: SocketHandlers): SocketWatcher =>
    watchSocket('/ws/system/metrics', handlers),
};

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

export interface AppendMessageBody {
  role: 'user' | 'assistant' | 'tool' | 'system';
  content: string;
  stats?: Record<string, unknown>;
  attachments?: Array<{ name: string; content: string }>;
  reasoning?: string;
  tool_call_id?: string;
}

export const chatsApi = {
  /**
   * Conversation list.
   *
   * The backend wraps it in `{conversations: [...]}`. Typing the reply as a bare
   * `Conversation[]` here made `conversations.data ?? []` an **object**, so
   * `sortConversations` threw inside the sidebar's `useMemo` and every user saw
   * "No conversations yet" no matter how many they had. Only a real browser caught
   * it: the API reply is a valid 200 and the shape error happens downstream
   * (2026-11-23).
   */
  /**
   * Conversation list.
   *
   * The backend replies with `{conversations: [...]}`; this unwraps it. Typing the
   * reply as a bare array made `conversations.data ?? []` an **object**, so
   * `sortConversations` threw inside the sidebar's `useMemo` and every user saw
   * "No conversations yet" regardless of how many they had. The reply was a valid
   * 200, so only a real browser could see it (2026-11-23).
   */
  list: (limit = 200) =>
    apiGet<{ conversations: Conversation[] }>(`/api/chats${qs({ limit })}`).then(
      (payload) => payload.conversations ?? [],
    ),
  search: (query: string, limit = 30) =>
    apiGet<{ results: ChatSearchHit[] }>(`/api/chats/search${qs({ q: query, limit })}`),
  create: (body: {
    title?: string;
    instance_id?: string | null;
    model?: string | null;
    params?: Record<string, unknown>;
    knowledge_base_id?: string | null;
    model_key?: string | null;
  }) => apiPost<Conversation>('/api/chats', body),
  detail: (id: string) => apiGet<ConversationDetail>(`/api/chats/${enc(id)}`),
  update: (id: string, body: Partial<Pick<Conversation, 'title' | 'pinned' | 'params' | 'instance_id' | 'model' | 'model_key' | 'knowledge_base_id' | 'tool_root' | 'project_id'>>) =>
    apiPatch<Conversation>(`/api/chats/${enc(id)}`, body),
  remove: (id: string) => apiDelete<Record<string, unknown>>(`/api/chats/${enc(id)}`),
  appendMessage: (id: string, body: AppendMessageBody) =>
    apiPost<ChatMessage>(`/api/chats/${enc(id)}/messages`, body),
  updateMessage: (
    id: string,
    messageId: string | number,
    body: { content?: string; truncate_after?: boolean; reasoning?: string },
  ) =>
    apiPatch<{ deleted: number; title: string | null }>(
      `/api/chats/${enc(id)}/messages/${enc(String(messageId))}`,
      body,
    ),
  deleteMessage: (id: string, messageId: string | number) =>
    apiDelete<Record<string, unknown>>(`/api/chats/${enc(id)}/messages/${enc(String(messageId))}`),
  branch: (id: string, body: { message_id?: number | string; title?: string } = {}) =>
    apiPost<ConversationDetail>(`/api/chats/${enc(id)}/branch`, body),
  clear: () => apiPost<{ deleted: number }>('/api/chats/clear'),
  purge: (body: { older_than_days?: number; keep?: number }) =>
    apiPost<{ deleted: number }>('/api/chats/purge', body),
  export: (id: string) => apiGet<Record<string, unknown>>(`/api/chats/${enc(id)}/export`),
  import: (payload: unknown) =>
    apiPost<{ imported: number; skipped?: number }>('/api/chats/import', payload),
};

export const promptsApi = {
  list: () => apiGet<{ prompts: Prompt[] }>('/api/prompts'),
  create: (body: { title: string; content: string }) => apiPost<Prompt>('/api/prompts', body),
  update: (id: string, body: { title?: string; content?: string }) =>
    apiPatch<Prompt>(`/api/prompts/${enc(id)}`, body),
  remove: (id: string) => apiDelete<Record<string, unknown>>(`/api/prompts/${enc(id)}`),
};

// ---------------------------------------------------------------------------
// Persistent memory
// ---------------------------------------------------------------------------

export interface MemoryScope {
  scope: string;
  revision: number;
  entries: string[];
  bytes: number;
  max_bytes: number;
  updated_at: number | null;
}

export const memoryApi = {
  get: (scope = 'personal') =>
    apiGet<MemoryScope>(`/api/memory${qs({ scope })}`),
  replace: (scope: string, entries: string[]) =>
    apiPut<MemoryScope>('/api/memory', { scope, entries }),
  clear: (scope = 'personal') => apiDelete<{ removed: boolean }>(`/api/memory${qs({ scope })}`),
};

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

export interface Project {
  id: string;
  name: string;
  description: string;
  knowledge_base_id: string | null;
  /** Derived from the id (`project:<id>`), never stored separately. */
  memory_scope: string;
  conversation_count: number | null;
  created_at: number;
  updated_at: number;
}

export const projectsApi = {
  list: () => apiGet<{ projects: Project[] }>('/api/projects'),
  detail: (id: string) => apiGet<Project>(`/api/projects/${enc(id)}`),
  create: (body: { name: string; description?: string; knowledge_base_id?: string | null }) =>
    apiPost<Project>('/api/projects', body),
  update: (id: string, body: { name?: string; description?: string; knowledge_base_id?: string | null }) =>
    apiPatch<Project>(`/api/projects/${enc(id)}`, body),
  /** Deletes the grouping only; its conversations become ungrouped. */
  remove: (id: string) => apiDelete<{ deleted: boolean; conversations_kept: boolean }>(`/api/projects/${enc(id)}`),
};

// ---------------------------------------------------------------------------
// Skills
// ---------------------------------------------------------------------------

export interface Skill {
  id: string;
  name: string;
  description: string;
  instructions: string;
  /** `null` = no restriction; `[]` = no tools at all. */
  allowed_tools: string[] | null;
  enabled: boolean;
  created_at: number;
  updated_at: number;
}

export const skillsApi = {
  list: (enabledOnly = false) =>
    apiGet<{ skills: Skill[] }>(`/api/skills${qs({ enabled_only: enabledOnly ? 'true' : undefined })}`),
  create: (body: {
    name: string;
    instructions?: string;
    allowed_tools?: string[] | null;
    description?: string;
    enabled?: boolean;
  }) => apiPost<Skill>('/api/skills', body),
  update: (id: string, body: Partial<Omit<Skill, 'id' | 'created_at' | 'updated_at'>>) =>
    apiPatch<Skill>(`/api/skills/${enc(id)}`, body),
  remove: (id: string) => apiDelete<{ deleted: boolean }>(`/api/skills/${enc(id)}`),
};

// ---------------------------------------------------------------------------
// Scheduled tasks
// ---------------------------------------------------------------------------

/** Mirrors `core.schedule_rules.Schedule`; `kind` is the discriminant. */
export type Schedule =
  | { kind: 'once' }
  | { kind: 'hourly'; hours: number }
  | { kind: 'daily'; at: string; timezone?: string }
  | { kind: 'weekly'; at: string; weekday: number; timezone?: string };

export interface ScheduledTask {
  id: string;
  name: string;
  prompt: string;
  schedule: Schedule;
  /** Epoch **milliseconds**, like `next_run_at` — not seconds. */
  anchored_at: number | null;
  instance_id: string | null;
  enabled: boolean;
  next_run_at: number | null;
  created_at: number;
  updated_at: number;
  schedule_label: string;
}

export interface ScheduledTaskRun {
  id: string;
  task_id: string;
  status: 'running' | 'ok' | 'failed';
  started_at: number;
  finished_at: number | null;
  conversation_id: string | null;
  error: string | null;
  error_code: string | null;
}

export const scheduledApi = {
  list: () => apiGet<{ tasks: ScheduledTask[] }>('/api/scheduled-tasks'),
  detail: (id: string) => apiGet<ScheduledTask>(`/api/scheduled-tasks/${enc(id)}`),
  create: (body: {
    name: string;
    prompt: string;
    schedule: Schedule;
    instance_id?: string | null;
    enabled?: boolean;
  }) => apiPost<ScheduledTask>('/api/scheduled-tasks', body),
  update: (
    id: string,
    body: Partial<{
      name: string;
      prompt: string;
      schedule: Schedule;
      instance_id: string | null;
      enabled: boolean;
    }>,
  ) => apiPatch<ScheduledTask>(`/api/scheduled-tasks/${enc(id)}`, body),
  remove: (id: string) => apiDelete<{ removed: boolean }>(`/api/scheduled-tasks/${enc(id)}`),
  runs: (id: string, limit = 20) =>
    apiGet<{ runs: ScheduledTaskRun[] }>(
      `/api/scheduled-tasks/${enc(id)}/runs${qs({ limit })}`,
    ),
  /** Execute now. A refused run answers 409 with `{code, message}`. */
  runNow: (id: string) =>
    apiPost<{ run_id: string; conversation_id: string }>(
      `/api/scheduled-tasks/${enc(id)}/run`,
      {},
      { timeoutMs: LONG_TIMEOUT_MS },
    ),
};

/**
 * Start a streaming chat completion for one instance.
 *
 * The caller owns the AbortController: a generation can legitimately run for
 * minutes, so this call has no timeout. It also does not parse the stream —
 * `ported/chatStream.ts` does, and is unit-tested independently.
 */
export function streamChatCompletion(
  instanceId: string,
  body: Record<string, unknown>,
  options: { signal?: AbortSignal } = {},
): Promise<Response> {
  return openStream(
    `/api/instances/${enc(instanceId)}/chat/completions`,
    body,
    options,
  );
}

export const chatApi = {
  /** Running instances that can accept chat completions. */
  instances: () =>
    apiGet<Array<Instance & { browser_url?: string }>>('/api/chat/instances'),
  executeTool: (body: { name: string; arguments: Record<string, unknown>; root: string }) =>
    apiPost<{ ok: boolean; result?: unknown; error?: string }>('/api/chat/tools/execute', body, {
      timeoutMs: LONG_TIMEOUT_MS,
    }),
  /**
   * Describe what an approved call would touch — resolved path, full argv, risk.
   * Pure: no execution. Called when the approval card renders.
   */
  previewTool: (body: { name: string; arguments: Record<string, unknown>; root: string }) =>
    apiPost<{
      ok: boolean;
      risk?: string;
      summary?: string;
      target?: string;
      argv?: string[];
      cwd?: string;
      reason?: string;
      diff?: { added?: number; removed?: number };
    }>('/api/chat/tools/preview', body, { timeoutMs: LONG_TIMEOUT_MS }),
};

// ---------------------------------------------------------------------------
// Knowledge
// ---------------------------------------------------------------------------

export const knowledgeApi = {
  list: () => apiGet<{ bases: KnowledgeBase[] }>('/api/knowledge'),
  /** Detail returns `{ base, documents }` — not a merged object. */
  detail: (id: string) => apiGet<KnowledgeDetail>(`/api/knowledge/${enc(id)}`),
  create: (body: Record<string, unknown>) => apiPost<KnowledgeBase>('/api/knowledge', body),
  update: (id: string, body: Record<string, unknown>) =>
    apiPatch<KnowledgeBase>(`/api/knowledge/${enc(id)}`, body),
  remove: (id: string) => apiDelete<Record<string, unknown>>(`/api/knowledge/${enc(id)}`),
  addDocuments: (id: string, paths: string[]) =>
    apiPost<{ added: number }>(
      `/api/knowledge/${enc(id)}/documents`,
      { paths },
      { timeoutMs: INDEX_TIMEOUT_MS },
    ),
  removeDocument: (id: string, documentId: string) =>
    apiDelete<Record<string, unknown>>(
      `/api/knowledge/${enc(id)}/documents/${enc(documentId)}`,
    ),
  chunks: (id: string, documentId: string, limit = 200) =>
    apiGet<{ document: KnowledgeDocument; chunks: KnowledgeChunk[] }>(
      `/api/knowledge/${enc(id)}/documents/${enc(documentId)}/chunks${qs({ limit })}`,
    ),
  index: (id: string, force = false) =>
    apiPost<{ scheduled: boolean; force: boolean }>(`/api/knowledge/${enc(id)}/index`, { force }),
  cancel: (id: string) =>
    apiPost<{ cancelled: boolean }>(`/api/knowledge/${enc(id)}/cancel`),
  search: (id: string, query: string, limit?: number) =>
    apiGet<{ results: KnowledgeSearchHit[] }>(
      `/api/knowledge/${enc(id)}/search${qs({ q: query, limit })}`,
      { timeoutMs: SEARCH_TIMEOUT_MS },
    ),
  unloadServices: () =>
    apiPost<{ released: number }>('/api/knowledge/services/unload'),
  servicesStatus: () => apiGet<KnowledgeServicesStatus>('/api/knowledge/services/status'),
};

// ---------------------------------------------------------------------------
// Presets / history / backups
// ---------------------------------------------------------------------------

export const presetsApi = {
  list: () => apiGet<Preset[]>('/api/presets'),
  detail: (id: string) => apiGet<Preset>(`/api/presets/${enc(id)}`),
  create: (body: Record<string, unknown>) => apiPost<Preset>('/api/presets', body),
  update: (id: string, body: Record<string, unknown>) =>
    apiPut<Preset>(`/api/presets/${enc(id)}`, body),
  remove: (id: string) => apiDelete<Record<string, unknown>>(`/api/presets/${enc(id)}`),
  clone: (id: string, name: string) =>
    apiPost<Preset>(`/api/presets/${enc(id)}/clone`, { name }),
  import: (body: unknown) => apiPost<Preset>('/api/presets/import', body),
  export: (id: string) => apiGet<Record<string, unknown>>(`/api/presets/${enc(id)}/export`),
  diff: (id1: string, id2: string) =>
    apiPost<Record<string, unknown>>('/api/presets/diff', { id1, id2 }),
};

/**
 * History.
 *
 * The id is a **number**: `history.db.launch_history.id` is a SQLite AUTOINCREMENT
 * and the route declares `record_id: int`. This client used to type every id as
 * `string` (copied from the other domains, whose ids are opaque `chat_…`/`proj_…`
 * tokens), which typechecked fine and would have shipped a call site passing the
 * wrong shape to `enc()`. §2.41: a type that disagrees with the route is a lie the
 * compiler cannot catch.
 */
export const historyApi = {
  list: (limit = 50, offset = 0) =>
    apiGet<{ records: HistoryRecord[]; total?: number }>(
      `/api/history${qs({ limit, offset })}`,
    ),
  detail: (id: number) => apiGet<HistoryRecord>(`/api/history/${enc(String(id))}`),
  remove: (id: number) => apiDelete<Record<string, unknown>>(`/api/history/${enc(String(id))}`),
  clear: () => apiPost<{ deleted: number }>('/api/history/clear'),
  relaunch: (id: number) => apiPost<Instance>(`/api/history/${enc(String(id))}/relaunch`),
};

export const backupsApi = {
  list: () => apiGet<{ backups: BackupEntry[]; pending?: string | null }>('/api/backups'),
  create: (includeLogs = false) =>
    apiPost<BackupEntry>('/api/backups', { include_logs: includeLogs }, {
      timeoutMs: INDEX_TIMEOUT_MS,
    }),
  inspect: (name: string) => apiGet<Record<string, unknown>>(`/api/backups/${enc(name)}`),
  remove: (name: string) => apiDelete<Record<string, unknown>>(`/api/backups/${enc(name)}`),
  restore: (name: string) =>
    apiPost<{ restart_required: boolean }>('/api/backups/restore', { name }),
  cancelPending: () => apiDelete<Record<string, unknown>>('/api/backups/pending'),
};

// ---------------------------------------------------------------------------
// Local API server (management plane only)
// ---------------------------------------------------------------------------

export const apiServerApi = {
  status: () => apiGet<ApiServerStatus>('/api/api-server/status'),
  logs: (limit = 100) =>
    apiGet<{ entries: ApiServerLogEntry[]; capacity: number }>(
      `/api/api-server/logs${qs({ limit })}`,
    ),
  clearLogs: () => apiDelete<Record<string, unknown>>('/api/api-server/logs'),
  restart: () => apiPost<ApiServerStatus>('/api/api-server/restart', {}),
  recipes: () => apiGet<{ recipes: ApiServerRecipe[] }>('/api/api-server/recipes'),
  /** Remove the gateway key. Its own route: an empty key in a config PUT means "keep". */
  clearKey: () => apiDelete<{ cleared: boolean }>('/api/api-server/key'),
};

// ---------------------------------------------------------------------------
// External engines (registry only)
// ---------------------------------------------------------------------------

export const enginesApi = {
  list: () => apiGet<EngineList>('/api/engines'),
  upsert: (body: {
    id?: string;
    kind: string;
    label?: string;
    base_url: string;
    model?: string;
    api_key_env?: string;
  }) => apiPost<EngineRecord>('/api/engines', body),
  remove: (id: string) => apiDelete<{ deleted: boolean }>(`/api/engines/${enc(id)}`),
  probe: (id: string) =>
    apiPost<{ ok: boolean; models: string[]; error: string | null; engine: EngineRecord | null }>(
      `/api/engines/${enc(id)}/probe`,
      undefined,
      { timeoutMs: LONG_TIMEOUT_MS },
    ),
  capabilities: (id: string) =>
    apiGet<{
      id: string;
      kind: string;
      launch: boolean;
      capabilities: string[];
      exact_token_count: boolean;
    }>(`/api/engines/${enc(id)}/capabilities`),
};

// ---------------------------------------------------------------------------
// Usage statistics
// ---------------------------------------------------------------------------

export const usageApi = {
  summary: (days = 30) => apiGet<UsageSummary>(`/api/usage/summary${qs({ days })}`),
};

// ---------------------------------------------------------------------------
// Aggregate export, for call sites that prefer one object
// ---------------------------------------------------------------------------

export const api = {
  system: systemApi,
  params: paramsApi,
  models: modelsApi,
  runtimes: runtimesApi,
  instances: instancesApi,
  chats: chatsApi,
  chat: chatApi,
  prompts: promptsApi,
  knowledge: knowledgeApi,
  presets: presetsApi,
  history: historyApi,
  backups: backupsApi,
  apiServer: apiServerApi,
  engines: enginesApi,
  usage: usageApi,
  memory: memoryApi,
  projects: projectsApi,
  skills: skillsApi,
  scheduled: scheduledApi,
  streamChatCompletion,
} as const;

export type { RequestOptions, SocketWatcher, SocketHandlers };
export type {
  ChatAttachment,
  ChatMessage,
  ChatToolCall,
  Conversation,
  ConversationDetail,
  Instance,
  ModelEntry,
  ParamSchema,
  Preset,
  RuntimeProfile,
};
