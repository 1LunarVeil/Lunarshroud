// 后端 API 封装
// 在 Tauri 环境中通过 invoke 获取后端 URL，使用绝对路径调用
// 在普通浏览器环境中使用相对路径（同源）
// 错误处理：统一抛出含 message 字段的 Error

import { createReconnectDelays } from './runtime.mjs';
import { t } from './i18n.mjs';

// ========== 后端 URL 管理 ==========

let _backendBase = ''; // 后端基础 URL，如 http://127.0.0.1:17520
let _backendToken = '';

// Tauri v2 中 invoke 位于 window.__TAURI__.core.invoke；旧版在 window.__TAURI__.invoke
export function tauriInvoke(cmd, args) {
  const tauri = window.__TAURI__;
  let invoke = null;
  if (tauri) {
    invoke = tauri.core?.invoke || tauri.invoke;
  }
  if (!invoke) {
    throw new Error(t('api.tauriUnavailable'));
  }
  return args === undefined ? invoke(cmd) : invoke(cmd, args);
}

// 后端重启后会话令牌可能变化，重新获取会话前必须丢弃缓存
function _resetSessionCache() {
  _backendBase = '';
  _backendToken = '';
}

async function _ensureBackendUrl() {
  if (_backendBase) return _backendBase;

  // 在 Tauri 环境中，通过 invoke 获取后端 URL
  if (window.__TAURI__) {
    try {
      const session = await tauriInvoke('get_backend_session');
      if (session && session.url) {
        _backendBase = session.url;
        _backendToken = session.token || '';
        console.log('[API] 后端 URL:', _backendBase);
        window.__LLAMA_BACKEND_URL__ = _backendBase;
        return _backendBase;
      }
      window.__LLAMA_BACKEND_URL_ERROR__ = 'backend not ready';
    } catch (e) {
      console.warn('获取后端 URL 失败:', e);
      window.__LLAMA_BACKEND_URL_ERROR__ = String(e && e.message ? e.message : e);
    }
    // 打包态**绝不回落到硬编码端口**：外壳只在 HMAC 就绪校验通过后才交出
    // URL 与令牌。校验未通过时（后端正在重启）那个端口上可能是任意本地
    // 进程；带着令牌去连它，正是要防的令牌窃取。抛错让调用方重试。
    throw new Error(t('api.backendNotReady'));
  }

  // 降级：普通浏览器里 UI 由后端自服务，location.origin 就是后端。
  // 但 Tauri webview 的地址也是 http(s)://tauri.localhost —— 那不是后端，
  // 必须排除，否则下面的默认端口兜底永远不可达（审计 F24）。
  const hostname = window.location ? window.location.hostname || '' : '';
  if (
    window.location &&
    window.location.protocol.startsWith('http') &&
    !hostname.endsWith('tauri.localhost')
  ) {
    _backendBase = window.location.origin;
    console.log('[API] 降级后端 URL:', _backendBase);
    window.__LLAMA_BACKEND_URL__ = _backendBase;
    return _backendBase;
  }

  // 最后兜底：启动器默认端口
  const fallback = 'http://127.0.0.1:17520';
  console.warn('[API] 使用硬编码兜底后端 URL:', fallback);
  _backendBase = fallback;
  window.__LLAMA_BACKEND_URL__ = _backendBase;
  return _backendBase;
}

// ========== 内部工具 ==========

const RETRY_AFTER_MS = 1500;

async function _request(method, path, body, attempt = 0) {
  let base;
  try {
    base = await _ensureBackendUrl();
  } catch (e) {
    // 后端重启中（外壳尚未确认新代身份）：等一个退避周期后重试一次，
    // 期间不建立任何带令牌的请求。
    if (attempt < 1 && window.__TAURI__) {
      await new Promise((resolve) => setTimeout(resolve, RETRY_AFTER_MS));
      _resetSessionCache();
      return _request(method, path, body, attempt + 1);
    }
    throw e;
  }
  const url = base ? `${base}${path}` : path;
  window.__LLAMA_LAST_REQUEST_URL__ = url;
  const opts = {
    method,
    headers: {},
  };
  if (_backendToken) {
    opts.headers['X-LunarShroud-Token'] = _backendToken;
  }
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 20000);
  opts.signal = controller.signal;
  if (body !== undefined && method !== 'GET') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(url, opts);
  } catch (e) {
    if (e && e.name === 'AbortError') {
      throw new Error(t('api.requestTimeout'));
    }
    // 网络失败可能是后端正在重启：只对**幂等**请求自动重试。
    // POST/PUT/DELETE 可能已经在后端执行过，响应丢失时重试会创建重复实例/
    // 预设或重复停止；这类请求交给调用方决定是否重试。
    if (attempt < 1 && (method === 'GET' || method === 'HEAD')) {
      await new Promise((resolve) => setTimeout(resolve, RETRY_AFTER_MS));
      _resetSessionCache();
      return _request(method, path, body, attempt + 1);
    }
    throw new Error(t('api.networkError', { message: e.message }));
  } finally {
    clearTimeout(timeoutId);
  }
  // 会话令牌失效（后端已重启）：重新获取会话后重试一次
  if (resp.status === 401 && attempt < 1 && window.__TAURI__) {
    _resetSessionCache();
    return _request(method, path, body, attempt + 1);
  }
  const ctype = resp.headers.get('content-type') || '';
  let data = null;
  if (ctype.includes('application/json')) {
    data = await resp.json();
  } else {
    data = await resp.text();
  }
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    if (data && typeof data === 'object') {
      msg = data.error || data.detail || msg;
    } else if (typeof data === 'string' && data) {
      msg = data;
    }
    throw new Error(msg);
  }
  return data;
}

