// 轻量 i18n：零依赖的字典翻译 + 语言检测/持久化
// 语言切换后由调用方刷新页面（页面在模块加载时读取语言）

import { ZH_CN } from './locales/zh-CN.mjs';
import { EN_US } from './locales/en-US.mjs';

const LOCALES = { 'zh-CN': ZH_CN, 'en-US': EN_US };
const STORAGE_KEY = 'lunarshroud_locale';

export const LOCALE_NAMES = { 'zh-CN': '简体中文', 'en-US': 'English' };

export function detectLocale() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && LOCALES[saved]) return saved;
  } catch { /* localStorage 不可用时忽略 */ }
  const lang = (navigator.language || 'zh-CN').toLowerCase();
  return lang.startsWith('zh') ? 'zh-CN' : 'en-US';
}

let _locale = detectLocale();

export function getLocale() {
  return _locale;
}

export function setLocale(locale) {
  if (!LOCALES[locale] || locale === _locale) return false;
  try {
    localStorage.setItem(STORAGE_KEY, locale);
  } catch { /* 忽略持久化失败 */ }
  _locale = locale;
  return true;
}

/**
 * 翻译一个 key；支持 {name} 占位符插值。
 * 缺失时回退中文词典，再回退 key 本身（便于发现漏翻）。
 */
export function t(key, params) {
  const dict = LOCALES[_locale];
  let text = dict[key];
  if (text === undefined) text = ZH_CN[key];
  if (text === undefined) return key;
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      text = text.split(`{${name}}`).join(String(value));
    }
  }
  return text;
}

// 后端错误信息（英文）→ 本地化文案。
// 未匹配时原样返回后端消息。
const ERROR_PATTERNS = [
  [/port .+ is already in use/, 'err.portInUse'],
  [/no available port in range/, 'err.noFreePort'],
  [/model file not found/, 'err.modelNotFound'],
  [/a model file path is required/, 'err.modelRequired'],
  [/llama-server executable not found/, 'err.serverNotFound'],
  [/a draft model path is required/, 'err.draftRequired'],
  [/draft model file not found/, 'err.draftNotFound'],
  [/mmproj file not found/, 'err.mmprojNotFound'],
  [/an API key is required for non-loopback/, 'err.apiKeyRequired'],
  [/host must be a valid IPv4 address/, 'err.badHost'],
  [/port must be an integer/, 'err.portInteger'],
  [/port out of range/, 'err.portRange'],
  [/startup timed out/, 'err.startupTimeout'],
  [/instance not found/, 'err.instanceNotFound'],
  [/is still running; stop it before removing/, 'err.stillRunning'],
  [/preset not found/, 'err.presetNotFound'],
  [/preset name is required/, 'err.presetNameRequired'],
  [/unsupported config key/, 'err.unknownConfigKey'],
  [/invalid launcher session/, 'err.invalidSession'],
  [/request body too large/, 'err.bodyTooLarge'],
  [/too many launch parameters/, 'err.tooManyParams'],
  [/llama-server executable is not configured/, 'err.serverNotConfigured'],
  [/previous launcher session ended while this instance was still running/, 'err.instanceInterrupted'],
];

export function localizeBackendError(message) {
  if (!message) return '';
  const text = String(message);
  for (const [pattern, key] of ERROR_PATTERNS) {
    if (pattern.test(text)) return t(key);
  }
  return text;
}
