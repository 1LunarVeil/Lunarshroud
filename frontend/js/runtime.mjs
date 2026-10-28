/** Coalesce overlapping async calls into the currently running promise. */
export function createSingleFlight(task) {
  let current = null;
  return function run(...args) {
    if (current) return current;
    current = Promise.resolve()
      .then(() => task(...args))
      .finally(() => { current = null; });
    return current;
  };
}

/**
 * Exponential backoff for reconnect attempts.
 * recordFailure() returns the delay to wait before the next attempt;
 * recordSuccess() resets the sequence after a working connection.
 */
export function createReconnectDelays({ baseMs = 1000, maxMs = 15000 } = {}) {
  let failures = 0;
  return {
    recordSuccess() { failures = 0; },
    recordFailure() {
      const delay = Math.min(maxMs, baseMs * 2 ** failures);
      failures = Math.min(failures + 1, 30);
      return delay;
    },
    get failures() { return failures; },
  };
}

/** Append one node while keeping a hard upper bound on rendered children. */
export function boundedAppend(container, node, maxChildren) {
  container.appendChild(node);
  while (container.children.length > maxChildren && container.firstElementChild) {
    container.removeChild(container.firstElementChild);
  }
}

/**
 * 这个主机地址是不是回环（决定 Chat 能否用 iframe 嵌）。
 *
 * 为什么必须判断而不是"嵌进去试试"：两条路都走不通，且都不是我们能绕开的 —
 * 1. `tauri.conf.json` 的 CSP 只允许 `frame-src http://127.0.0.1:*`，
 *    绑 LAN IP 的实例嵌进来必然是一片空白（`SECURITY_MODEL.md` 把它列为已知残余风险）；
 * 2. 非回环实例强制要求 API Key，而 iframe 无法把 token 注入进去。
 *
 * 所以非回环直接给"在新窗口打开"，不要拿一个白屏让用户猜。
 * 地址缺失时按回环处理（保持既有行为，不因为探测不到就改变表现）。
 */
export function isLoopbackHost(host) {
  const value = String(host || '').trim().toLowerCase().replace(/^\[|\]$/g, '');
  if (!value) return true;
  if (value === 'localhost' || value === '::1' || value === '0:0:0:0:0:0:0:1') return true;
  // 必须是完整的 IPv4 字面量，且四个字节都合法、第一个字节是 127。
  // 只做 /^127\./ 前缀判断会把 `127.0.0.1.evil.com` 也当成回环（虽然当前
  // 后端会用 ipaddress 再校验一次，前端也不该自己造出这个歧义）。
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(value);
  if (!match) return false;
  const octets = match.slice(1).map((part) => Number(part));
  return octets[0] === 127 && octets.every((octet) => octet <= 255);
}

export function normalizeHttpUrl(value) {
  try {
    const url = new URL(String(value));
    if (!['http:', 'https:'].includes(url.protocol)) return null;
    if (url.username || url.password) return null;
    return url.href;
  } catch (_error) {
    return null;
  }
}

/**
 * Parse a numeric form control without normalising the raw text.
 *
 * The parameter editor must not write a parsed value back into the focused
 * input on every keystroke: intermediate states such as "-" or "0." are valid
 * while typing but would be erased by Number()/parseFloat() round-tripping
 * (typing "0.75" became "750", typing "-1" became "1").
 *
 * Returns `{ value, valid }`:
 *   - empty input -> `{ value: null, valid: true }`
 *   - complete number -> `{ value: <number>, valid: true }`
 *   - intermediate/invalid text -> `{ value: null, valid: false }`
 */
/**
 * Build the user model-defaults payload from the current parameter values.
 *
 * Only keys that differ from the global defaults are stored; writing the whole
 * default dictionary would turn every upstream default into a user override
 * and silently block future default changes. Managed keys are never included
 * (the backend rejects them anyway).
 */
/**
 * Group model-scan entries by a logical key (manifest model:).
 *
 * Entries without a key go into the empty-string group; the UI decides how to
 * label/order it. Order is first-seen to keep the scan's file order stable.
 */
export function groupModelsByKey(models, keyOf) {
  const groups = new Map();
  for (const model of models || []) {
    const key = String(keyOf(model) || '');
    if (!groups.has(key)) groups.set(key, { key, models: [] });
    groups.get(key).models.push(model);
  }
  return [...groups.values()];
}

export function diffModelDefaults(current, defaults, managedKeys = []) {
  const managed = new Set(managedKeys);
  const changed = {};
  for (const [key, value] of Object.entries(current || {})) {
    if (managed.has(key)) continue;
    if (JSON.stringify(value) !== JSON.stringify(defaults ? defaults[key] : undefined)) {
      changed[key] = value;
    }
  }
  return changed;
}

export function parseNumericInput(raw, type = 'number') {
  const text = String(raw ?? '').trim();
  if (text === '') return { value: null, valid: true };
  const value = type === 'float' ? Number.parseFloat(text) : Number(text);
  if (!Number.isFinite(value)) return { value: null, valid: false };
  return { value, valid: true };
}

/**
 * 判断参数在当前取值下是否激活（depends_on / depends_on_nonempty 联动）。
 * 与后端 core.param_builder._is_param_active 的语义保持一致。
 */
