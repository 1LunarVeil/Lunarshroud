/**
 * Backend session and transport.
 *
 * Security contract (AGENTS.md §2.2) that this file must preserve:
 *
 * - In the packaged app the shell generates a fresh session id + token per
 *   backend generation and only publishes them after it has verified an
 *   HMAC proof over a nonce it chose. `get_backend_session` returns `null`
 *   while a generation is starting or restarting.
 * - We therefore **never** fall back to a hardcoded port inside Tauri. If the
 *   shell has not vouched for the peer, there is no safe URL to talk to: an
 *   arbitrary local process may hold that port, and handing it the token would
 *   let it rewrite `llama_server_path`.
 * - The token travels in `X-LunarShroud-Token`, never in a URL or query string.
 */

import { ApiError, formatApiErrorPayload } from './errors';

export interface BackendSession {
  url: string;
  token: string;
}

/** Tauri v2 exposes `window.__TAURI__.core.invoke`; v1 used `window.__TAURI__.invoke`. */
interface TauriGlobal {
  core?: { invoke?: (cmd: string, args?: unknown) => Promise<unknown> };
  invoke?: (cmd: string, args?: unknown) => Promise<unknown>;
}

declare global {
  interface Window {
    __TAURI__?: TauriGlobal;
    __LLAMA_BACKEND_URL__?: string;
    __LLAMA_BACKEND_URL_ERROR__?: string;
    __LLAMA_LAST_REQUEST_URL__?: string;
  }
}

export function inTauri(): boolean {
  return typeof window !== 'undefined' && Boolean(window.__TAURI__);
}

function invokeCommand<T>(command: string, args?: unknown): Promise<T> {
  const tauri = window.__TAURI__;
  const invoke = tauri?.core?.invoke ?? tauri?.invoke;
  if (!invoke) {
    return Promise.reject(new Error('Tauri IPC bridge is unavailable'));
  }
  return args === undefined
    ? (invoke(command) as Promise<T>)
    : (invoke(command, args) as Promise<T>);
}

export interface PathFilter {
  /** Shown in the dialog's file-type dropdown. */
  name: string;
  /** Extensions without the dot, e.g. `['exe']`. */
  extensions: string[];
}

export interface PickPathOptions {
  /** Restrict a file dialog to these types; ignored for directories. */
  filters?: PathFilter[];
  /** Directory the dialog opens in — usually the field's current value. */
  current?: string;
  /**
   * Question to ask when no native dialog is available (browser dev channel).
   * Omit to return `null` instead of prompting.
   */
  prompt?: string;
}

/**
 * Ask the OS for a file or directory, returning an absolute path or `null`.
 *
 * **`null` means "the user did not choose", not "error".** Every caller must leave
 * its field untouched on `null`: the two pre-existing call sites fall back to
 * `window.prompt` whenever the result is falsy, which pops a dialog every time the
 * user cancels. `prompt` here is opt-in and used only outside Tauri.
 *
 * This is the single place that talks to the shell's dialog commands. Before it,
 * `KnowledgePage` and `ChatPage` each hand-rolled a `window.__TAURI__` cast, and the
 * other eight path fields had no picker at all.
 */
export async function pickPath(
  kind: 'file' | 'dir',
  options: PickPathOptions = {},
): Promise<string | null> {
  if (inTauri()) {
    const command = kind === 'file' ? 'open_file_dialog' : 'open_directory_dialog';
    const args: Record<string, unknown> = {};
    if (options.current) args.current = options.current;
    if (kind === 'file' && options.filters?.length) args.filters = options.filters;
    try {
      const picked = await invokeCommand<string | null>(command, args);
      return typeof picked === 'string' && picked.trim() ? picked : null;
    } catch {
      return null;
    }
  }
  if (options.prompt === undefined) return null;
  const typed = window.prompt(options.prompt, options.current ?? '');
  return typed && typed.trim() ? typed.trim() : null;
}

