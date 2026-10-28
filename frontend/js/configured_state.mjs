// "这台机器上到底有没有一个可用的 llama-server" —— 唯一一份判断逻辑。
//
// 这个判断以前在三个地方各写一遍，而且都只看 `config.llama_server_path` 这一个
// 字段：设置页保存前的拦截、仪表盘的配置提示条、新手指南清单。而启动路径实际接受
// `server_path or cfg["llama_server_path"]`，也就是运行环境注册表里的构建同样能用。
// 结果是一个只用运行环境、完全能正常启动的用户被三处界面同时判定为"没配置"，
// 并且因为设置页拦的是整个保存动作，他连主题和端口都改不了。
//
// 判据只有这一份：三处必须给同一个答案，否则修一处漏两处。
// 探测失败（probe.probed=false）的 profile 也算可用——探测只回答"这个构建支持
// 哪些 flag"，不影响它能不能被启动；把它算成"没配置"就又把用户推回同一个坑。

/**
 * @param {object} state
 * @param {string} state.serverPath   config.llama_server_path
 * @param {Array<object>} [state.runtimes] 已注册的运行环境 profile
 * @returns {boolean}
 */
export function hasUsableServer({ serverPath, runtimes } = {}) {
  if (String(serverPath || '').trim()) return true;
  return (
    Array.isArray(runtimes) &&
    runtimes.some((runtime) => runtime && String(runtime.executable_path || '').trim())
  );
}