export function isParamEnabled(param, values) {
  if (param.depends_on) {
    for (const [depKey, depVal] of Object.entries(param.depends_on)) {
      if (values[depKey] !== depVal) return false;
    }
  }
  // 多值依赖：多值参数的当前选中项命中 allowed 中任意一个即启用
  if (param.depends_on_any) {
    for (const [depKey, allowed] of Object.entries(param.depends_on_any)) {
      const current = values[depKey];
      const selected = Array.isArray(current)
        ? current
        : (current === undefined || current === null || current === '' ? [] : [current]);
      if (!selected.some((item) => allowed.includes(item))) return false;
    }
  }
  if (param.depends_on_nonempty) {
    const v = values[param.depends_on_nonempty];
    if (v === '' || v === null || v === undefined) return false;
    if (v === 'none') return false;
  }
  return true;
}

/**
 * 把上游（本机构建）声明的合法取值合并进 schema 的静态选项。
 *
 * schema 的 options 是一份**快照**，而 llama.cpp 会新增取值（实测 build 10960
 * 的 --spec-type 多了 draft-dspark）。没有这个函数，用户永远选不到新实现。
 *
 * 合并规则：schema 的在前（它们的中文说明是写好的），上游多出来的追加在后；
 * 上游为 null（没探测到）时原样返回 schema 选项 —— 拿不到就说拿不到，
 * 不能用猜的去凑。
 */
export function mergeUpstreamOptions(schemaOptions, upstreamOptions) {
  const base = Array.isArray(schemaOptions) ? schemaOptions : [];
  if (!Array.isArray(upstreamOptions) || upstreamOptions.length === 0) return base.slice();
  const merged = base.slice();
  for (const value of upstreamOptions) {
    if (!merged.some((item) => String(item) === String(value))) merged.push(value);
  }
  return merged;
}

/**
 * 端口等于 schema 默认值（或缺失）时视为“未指定”，应由后端自动分配，
 * 避免连续启动第二个实例时撞默认端口。与后端 params.get("port", 0) 语义一致。
 */
export function shouldAutoAssignPort(params, defaults) {
  return params.port === undefined || params.port === defaults.port;
}

/**
 * Monotonic request token for "latest response wins" UI loads.
 *
 * `begin()` starts a new generation; `isCurrent(token)` is false as soon as
 * another load starts or the view is left. This is intentionally tiny: the
 * rule is easier to keep correct than ad-hoc request-id booleans in each page.
 */
export function createRequestGate() {
  let generation = 0;
  return {
    begin() {
      generation += 1;
      return generation;
    },
    isCurrent(token) {
      return token === generation;
    },
    cancel() {
      generation += 1;
    },
  };
}

function formatApiDetailItem(item) {
  if (item === null || item === undefined) return '';
  if (typeof item === 'string') return item;
  if (typeof item !== 'object') return String(item);
  const rawLoc = Array.isArray(item.loc) ? item.loc : [];
  const loc = rawLoc
    .filter((part) => !['query', 'body', 'path'].includes(String(part)))
    .join('.');
  const message = String(item.msg || item.message || '').trim();
  if (loc && message) return `${loc}: ${message}`;
  return message || loc;
}

/** Normalize FastAPI/JSON error payloads without producing "[object Object]". */
export function formatApiErrorPayload(payload, fallback = '') {
  if (payload === null || payload === undefined) return fallback;
  if (typeof payload === 'string') return payload.trim() || fallback;
  if (Array.isArray(payload)) {
    const parts = payload.map(formatApiDetailItem).filter(Boolean);
    return parts.join('; ') || fallback;
  }
  if (typeof payload === 'object') {
    if (typeof payload.error === 'string' && payload.error.trim()) {
      return payload.error.trim();
    }
    if (payload.detail !== undefined) {
      const detail = formatApiErrorPayload(payload.detail, '');
      if (detail) return detail;
    }
    if (typeof payload.message === 'string' && payload.message.trim()) {
      return payload.message.trim();
    }
  }
  return fallback;
}

/** A bounded display name for an instance created from a model filename. */
export function instanceNameFromFilename(filename, maxLength = 100) {
  const limit = Number.isFinite(Number(maxLength)) ? Math.max(1, Math.floor(Number(maxLength))) : 100;
  return String(filename || '').trim().slice(0, limit);
}

/**
 * Revoke a Blob URL after the browser has had a chance to start the download.
 * Synchronous revocation cancels downloads in some WebView2 builds.
 */
export function scheduleObjectUrlRevoke(url, revoke, delayMs = 1000) {
  const delay = Number.isFinite(Number(delayMs)) ? Math.max(0, Number(delayMs)) : 1000;
  return setTimeout(() => {
    try {
      revoke(url);
    } catch {
      /* revocation is best-effort */
    }
  }, delay);
}

/** Toggle a control's busy state; safe for callers that may not have a button. */
export function setButtonBusy(button, busy) {
  if (!button || typeof button !== 'object') return;
  button.disabled = Boolean(busy);
  if (busy) button.setAttribute('aria-busy', 'true');
  else button.removeAttribute('aria-busy');
}
