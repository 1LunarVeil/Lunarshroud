/**
 * Tool-call policy for the chat page: which tools may run, and what a call will
 * touch before the user approves it.
 *
 * Ported from `frontend/js/chat_tools.mjs`. The 1.0 module only handles the three
 * read-only llama-server tools; 2.0 adds write and command tools in the Agent
 * workbench (Phase 3), which live in `agentTools.ts`. Both share the path
 * sandbox below — that sandbox is the security boundary and must have exactly one
 * implementation.
 *
 * Why the whitelist exists at all: llama-server's own `--tools` executes
 * read_file/grep_search/grep on the host with **no approval step** (measured on
 * b11929: `--help` offers `--tools` and `--tools-runtime`, no "ask first" switch).
 * Inheriting that would hand the model a silent read of `%USERPROFILE%`. So the
 * launcher declares tools itself, shows an approval bar, and calls
 * `POST /api/chat/tools/execute` only after a human says yes.
 *
 * Pure functions, no DOM, no filesystem: the browser has no `fs` access and every
 * verdict here must be exhaustively testable.
 */

/** Tools that may be executed. Names match llama-server's built-ins. */
export const READ_ONLY_TOOLS = Object.freeze(['read_file', 'file_glob_search', 'grep_search'] as const);

export type ReadOnlyTool = (typeof READ_ONLY_TOOLS)[number];

export interface ToolDeclaration {
  type: 'function';
  function: {
    name: string;
    description: string;
    parameters: {
      type: 'object';
      properties: Record<string, { type: string; description: string; items?: { type: string } }>;
      required: string[];
    };
  };
}

/**
 * Tool declarations sent to the model.
 *
 * The description states "paths are relative to the authorized folder" because
 * the model cannot see the local filesystem; without it, it tries drive-letter
 * absolute paths and is rejected every time.
 *
 * Typed as a mutable array literal so the object shapes widen to
 * `Record<string, …>` rather than narrowing to exact key sets.
 */
export const TOOL_DECLARATIONS: ToolDeclaration[] = [
  {
    type: 'function',
    function: {
      name: 'read_file',
      description:
        'Read a UTF-8 text file inside the folder the user authorized. The path must be relative to that folder.',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'Path relative to the authorized folder.' },
        },
        required: ['path'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'file_glob_search',
      description:
        'List files inside the authorized folder whose name or relative path matches a glob pattern (e.g. "*.md").',
      parameters: {
        type: 'object',
        properties: {
          pattern: { type: 'string', description: 'Glob pattern, e.g. "*.txt".' },
          path: { type: 'string', description: 'Optional subfolder, relative to the authorized folder.' },
        },
        required: ['pattern'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'grep_search',
      description: 'Search for a literal string in text files inside the authorized folder.',
      parameters: {
        type: 'object',
        properties: {
          query: { type: 'string', description: 'Literal text to search for.' },
          path: { type: 'string', description: 'Optional subfolder, relative to the authorized folder.' },
          pattern: { type: 'string', description: 'Optional file name filter, e.g. "*.log".' },
        },
        required: ['query'],
      },
    },
  },
];

/** Byte cap for one read; must stay below `chat_store.MAX_CONTENT_CHARS`. */
export const MAX_READ_BYTES = 256 * 1024;
export const MAX_SEARCH_RESULTS = 100;

export type ToolRejectionReason =
  | 'not-allowed'
  | 'not-a-name'
  | 'no-root'
  | 'empty-path'
  | 'absolute-path'
  | 'unc-path'
  | 'outside-root'
  | 'is-root'
  | 'bad-arguments'
  | 'missing-pattern'
  | 'missing-query'
  | 'pattern-escape'
  | 'tool-not-allowed'
  | 'tool-unknown';

export interface ToolPathVerdict {
  ok: boolean;
  path?: string;
  reason?: ToolRejectionReason;
}

function normalizeSlashes(value: string): string {
  return value.replace(/\\/g, '/');
}

/** Collapse `.` and `..`; escaping above the root yields a sentinel that cannot match. */
function normalizeSegments(value: string): string {
  const segments: string[] = [];
  for (const part of value.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') {
      if (!segments.length) return '\u0000escaped';
      segments.pop();
      continue;
    }
    segments.push(part);
  }
  return segments.join('/');
}

