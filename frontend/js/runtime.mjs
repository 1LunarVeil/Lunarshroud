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