/**
 * Reveal an absolute path in the OS file manager.
 *
 * One IPC wrapper, like `pickPath`: the shell command validates that the path is
 * absolute, exists, and is not a UNC/URL target before handing it to
 * `ShellExecuteW` (no bare `explorer.exe` — AGENTS §2.1). Outside Tauri there is
 * no OS integration, so this reports unsupported rather than pretending.
 */
export async function openPath(path: string): Promise<{ ok: boolean; error?: string }> {
  const trimmed = path.trim();
  if (!trimmed) return { ok: false, error: 'empty path' };
  if (!inTauri()) {
    return { ok: false, error: 'opening a folder needs the packaged app' };
  }
  try {
    await invokeCommand<null>('open_path', { path: trimmed });
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

let cached: BackendSession | null = null;
/** In-flight session lookup, so a burst of requests shares one IPC round trip. */
let pending: Promise<BackendSession> | null = null;

/** Drop the cached session. Must be called on 401 and before any retry, because
 *  a restarted backend issues a different token. */
export function resetBackendSession(): void {
  cached = null;
  pending = null;
}

/**
 * Resolve the backend base URL + token.
 *
 * Tauri: ask the shell. It returns `null` until a generation has passed the
 * readiness HMAC check — we wait and retry a bounded number of times rather
 * than connect to an unverified peer.
 *
 * Browser (source-run channel): the UI is served by the backend itself, so the
 * page origin *is* the backend origin. `tauri.localhost` is explicitly excluded
 * from that path — it is a WebView origin, not a server.
 */
export async function ensureBackendSession(): Promise<BackendSession> {
  if (cached) return cached;
  if (pending) return pending;

  pending = resolveSession().then(
    (session) => {
      cached = session;
      pending = null;
      window.__LLAMA_BACKEND_URL__ = session.url;
      return session;
    },
    (error: unknown) => {
      pending = null;
      throw error;
    },
  );
  return pending;
}

const READY_RETRY_DELAYS_MS = [250, 500, 1000, 2000] as const;

async function resolveSession(): Promise<BackendSession> {
  if (inTauri()) {
    for (let attempt = 0; attempt <= READY_RETRY_DELAYS_MS.length; attempt += 1) {
      const session = await invokeCommand<BackendSession | null>('get_backend_session');
      if (session && session.url) {
        return { url: session.url, token: session.token ?? '' };
      }
      const delay = READY_RETRY_DELAYS_MS[attempt];
      if (delay === undefined) break;
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
    window.__LLAMA_BACKEND_URL_ERROR__ = 'backend not ready';
    // No hardcoded-port fallback here on purpose — see the file header.
    throw new Error('The backend session is not ready yet');
  }

  const location = window.location;
  const hostname = location?.hostname ?? '';
  if (location && location.protocol.startsWith('http') && !hostname.endsWith('tauri.localhost')) {
    return { url: location.origin, token: '' };
  }

  // Source-run development with the UI served from Vite (port 5173) while the
  // backend listens on its own port. Only reachable outside a packaged build.
  const devPort = import.meta.env.VITE_BACKEND_PORT;
  if (typeof devPort === 'string' && devPort) {
    return { url: `http://127.0.0.1:${devPort}`, token: '' };
  }

  throw new Error('Cannot determine the backend URL outside Tauri');
}

export interface RequestOptions {
  /** Milliseconds before the request is aborted. `null` disables the timeout. */
  timeoutMs?: number | null;
  signal?: AbortSignal;
  /** Extra headers, merged after the defaults. */
  headers?: Record<string, string>;
  /** Skip the single automatic retry (used by calls that must not double-execute). */
  noRetry?: boolean;
}

const DEFAULT_TIMEOUT_MS = 20_000;
/** Requests that can legitimately take minutes (model probes, embedding cold start). */
export const LONG_TIMEOUT_MS = 45_000;
export const INDEX_TIMEOUT_MS = 120_000;
export const SEARCH_TIMEOUT_MS = 300_000;
const SESSION_RETRY_DELAY_MS = 1_500;

/** Build a query string, dropping empty values (mirrors the 1.0 behaviour). */
export function buildQuery(params: Record<string, unknown> | undefined): string {
  if (!params) return '';
  const parts: string[] = [];
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
  }
  return parts.length ? `?${parts.join('&')}` : '';
}

async function readBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get('content-type') ?? '';
  if (contentType.includes('application/json')) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
  const text = await response.text();
  return text || null;
}

/** Internal: one fetch attempt with a timeout and a merged abort signal. */
async function attempt(
  method: string,
  path: string,
  body: unknown,
  options: RequestOptions,
): Promise<Response> {
  const session = await ensureBackendSession();
  const url = `${session.url}${path}`;
  window.__LLAMA_LAST_REQUEST_URL__ = url;

  const headers: Record<string, string> = { ...options.headers };
  if (session.token) headers['X-LunarShroud-Token'] = session.token;
  if (body !== undefined && method !== 'GET' && method !== 'HEAD') {
    headers['Content-Type'] = 'application/json';
  }

  const timeoutMs = options.timeoutMs === undefined ? DEFAULT_TIMEOUT_MS : options.timeoutMs;
  const controller = new AbortController();
  const timer =
    timeoutMs === null ? null : setTimeout(() => controller.abort(), timeoutMs);
  // A caller-supplied signal (user pressed stop) must cancel the request too.
  const onCallerAbort = () => controller.abort();
  options.signal?.addEventListener('abort', onCallerAbort, { once: true });

  try {
    return await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } finally {
    if (timer !== null) clearTimeout(timer);
    options.signal?.removeEventListener('abort', onCallerAbort);
  }
}

/**
 * Perform a JSON request.
 *
 * Retry policy, and why it is asymmetric:
 * - a 401 means the backend restarted with a new token → refresh the session and
 *   replay once. Safe for every method because the first attempt was rejected
 *   before the handler ran.
 * - a network failure is retried **only** for GET/HEAD. A POST may already have
 *   been executed; replaying it could start a second instance or duplicate a
 *   preset.
 */
export async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  options: RequestOptions = {},
): Promise<T> {
  const canRetry = !options.noRetry;
  let response: Response;
  try {
    response = await attempt(method, path, body, options);
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      if (options.signal?.aborted) throw error;
      throw new ApiError('The request timed out', 0, null);
    }
    const idempotent = method === 'GET' || method === 'HEAD';
    if (canRetry && idempotent) {
      await new Promise((resolve) => setTimeout(resolve, SESSION_RETRY_DELAY_MS));
      resetBackendSession();
      response = await attempt(method, path, body, options);
    } else {
      throw new ApiError(
        error instanceof Error ? error.message : 'Network request failed',
        0,
        null,
      );
    }
  }

  if (response.status === 401 && canRetry) {
    resetBackendSession();
    response = await attempt(method, path, body, options);
  }

  const payload = await readBody(response);
  if (!response.ok) {
    throw new ApiError(
      formatApiErrorPayload(payload, `HTTP ${response.status}`),
      response.status,
      payload,
    );
  }
  return payload as T;
}