/**
 * Resolve a model-supplied path to an absolute path inside the authorized root,
 * or reject it.
 *
 * Every rejection below is a real attack surface, not pedantry:
 * - **absolute path** (`C:\Windows\...`, `/etc/passwd`): the model can invent any
 *   path, and there is no reason to read outside the authorized root;
 * - **`..` escape** (`../../secrets`): only visible after normalization, so the
 *   comparison must happen on the normalized form;
 * - **drive prefix** (`D:foo`, a legal *relative* path on Windows meaning "the
 *   current directory on D:") whose meaning depends on the process CWD;
 * - **UNC path** (`\\server\share`) which initiates a network request.
 */
export function resolveToolPath(root: string, candidate: string | null | undefined): ToolPathVerdict {
  const base = normalizeSlashes(String(root || '').trim());
  // Test UNC **before** normalization: `\\server\share` becomes `//server/share`,
  // which the "starts with / = absolute" rule would catch first, and the user
  // would see `absolute-path` instead of the more accurate `unc-path`.
  const rawInput = String(candidate ?? '').trim();
  if (rawInput.startsWith('\\\\') || rawInput.startsWith('//')) {
    return { ok: false, reason: 'unc-path' };
  }
  const raw = normalizeSlashes(rawInput);
  if (!base) return { ok: false, reason: 'no-root' };
  if (!raw) return { ok: false, reason: 'empty-path' };
  if (/^[a-z]:/i.test(raw)) return { ok: false, reason: 'absolute-path' };
  if (raw.startsWith('/')) return { ok: false, reason: 'absolute-path' };

  const joined = normalizeSegments(`${base}/${raw}`);
  const normalizedBase = normalizeSegments(base);
  // The prefix comparison must include the separator: `C:/data-evil` is not a
  // child of `C:/data`.
  if (joined !== normalizedBase && !joined.startsWith(`${normalizedBase}/`)) {
    return { ok: false, reason: 'outside-root' };
  }
  if (joined === normalizedBase) return { ok: false, reason: 'is-root' };
  return { ok: true, path: joined };
}

/**
 * Same as `resolveToolPath`, but the root itself is legal.
 *
 * Search tools default to `path: '.'` ("search the whole authorized folder"),
 * which is the common case, not an escape. Read tools must not accept it — "read
 * a directory" is meaningless.
 */
export function resolveSearchRoot(
  root: string,
  candidate: string | null | undefined,
): ToolPathVerdict {
  const base = normalizeSlashes(String(root || '').trim());
  const rawInput = String(candidate ?? '').trim();
  if (rawInput.startsWith('\\\\') || rawInput.startsWith('//')) {
    return { ok: false, reason: 'unc-path' };
  }
  const raw = normalizeSlashes(rawInput);
  if (!base) return { ok: false, reason: 'no-root' };
  if (/^[a-z]:/i.test(raw)) return { ok: false, reason: 'absolute-path' };
  if (raw.startsWith('/')) return { ok: false, reason: 'absolute-path' };
  const normalizedBase = normalizeSegments(base);
  // Empty or "." explicitly means "the whole authorized folder".
  if (!raw || raw === '.') return { ok: true, path: normalizedBase };
  const joined = normalizeSegments(`${base}/${raw}`);
  if (joined !== normalizedBase && !joined.startsWith(`${normalizedBase}/`)) {
    return { ok: false, reason: 'outside-root' };
  }
  return { ok: true, path: joined };
}

export interface ExecutableVerdict {
  ok: boolean;
  reason?: 'not-allowed' | 'not-a-name';
}

export function isExecutableTool(name: unknown): ExecutableVerdict {
  if (typeof name !== 'string' || !name.trim()) return { ok: false, reason: 'not-a-name' };
  const tool = name.trim();
  if (!(READ_ONLY_TOOLS as ReadonlyArray<string>).includes(tool) && !(tool in TOOL_RISK)) {
    return { ok: false, reason: 'not-allowed' };
  }
  return { ok: true };
}

