// 工具调用的**执行侧**策略：哪些工具真的可以跑、跑之前要什么约束。
//
// 为什么这里只有三个只读工具：llama-server 自带的 `--tools` 是 read_file /
// file_glob_search / grep_search，它们在**宿主机上直接执行**、没有批准环节
// （实测 b11929 的 --help 只有 `--tools` 与 `--tools-runtime`，没有"先问用户"
// 的开关）。照搬进来等于给模型一个静默读了 %USERPROFILE% 就能把内容发出去的
// 能力。所以本模块的定位是：
//
//   1. 只承认这三种**只读**工具；写文件/执行命令一律不在名单里。
//   2. 所有路径必须先过 `resolveToolPath`：绝对路径、`..` 逃逸、reparse point
//      一律拒绝，且必须落在用户**显式授权**的根目录内。
//   3. 执行仍然要人批准（见 chat.js 的批准条）。本模块只负责"如果批准了，
//      它最多能做到什么"。
//
// 纯函数、不碰 DOM、不碰文件系统：便于穷举测试。真正的 IO 在调用方。

/** 允许执行的工具。名字与 llama-server 内置工具一致，便于用户对照。 */
export const READ_ONLY_TOOLS = Object.freeze(['read_file', 'file_glob_search', 'grep_search']);

/**
 * 发给模型的工具声明。
 *
 * 描述里明确写"路径相对于已授权目录"，因为模型看不到本地文件系统；不写这句，
 * 它会尝试带盘符的绝对路径，然后每次都被拒。
 */
export const TOOL_DECLARATIONS = Object.freeze([
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
]);

/** 单次读取的字节上限。超过就截断并明确标注，绝不静默给半截内容。 */
export const MAX_READ_BYTES = 256 * 1024;
/** 搜索结果条数上限。 */
export const MAX_SEARCH_RESULTS = 100;

/**
 * 判断一个工具名能不能被执行。
 *
 * @returns {{ok: boolean, reason?: 'not-allowed'|'not-a-name'}}
 */
export function isExecutableTool(name) {
  if (typeof name !== 'string' || !name.trim()) return { ok: false, reason: 'not-a-name' };
  if (!READ_ONLY_TOOLS.includes(name.trim())) return { ok: false, reason: 'not-allowed' };
  return { ok: true };
}

/**
 * 把模型给的路径解析成"根目录内的绝对路径"，否则拒绝。
 *
 * 拒绝的情形（每一条都是真实攻击面，不是洁癖）：
 * - **绝对路径**：`C:\Windows\...`、`/etc/passwd` —— 模型可以编出任何路径，
 *   而授权根之外的读取没有任何理由。
 * - **`..` 逃逸**：`../../secrets` 在被拼接后才暴露，所以必须先规范化再比较。
 * - **盘符前缀**：`D:foo`（相对当前 D 盘目录）在 Windows 上是合法的相对路径，
 *   含义取决于进程 CWD —— 不可预测。
 * - **UNC 路径**：`\\server\share` 会发起网络请求。
 *
 * @param {string} root   用户授权的根目录（已规范化的绝对路径）
 * @param {string} candidate 模型给的路径
 * @returns {{ok: boolean, path?: string, reason?: string}}
 */
export function resolveToolPath(root, candidate) {
  const base = normalizeSlashes(String(root || '').trim());
  // 归一化之前先判 UNC：`\\server\share` 归一化后是 `//server/share`，会被
  // 下面的"以 / 开头 = 绝对路径"规则先抓到，于是用户看到的是 `absolute-path`
  // 而不是更准确的 `unc-path`。顺序在这里是有语义的。
  const rawInput = String(candidate == null ? '' : candidate).trim();
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
  // 前缀比较必须带上分隔符：`C:/data-evil` 不该被当成 `C:/data` 的子路径。
  if (joined !== normalizedBase && !joined.startsWith(`${normalizedBase}/`)) {
    return { ok: false, reason: 'outside-root' };
  }
  if (joined === normalizedBase) return { ok: false, reason: 'is-root' };
  return { ok: true, path: joined };
}

/**
 * 与 `resolveToolPath` 相同，但"根目录本身"是合法的。
 *
 * 搜索类工具的默认 `path` 就是 `.`（"搜整个授权目录"），那不是逃逸，是最常见
 * 的用法。读取类工具不能接受它 —— "读一个目录"没有意义。
 */
export function resolveSearchRoot(root, candidate) {
  const base = normalizeSlashes(String(root || '').trim());
  const rawInput = String(candidate == null ? '' : candidate).trim();
  if (rawInput.startsWith('\\\\') || rawInput.startsWith('//')) {
    return { ok: false, reason: 'unc-path' };
  }
  const raw = normalizeSlashes(rawInput);
  if (!base) return { ok: false, reason: 'no-root' };
  if (/^[a-z]:/i.test(raw)) return { ok: false, reason: 'absolute-path' };
  if (raw.startsWith('/')) return { ok: false, reason: 'absolute-path' };
  const normalizedBase = normalizeSegments(base);
  // 空串或 "." 明确表示"整个授权目录"
  if (!raw || raw === '.') return { ok: true, path: normalizedBase };
  const joined = normalizeSegments(`${base}/${raw}`);
  if (joined !== normalizedBase && !joined.startsWith(`${normalizedBase}/`)) {
    return { ok: false, reason: 'outside-root' };
  }
  return { ok: true, path: joined };
}

function normalizeSlashes(value) {
  return value.replace(/\\/g, '/');
}

