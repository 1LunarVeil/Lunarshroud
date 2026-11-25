/**
 * Minimal i18n: dictionary lookup with `{placeholder}` interpolation, plus
 * detection/persistence. No runtime dependency — a ~1.4k-key dictionary does not
 * justify an i18n framework, and an extra library would have to be bundled into
 * the Tauri binary.
 *
 * Ported from 1.0 with one behavioural change: `setLocale` reports success and
 * callers re-render through React state instead of reloading the page. A reload
 * on language switch discards in-flight streaming and unsent drafts.
 */

import { EN_US } from './en-US';
import { ZH_CN } from './zh-CN';
import { EN_US_EXTRA, ZH_CN_EXTRA } from './extra';

export type Locale = 'zh-CN' | 'en-US';

/**
 * Merged dictionaries.
 *
 * `zh-CN.ts` / `en-US.ts` are generated from the 1.0 dictionaries by
 * `scripts/port-locales.mjs`; `extra.ts` holds the keys 2.0 adds. Merging here —
 * rather than appending into the generated files — means re-running the port does
 * not delete hand-written entries.
 */
const DICTIONARIES: Record<Locale, Record<string, string>> = {
  'zh-CN': { ...ZH_CN, ...ZH_CN_EXTRA },
  'en-US': { ...EN_US, ...EN_US_EXTRA },
};

/** The base locale, used as the fallback for any missing translation. */
const BASE: Record<string, string> = DICTIONARIES['zh-CN'];

const STORAGE_KEY = 'lunarshroud_locale';

export const LOCALE_NAMES: Record<Locale, string> = {
  'zh-CN': '简体中文',
  'en-US': 'English',
};

export function isLocale(value: unknown): value is Locale {
  return value === 'zh-CN' || value === 'en-US';
}

export function detectLocale(): Locale {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (isLocale(saved)) return saved;
  } catch {
    // localStorage can be unavailable in a locked-down profile; fall through.
  }
  const language = (navigator.language || 'zh-CN').toLowerCase();
  return language.startsWith('zh') ? 'zh-CN' : 'en-US';
}

let current: Locale = detectLocale();

export function getLocale(): Locale {
  return current;
}

/** Persist and switch. Returns true when the locale actually changed. */
export function setLocale(locale: Locale): boolean {
  if (locale === current) return false;
  if (!isLocale(locale)) return false;
  try {
    localStorage.setItem(STORAGE_KEY, locale);
  } catch {
    // A failed write must not block the in-memory switch.
  }
  current = locale;
  return true;
}

export type TranslateParams = Record<string, string | number | boolean | null | undefined>;

/**
 * Translate a key, interpolating `{name}` placeholders.
 *
 * Fallback chain: current locale → zh-CN (the base locale) → the key itself.
 * Returning the key rather than an empty string is deliberate: a missing
 * translation then looks obviously wrong in the UI instead of rendering as a
 * blank space that nobody notices.
 */
export function translate(key: string, params?: TranslateParams): string {
  const dictionary = DICTIONARIES[current];
  // `Object.hasOwn`, not `dictionary[key]`: a plain object lookup walks the
  // prototype chain, so `translate('constructor')` returns the Object constructor
  // — a function where a string is expected, which React then refuses to render.
  // Dynamic keys (`agent.risk.${risk}`, `nav.${route}`) make that reachable from
  // backend-supplied data, so every lookup here must be an own-property check.
  let text = Object.hasOwn(dictionary, key) ? dictionary[key] : undefined;
  if (text === undefined && Object.hasOwn(BASE, key)) text = BASE[key];
  if (text === undefined) return key;
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      text = text.split(`{${name}}`).join(String(value ?? ''));
    }
  }
  return text;
}