/**
 * Parse a model-supplied argument string.
 *
 * Returns `null` rather than throwing: the model occasionally emits malformed
 * JSON, and the UI must show "arguments are not valid JSON" and block execution
 * instead of letting the whole render crash.
 */
export function parseToolCallArguments(raw: unknown): Record<string, unknown> | null {
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  const text = String(raw ?? '').trim();
  if (!text) return {};
  try {
    const parsed: unknown = JSON.parse(text);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

export interface ToolExecutionPlan {
  ok: boolean;
  tool: string;
  summary: string;
  path?: string;
  reason?: ToolRejectionReason;
}

/**
 * Readable preview of what approving this call would do.
 *
 * An approval bar that only says "allow get_weather" gives the user nothing to
 * judge. AGENTS §2.3's "a capability is not an escalation hatch" lands here: the
 * actual arguments must be laid out before the decision.
 */
export function describeToolExecution(
  name: unknown,
  rawArguments: unknown,
  root: string,
): ToolExecutionPlan {
  const verdict = isExecutableTool(name);
  const tool = String(name ?? '').trim();
  if (!verdict.ok) {
    return {
      ok: false,
      tool,
      summary: '',
      reason: verdict.reason === 'not-allowed' ? 'tool-not-allowed' : 'tool-unknown',
    };
  }
  const args = parseToolCallArguments(rawArguments);
  if (args === null) return { ok: false, tool, summary: '', reason: 'bad-arguments' };

  if (tool === 'read_file') {
    const resolved = resolveToolPath(root, String(args.path ?? ''));
    if (!resolved.ok) return { ok: false, tool, summary: '', ...(resolved.reason ? { reason: resolved.reason } : {}) };
    return { ok: true, tool, summary: resolved.path ?? '', ...(resolved.path ? { path: resolved.path } : {}) };
  }

  if (tool === 'file_glob_search') {
    const pattern = String(args.pattern ?? args.glob ?? '').trim();
    if (!pattern) return { ok: false, tool, summary: '', reason: 'missing-pattern' };
    // The glob itself is never resolved to a path (it walks a directory), but a
    // `..` in the pattern is meaningless and could make some implementations walk
    // outside the root.
    if (pattern.includes('..')) return { ok: false, tool, summary: '', reason: 'pattern-escape' };
    const resolved = resolveSearchRoot(root, String(args.path ?? '.'));
    if (!resolved.ok) return { ok: false, tool, summary: '', ...(resolved.reason ? { reason: resolved.reason } : {}) };
    return {
      ok: true,
      tool,
      summary: `${resolved.path ?? ''} :: ${pattern}`,
      ...(resolved.path ? { path: resolved.path } : {}),
    };
  }

  if (tool === 'grep_search') {
    const query = String(args.query ?? args.pattern ?? '').trim();
    if (!query) return { ok: false, tool, summary: '', reason: 'missing-query' };
    const resolved = resolveSearchRoot(root, String(args.path ?? '.'));
    if (!resolved.ok) return { ok: false, tool, summary: '', ...(resolved.reason ? { reason: resolved.reason } : {}) };
    return {
      ok: true,
      tool,
      summary: `${resolved.path ?? ''} :: ${query}`,
      ...(resolved.path ? { path: resolved.path } : {}),
    };
  }

  if (tool === 'write_file' || tool === 'edit_file') {
    const resolved = resolveToolPath(root, String(args.path ?? ''));
    if (!resolved.ok) return { ok: false, tool, summary: '', ...(resolved.reason ? { reason: resolved.reason } : {}) };
    if (tool === 'write_file' && typeof args.content !== 'string') {
      return { ok: false, tool, summary: '', reason: 'bad-arguments' };
    }
    if (tool === 'edit_file') {
      const oldText = args.old_text ?? args.oldText;
      const newText = args.new_text ?? args.newText;
      if (typeof oldText !== 'string' || !oldText) {
        return { ok: false, tool, summary: '', reason: 'bad-arguments' };
      }
      if (typeof newText !== 'string') {
        return { ok: false, tool, summary: '', reason: 'bad-arguments' };
      }
    }
    // The path is what the user is agreeing to overwrite; that is the whole point
    // of showing a preview for a write.
    return {
      ok: true,
      tool,
      summary: resolved.path ?? '',
      ...(resolved.path ? { path: resolved.path } : {}),
    };
  }

  if (tool === 'run_command') {
    const argv = Array.isArray(args.argv) ? args.argv.map((item) => String(item)) : [];
    if (!argv.length) return { ok: false, tool, summary: '', reason: 'bad-arguments' };
    // No shell: the raw argv is what will run, and it is what the user must see.
    // Joining with spaces is for display only; the executor passes the list.
    const cwd = String(args.cwd ?? '.').trim() || '.';
    const resolved = resolveSearchRoot(root, cwd);
    if (!resolved.ok) return { ok: false, tool, summary: '', ...(resolved.reason ? { reason: resolved.reason } : {}) };
    return {
      ok: true,
      tool,
      summary: `${resolved.path ?? ''}> ${argv.join(' ')}`,
      ...(resolved.path ? { path: resolved.path } : {}),
    };
  }

  if (tool === 'web_search') {
    const query = String(args.query ?? '').trim();
    if (!query) return { ok: false, tool, summary: '', reason: 'missing-query' };
    // The query itself is the sensitive part: it is about to leave the machine,
    // so it is spelled out in full on the card.
    return { ok: true, tool, summary: query };
  }

  return { ok: false, tool, summary: '', reason: 'tool-not-allowed' };
}

export interface ToolResultMessage {
  role: 'tool';
  tool_call_id: string;
  content: string;
}

/** Wrap one execution result into the `tool` message fed back to the model. */
export function buildToolResultMessage(
  call: { id?: string; tool_call_id?: string } | null | undefined,
  { content }: { ok?: boolean; content?: unknown },
): ToolResultMessage | null {
  const id = call && (call.id || call.tool_call_id);
  if (!id) return null;
  return {
    role: 'tool',
    tool_call_id: String(id),
    content: String(content ?? '').slice(0, MAX_READ_BYTES),
  };
}

/** Whether every call in an assistant message already has a result (no re-prompt). */
export function pendingToolCalls(message: {
  toolCalls?: Array<{ id?: string; tool_call_id?: string }> | null;
  toolResults?: Record<string, unknown> | null;
} | null | undefined): Array<{ id?: string; tool_call_id?: string }> {
  const calls = Array.isArray(message?.toolCalls) ? message.toolCalls : [];
  const results =
    message?.toolResults && typeof message.toolResults === 'object' ? message.toolResults : {};
  return calls.filter((call) => {
    const id = call && (call.id || call.tool_call_id);
    return Boolean(id) && !(id && id in results);
  });
}

/**
 * Declarations for the write and command tools.
 *
 * These are the Agent workbench's tools, now reachable from the chat page when
 * the conversation enables agent mode. Each one **requires approval per call** —
 * the same gate the workbench uses. The risk classes (`write` / `command`) are the
 * backend's (`agent_tools.RISK_LEVELS`); the UI only decides presentation.
 *
 * A separate list rather than a flag on `TOOL_DECLARATIONS` because the two sets
 * have different prerequisites: reads need a folder, writes/commands need a folder
 * **and** an explicit opt-in. Merging them would make it impossible to offer
 * reads without also offering `run_command`.
 */
export const AGENT_TOOL_DECLARATIONS: ToolDeclaration[] = [
  {
    type: 'function',
    function: {
      name: 'write_file',
      description:
        'Create or replace a UTF-8 text file inside the authorized folder. The write is atomic. Requires approval.',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'Path relative to the authorized folder.' },
          content: { type: 'string', description: 'The full file content to write.' },
        },
        required: ['path', 'content'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'edit_file',
      description:
        'Replace one exact, unique substring in a file inside the authorized folder. Fails if the text is not found or not unique. Requires approval.',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'Path relative to the authorized folder.' },
          old_text: { type: 'string', description: 'Exact text to replace.' },
          new_text: { type: 'string', description: 'Replacement text.' },
          replace_all: { type: 'boolean', description: 'Replace every occurrence instead of requiring uniqueness.' },
        },
        required: ['path', 'old_text', 'new_text'],
      },
    },
  },
  {
    type: 'function',
    function: {
      name: 'run_command',
      description:
        'Run a program with an argument list (no shell). argv[0] is resolved to an absolute path. Requires approval, and the approval card shows the full command line.',
      parameters: {
        type: 'object',
        properties: {
          argv: {
            type: 'array',
            description: 'Program and arguments, e.g. ["git", "status", "--short"].',
            items: { type: 'string' },
          },
          timeout_seconds: { type: 'number', description: 'Optional timeout; capped by the backend.' },
          cwd: { type: 'string', description: 'Optional working directory, relative to the authorized folder.' },
        },
        required: ['argv'],
      },
    },
  },
];

