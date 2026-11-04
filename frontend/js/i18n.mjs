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
  // 附件超限有两条不同的后端消息：429/400 来自存储层（会话总额度），
  // 413 来自传输层（上一条）。两条都必须本地化 —— 否则用户看到的是
  // 一句英文原文，而它恰好是"你刚拖进来的那张图太大了"。
  [/conversation attachments are too large/, 'err.attachmentTooLarge'],
  [/attachment .* is too large/, 'err.attachmentTooLarge'],
  [/too many launch parameters/, 'err.tooManyParams'],
  [/llama-server executable is not configured/, 'err.serverNotConfigured'],
  [/previous launcher session ended while this instance was still running/, 'err.instanceInterrupted'],
  [/conversation limit reached/, 'err.conversationLimit'],
  [/message limit reached/, 'err.messageLimit'],
  [/prompt limit reached/, 'err.promptLimit'],
  [/role must be system, user or assistant/, 'err.chatRole'],
  [/instance_id must be a string/, 'err.chatInstanceId'],
  [/message_id must be an integer/, 'err.chatMessageId'],
  [/knowledge base not found/, 'err.knowledgeNotFound'],
  [/has no indexed content/, 'err.knowledgeEmpty'],
  [/no embedding model is configured/, 'err.knowledgeEmbeddingRequired'],
  [/scanned and needs an OCR model/, 'err.knowledgeOcrRequired'],
  [/OCR needs the multimodal projector/, 'err.knowledgeOcrMmproj'],
  [/knowledge service (failed to start|could not start|did not become ready)/, 'err.knowledgeService'],
  [/knowledge retrieval failed|service unreachable|embedding service returned|rerank service returned/, 'err.knowledgeRetrieval'],
  [/chunk limit/, 'err.knowledgeChunkLimit'],
  [/size budget|too many documents|too many files/, 'err.knowledgeTooLarge'],
  // 这一条必须**只**匹配真正与知识库有关的错误。
  //
  // 2026-11-03 实测缺陷：它原先还包含 `must be a number|must be a string|
  // must be a boolean|must be at most`，于是任何 400 —— 包括
  // `seed must be a number`（新建会话时的一个 `null` 草稿值）—— 都被翻译成
  // "知识库设置无效"。用户明明没有任何知识库，却被指着去查知识库设置；
  // 真正的字段名从头到尾没有出现在界面上。宽泛的错误映射不是"贴心的兜底"，
  // 它会**吃掉真实诊断**，比不翻译更糟。
  //
  // 通用的字段类型错误现在走下面的 `err.invalidValue`，文案里带上原始消息。
  [/knowledge base setting|knowledge base name is invalid|top_k must|chunk_chars must/, 'err.knowledgeSetting'],
  [/document not found/, 'err.knowledgeDocumentNotFound'],
  [/must be absolute/, 'err.knowledgePathAbsolute'],
  [/knowledge source does not exist/, 'err.knowledgeSourceMissing'],
  [/no supported files were found/, 'err.knowledgeNoFiles'],
  [/knowledge base name is required/, 'err.knowledgeNameRequired'],
  [/no longer matches this index|query embedding dimension/, 'err.knowledgeModelMismatch'],
  [/reranking is enabled but no rerank model/, 'err.knowledgeRerankRequired'],
  [/could not be read|no extractable text|unsupported knowledge file type|binary files are not supported/, 'err.knowledgeExtract'],
  // 存储不可写：必须说清"这是磁盘/权限问题"，不能让用户去猜。
  [/data directory is not writable|readonly database|read-only database/, 'err.storageReadonly'],
  [/database is busy|database is locked/, 'err.storageBusy'],
  // 通用参数错误，保留字段名（`{detail}` 由调用方填入原始消息）。
  [/(must be a number|must be a string|must be a boolean|must be an integer|must be at most|must be finite)/, 'err.invalidValue'],
];

export function localizeBackendError(message) {
  if (!message) return '';
  const text = String(message);
  for (const [pattern, key] of ERROR_PATTERNS) {
    if (pattern.test(text)) {
      // `err.invalidValue` 是通用兜底：译文本身不带信息量，把后端的原文接在
      // 后面，否则用户看到"参数无效"却不知道是哪个字段 —— 那和没翻译一样。
      // 其余键的译文是完整句子，直接返回。
      if (key === 'err.invalidValue') return `${t(key)}: ${text}`;
      return t(key);
    }
  }
  return text;
}