export const apiGet = <T>(path: string, options?: RequestOptions) =>
  request<T>('GET', path, undefined, options);
export const apiPost = <T>(path: string, body?: unknown, options?: RequestOptions) =>
  request<T>('POST', path, body, options);
export const apiPut = <T>(path: string, body?: unknown, options?: RequestOptions) =>
  request<T>('PUT', path, body, options);
export const apiPatch = <T>(path: string, body?: unknown, options?: RequestOptions) =>
  request<T>('PATCH', path, body, options);
export const apiDelete = <T>(path: string, options?: RequestOptions) =>
  request<T>('DELETE', path, undefined, options);

/**
 * Open a streaming request (SSE chat completion).
 *
 * No client timeout: a single generation can run for minutes, and the caller
 * owns cancellation through `signal`. A 401 is replayed once with a refreshed
 * session, which is why the fetch is wrapped rather than reused from `request`.
 */
export async function openStream(
  path: string,
  body: unknown,
  options: { signal?: AbortSignal } = {},
): Promise<Response> {
  const doFetch = async (): Promise<Response> => {
    const session = await ensureBackendSession();
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (session.token) headers['X-LunarShroud-Token'] = session.token;
    window.__LLAMA_LAST_REQUEST_URL__ = `${session.url}${path}`;
    return fetch(`${session.url}${path}`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      ...(options.signal ? { signal: options.signal } : {}),
    });
  };

  let response = await doFetch();
  if (response.status === 401) {
    resetBackendSession();
    response = await doFetch();
  }
  if (!response.ok) {
    const payload = await readBody(response);
    throw new ApiError(
      formatApiErrorPayload(payload, `HTTP ${response.status}`),
      response.status,
      payload,
    );
  }
  return response;
}