/** The web-search declaration; sent only when settings enable it. */
export const WEB_SEARCH_DECLARATION: ToolDeclaration = {
  type: 'function',
  function: {
    name: 'web_search',
    description:
      'Search the web through the endpoint the user configured. Requires approval for every call.',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'The search query.' },
      },
      required: ['query'],
    },
  },
};

/** Risk class per tool, mirroring `backend/core/agent_tools.RISK_LEVELS`. */
export const TOOL_RISK: Readonly<Record<string, 'normal' | 'write' | 'command'>> = Object.freeze({
  read_file: 'normal',
  file_glob_search: 'normal',
  grep_search: 'normal',
  write_file: 'write',
  edit_file: 'write',
  run_command: 'command',
  web_search: 'command',
});

/** Tools that always need a human answer before running. */
export function needsApproval(toolName: string): boolean {
  return TOOL_RISK[toolName] !== 'normal';
}

/**
 * Declarations for a chat turn.
 *
 * Prerequisites are cumulative and explicit:
 * - a tool root is required for anything that touches the filesystem;
 * - write/command tools additionally require `agentMode`;
 * - `web_search` additionally requires the setting to be enabled.
 *
 * Server-side `--tools all` still wins (returns `[]`): two implementations of one
 * capability diverge, and the built-in one has no approval step.
 */