/** 消掉 `.` 与 `..`。逃逸到根之上时返回空串的父级标记，让比较必然失败。 */
function normalizeSegments(value) {
  const segments = [];
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
 * 把模型给的 arguments（JSON 字符串）解析成对象。
 *
 * 解析失败返回 `null` 而不是抛错：模型偶尔会产出坏 JSON，界面应显示"参数不是
 * 合法 JSON"并**禁止**执行，而不是让整个渲染炸掉。
 */
export function parseToolCallArguments(raw) {
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) return raw;
  const text = String(raw == null ? '' : raw).trim();
  if (!text) return {};
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * 执行前的**可读预览**：告诉用户"点允许会发生什么"。
 *
 * 批准界面如果只写"允许执行 get_weather"，用户无法判断风险；必须把实际参数
 * 摊开。这也是本项目"能力不是越权入口"（AGENTS §2.3）在 UI 上的落点。
 *
 * @returns {{ok: boolean, tool: string, summary: string, reason?: string}}
 */
export function describeToolExecution(name, rawArguments, root) {
  const verdict = isExecutableTool(name);
  const tool = String(name || '').trim();
  if (!verdict.ok) {
    return {
      ok: false,
      tool,
      summary: '',
      reason: verdict.reason === 'not-allowed' ? 'tool-not-allowed' : 'tool-unknown',
    };
  }
  const args = parseToolCallArguments(rawArguments);
  if (args === null) {
    return { ok: false, tool, summary: '', reason: 'bad-arguments' };
  }
  if (tool === 'read_file') {
    const resolved = resolveToolPath(root, args.path);
    if (!resolved.ok) return { ok: false, tool, summary: '', reason: resolved.reason };
    return { ok: true, tool, summary: resolved.path, path: resolved.path };
  }
  if (tool === 'file_glob_search') {
    const pattern = String(args.pattern || args.glob || '').trim();
    if (!pattern) return { ok: false, tool, summary: '', reason: 'missing-pattern' };
    // glob 本身不解析成路径（它要遍历目录），但 `..` 出现在模式里没有意义，
    // 且可能让某些实现走到根之外。
    if (pattern.includes('..')) return { ok: false, tool, summary: '', reason: 'pattern-escape' };
    // 搜索允许"整个授权目录"，所以用 resolveSearchRoot（"." 合法）
    const resolved = resolveSearchRoot(root, args.path || '.');
    if (!resolved.ok) return { ok: false, tool, summary: '', reason: resolved.reason };
    return { ok: true, tool, summary: `${resolved.path} :: ${pattern}`, path: resolved.path };
  }
  // grep_search
  const query = String(args.query || args.pattern || '').trim();
  if (!query) return { ok: false, tool, summary: '', reason: 'missing-query' };
  const resolved = resolveSearchRoot(root, args.path || '.');
  if (!resolved.ok) return { ok: false, tool, summary: '', reason: resolved.reason };
  return { ok: true, tool, summary: `${resolved.path} :: ${query}`, path: resolved.path };
}

/** 把一次工具执行的结果包成要回填给模型的 tool 消息。 */
export function buildToolResultMessage(call, { ok, content }) {
  const id = call && (call.id || call.tool_call_id);
  if (!id) return null;
  return {
    role: 'tool',
    tool_call_id: String(id),
    content: String(content == null ? '' : content).slice(0, MAX_READ_BYTES),
  };
}

/** 一条 assistant 消息里的 tool_calls 是否已经全部有结果（避免重复弹批准）。 */
export function pendingToolCalls(message) {
  const calls = Array.isArray(message?.toolCalls) ? message.toolCalls : [];
  const results = message?.toolResults && typeof message.toolResults === 'object'
    ? message.toolResults
    : {};
  return calls.filter((call) => {
    const id = call && (call.id || call.tool_call_id);
    return id && !(id in results);
  });
}

/**
 * 本次请求要不要附工具声明，附哪些。
 *
 * 规则是**服务端能力优先**：
 *  - 实例带 `--tools` 启动 → 工具由 llama-server 在宿主机执行、没有批准环节；
 *    chat 页不再声明第二份，避免同一件事两套实现、行为还不一致。返回空数组。
 *  - 实例没有内置工具、且用户已授权目录 → 声明**全部只读工具**，走标准
 *    tool_calls 往返（每条都要人批准）。
 *
 * 抽成纯函数有两个理由：它要能被穷举测试（浏览器里点不出"实例没有内置工具"
 * 这个状态），而且它正好是曾经写错的地方 —— 旧实现用
 * `TOOL_DECLARATIONS.filter((d) => enabled.includes(d.function.name))`，
 * 而 `enabled` 在这一分支必然是空数组，于是结果**恒为空**，整条客户端只读
 * 工具链路（批准条 / 执行端点 / 220 行后端执行器）全是不可达的死代码。
 *
 * @param {string[]} declaredTools 实例启动参数里声明了哪些只读工具
 * @param {string} toolRoot 用户授权的目录（空串 = 未授权）
 * @param {Array} declarations 工具声明清单（默认 TOOL_DECLARATIONS）
 * @returns {Array} 要发给模型的 tools 数组；空数组表示"不附"
 */
export function selectToolDeclarations(
  declaredTools,
  toolRoot,
  declarations = TOOL_DECLARATIONS
) {
  const enabled = Array.isArray(declaredTools) ? declaredTools : [];
  if (enabled.length) return [];
  if (!String(toolRoot || '').trim()) return [];
  return declarations.slice();
}