/** Curried variant for components that capture `t` once. */
export function translator(locale: Locale = current) {
  return (key: string, params?: TranslateParams): string => {
    const dictionary = DICTIONARIES[locale];
    // `Object.hasOwn` narrows the key but not the value type (`Record<string,
    // string>` still admits undefined under `noUncheckedIndexedAccess`), hence the
    // explicit nullish fallback on each branch.
    const resolved = Object.hasOwn(dictionary, key)
      ? dictionary[key]
      : Object.hasOwn(BASE, key)
        ? BASE[key]
        : key;
    let text: string = resolved ?? key;
    if (params) {
      for (const [name, value] of Object.entries(params)) {
        text = text.split(`{${name}}`).join(String(value ?? ''));
      }
    }
    return text;
  };
}

/**
 * Backend error message (English, from FastAPI) → localized text.
 *
 * Order matters: the first match wins, so the specific patterns come before the
 * generic ones.
 *
 * `err.invalidValue` is intentionally narrow. 1.0 also matched
 * `must be a number|must be a string|...` inside the *knowledge-base* pattern,
 * so a plain `seed must be a number` 400 was reported as "invalid knowledge base
 * settings" — the user was told to check a feature they were not using, and the
 * real field name never appeared. A generic field-type error must keep the
 * backend's own wording appended, and the knowledge-specific branch must only
 * match genuinely knowledge-related phrasing.
 */
const ERROR_PATTERNS: ReadonlyArray<readonly [RegExp, string]> = [
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
  // Two distinct attachment limits: 400/429 from the store (conversation total)
  // and 413 from the transport. Both must be localized.
  [/conversation attachments are too large/, 'err.attachmentTooLarge'],
  [/attachment .* is too large/, 'err.attachmentTooLarge'],
  [/too many launch parameters/, 'err.tooManyParams'],
  [/llama-server executable is not configured/, 'err.serverNotConfigured'],
  [
    /previous launcher session ended while this instance was still running/,
    'err.instanceInterrupted',
  ],
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
  [
    /knowledge service (failed to start|could not start|did not become ready)/,
    'err.knowledgeService',
  ],
  [
    /knowledge retrieval failed|service unreachable|embedding service returned|rerank service returned/,
    'err.knowledgeRetrieval',
  ],
  [/chunk limit/, 'err.knowledgeChunkLimit'],
  [/size budget|too many documents|too many files/, 'err.knowledgeTooLarge'],
  // Knowledge-specific phrasing only — see the doc comment above.
  [
    /knowledge base setting|knowledge base name is invalid|top_k must|chunk_chars must/,
    'err.knowledgeSetting',
  ],
  [/document not found/, 'err.knowledgeDocumentNotFound'],
  [/must be absolute/, 'err.knowledgePathAbsolute'],
  [/knowledge source does not exist/, 'err.knowledgeSourceMissing'],
  [/no supported files were found/, 'err.knowledgeNoFiles'],
  [/knowledge base name is required/, 'err.knowledgeNameRequired'],
  [/no longer matches this index|query embedding dimension/, 'err.knowledgeModelMismatch'],
  [/reranking is enabled but no rerank model/, 'err.knowledgeRerankRequired'],
  [
    /could not be read|no extractable text|unsupported knowledge file type|binary files are not supported/,
    'err.knowledgeExtract',
  ],
  // Storage unwritable: say it is a disk/permission problem rather than letting
  // the user guess.
  [/data directory is not writable|readonly database|read-only database/, 'err.storageReadonly'],
  [/database is busy|database is locked/, 'err.storageBusy'],
  // Generic field-type error, last. Callers append the backend's own message so
  // the offending field name survives.
  [
    /(must be a number|must be a string|must be a boolean|must be an integer|must be at most|must be finite)/,
    'err.invalidValue',
  ],
];

export function localizeBackendError(message: unknown): string {
  if (!message) return '';
  const text = String(message);
  for (const [pattern, key] of ERROR_PATTERNS) {
    if (pattern.test(text)) {
      // `err.invalidValue` says nothing on its own, so the raw backend text is
      // appended; "invalid parameter" without a field name is as useless as no
      // translation at all.
      if (key === 'err.invalidValue') return `${translate(key)}: ${text}`;
      return translate(key);
    }
  }
  return text;
}