export type SocketStatus = 'connected' | 'reconnecting' | 'closed';

export interface SocketHandlers {
  onMessage: (message: unknown) => void;
  onStatus?: (status: SocketStatus) => void;
}

export interface SocketWatcher {
  close: () => void;
}

/** 1s → 2s → 4s → 8s → 15s cap. */
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000] as const;

/**
 * A WebSocket that reconnects with exponential backoff and refreshes the session
 * before each attempt (a restarted backend issues a new token).
 *
 * The token is passed as a subprotocol, never as a query parameter: the backend
 * validates origin + token in the handshake, and URLs end up in logs.
 */
export function watchSocket(path: string, handlers: SocketHandlers): SocketWatcher {
  let socket: WebSocket | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;
  let attemptIndex = 0;

  const notify = (status: SocketStatus) => {
    try {
      handlers.onStatus?.(status);
    } catch {
      // A broken status callback must not stop reconnection.
    }
  };

  const scheduleReconnect = () => {
    if (closed) return;
    notify('reconnecting');
    const delay = RECONNECT_DELAYS_MS[Math.min(attemptIndex, RECONNECT_DELAYS_MS.length - 1)] ?? 15000;
    attemptIndex += 1;
    timer = setTimeout(() => {
      timer = null;
      resetBackendSession();
      void open();
    }, delay);
  };

  const open = async () => {
    if (closed) return;
    let url: string;
    let token: string;
    try {
      const session = await ensureBackendSession();
      url = `${session.url.replace(/^http/, 'ws')}${path}`;
      token = session.token;
    } catch {
      scheduleReconnect();
      return;
    }
    if (closed) return;

    const sock = token ? new WebSocket(url, ['lunarshroud', token]) : new WebSocket(url);
    socket = sock;

    sock.onopen = () => {
      attemptIndex = 0;
      notify('connected');
    };
    sock.onmessage = (event: MessageEvent<string>) => {
      if (closed) return;
      let message: unknown;
      try {
        message = JSON.parse(event.data);
      } catch {
        console.error('[ws] unparseable frame dropped');
        return;
      }
      try {
        handlers.onMessage(message);
      } catch (error) {
        // A throwing handler must not silently kill live updates.
        console.error('[ws] message handler failed', error);
      }
    };
    sock.onclose = () => {
      if (closed || socket !== sock) return;
      socket = null;
      scheduleReconnect();
    };
    sock.onerror = () => {
      // `onclose` follows; reconnection is handled there exactly once.
    };
  };

  void open();

  return {
    close() {
      closed = true;
      notify('closed');
      if (timer !== null) clearTimeout(timer);
      if (socket) {
        try {
          socket.close();
        } catch {
          // already closed
        }
        socket = null;
      }
    },
  };
}

/**
 * Subscribe to a custom `X-LunarShroud-*` response header.
 *
 * Every header the UI reads must also appear in the backend's CORS
 * `expose_headers` list, or the packaged build (cross-origin) reads `null`.
 * `backend/tests/test_frontend_contracts.py` scans this file's siblings for
 * `headers.get('x-lunarshroud-…')` call sites and cross-checks the list.
 */
export function readContractHeader(response: Response, name: string): string | null {
  return response.headers.get(name);
}