const _get = (path) => _request('GET', path);
const _post = (path, body) => _request('POST', path, body);
const _put = (path, body) => _request('PUT', path, body);
const _del = (path) => _request('DELETE', path);

// 构建查询字符串
function _qs(obj) {
  const parts = [];
  for (const [k, v] of Object.entries(obj)) {
    if (v === undefined || v === null || v === '') continue;
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  }
  return parts.length ? `?${parts.join('&')}` : '';
}

// WebSocket 地址工厂
async function _wsUrl(path) {
  const base = await _ensureBackendUrl();
  if (base) {
    return base.replace(/^http/, 'ws') + path;
  }
  // 降级：基于当前页面
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}${path}`;
}

function _openWebSocket(url) {
  return _backendToken
    ? new WebSocket(url, ['lunarshroud', _backendToken])
    : new WebSocket(url);
}

// ========== 自动重连 WebSocket ==========

/**
 * 创建带自动重连的 WebSocket 监听器。
 * 断开后按指数退避重连（1s → 15s 封顶），每次重连前重置会话缓存，
 * 以便后端重启换发新令牌后仍能恢复。
 *
 * @param {string} path WebSocket 路径
 * @param {{onMessage: Function, onStatus?: Function}} handlers
 *   onMessage(message)   收到 JSON 消息
 *   onStatus(state)      连接状态：'connected' | 'reconnecting' | 'closed'
 * @returns {{close: Function}} 监听器控制器，close() 后不再重连
 */
function _createWebSocketWatcher(path, { onMessage, onStatus } = {}) {
  let socket = null;
  let timer = null;
  let closed = false;
  const backoff = createReconnectDelays();

  const notify = (state) => {
    if (typeof onStatus === 'function') {
      try { onStatus(state); } catch { /* 状态回调异常不影响重连 */ }
    }
  };

  async function open() {
    if (closed) return;
    try {
      const url = await _wsUrl(path);
      if (closed) return;
      const sock = _openWebSocket(url);
      socket = sock;
      sock.onopen = () => {
        backoff.recordSuccess();
        notify('connected');
      };
      sock.onmessage = (ev) => {
        if (closed) return;
        let message;
        try {
          message = JSON.parse(ev.data);
        } catch (error) {
          console.error('[WS] 收到无法解析的消息', error);
          return;
        }
        try {
          onMessage(message);
        } catch (error) {
          // 处理函数抛错不能让整个实时推送静默停摆；至少留下可诊断的日志。
          console.error('[WS] 消息处理失败', error);
        }
      };
      sock.onclose = () => {
        if (closed || socket !== sock) return;
        socket = null;
        scheduleReconnect();
      };
      sock.onerror = () => {
        // onclose 随后触发，统一在 onclose 中处理
      };
    } catch (error) {
      console.warn('[WS] 连接失败，将按退避重连', error);
      scheduleReconnect();
    }
  }

  function scheduleReconnect() {
    notify('reconnecting');
    const delay = backoff.recordFailure();
    timer = setTimeout(() => {
      _resetSessionCache(); // 后端重启后令牌会变化
      open();
    }, delay);
  }

  open();
  return {
    close() {
      closed = true;
      notify('closed');
      if (timer) clearTimeout(timer);
      if (socket) {
        try { socket.close(); } catch { /* 已关闭 */ }
        socket = null;
      }
    },
  };
}

// ========== 系统接口 ==========

const Api = {
  // 系统配置
  getSystemConfig: () => _get('/api/system/config'),
  updateSystemConfig: (body) => _put('/api/system/config', body),

  // 系统资源快照
  getSystemOverview: () => _get('/api/system/overview'),

  // 参数 Schema
  getParamSchema: () => _get('/api/params/schema'),
  getParamDefaults: () => _get('/api/params/defaults'),
  getParamRecipes: () => _get('/api/params/recipes'),
  buildCommand: (params) => _post('/api/params/build-command', { params }),

  // 预设管理
  listPresets: () => _get('/api/presets'),
  getPreset: (id) => _get(`/api/presets/${encodeURIComponent(id)}`),
  createPreset: (body) => _post('/api/presets', body),
  updatePreset: (id, body) => _put(`/api/presets/${encodeURIComponent(id)}`, body),
  deletePreset: (id) => _del(`/api/presets/${encodeURIComponent(id)}`),
  clonePreset: (id, name) => _post(`/api/presets/${encodeURIComponent(id)}/clone`, { name }),
  importPreset: (body) => _post('/api/presets/import', body),
  exportPreset: (id) => _get(`/api/presets/${encodeURIComponent(id)}/export`),
  diffPresets: (id1, id2) => _post('/api/presets/diff', { id1, id2 }),

  // 能力探测（本机构建 + 指定模型）
  // 不带 modelPath 时只返回构建级能力；带上会额外探测该 GGUF 与同目录配套文件
  getCapabilities: (modelPath) => _get(`/api/system/capabilities${_qs({ model: modelPath })}`),

  // 启动前检查（只读）：缺路径、模型不是 GGUF、嵌入模型未开嵌入端点、显存/端口风险…
  // body 与 createInstance 相同（preset_id 或 params，可选 param_overrides）
  diagnoseLaunch: (body) => _post('/api/diagnose', body),

  // 模型扫描
  // withCapabilities=true 会让每个条目附带能力摘要（原生 ctx、量化、MTP 形态…），
  // 前端列表因此不必为每个模型各发一次请求
  scanModels: (dir, withCapabilities = false) =>
    _get(
      `/api/models/scan${_qs({
        directory: dir,
        with_capabilities: withCapabilities ? 'true' : undefined,
      })}`
    ),
  browseDirectory: (dir) => _get(`/api/models/browse${_qs({ directory: dir })}`),

  // 显存预估：按**当前参数值**估算，所以参数一变就要重算（调用方自行防抖）
  estimateVram: (query) =>
    _get(
      `/api/models/vram-estimate${_qs({
        model: query.model,
        ctx_size: query.ctx_size,
        n_gpu_layers: query.n_gpu_layers,
        cache_type_k: query.cache_type_k,
        cache_type_v: query.cache_type_v,
        parallel: query.parallel,
        mmproj: query.mmproj,
      })}`
    ),

  // 实例管理
  listInstances: () => _get('/api/instances'),
  createInstance: (body) => _post('/api/instances', body),
  getInstance: (id) => _get(`/api/instances/${encodeURIComponent(id)}`),
  getInstanceCommand: (id) => _get(`/api/instances/${encodeURIComponent(id)}/command`),
  getInstanceDiagnosis: (id) =>
    _get(`/api/instances/${encodeURIComponent(id)}/diagnosis`),
  stopInstance: (id, force = false) =>
    _post(`/api/instances/${encodeURIComponent(id)}/stop`, { force }),
  restartInstance: (id) =>
    _post(`/api/instances/${encodeURIComponent(id)}/restart`),
  removeInstance: (id) => _del(`/api/instances/${encodeURIComponent(id)}`),

  // 启动历史
  listHistory: (limit = 50, offset = 0) =>
    _get(`/api/history${_qs({ limit, offset })}`),
  getHistory: (id) => _get(`/api/history/${encodeURIComponent(id)}`),
  deleteHistory: (id) => _del(`/api/history/${encodeURIComponent(id)}`),
  clearHistory: () => _post('/api/history/clear'),
  relaunchHistory: (id) => _post(`/api/history/${encodeURIComponent(id)}/relaunch`),

  // ========== WebSocket 监听器（自动重连） ==========

  /**
   * 订阅实例日志（断线自动重连）
   * @param {string} instanceId 实例 ID
   * @param {{onMessage: (msg: object) => void, onStatus?: (state: string) => void}} handlers
   * @returns {{close: Function}} 监听器控制器
   */
  watchInstanceLogs(instanceId, handlers) {
    return _createWebSocketWatcher(
      `/ws/instances/${encodeURIComponent(instanceId)}/logs`,
      handlers
    );
  },

  /**
   * 订阅实例列表快照（任意实例状态变化即推送，断线自动重连）
   * @param {{onMessage: (msg: object) => void, onStatus?: (state: string) => void}} handlers
   * @returns {{close: Function}} 监听器控制器
   */
  watchInstances(handlers) {
    return _createWebSocketWatcher('/ws/instances', handlers);
  },

  /**
   * 订阅系统资源指标（断线自动重连）
   * @param {{onMessage: (msg: object) => void, onStatus?: (state: string) => void}} handlers
   * @returns {{close: Function}} 监听器控制器
   */
  watchSystemMetrics(handlers) {
    return _createWebSocketWatcher('/ws/system/metrics', handlers);
  },
};

export default Api;
