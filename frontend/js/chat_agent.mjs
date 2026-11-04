// 从实例的启动 argv 归纳 Agent / MCP 状态。
//
// 为什么读 argv 而不是查端点：本机实测 llama-server b11698 **没有**任何 MCP 的
// REST 端点 —— `/api/mcp`、`/mcp/servers`、`/props` 里的 mcp 字段全部 404 或
// 不存在（`/props` 只有 `cors_proxy_enabled`）。MCP 服务器是在它进程内由 WebUI
// 管理、或经 `--mcp-servers-json` / `--mcp-servers-config` 在启动时传入的。
// 后者是我们唯一能如实反映的部分：与其编一个不存在的接口，不如把用户真正
// 配了什么显示出来。
//
// 纯函数 + 文案注入：[describeAgentState] 不碰 DOM、不读 i18n，测试可以穷举
// 各种 argv 形状；文案由调用方传进来，语言切换时不用重新导入模块。

/**
 * @param {string[]} argv 已脱敏的启动参数（来自 /api/instances/{id}/command）
 * @param {object} labels 文案；缺省时用英文兜底，保证模块单独可用
 * @returns {{enabled: boolean, hasMcp: boolean, label: string, title: string}}
 */
export function describeAgentState(argv, labels = {}) {
  const list = Array.isArray(argv) ? argv.map((item) => String(item)) : [];
  const has = (flag) => list.some((token) => token === flag || token.startsWith(`${flag}=`));
  const valueOf = (flag) => {
    for (let i = 0; i < list.length; i += 1) {
      if (list[i] === flag && i + 1 < list.length) return list[i + 1];
      if (list[i].startsWith(`${flag}=`)) return list[i].slice(flag.length + 1);
    }
    return '';
  };

  const agent = has('--agent');
  const tools = valueOf('--tools');
  const proxy = has('--ui-mcp-proxy');
  const mcpJson = valueOf('--mcp-servers-json');
  const mcpConfig = valueOf('--mcp-servers-config');
  const mcpCount = countMcpServers(mcpJson);

  const enabled = agent || Boolean(tools) || proxy || Boolean(mcpJson) || Boolean(mcpConfig);
  if (!enabled) {
    return { enabled: false, hasMcp: false, label: '', title: '' };
  }

  const parts = [];
  if (agent) parts.push(labels.agentMode || 'Agent mode');
  if (tools) parts.push(formatTools(labels.tools, tools));
  if (proxy) parts.push(labels.mcpProxy || 'MCP CORS proxy');
  if (mcpCount) parts.push(formatServers(labels.mcpServers, mcpCount));
  else if (mcpConfig) parts.push(labels.mcpFromFile || 'MCP servers from config file');

  return {
    enabled: true,
    hasMcp: proxy || mcpCount > 0 || Boolean(mcpConfig),
    label: labels.badge || 'Agent',
    title: parts.join(' · '),
  };
}

function formatTools(template, tools) {
  return typeof template === 'function' ? template(tools) : `Tools: ${tools}`;
}

function formatServers(template, count) {
  return typeof template === 'function' ? template(count) : `${count} MCP server(s)`;
}

/**
 * 数一数内联 MCP JSON 里定义了几个 server；解析不了就返回 0（不臆测）。
 *
 * 兼容三种写法：`{mcpServers:{...}}`（Cursor 格式，llama.cpp 采用）、
 * `{servers:{...}}`、以及裸的 `{name:{...}}`。
 */
export function countMcpServers(json) {
  if (!json) return 0;
  try {
    const parsed = JSON.parse(json);
    const servers = parsed?.mcpServers || parsed?.servers || parsed;
    if (servers && typeof servers === 'object' && !Array.isArray(servers)) {
      return Object.keys(servers).length;
    }
  } catch {
    /* 用户可能手填了不合法 JSON；不给数字，徽标仍可靠 --ui-mcp-proxy 亮起 */
  }
  return 0;
}