export function chatToolDeclarations(options: {
  declaredTools?: string[] | null;
  toolRoot?: string | null;
  agentMode?: boolean;
  webSearchEnabled?: boolean;
  webSearchConfigured?: boolean;
}): ToolDeclaration[] {
  const enabled = Array.isArray(options.declaredTools) ? options.declaredTools : [];
  if (enabled.length) return [];
  const hasRoot = Boolean(String(options.toolRoot || '').trim());
  if (!hasRoot) return [];

  const out = TOOL_DECLARATIONS.slice();
  if (options.agentMode) out.push(...AGENT_TOOL_DECLARATIONS);
  if (options.webSearchEnabled && options.webSearchConfigured) out.push(WEB_SEARCH_DECLARATION);
  return out;
}

/**
 * Which tool declarations to attach to this request.
 *
 * Rule: **server capability wins**.
 * - instance started with `--tools` → tools execute inside llama-server on the
 *   host with no approval step; the chat page does not declare a second copy,
 *   because two implementations of one capability diverge. Returns `[]`.
 * - no built-in tools and the user authorized a folder → declare all read-only
 *   tools and use the standard `tool_calls` round trip (each one approved).
 *
 * This is a pure function for two reasons: the "instance has no built-in tools"
 * state cannot be reached by clicking in a browser, and it is exactly where 1.0
 * had a bug — the old implementation filtered declarations by an `enabled` list
 * that was necessarily empty in that branch, so the result was **always empty**
 * and the entire client-side tool path (approval bar, execute endpoint, 220-line
 * backend executor) was dead code.
 */
export function selectToolDeclarations(
  declaredTools: string[] | null | undefined,
  toolRoot: string | null | undefined,
  declarations: ReadonlyArray<ToolDeclaration> = TOOL_DECLARATIONS,
): ToolDeclaration[] {
  const enabled = Array.isArray(declaredTools) ? declaredTools : [];
  if (enabled.length) return [];
  if (!String(toolRoot || '').trim()) return [];
  return declarations.slice();
}
