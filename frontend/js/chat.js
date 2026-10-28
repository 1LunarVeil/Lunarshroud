// Chat page: native streaming chat plus the llama-server WebUI fallback.
//
// Native requests go through the backend's loopback proxy, so the webview
// never talks to llama-server directly (no CORS, and API keys stay server-side).

import {
  createRequestGate,
  isLoopbackHost,
  normalizeHttpUrl,
  scheduleObjectUrlRevoke,
} from './runtime.mjs';
import { getLocale, localizeBackendError, t } from './i18n.mjs';
import { markdownPlainText, renderMarkdown } from './markdown.mjs';
import {
  buildChatStats,
  contextUsage,
  createSseParser,
  extractChunk,
  reasoningPanelOpen,
} from './chat_stream.mjs';
import {
  messageCopyText,
  messageError,
  messageExportLines,
  messageUsableForContext,
  shouldPersistAssistantMessage,
} from './chat_message_text.mjs';
import {
  ATTACHMENT_ACCEPT,
  MAX_ATTACHMENTS,
  MAX_ATTACHMENT_CHARS,
  MAX_MESSAGE_ATTACHMENT_CHARS,
  attachmentBaseName,
  buildMessagePayloadContent,
  formatAttachmentSize,
  isSupportedAttachmentName,
  looksBinaryText,
} from './chat_attachments.mjs';
import { highlightParts } from './chat_search.mjs';

export const id = 'chat';
export const title = 'Chat';

const MODE_KEY = 'lunarshroud_chat_mode';
const ACTIVE_KEY = 'lunarshroud_chat_active';
const DEFAULT_PARAMS = { system: '', temperature: 0.7, max_tokens: -1 };

let ctx = null;
let root = null;
let panelEl = null;

let instances = [];
let selectedInstanceId = '';
let knowledgeBases = [];
let knowledgeLoaded = false;
let selectedKnowledgeId = '';
let unsubscribe = null;
let mode = readMode();
// 思考过程面板的默认开合偏好，来自 config.chat_reasoning。
// 取不到配置时按 'auto' 走，绝不因为读失败就永久隐藏面板。
let reasoningMode = 'auto';

let conversations = [];
let conversationQuery = '';
let searchResults = [];
let searchLoading = false;
let searchError = '';
let searchTimer = null;
let searchRequestId = 0;
let activeConversationId = '';
let activeConversation = null;
const conversationLoadGate = createRequestGate();
let draftParams = { ...DEFAULT_PARAMS };
let messages = [];
let pendingAttachments = [];
let dragDepth = 0;
let streaming = false;
let sending = false;
let abortController = null;
let activeStreamKey = null;
let streamGeneration = 0;
let renderQueued = false;
let autoScroll = true;
let lastWebUiKey = '';

export function init(el, appCtx) {
  root = el;
  ctx = appCtx;
  root.innerHTML = `
    <div class="chat-page-v2">
      <div class="chat-toolbar">
        <div class="chat-toolbar-left">
          <div class="chat-instance-selector">
            <label class="chat-instance-label" for="chat-instance-select">${ctx.escapeHtml(
              t('chat.instance')
            )}</label>
            <select id="chat-instance-select" class="form-control form-control-sm">
              <option value="">${ctx.escapeHtml(t('chat.loading'))}</option>
            </select>
          </div>
          <div class="chat-instance-selector chat-kb-selector">
            <label class="chat-instance-label" for="chat-kb-select">${ctx.escapeHtml(
              t('chat.knowledge')
            )}</label>
            <select id="chat-kb-select" class="form-control form-control-sm">
              <option value="">${ctx.escapeHtml(t('chat.knowledgeNone'))}</option>
            </select>
          </div>
          <div class="chat-instance-info" id="chat-instance-info"></div>
        </div>
        <div class="chat-mode-switch" role="group" aria-label="${ctx.escapeHtml(
          t('chat.modeLabel')
        )}">
          <button type="button" class="chat-mode-btn" data-chat-mode="native" aria-pressed="false">${ctx.escapeHtml(
            t('chat.nativeMode')
          )}</button>
          <button type="button" class="chat-mode-btn" data-chat-mode="webui" aria-pressed="false">${ctx.escapeHtml(
            t('chat.webuiMode')
          )}</button>
        </div>
        <div class="chat-toolbar-right">
          <button class="btn btn-ghost btn-sm" id="btn-chat-new" title="${ctx.escapeHtml(
            t('chat.newChat')
          )}" aria-label="${ctx.escapeHtml(t('chat.newChat'))}">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          </button>
          <button class="btn btn-ghost btn-sm" id="btn-chat-prompts" title="${ctx.escapeHtml(
            t('chat.prompts')
          )}" aria-label="${ctx.escapeHtml(t('chat.prompts'))}">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/><line x1="8" y1="7" x2="16" y2="7"/><line x1="8" y1="11" x2="14" y2="11"/></svg>
          </button>
          <button class="btn btn-ghost btn-sm" id="btn-chat-settings" title="${ctx.escapeHtml(
            t('chat.settings')
          )}" aria-label="${ctx.escapeHtml(t('chat.settings'))}">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
          </button>
          <button class="btn btn-ghost btn-sm" id="btn-chat-export" title="${ctx.escapeHtml(
            t('chat.export')
          )}" aria-label="${ctx.escapeHtml(t('chat.export'))}">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          </button>
          <button class="btn btn-ghost btn-sm" id="btn-chat-refresh" title="${ctx.escapeHtml(
            t('chat.refresh')
          )}" aria-label="${ctx.escapeHtml(t('chat.refresh'))}">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
          </button>
          <button class="btn btn-ghost btn-sm" id="btn-chat-external" title="${ctx.escapeHtml(
            t('chat.openExternal')
          )}" aria-label="${ctx.escapeHtml(t('chat.openExternal'))}" disabled>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          </button>
        </div>
      </div>
      <div class="chat-main">
        <section class="chat-native" id="chat-native">
          <div class="chat-thread" id="chat-thread" role="log"></div>
          <button class="chat-scroll-bottom btn btn-secondary btn-sm" id="chat-scroll-bottom" hidden>${ctx.escapeHtml(
            t('chat.scrollBottom')
          )}</button>
          <div class="chat-composer-wrap">
            <div class="chat-composer">
              <div class="chat-pending-attachments" id="chat-pending-attachments" hidden></div>
              <textarea id="chat-input" class="chat-input" rows="1" placeholder="${ctx.escapeHtml(
                t('chat.composerPlaceholder')
              )}" aria-label="${ctx.escapeHtml(t('chat.composerPlaceholder'))}"></textarea>
              <div class="chat-composer-bar">
                <div class="chat-composer-left">
                  <span class="chat-model-badge" id="chat-model-badge"></span>
                  <span class="chat-context-meter" id="chat-context-meter" hidden>
                    <span class="chat-context-track" aria-hidden="true"><span class="chat-context-fill"></span></span>
                    <span class="chat-context-label"></span>
                  </span>
                  <span class="chat-stream-state" id="chat-stream-state" aria-live="polite"></span>
                </div>
                <div class="chat-composer-actions">
                  <button class="btn btn-ghost btn-sm" id="btn-chat-attach" title="${ctx.escapeHtml(
                    t('chat.attachFiles')
                  )}" aria-label="${ctx.escapeHtml(t('chat.attachFiles'))}">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
                  </button>
                  <input id="chat-file-input" class="visually-hidden" type="file" multiple accept="${ctx.escapeHtml(
                    ATTACHMENT_ACCEPT
                  )}" tabindex="-1" aria-hidden="true" />
                  <button class="btn btn-secondary btn-sm" id="btn-chat-stop" hidden>${ctx.escapeHtml(
                    t('chat.stop')
                  )}</button>
                  <button class="btn btn-primary btn-sm" id="btn-chat-send">${ctx.escapeHtml(
                    t('chat.send')
                  )}</button>
                </div>
              </div>
            </div>
            <div class="chat-composer-hint">${ctx.escapeHtml(t('chat.composerHint'))}</div>
            <div class="chat-drop-hint" id="chat-drop-hint" hidden>${ctx.escapeHtml(
              t('chat.attachDrop')
            )}</div>
          </div>
        </section>
        <div class="chat-frame-container" id="chat-frame-container" hidden></div>
      </div>
      <div class="visually-hidden" id="chat-live" aria-live="polite"></div>
    </div>
  `;
  bindStaticEvents();
  setMode(mode, { render: false });
  updateComposerState();
  loadReasoningMode();
}

/**
 * 读取"思考过程"开合偏好。失败时保持 'auto'：
 * 读不到配置不该把面板永久藏起来，那比默认值更糟。
 */
async function loadReasoningMode() {
  try {
    const cfg = await ctx.Api.getSystemConfig();
    const value = String((cfg && cfg.chat_reasoning) || 'auto');
    reasoningMode = ['auto', 'expand', 'collapse'].includes(value) ? value : 'auto';
  } catch {
    reasoningMode = 'auto';
  }
}

export function renderPanel(el) {
  panelEl = el;
  renderConversationList();
}

function preventFileDropNavigation(event) {
  if (Array.from(event.dataTransfer?.types || []).includes('Files')) {
    event.preventDefault();
  }
}

export function onShow() {
  unsubscribe = ctx.subscribeInstances(updateInstances);
  document.addEventListener('keydown', onKeydown);
  document.addEventListener('dragover', preventFileDropNavigation);
  document.addEventListener('drop', preventFileDropNavigation);
  const pendingInstance = window.__pendingChatInstanceId;
  if (pendingInstance) {
    delete window.__pendingChatInstanceId;
    if (instances.some((item) => item.id === pendingInstance)) {
      selectedInstanceId = pendingInstance;
      const select = root.querySelector('#chat-instance-select');
      if (select) select.value = pendingInstance;
      updateInstanceInfo();
    }
  }
  if (mode === 'native') renderThread();
  updateComposerState();
  loadConversations();
  loadKnowledgeBases();
  if (window.__pendingChatNew) {
    delete window.__pendingChatNew;
    newChat();
  }
}

export function onHide() {
  if (unsubscribe) unsubscribe();
  unsubscribe = null;
  conversationLoadGate.cancel();
  dragDepth = 0;
  if (searchTimer) {
    clearTimeout(searchTimer);
    searchTimer = null;
  }
  searchRequestId += 1;
  searchLoading = false;
  const dropHint = root.querySelector('#chat-drop-hint');
  if (dropHint) dropHint.hidden = true;
  document.removeEventListener('keydown', onKeydown);
  document.removeEventListener('dragover', preventFileDropNavigation);
  document.removeEventListener('drop', preventFileDropNavigation);
  if (abortController) {
    abortController.abort();
    abortController = null;
  }
}

function readMode() {
  try {
    return localStorage.getItem(MODE_KEY) === 'webui' ? 'webui' : 'native';
  } catch {
    return 'native';
  }
}

function writeMode(value) {
  try {
    localStorage.setItem(MODE_KEY, value);
  } catch {
    /* storage unavailable */
  }
}

function onKeydown(event) {
  if (!(event.ctrlKey || event.metaKey)) return;
  if (event.key === 'f') {
    if (mode !== 'native' || !panelEl || panelEl.offsetParent === null) return;
    if (document.querySelector('#app-modal-overlay')) return;
    const search = panelEl.querySelector('#chat-conversation-search');
    if (!search) return;
    event.preventDefault();
    search.focus();
    search.select();
    return;
  }
  if (event.key !== 'r') return;
  if (!root.querySelector('#chat-empty')) return;
  if (document.querySelector('#app-modal-overlay')) return;
  event.preventDefault();
  relaunchLastModel();
}

// ---------- instance state ----------

function browserHost(inst) {
  return inst.host === '0.0.0.0' ? '127.0.0.1' : inst.host;
}

function currentInstance() {
  return instances.find((item) => item.id === selectedInstanceId) || null;
}

async function loadKnowledgeBases() {
  try {
    const response = await ctx.Api.listKnowledge();
    knowledgeBases = Array.isArray(response?.bases) ? response.bases : [];
    knowledgeLoaded = true;
    renderKnowledgeOptions();
  } catch {
    // The knowledge page shows the actionable error; chat stays usable without RAG.
    knowledgeBases = [];
    knowledgeLoaded = true;
    renderKnowledgeOptions();
  }
}

function renderKnowledgeOptions() {
  const select = root?.querySelector('#chat-kb-select');
  if (!select) return;
  if (
    knowledgeLoaded &&
    selectedKnowledgeId &&
    !knowledgeBases.some((base) => base.id === selectedKnowledgeId)
  ) {
    selectedKnowledgeId = '';
  }
  const options = [
    `<option value="">${ctx.escapeHtml(t('chat.knowledgeNone'))}</option>`,
    ...knowledgeBases.map((base) => {
      const count = Number(base.chunk_count) || 0;
      const label = count
        ? t('chat.knowledgeOption', { name: base.name, count })
        : t('chat.knowledgeOptionEmpty', { name: base.name });
      return `<option value="${ctx.escapeHtml(base.id)}">${ctx.escapeHtml(label)}</option>`;
    }),
  ];
  select.innerHTML = options.join('');
  select.value = selectedKnowledgeId;
}

function lastUserQuestion() {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === 'user' && !message.error) {
      return String(message.content || '').trim();
    }
  }
  return '';
}

function decodeSourcesHeader(value) {
  if (!value || typeof value !== 'string') return [];
  try {
    const padded = value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (value.length % 4)) % 4);
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    const parsed = JSON.parse(new TextDecoder().decode(bytes));
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (source) =>
        source &&
        Number.isInteger(Number(source.number)) &&
        typeof source.title === 'string'
    );
  } catch {
    return [];
  }
}

function updateInstances(list) {
  instances = (Array.isArray(list) ? list : [])
    .filter((item) => item.status === 'running')
    // Knowledge services are llama-server processes, but they cannot answer a
    // chat turn. Showing them here only lets the user send a prompt to an
    // embedding/rerank endpoint and get a confusing upstream error.
    .filter((item) => !String(item.name || '').startsWith('[KB] ') && !item.params?.embeddings && !item.params?.rerank)
    .map((item) => {
      const host = browserHost(item);
      return {
        ...item,
        url: `http://${host}:${item.port}`,
        loopback: isLoopbackHost(host),
      };
    });

  const select = root.querySelector('#chat-instance-select');
  const externalBtn = root.querySelector('#btn-chat-external');
  if (externalBtn) externalBtn.disabled = instances.length === 0;
  const previous = selectedInstanceId;

  if (!instances.length) {
    select.innerHTML = `<option value="">${ctx.escapeHtml(t('chat.noRunning'))}</option>`;
    selectedInstanceId = '';
    lastWebUiKey = '';
    updateInstanceInfo();
    updateComposerState();
    if (mode === 'webui') showEmpty();
    renderThread();
    return;
  }

  select.replaceChildren();
  for (const instance of instances) {
    const option = document.createElement('option');
    option.value = String(instance.id || '');
    option.textContent = `${instance.name || ''} · ${instance.host}:${instance.port}`;
    select.appendChild(option);
  }

  const boundId = activeConversation && activeConversation.instance_id;
  const bound = boundId && instances.find((item) => item.id === boundId);
  if (previous && instances.find((item) => item.id === previous)) {
    selectedInstanceId = previous;
  } else if (bound) {
    selectedInstanceId = bound.id;
  } else {
    selectedInstanceId = instances[0].id;
  }
  select.value = selectedInstanceId;

  updateInstanceInfo();
  updateComposerState();
  if (mode === 'webui') loadWebUI();
  else if (!messages.length) renderThread();
}

function updateInstanceInfo() {
  const infoEl = root.querySelector('#chat-instance-info');
  const badge = root.querySelector('#chat-model-badge');
  const inst = currentInstance();
  if (!infoEl) return;
  if (inst) {
    const modelName = String(inst.model_path || '').split(/[\\/]/).pop();
    const label = [modelName, inst.runtime_label].filter(Boolean).join(' · ');
    infoEl.textContent = label;
    if (badge) badge.textContent = label;
  } else {
    infoEl.textContent = '';
    if (badge) badge.textContent = '';
  }
}

// ---------- mode ----------

function setMode(value, { render = true } = {}) {
  mode = value === 'webui' ? 'webui' : 'native';
  writeMode(mode);
  root.querySelectorAll('[data-chat-mode]').forEach((button) => {
    const active = button.dataset.chatMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  const native = root.querySelector('#chat-native');
  const frame = root.querySelector('#chat-frame-container');
  const refresh = root.querySelector('#btn-chat-refresh');
  const prompts = root.querySelector('#btn-chat-prompts');
  if (native) native.hidden = mode !== 'native';
  if (frame) frame.hidden = mode !== 'webui';
  if (refresh) refresh.disabled = mode !== 'webui';
  if (prompts) prompts.disabled = mode !== 'native';
  if (!render) return;
  if (mode === 'webui') {
    loadWebUI();
  } else {
    renderThread();
    updateComposerState();
    root.querySelector('#chat-input')?.focus();
  }
}

// ---------- conversations ----------

async function loadConversations() {
  try {
    const data = await ctx.Api.listChats();
    conversations = Array.isArray(data.conversations) ? data.conversations : [];
    if (activeConversation) {
      const found = conversations.find((item) => item.id === activeConversation.id);
      if (found) {
        activeConversation.title = found.title;
        activeConversation.message_count = found.message_count;
      }
    }
    renderConversationList();
    refreshActiveSearch();
    if (!activeConversation && conversations.length) {
      let stored = '';
      try {
        stored = localStorage.getItem(ACTIVE_KEY) || '';
      } catch {
        /* storage unavailable */
      }
      const preferred = conversations.find((item) => item.id === stored) || conversations[0];
      await selectConversation(preferred.id, { focus: false, quiet: true });
    } else if (activeConversationId) {
      try {
        localStorage.setItem(ACTIVE_KEY, activeConversationId);
      } catch {
        /* storage unavailable */
      }
    }
  } catch (error) {
    if (!conversations.length) {
      ctx.showToast(`${t('chat.loadFailed')}: ${localizeBackendError(error.message)}`, 'error');
    }
  }
}

async function refreshConversations() {
  try {
    const data = await ctx.Api.listChats();
    conversations = Array.isArray(data.conversations) ? data.conversations : [];
    if (activeConversation) {
      const found = conversations.find((item) => item.id === activeConversation.id);
      if (found) {
        activeConversation.title = found.title;
        activeConversation.message_count = found.message_count;
      }
    }
    renderConversationList();
    refreshActiveSearch();
  } catch {
    /* background refresh: keep the current list */
  }
}

function highlightedHtml(text, query) {
  return highlightParts(text, query)
    .map((part) =>
      part.match
        ? `<mark>${ctx.escapeHtml(part.text)}</mark>`
        : ctx.escapeHtml(part.text)
    )
    .join('');
}

function conversationItemHtml(conversation) {
  const active = conversation.id === activeConversationId;
  const title = conversation.title || t('chat.untitled');
  const count = Number(conversation.message_count) || 0;
  return `
    <div class="panel-chat-item" role="listitem">
      <button type="button" class="panel-list-item ${active ? 'active' : ''}" data-chat-id="${ctx.escapeHtml(
        conversation.id
      )}"${active ? ' aria-current="true"' : ''}>
        <span class="panel-list-row"><span class="panel-list-label">${ctx.escapeHtml(title)}</span></span>
        <span class="panel-list-meta">${ctx.escapeHtml(t('chat.messageCount', { count }))}</span>
      </button>
      <button type="button" class="panel-chat-delete" data-delete-chat="${ctx.escapeHtml(
        conversation.id
      )}" aria-label="${ctx.escapeHtml(t('chat.deleteChat'))}" title="${ctx.escapeHtml(
        t('chat.deleteChat')
      )}">&times;</button>
    </div>`;
}

function searchResultHtml(result, query) {
  const active = result.id === activeConversationId;
  const title = result.title || t('chat.untitled');
  const count = Number(result.message_count) || 0;
  const typeLabel =
    result.match_type === 'attachment'
      ? t('chat.searchAttachment')
      : result.match_type === 'message'
        ? t('chat.searchMessage')
        : t('chat.searchTitle');
  const metaParts = [typeLabel];
  if (result.attachment_name) metaParts.push(result.attachment_name);
  metaParts.push(t('chat.messageCount', { count }));
  const showSnippet =
    result.snippet && !(result.match_type === 'title' && result.snippet === title);
  return `
    <button type="button" class="panel-list-item panel-search-item ${
      active ? 'active' : ''
    }" data-chat-id="${ctx.escapeHtml(result.id)}" data-search-result${
      active ? ' aria-current="true"' : ''
    }>
      <span class="panel-list-row"><span class="panel-list-label">${highlightedHtml(
        title,
        query
      )}</span></span>
      <span class="panel-list-meta">${ctx.escapeHtml(metaParts.join(' · '))}</span>
      ${
        showSnippet
          ? `<span class="panel-list-snippet">${highlightedHtml(result.snippet, query)}</span>`
          : ''
      }
    </button>`;
}

function renderConversationBody() {
  if (!panelEl) return;
  const body = panelEl.querySelector('#chat-panel-body');
  const clear = panelEl.querySelector('#chat-search-clear');
  if (!body) return;
  const query = conversationQuery.trim();
  if (clear) clear.hidden = !query;
  if (!query) {
    body.innerHTML = conversations.length
      ? `<div class="panel-chat-list" role="list" aria-label="${ctx.escapeHtml(
          t('chat.conversationList')
        )}">${conversations.map(conversationItemHtml).join('')}</div>
         <button type="button" class="btn btn-ghost btn-sm panel-chat-clear" id="btn-chat-clear">${ctx.escapeHtml(
           t('chat.clearChats')
         )}</button>`
      : `<div class="panel-chat-empty"><div class="panel-chat-empty-title">${ctx.escapeHtml(
          t('chat.noConversations')
        )}</div><div class="panel-chat-empty-hint">${ctx.escapeHtml(
          t('chat.noConversationsHint')
        )}</div></div>`;
    return;
  }
  if (searchLoading) {
    body.innerHTML = `<div class="panel-chat-empty"><div class="panel-chat-empty-hint"><span class="loading"></span> ${ctx.escapeHtml(
      t('chat.searchLoading')
    )}</div></div>`;
    return;
  }
  if (searchError) {
    body.innerHTML = `<div class="panel-chat-empty"><div class="panel-chat-empty-title">${ctx.escapeHtml(
      t('chat.searchFailed')
    )}</div><div class="panel-chat-empty-hint">${ctx.escapeHtml(searchError)}</div></div>`;
    return;
  }
  if (!searchResults.length) {
    body.innerHTML = `<div class="panel-chat-empty"><div class="panel-chat-empty-title">${ctx.escapeHtml(
      t('chat.searchEmpty')
    )}</div><div class="panel-chat-empty-hint">${ctx.escapeHtml(
      t('chat.searchEmptyHint')
    )}</div></div>`;
    return;
  }
  body.innerHTML = `
    <div class="panel-chat-search-count">${ctx.escapeHtml(
      t('chat.searchResultCount', { count: searchResults.length })
    )}</div>
    <div class="panel-chat-list" role="list" aria-label="${ctx.escapeHtml(
      t('chat.searchResults')
    )}">${searchResults
      .map(
        (result) =>
          `<div class="panel-chat-item" role="listitem">${searchResultHtml(result, query)}</div>`
      )
      .join('')}</div>`;
}

function renderConversationList() {
  if (!panelEl) return;
  const searchInputFocused = document.activeElement?.id === 'chat-conversation-search';
  const caret = searchInputFocused ? document.activeElement.selectionStart : null;
  panelEl.innerHTML = `
    <div class="panel-chat-head">
      <span>${ctx.escapeHtml(t('chat.conversations'))}</span>
      <button type="button" class="btn btn-ghost btn-sm" id="btn-chat-new-panel">${ctx.escapeHtml(
        t('chat.newChat')
      )}</button>
    </div>
    <div class="panel-chat-search" role="search">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input id="chat-conversation-search" type="search" autocomplete="off" spellcheck="false"
        value="${ctx.escapeHtml(conversationQuery)}"
        placeholder="${ctx.escapeHtml(t('chat.searchPlaceholder'))}"
        aria-label="${ctx.escapeHtml(t('chat.searchPlaceholder'))}"
        aria-controls="chat-panel-body" />
      <button type="button" class="panel-chat-search-clear" id="chat-search-clear" ${
        conversationQuery.trim() ? '' : 'hidden'
      } title="${ctx.escapeHtml(t('chat.searchClear'))}" aria-label="${ctx.escapeHtml(
        t('chat.searchClear')
      )}">&times;</button>
    </div>
    <div id="chat-panel-body"></div>
  `;
  renderConversationBody();
  if (searchInputFocused) {
    const input = panelEl.querySelector('#chat-conversation-search');
    if (input) {
      input.focus();
      try {
        input.setSelectionRange(caret ?? input.value.length, caret ?? input.value.length);
      } catch {
        /* selection unavailable */
      }
    }
  }
  panelEl.onclick = async (event) => {
    const deleteBtn = event.target.closest('[data-delete-chat]');
    if (deleteBtn) {
      event.stopPropagation();
      await deleteConversation(deleteBtn.dataset.deleteChat);
      return;
    }
    const item = event.target.closest('[data-chat-id]');
    if (item) {
      await selectConversation(item.dataset.chatId);
      return;
    }
    if (event.target.closest('#btn-chat-new-panel')) {
      await newChat();
      return;
    }
    if (event.target.closest('#chat-search-clear')) {
      clearConversationSearch({ focus: true });
      return;
    }
    if (event.target.closest('#btn-chat-clear')) {
      await clearConversations();
    }
  };
  panelEl.oninput = (event) => {
    if (event.target.id === 'chat-conversation-search') {
      scheduleConversationSearch(event.target.value);
    }
  };
  panelEl.onkeydown = (event) => {
    if (event.isComposing) return;
    if (event.target.id === 'chat-conversation-search') {
      if (event.key === 'Escape' && conversationQuery) {
        event.preventDefault();
        clearConversationSearch({ focus: true });
        return;
      }
      if (event.key === 'Enter' && searchResults.length) {
        event.preventDefault();
        selectConversation(searchResults[0].id);
        return;
      }
      if (event.key === 'ArrowDown' && searchResults.length) {
        event.preventDefault();
        panelEl.querySelector('[data-search-result]')?.focus();
        return;
      }
    }
    if (!event.target.closest('[data-search-result]')) return;
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    const items = [...panelEl.querySelectorAll('[data-search-result]')];
    const index = items.indexOf(event.target.closest('[data-search-result]'));
    if (index < 0) return;
    event.preventDefault();
    const next = event.key === 'ArrowDown' ? index + 1 : index - 1;
    if (next < 0) {
      panelEl.querySelector('#chat-conversation-search')?.focus();
    } else {
      items[Math.min(next, items.length - 1)]?.focus();
    }
  };
}

function clearConversationSearch({ focus = false } = {}) {
  conversationQuery = '';
  searchResults = [];
  searchLoading = false;
  searchError = '';
  searchRequestId += 1;
  if (searchTimer) {
    clearTimeout(searchTimer);
    searchTimer = null;
  }
  const input = panelEl?.querySelector('#chat-conversation-search');
  if (input) input.value = '';
  renderConversationBody();
  if (focus) input?.focus();
}

function scheduleConversationSearch(value) {
  conversationQuery = value;
  searchError = '';
  const query = conversationQuery.trim();
  const requestId = ++searchRequestId;
  if (searchTimer) {
    clearTimeout(searchTimer);
    searchTimer = null;
  }
  if (!query) {
    searchResults = [];
    searchLoading = false;
    renderConversationBody();
    return;
  }
  searchResults = [];
  searchLoading = true;
  renderConversationBody();
  searchTimer = setTimeout(() => {
    runConversationSearch(query, requestId);
  }, 220);
}

async function runConversationSearch(query, requestId) {
  try {
    const data = await ctx.Api.searchChats(query, 30);
    if (requestId !== searchRequestId) return;
    searchResults = Array.isArray(data.results) ? data.results : [];
  } catch (error) {
    if (requestId !== searchRequestId) return;
    searchResults = [];
    searchError = localizeBackendError(error.message);
  } finally {
    if (requestId === searchRequestId) {
      searchLoading = false;
      renderConversationBody();
    }
  }
}

function refreshActiveSearch() {
  const query = conversationQuery.trim();
  if (!query || !panelEl) return;
  const requestId = ++searchRequestId;
  searchLoading = true;
  renderConversationBody();
  runConversationSearch(query, requestId);
}

async function selectConversation(conversationId, { focus = true, quiet = false } = {}) {
  if (streaming || sending) {
    if (!quiet) ctx.showToast(t(streaming ? 'chat.stopping' : 'chat.sending'), 'info');
    return;
  }
  const request = conversationLoadGate.begin();
  try {
    const detail = await ctx.Api.getChat(conversationId);
    // The user may have clicked another conversation while this request was
    // in flight. Never let an older response overwrite the newer selection.
    if (!conversationLoadGate.isCurrent(request)) return;
    activeConversation = detail;
    activeConversationId = detail.id;
    messages = (Array.isArray(detail.messages) ? detail.messages : []).map(normalizeMessage);
    draftParams = { ...DEFAULT_PARAMS, ...(detail.params || {}) };
    selectedKnowledgeId = detail.knowledge_base_id || '';
    renderKnowledgeOptions();
    pendingAttachments = [];
    renderPendingAttachments();
    if (detail.instance_id) {
      const bound = instances.find((item) => item.id === detail.instance_id);
      if (bound) {
        selectedInstanceId = bound.id;
        const select = root.querySelector('#chat-instance-select');
        if (select) select.value = bound.id;
        updateInstanceInfo();
      }
    }
    try {
      localStorage.setItem(ACTIVE_KEY, activeConversationId);
    } catch {
      /* storage unavailable */
    }
    renderThread();
    updateComposerState();
    renderConversationList();
    if (focus && mode === 'native') root.querySelector('#chat-input')?.focus();
  } catch (error) {
    if (!conversationLoadGate.isCurrent(request)) return;
    if (!quiet) {
      ctx.showToast(`${t('chat.loadFailed')}: ${localizeBackendError(error.message)}`, 'error');
    }
  }
}

async function newChat({ preserveAttachments = false } = {}) {
  if (streaming || sending) {
    ctx.showToast(t(streaming ? 'chat.stopping' : 'chat.sending'), 'info');
    return;
  }
  // Starting a new chat invalidates any older conversation-detail request.
  conversationLoadGate.cancel();
  if (activeConversation && !messages.length) {
    root.querySelector('#chat-input')?.focus();
    return;
  }
  const inst = currentInstance();
  if (!inst) {
    ctx.showToast(t('chat.newChatNeedsInstance'), 'warning');
    return;
  }
  try {
    const conversation = await ctx.Api.createChat({
      title: '',
      instance_id: inst.id,
      model: inst.model_path || '',
      params: draftParams,
      knowledge_base_id: selectedKnowledgeId || null,
    });
    activeConversation = conversation;
    activeConversationId = conversation.id;
    messages = [];
    draftParams = { ...DEFAULT_PARAMS, ...(conversation.params || {}) };
    if (conversationQuery) clearConversationSearch();
    if (!preserveAttachments) {
      pendingAttachments = [];
      renderPendingAttachments();
    }
    try {
      localStorage.setItem(ACTIVE_KEY, activeConversationId);
    } catch {
      /* storage unavailable */
    }
    renderThread();
    updateComposerState();
    await refreshConversations();
    if (mode !== 'native') setMode('native');
    root.querySelector('#chat-input')?.focus();
  } catch (error) {
    ctx.showToast(`${t('chat.createFailed')}: ${localizeBackendError(error.message)}`, 'error');
  }
}

async function deleteConversation(conversationId) {
  if (streaming) {
    ctx.showToast(t('chat.stopping'), 'info');
    return;
  }
  const found = conversations.find((item) => item.id === conversationId);
  const title = found?.title || t('chat.untitled');
  const confirmed = await ctx.showConfirm(
    t('chat.deleteChatConfirm', { title }),
    t('chat.deleteChat')
  );
  if (!confirmed) return;
  try {
    await ctx.Api.deleteChat(conversationId);
    ctx.showToast(t('chat.deletedChat'), 'success');
    if (activeConversationId === conversationId) {
      activeConversation = null;
      activeConversationId = '';
      messages = [];
      pendingAttachments = [];
      renderPendingAttachments();
      renderThread();
      updateComposerState();
    }
    await loadConversations();
  } catch (error) {
    ctx.showToast(`${t('chat.deleteChat')}: ${localizeBackendError(error.message)}`, 'error');
  }
}

async function clearConversations() {
  if (streaming) {
    ctx.showToast(t('chat.stopping'), 'info');
    return;
  }
  const confirmed = await ctx.showConfirm(t('chat.clearChatsConfirm'), t('chat.clearChats'));
  if (!confirmed) return;
  try {
    await ctx.Api.clearChats();
    conversations = [];
    activeConversation = null;
    activeConversationId = '';
    messages = [];
    pendingAttachments = [];
    renderPendingAttachments();
    renderThread();
    updateComposerState();
    renderConversationList();
  } catch (error) {
    ctx.showToast(`${t('chat.clearChats')}: ${localizeBackendError(error.message)}`, 'error');
  }
}

// ---------- messages ----------

function normalizeMessage(message) {
  const stats = message.stats && typeof message.stats === 'object' ? message.stats : {};
  const attachments = Array.isArray(message.attachments)
    ? message.attachments.map((attachment, index) => ({
        id: attachment.id ?? index,
        name: String(attachment.name || 'file'),
        size: Number(attachment.size) || 0,
        content: String(attachment.content || ''),
      }))
    : [];
  return {
    ...message,
    key: String(message.id),
    stats,
    attachments,
    // Keep a stream failure visible after reload: it is persisted in
    // stats.error because the messages table has no separate error column.
    error: messageError(message),
    // 一等列优先；只有在对端是旧后端（还没这列）时才回落到被截断的 stats 副本。
    reasoning:
      typeof message.reasoning === 'string' && message.reasoning
        ? message.reasoning
        : typeof stats.reasoning === 'string'
          ? stats.reasoning
          : '',
    // 手动开合状态记在消息对象上，不留在 DOM 上：列表每来一个 chunk 就整块
    // 重绘，<details> 的 open 是 DOM 状态，重绘一次就被冲掉了。
    reasoningTouched: false,
    reasoningChosen: false,
  };
}

function nativeEmptyHtml() {
  const inst = currentInstance();
  if (!inst) {
    return `
      <div class="chat-native-empty" id="chat-empty">
        <div class="chat-empty-title">${ctx.escapeHtml(t('chat.emptyTitle'))}</div>
        <div class="chat-empty-hint">${ctx.escapeHtml(t('chat.emptyHint'))}</div>
        <div class="chat-empty-actions">
          <button class="btn btn-primary btn-sm" id="chat-goto-dashboard">${ctx.escapeHtml(
            t('chat.gotoDashboard')
          )}</button>
          <button class="btn btn-secondary btn-sm" id="chat-reload-last">${ctx.escapeHtml(
            t('chat.reloadLast')
          )} <span class="kbd">Ctrl+R</span></button>
        </div>
      </div>`;
  }
  return `
    <div class="chat-native-empty" id="chat-native-empty">
      <div class="chat-native-empty-mark" aria-hidden="true">✦</div>
      <div class="chat-empty-title">${ctx.escapeHtml(t('chat.nativeEmptyTitle'))}</div>
      <div class="chat-empty-hint">${ctx.escapeHtml(t('chat.nativeEmptyHint'))}</div>
      <div class="chat-suggestions">
        ${[1, 2, 3]
          .map(
            (index) =>
              `<button type="button" class="chat-suggestion" data-suggestion="${ctx.escapeHtml(
                t(`chat.suggestion${index}`)
              )}">${ctx.escapeHtml(t(`chat.suggestion${index}`))}</button>`
          )
          .join('')}
      </div>
    </div>`;
}

function renderThread({ stick = true } = {}) {
  const thread = root.querySelector('#chat-thread');
  if (!thread) return;
  if (!messages.length) {
    thread.innerHTML = nativeEmptyHtml();
    updateScrollButton();
    return;
  }
  thread.innerHTML = messages.map((message, index) => renderMessageHtml(message, index)).join('');
  if (stick && autoScroll) scrollToBottom();
  updateScrollButton();
}

function renderMessageHtml(message, index) {
  const role = message.role === 'user' ? 'user' : 'assistant';
  const label = role === 'user' ? t('chat.you') : t('chat.assistant');
  const avatar = role === 'user' ? t('chat.you').slice(0, 1) : 'AI';
  const key = message.key || `m-${index}`;
  const markdown = message.content
    ? renderMarkdown(message.content, { copy: t('chat.copyCode') })
    : '';
  const typing = message.streaming && !message.content && !message.reasoning;
  const panelOpen = reasoningPanelOpen({
    mode: reasoningMode,
    streaming: Boolean(message.streaming),
    touched: Boolean(message.reasoningTouched),
    chosen: Boolean(message.reasoningChosen),
    hasReasoning: Boolean(message.reasoning),
  });
  const reasoning = message.reasoning
    ? `<details class="chat-reasoning" data-reasoning-key="${ctx.escapeHtml(
        key
      )}"${panelOpen ? ' open' : ''}>
         <summary>${ctx.escapeHtml(t('chat.thinking'))}</summary>
         <div class="chat-reasoning-body">${renderMarkdown(message.reasoning, {
           copy: t('chat.copyCode'),
         })}</div>
       </details>`
    : '';
  const stats = statsLine(message.stats);
  const citations = sourcesHtml(message);
  const attachmentChips = (message.attachments || [])
    .map(
      (attachment, attachmentIndex) => `
        <button type="button" class="chat-attachment-chip" data-preview-message="${ctx.escapeHtml(
          key
        )}" data-preview-index="${attachmentIndex}" title="${ctx.escapeHtml(
          t('chat.previewAttachment')
        )}">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
          <span class="chat-attachment-name">${ctx.escapeHtml(attachment.name)}</span>
          <span class="chat-attachment-size">${ctx.escapeHtml(
            formatAttachmentSize(attachment.size)
          )}</span>
        </button>`
    )
    .join('');
  const isLastAssistant =
    role === 'assistant' && index === messages.length - 1 && !message.streaming && !streaming;
  const showContent =
    Boolean(message.content) || Boolean(message.reasoning) || Boolean(typing) || Boolean(message.streaming);
  const persisted = message.id !== undefined && message.id !== null && !message.streaming;
  const canEdit = persisted && role === 'user' && !streaming;
  const canBranch = persisted && !streaming;
  return `
    <article class="chat-msg chat-msg-${role}" data-msg-key="${ctx.escapeHtml(key)}">
      <div class="chat-avatar" aria-hidden="true">${ctx.escapeHtml(avatar)}</div>
      <div class="chat-msg-main">
        <div class="chat-msg-head">
          <span class="chat-msg-role">${ctx.escapeHtml(label)}</span>
          <span class="chat-msg-time">${formatClock(message.created_at)}</span>
          <span class="chat-msg-actions">
            ${
              canEdit
                ? `<button type="button" class="chat-text-btn" data-edit-message="${ctx.escapeHtml(
                    key
                  )}">${ctx.escapeHtml(t('chat.edit'))}</button>`
                : ''
            }
            ${
              canBranch
                ? `<button type="button" class="chat-text-btn" data-branch-message="${ctx.escapeHtml(
                    key
                  )}">${ctx.escapeHtml(t('chat.branch'))}</button>`
                : ''
            }
            ${
              message.content
                ? `<button type="button" class="chat-text-btn" data-copy-message="${ctx.escapeHtml(
                    key
                  )}">${ctx.escapeHtml(t('chat.copy'))}</button>`
                : ''
            }
            ${
              // 有思考过程时把"复制"拆成两个可见动作。以前只有一个按钮、
              // 复制出来却少一半内容，那是静默丢失，不是简洁。
              message.content && message.reasoning
                ? `<button type="button" class="chat-text-btn" data-copy-message="${ctx.escapeHtml(
                    key
                  )}" data-copy-reasoning="1">${ctx.escapeHtml(t('chat.copyWithThinking'))}</button>`
                : ''
            }
            ${
              isLastAssistant
                ? `<button type="button" class="chat-text-btn" data-regenerate>${ctx.escapeHtml(
                    t('chat.regenerate')
                  )}</button>`
                : ''
            }
          </span>
        </div>
        ${reasoning}
        ${
          attachmentChips
            ? `<div class="chat-msg-attachments">${attachmentChips}</div>`
            : ''
        }
        ${
          showContent
            ? `<div class="chat-msg-content" data-msg-content>${
                typing
                  ? `<span class="chat-typing" aria-label="${ctx.escapeHtml(
                      t('chat.generating')
                    )}"><span></span><span></span><span></span></span>`
                  : markdown
              }${message.streaming && !typing ? '<span class="chat-cursor" aria-hidden="true"></span>' : ''}</div>`
            : ''
        }
        ${
          message.error
            ? `<div class="chat-msg-error" role="alert">${ctx.escapeHtml(message.error)}</div>`
            : ''
        }
        ${citations}
        ${stats ? `<div class="chat-msg-stats">${ctx.escapeHtml(stats)}</div>` : ''}
      </div>
    </article>`;
}

function sourcesHtml(message) {
  const sources = Array.isArray(message?.stats?.sources) ? message.stats.sources : [];
  if (!sources.length) return '';
  const items = sources
    .map((source) => {
      const number = Number(source.number) || 0;
      const page = source.page ? t('chat.sourcePage', { page: source.page }) : '';
      // OCR'd sources are best-effort text: the citation must say so instead of
      // looking like a verbatim quotation of the statute.
      const ocr = source.ocr
        ? `<span class="chat-source-ocr" title="${ctx.escapeHtml(t('chat.sourceOcrHelp'))}">${ctx.escapeHtml(t('chat.sourceOcr'))}</span>`
        : '';
      const meta = [page, source.heading].filter(Boolean).join(' · ');
      return `<li class="chat-source">
        <span class="chat-source-number">[${number}]</span>
        <div class="chat-source-body">
          <div class="chat-source-title">${ctx.escapeHtml(String(source.title || ''))}${ocr}</div>
          ${meta ? `<div class="chat-source-meta">${ctx.escapeHtml(meta)}</div>` : ''}
          ${source.snippet ? `<div class="chat-source-snippet">${ctx.escapeHtml(String(source.snippet))}</div>` : ''}
        </div>
      </li>`;
    })
    .join('');
  return `<details class="chat-sources">
    <summary>${ctx.escapeHtml(t('chat.sources', { count: sources.length }))}</summary>
    <ol class="chat-source-list">${items}</ol>
  </details>`;
}

function statsLine(stats) {
  if (!stats || !Object.keys(stats).length) return '';
  const parts = [];
  if (stats.stopped) parts.push(t('chat.partial'));
  const tokens = Number.isFinite(stats.completion_tokens)
    ? stats.completion_tokens
    : Number.isFinite(stats.predicted_n)
      ? stats.predicted_n
      : null;
  const elapsed = Number.isFinite(stats.elapsed_ms) ? stats.elapsed_ms : 0;
  let speed = Number.isFinite(stats.predicted_per_second) ? stats.predicted_per_second : null;
  if (!speed && tokens && elapsed > 0) speed = tokens / (elapsed / 1000);
  if (tokens) parts.push(t('chat.statsTokens', { count: tokens }));
  if (speed) parts.push(`${Number(speed).toFixed(1)} tok/s`);
  if (elapsed > 0) parts.push(t('chat.statsTime', { seconds: (elapsed / 1000).toFixed(1) }));
  return parts.join(' · ');
}

function formatClock(epochSeconds) {
  const value = Number(epochSeconds);
  if (!Number.isFinite(value) || value <= 0) return '';
  try {
    return new Date(value * 1000).toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return '';
  }
}

function updateStreamingBubble() {
  if (!activeStreamKey) return;
  const message = messages.find((item) => item.key === activeStreamKey);
  const thread = root.querySelector('#chat-thread');
  if (!message || !thread) return;
  const element = thread.querySelector(
    `[data-msg-key="${activeStreamKey.replace(/"/g, '')}"]`
  );
  if (!element) return;
  const content = element.querySelector('[data-msg-content]');
  if (!content) return;
  if (!message.content && !message.reasoning) {
    content.innerHTML = `<span class="chat-typing" aria-label="${ctx.escapeHtml(
      t('chat.generating')
    )}"><span></span><span></span><span></span></span>`;
  } else {
    content.innerHTML =
      (message.content
        ? renderMarkdown(message.content, { copy: t('chat.copyCode') })
        : `<div class="chat-thinking-note">${ctx.escapeHtml(t('chat.thinking'))}…</div>`) +
      '<span class="chat-cursor" aria-hidden="true"></span>';
  }
  if (autoScroll) scrollToBottom();
}

function scheduleStreamRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    updateStreamingBubble();
  });
}

function scrollToBottom() {
  const thread = root.querySelector('#chat-thread');
  if (!thread) return;
  thread.scrollTop = thread.scrollHeight;
  autoScroll = true;
  updateScrollButton();
}

function updateScrollButton() {
  const button = root.querySelector('#chat-scroll-bottom');
  if (!button) return;
  const thread = root.querySelector('#chat-thread');
  const distance = thread ? thread.scrollHeight - thread.scrollTop - thread.clientHeight : 0;
  button.hidden = distance < 120;
}

// ---------- local text attachments ----------

function renderPendingAttachments() {
  const container = root.querySelector('#chat-pending-attachments');
  if (!container) return;
  container.hidden = pendingAttachments.length === 0;
  container.innerHTML = pendingAttachments
    .map(
      (attachment, index) => `
        <span class="chat-attachment-chip chat-attachment-pending">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
          <span class="chat-attachment-name">${ctx.escapeHtml(attachment.name)}</span>
          <span class="chat-attachment-size">${ctx.escapeHtml(
            formatAttachmentSize(attachment.size)
          )}</span>
          <button type="button" class="chat-attachment-remove" data-remove-attachment="${index}" aria-label="${ctx.escapeHtml(
            t('chat.removeAttachment')
          )}" title="${ctx.escapeHtml(t('chat.removeAttachment'))}">&times;</button>
        </span>`
    )
    .join('');
}

function clearPendingAttachments() {
  pendingAttachments = [];
  renderPendingAttachments();
  updateComposerState();
}

async function addAttachmentFiles(fileList) {
  if (streaming || mode !== 'native' || !currentInstance()) return;
  const files = Array.from(fileList || []);
  if (!files.length) return;
  let totalChars = pendingAttachments.reduce(
    (sum, attachment) => sum + attachment.content.length,
    0
  );
  for (const file of files) {
    if (pendingAttachments.length >= MAX_ATTACHMENTS) {
      ctx.showToast(t('chat.attachTooMany', { count: MAX_ATTACHMENTS }), 'warning');
      break;
    }
    const name = attachmentBaseName(file.name);
    if (!isSupportedAttachmentName(name)) {
      ctx.showToast(t('chat.attachUnsupported', { name }), 'warning');
      continue;
    }
    if (file.size > 2 * 1024 * 1024) {
      ctx.showToast(t('chat.attachTooLarge', { name }), 'warning');
      continue;
    }
    let content = '';
    try {
      content = await file.text();
    } catch {
      ctx.showToast(t('chat.attachReadFailed', { name }), 'error');
      continue;
    }
    if (looksBinaryText(content)) {
      ctx.showToast(t('chat.attachBinary', { name }), 'warning');
      continue;
    }
    if (content.length > MAX_ATTACHMENT_CHARS) {
      ctx.showToast(t('chat.attachTooLarge', { name }), 'warning');
      continue;
    }
    if (totalChars + content.length > MAX_MESSAGE_ATTACHMENT_CHARS) {
      ctx.showToast(t('chat.attachTooLarge', { name }), 'warning');
      continue;
    }
    totalChars += content.length;
    pendingAttachments.push({
      name,
      content,
      size: file.size || new Blob([content]).size,
    });
  }
  renderPendingAttachments();
  updateComposerState();
  root.querySelector('#chat-input')?.focus();
}

function openAttachmentPreview(messageKey, index) {
  const message = findMessageByKey(messageKey);
  const attachment = message?.attachments?.[Number(index)];
  if (!attachment) return;
  const previewLimit = 30_000;
  const preview = attachment.content.slice(0, previewLimit);
  const truncated = attachment.content.length > previewLimit;
  const body = `
    <div class="chat-attachment-meta">
      <span>${ctx.escapeHtml(attachment.name)}</span>
      <span>${ctx.escapeHtml(formatAttachmentSize(attachment.size))}</span>
    </div>
    <pre class="chat-attachment-preview">${ctx.escapeHtml(
      preview || t('chat.attachmentEmpty')
    )}</pre>
    ${
      truncated
        ? `<div class="param-field-help">${ctx.escapeHtml(t('chat.attachmentTruncated'))}</div>`
        : ''
    }`;
  ctx.showModal(attachment.name, body, [
    {
      label: t('chat.copy'),
      class: 'btn-secondary',
      keepOpen: true,
      onClick: () => copyText(attachment.content),
    },
    { label: t('app.close'), class: 'btn-secondary' },
  ]);
}

// ---------- sending ----------

function updateComposerState() {
  const input = root.querySelector('#chat-input');
  const send = root.querySelector('#btn-chat-send');
  const stop = root.querySelector('#btn-chat-stop');
  const exportBtn = root.querySelector('#btn-chat-export');
  const attachBtn = root.querySelector('#btn-chat-attach');
  const fileInput = root.querySelector('#chat-file-input');
  const state = root.querySelector('#chat-stream-state');
  const inst = currentInstance();
  if (!input || !send || !stop) return;
  input.disabled = !inst || sending;
  const attachDisabled = !inst || streaming || sending;
  if (attachBtn) attachBtn.disabled = attachDisabled;
  if (fileInput) fileInput.disabled = attachDisabled;
  send.disabled =
    !inst || streaming || sending || (!input.value.trim() && pendingAttachments.length === 0);
  stop.hidden = !streaming;
  stop.textContent = t('chat.stop');
  if (exportBtn) exportBtn.disabled = !messages.length;
  input.placeholder = inst ? t('chat.composerPlaceholder') : t('chat.noInstance');
  if (state) state.textContent = streaming ? t('chat.generating') : sending ? t('chat.sending') : '';
  updateContextMeter();
}

function formatTokenCount(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return '0';
  if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(1)}M`;
  if (number >= 10_000) return `${Math.round(number / 1000)}K`;
  if (number >= 1000) return `${(number / 1000).toFixed(1)}K`;
  return String(Math.round(number));
}

function updateContextMeter() {
  const meter = root.querySelector('#chat-context-meter');
  if (!meter) return;
  const inst = currentInstance();
  const usage = contextUsage(messages, inst?.context_size || 0);
  if (!inst || !usage.total) {
    meter.hidden = true;
    return;
  }
  meter.hidden = false;
  meter.classList.toggle('near-limit', usage.nearLimit);
  const label = meter.querySelector('.chat-context-label');
  const fill = meter.querySelector('.chat-context-fill');
  const usedText = usage.used === null ? '—' : formatTokenCount(usage.used);
  const totalText = formatTokenCount(usage.total);
  if (label) {
    label.textContent =
      usage.used === null
        ? t('chat.contextEmpty', { total: totalText })
        : t('chat.contextUsage', { used: usedText, total: totalText });
  }
  if (fill) fill.style.width = `${Math.round(usage.ratio * 100)}%`;
  const exact = `${usage.used === null ? '—' : usage.used} / ${usage.total} tokens`;
  meter.title = `${t('chat.contextHint')}\n${exact}${
    usage.nearLimit ? `\n${t('chat.contextNearLimit')}` : ''
  }`;
  meter.setAttribute(
    'aria-label',
    `${label?.textContent || ''}${usage.nearLimit ? ` · ${t('chat.contextNearLimit')}` : ''}`
  );
}

function autoResize() {
  const input = root.querySelector('#chat-input');
  if (!input) return;
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
}

async function sendCurrent() {
  if (streaming || sending) return;
  const input = root.querySelector('#chat-input');
  const content = input?.value.trim() || '';
  const attachments = pendingAttachments.map(({ name, content: text }) => ({
    name,
    content: text,
  }));
  if (!content && !attachments.length) return;
  const inst = currentInstance();
  if (!inst) {
    ctx.showToast(t('chat.noInstance'), 'warning');
    return;
  }
  if (!activeConversation) {
    await newChat({ preserveAttachments: true });
    if (!activeConversation) return;
  }
  // Freeze conversation selection until the user message is persisted and the
  // stream has started. Without this, a selection response arriving mid-send
  // could make the next message land in a different thread than the one shown.
  sending = true;
  const conversationId = activeConversation.id;
  const targetInstanceId = inst.id;
  input.value = '';
  autoResize();
  updateComposerState();

  try {
    if (
      activeConversation.instance_id !== targetInstanceId ||
      activeConversation.model !== (inst.model_path || '')
    ) {
      const updated = await ctx.Api.updateChat(conversationId, {
        instance_id: targetInstanceId,
        model: inst.model_path || '',
      });
      if (activeConversationId !== conversationId) return;
      activeConversation = updated;
    }
    const saved = await ctx.Api.appendChatMessage(conversationId, {
      role: 'user',
      content,
      attachments,
    });
    if (activeConversationId !== conversationId) return;
    messages.push(normalizeMessage(saved));
    pendingAttachments = [];
    renderPendingAttachments();
    if (!activeConversation.title) {
      activeConversation.title =
        markdownPlainText(content).slice(0, 60) || attachments[0]?.name || '';
    }
    renderThread();
    await generateAssistant();
  } catch (error) {
    input.value = content;
    autoResize();
    ctx.showToast(`${t('chat.sendFailed')}: ${localizeBackendError(error.message)}`, 'error');
  } finally {
    sending = false;
    updateComposerState();
  }
}

async function generateAssistant() {
  const inst = currentInstance();
  if (!inst || streaming || !activeConversation) return;
  const conversationId = activeConversation.id;
  const generation = ++streamGeneration;
  const savedMessages = messages
    .filter(
      (message) =>
        messageUsableForContext(message) &&
        (message.role === 'user' || message.role === 'assistant')
    )
    .map((message) => ({
      role: message.role,
      content: buildMessagePayloadContent(message),
    }))
    .filter((message) => message.content.trim());
  const system = String(draftParams.system || '').trim();
  const payloadMessages = system
    ? [{ role: 'system', content: system }, ...savedMessages]
    : savedMessages;
  if (!payloadMessages.length) return;

  const assistant = {
    key: `stream-${Date.now()}`,
    role: 'assistant',
    content: '',
    reasoning: '',
    reasoningTouched: false,
    reasoningChosen: false,
    stats: {},
    created_at: Date.now() / 1000,
    streaming: true,
  };
  messages.push(assistant);
  activeStreamKey = assistant.key;
  streaming = true;
  abortController = new AbortController();
  renderThread();
  updateComposerState();
  const startedAt = performance.now();
  let usage = null;
  let timings = null;
  let finished = false;
  let failure = null;
  let knowledgeSources = [];

  try {
    const body = { messages: payloadMessages, stream: true };
    if (Number.isFinite(draftParams.temperature)) body.temperature = draftParams.temperature;
    if (Number.isFinite(draftParams.max_tokens) && draftParams.max_tokens >= 0) {
      body.max_tokens = draftParams.max_tokens;
    }
    if (Number.isFinite(draftParams.top_p)) body.top_p = draftParams.top_p;
    if (selectedKnowledgeId) {
      body.knowledge_base_id = selectedKnowledgeId;
      body.knowledge_query = lastUserQuestion();
      body.locale = getLocale();
    }
    const response = await ctx.Api.streamChatCompletion(inst.id, body, {
      signal: abortController.signal,
    });
    // 后端按模型上下文窗口裁掉了最旧的轮次时必须说出来。静默丢历史正是这一串
    // 问题（413 抢跑、思考过程被截断）的共同根：用户以为模型看到了全部对话。
    const dropped = Number.parseInt(
      response.headers?.get?.('x-lunarshroud-dropped-turns') || '0',
      10
    );
    if (Number.isFinite(dropped) && dropped > 0) {
      ctx.showToast(t('chat.turnsDropped', { count: dropped }), 'warning', 6000);
    }
    if (response.headers?.get?.('x-lunarshroud-knowledge') === 'miss') {
      ctx.showToast(t('chat.knowledgeMiss'), 'warning', 6000);
    }
    knowledgeSources = decodeSourcesHeader(
      response.headers?.get?.('x-lunarshroud-sources')
    );
    if (knowledgeSources.length) {
      assistant.stats = { ...(assistant.stats || {}), sources: knowledgeSources };
      scheduleStreamRender();
    }
    const reader = response.body?.getReader();
    if (!reader) throw new Error('empty response body');
    const decoder = new TextDecoder();
    const parser = createSseParser();
    const handleEvents = (events) => {
      for (const event of events) {
        if (event.done) {
          finished = true;
          continue;
        }
        if (event.raw !== undefined || !event.data) continue;
        const chunk = extractChunk(event.data);
        if (!chunk) continue;
        if (chunk.error) throw new Error(chunk.error);
        if (chunk.reasoning) {
          assistant.reasoning += chunk.reasoning;
          scheduleStreamRender();
        }
        if (chunk.content) {
          assistant.content += chunk.content;
          scheduleStreamRender();
        }
        if (chunk.usage) usage = chunk.usage;
        if (chunk.timings) timings = chunk.timings;
        if (chunk.finishReason) finished = true;
      }
    };
    while (!finished) {
      const { done, value } = await reader.read();
      if (done) break;
      handleEvents(parser.push(decoder.decode(value, { stream: true })));
    }
    handleEvents(parser.flush());
    try {
      await reader.cancel();
    } catch {
      /* already closed */
    }
  } catch (error) {
    if (error && error.name === 'AbortError') {
      assistant.stats = { ...buildChatStats({
        usage,
        timings,
        elapsedMs: performance.now() - startedAt,
        content: assistant.content,
        reasoningChars: assistant.reasoning.length,
      }), stopped: true };
    } else {
      failure = localizeBackendError(error?.message || String(error));
    }
  }

  assistant.streaming = false;
  const wasStopped = Boolean(assistant.stats?.stopped);
  const finalStats = buildChatStats({
    usage,
    timings,
    elapsedMs: performance.now() - startedAt,
    content: assistant.content,
    reasoningChars: assistant.reasoning.length,
  });
  if (wasStopped) finalStats.stopped = true;
  if (knowledgeSources.length) finalStats.sources = knowledgeSources;
  assistant.stats = finalStats;
  if (failure) {
    assistant.stats.error = failure;
    assistant.error = failure;
  }

  // A failure that happened before the first token still deserves a persisted
  // marker. Dropping the empty placeholder would make the failure disappear on
  // reload, leaving the user message with no visible explanation.
  if (!shouldPersistAssistantMessage({
    content: assistant.content,
    reasoning: assistant.reasoning,
    failure,
  })) {
    const index = messages.indexOf(assistant);
    if (index >= 0) messages.splice(index, 1);
  } else {
    try {
      const saved = await ctx.Api.appendChatMessage(conversationId, {
        role: 'assistant',
        content: assistant.content,
        // 完整思考过程走自己的一等列。以前它只存在于 stats 里、写库前被截到
        // 4000 字，所以"流式时看得见、重载就少一截"。
        reasoning: assistant.reasoning,
        stats: assistant.stats,
      });
      assistant.id = saved.id;
      assistant.key = String(saved.id);
      assistant.created_at = saved.created_at;
    } catch (error) {
      assistant.error = localizeBackendError(error.message);
    }
  }

  streaming = false;
  abortController = null;
  activeStreamKey = null;
  if (generation === streamGeneration) {
    renderThread();
    updateComposerState();
    if (failure) {
      ctx.showToast(`${t('chat.streamFailed')}: ${failure}`, 'error');
    } else if (assistant.stats?.stopped) {
      const live = root.querySelector('#chat-live');
      if (live) live.textContent = t('chat.assistantStopped');
    }
    await refreshConversations();
    root.querySelector('#chat-input')?.focus();
  }
}

function stopStreaming() {
  if (!abortController) return;
  const button = root.querySelector('#btn-chat-stop');
  if (button) button.textContent = t('chat.stopping');
  abortController.abort();
}

async function regenerateLast() {
  if (streaming) return;
  const lastAssistantIndex = [...messages]
    .map((message, index) => ({ message, index }))
    .reverse()
    .find((entry) => entry.message.role === 'assistant')?.index;
  if (lastAssistantIndex === undefined) return;
  const message = messages[lastAssistantIndex];
  if (message.id) {
    try {
      await ctx.Api.deleteChatMessage(activeConversation.id, message.id);
    } catch (error) {
      ctx.showToast(`${t('chat.regenerate')}: ${localizeBackendError(error.message)}`, 'error');
      return;
    }
  }
  messages.splice(lastAssistantIndex, 1);
  renderThread();
  await generateAssistant();
}

function findMessageByKey(key) {
  return messages.find((message) => message.key === key) || null;
}

function openEditMessage(key) {
  if (streaming) return;
  const index = messages.findIndex((message) => message.key === key);
  const message = messages[index];
  if (!message || !message.id || message.role !== 'user') return;
  const body = `
    <div class="chat-edit-wrap">
      <textarea id="chat-edit-content" class="chat-edit-textarea" rows="10" aria-label="${ctx.escapeHtml(
        t('chat.editTitle')
      )}">${ctx.escapeHtml(message.content || '')}</textarea>
      <div class="param-field-help">${ctx.escapeHtml(t('chat.editHelp'))}</div>
      ${
        message.attachments?.length
          ? `<div class="param-field-help">${ctx.escapeHtml(
              t('chat.editAttachmentsKept')
            )}</div>`
          : ''
      }
    </div>`;
  ctx.showModal(t('chat.editTitle'), body, [
    { label: t('app.cancel'), class: 'btn-secondary' },
    {
      label: t('chat.editSave'),
      class: 'btn-primary',
      onClick: async (overlay, _body, button) => {
        const textarea = overlay.querySelector('#chat-edit-content');
        const value = textarea ? textarea.value : '';
        if (!value.trim() && !message.attachments?.length) {
          ctx.showToast(t('chat.editEmpty'), 'warning');
          return false;
        }
        button.disabled = true;
        try {
          const result = await ctx.Api.updateChatMessage(activeConversationId, message.id, {
            content: value,
            truncate_after: true,
            // 改了助手回复的正文，原来的思考过程就不再对应这条答案了。
            // 留着它比删掉更糟：用户会读到一段推导不出这个结论的思路。
            ...(message.role === 'assistant' ? { reasoning: '' } : {}),
          });
          const updated = normalizeMessage(result.message || { ...message, content: value });
          messages = [...messages.slice(0, index), updated];
          if (activeConversation) {
            activeConversation.message_count = messages.length;
            if (result.conversation_title) {
              activeConversation.title = result.conversation_title;
              const listed = conversations.find((item) => item.id === activeConversation.id);
              if (listed) listed.title = result.conversation_title;
              renderConversationList();
            }
          }
          renderThread();
          updateComposerState();
          ctx.showToast(t('chat.editSaved'), 'success', 1500);
          setTimeout(() => {
            resumeAfterEdit();
          }, 0);
          return true;
        } catch (error) {
          button.disabled = false;
          ctx.showToast(
            `${t('chat.editFailed')}: ${localizeBackendError(error.message)}`,
            'error'
          );
          return false;
        }
      },
    },
  ]);
}

async function resumeAfterEdit() {
  const inst = currentInstance();
  if (!inst || !activeConversation || streaming) return;
  try {
    if (
      activeConversation.instance_id !== inst.id ||
      activeConversation.model !== (inst.model_path || '') ||
      (activeConversation.knowledge_base_id || '') !== (selectedKnowledgeId || '')
    ) {
      activeConversation = await ctx.Api.updateChat(activeConversation.id, {
        instance_id: inst.id,
        model: inst.model_path || '',
        knowledge_base_id: selectedKnowledgeId || null,
      });
    }
    await generateAssistant();
  } catch (error) {
    ctx.showToast(`${t('chat.sendFailed')}: ${localizeBackendError(error.message)}`, 'error');
  }
}

async function branchFromMessage(key) {
  if (streaming || !activeConversationId) return;
  const message = findMessageByKey(key);
  if (!message || !message.id) return;
  try {
    const sourceTitle = activeConversation?.title || t('chat.untitled');
    const detail = await ctx.Api.branchChat(activeConversationId, {
      message_id: message.id,
      title: `${sourceTitle} · ${t('chat.branch')}`,
    });
    activeConversation = detail;
    activeConversationId = detail.id;
    messages = (Array.isArray(detail.messages) ? detail.messages : []).map(normalizeMessage);
    draftParams = { ...DEFAULT_PARAMS, ...(detail.params || {}) };
    selectedKnowledgeId = detail.knowledge_base_id || '';
    renderKnowledgeOptions();
    pendingAttachments = [];
    renderPendingAttachments();
    if (detail.instance_id) {
      const bound = instances.find((item) => item.id === detail.instance_id);
      if (bound) {
        selectedInstanceId = bound.id;
        const select = root.querySelector('#chat-instance-select');
        if (select) select.value = bound.id;
        updateInstanceInfo();
      }
    }
    try {
      localStorage.setItem(ACTIVE_KEY, activeConversationId);
    } catch {
      /* storage unavailable */
    }
    renderThread();
    updateComposerState();
    await refreshConversations();
    root.querySelector('#chat-input')?.focus();
    ctx.showToast(t('chat.branchCreated'), 'success', 1800);
  } catch (error) {
    ctx.showToast(
      `${t('chat.branchFailed')}: ${localizeBackendError(error.message)}`,
      'error'
    );
  }
}

// ---------- prompt library ----------

const BUILTIN_PROMPT_KEYS = [
  ['Explain', 'ExplainText'],
  ['Review', 'ReviewText'],
  ['Tests', 'TestsText'],
  ['Summarize', 'SummarizeText'],
  ['Translate', 'TranslateText'],
  ['Debug', 'DebugText'],
];

function builtinPrompts() {
  return BUILTIN_PROMPT_KEYS.map(([titleKey, textKey], index) => ({
    id: `builtin:${index}`,
    title: t(`chat.prompt${titleKey}Title`),
    content: t(`chat.prompt${textKey}`),
  }));
}

function promptPreview(text) {
  return String(text || '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 90);
}

function insertPromptContent(content) {
  const input = root.querySelector('#chat-input');
  if (!input) return;
  const start = Number.isInteger(input.selectionStart) ? input.selectionStart : input.value.length;
  const end = Number.isInteger(input.selectionEnd) ? input.selectionEnd : input.value.length;
  const before = input.value.slice(0, start);
  const after = input.value.slice(end);
  input.value = `${before}${content}${after}`;
  const caret = before.length + content.length;
  try {
    input.setSelectionRange(caret, caret);
  } catch {
    /* selection unavailable */
  }
  autoResize();
  updateComposerState();
  input.focus();
}

function openPromptLibrary() {
  if (mode !== 'native') return;
  const builtins = builtinPrompts();
  let saved = [];
  const deletedIds = new Set();
  let saving = false;
  let editingPromptId = '';
  const container = document.createElement('div');
  container.className = 'chat-prompt-library';
  container.innerHTML = `
    <div class="chat-prompt-section">
      <div class="chat-prompt-section-title">${ctx.escapeHtml(t('chat.promptBuiltin'))}</div>
      <div class="chat-prompt-list">
        ${builtins
          .map(
            (prompt, index) => `
          <button type="button" class="chat-prompt-item" data-builtin-index="${index}">
            <span class="chat-prompt-item-title">${ctx.escapeHtml(prompt.title)}</span>
            <span class="chat-prompt-item-preview">${ctx.escapeHtml(promptPreview(prompt.content))}</span>
          </button>`
          )
          .join('')}
      </div>
    </div>
    <div class="chat-prompt-section">
      <div class="chat-prompt-section-title">${ctx.escapeHtml(t('chat.promptSaved'))}</div>
      <div class="chat-prompt-saved" id="chat-prompt-saved">
        <div class="chat-prompt-empty"><span class="loading"></span></div>
      </div>
    </div>
    <div class="chat-prompt-save">
      <div class="chat-prompt-section-title">${ctx.escapeHtml(t('chat.promptSaveCurrent'))}</div>
      <input id="chat-prompt-title" class="input" type="text" maxlength="120" placeholder="${ctx.escapeHtml(
        t('chat.promptTitlePlaceholder')
      )}" />
      <textarea id="chat-prompt-content" class="input chat-prompt-content" rows="4" placeholder="${ctx.escapeHtml(
        t('chat.promptContentPlaceholder')
      )}"></textarea>
      <div class="chat-prompt-save-actions">
        <button type="button" class="btn btn-ghost btn-sm" id="chat-prompt-cancel-edit" hidden>${ctx.escapeHtml(
          t('app.cancel')
        )}</button>
        <button type="button" class="btn btn-primary btn-sm" id="chat-prompt-save-btn">${ctx.escapeHtml(
          t('chat.promptSaveCurrent')
        )}</button>
      </div>
    </div>`;
  const composer = root.querySelector('#chat-input');
  const contentInput = container.querySelector('#chat-prompt-content');
  const titleInput = container.querySelector('#chat-prompt-title');
  const saveButton = container.querySelector('#chat-prompt-save-btn');
  const cancelEditButton = container.querySelector('#chat-prompt-cancel-edit');
  const initialContent = composer?.value || '';
  contentInput.value = initialContent;

  const resetPromptForm = () => {
    editingPromptId = '';
    if (titleInput) titleInput.value = '';
    if (contentInput) contentInput.value = initialContent;
    if (saveButton) saveButton.textContent = t('chat.promptSaveCurrent');
    if (cancelEditButton) cancelEditButton.hidden = true;
  };

  const beginPromptEdit = (prompt) => {
    editingPromptId = prompt.id;
    if (titleInput) titleInput.value = prompt.title || '';
    if (contentInput) contentInput.value = prompt.content || '';
    if (saveButton) saveButton.textContent = t('chat.promptSaveEdit');
    if (cancelEditButton) cancelEditButton.hidden = false;
    titleInput?.focus();
  };

  const renderSaved = () => {
    const list = container.querySelector('#chat-prompt-saved');
    if (!list) return;
    if (!saved.length) {
      list.innerHTML = `<div class="chat-prompt-empty">${ctx.escapeHtml(
        t('chat.promptEmpty')
      )}</div>`;
      return;
    }
    list.innerHTML = saved
      .map(
        (prompt) => `
        <div class="chat-prompt-saved-item">
          <button type="button" class="chat-prompt-item" data-saved-id="${ctx.escapeHtml(
            prompt.id
          )}">
            <span class="chat-prompt-item-title">${ctx.escapeHtml(prompt.title || '')}</span>
            <span class="chat-prompt-item-preview">${ctx.escapeHtml(
              promptPreview(prompt.content)
            )}</span>
          </button>
          <button type="button" class="chat-prompt-edit" data-edit-prompt="${ctx.escapeHtml(
            prompt.id
          )}" title="${ctx.escapeHtml(t('chat.promptEdit'))}" aria-label="${ctx.escapeHtml(
            t('chat.promptEdit')
          )}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>
          </button>
          <button type="button" class="chat-prompt-delete" data-delete-prompt="${ctx.escapeHtml(
            prompt.id
          )}" title="${ctx.escapeHtml(t('chat.promptDelete'))}" aria-label="${ctx.escapeHtml(
            t('chat.promptDelete')
          )}">&times;</button>
        </div>`
      )
      .join('');
  };

  container.addEventListener('click', async (event) => {
    const builtin = event.target.closest('[data-builtin-index]');
    if (builtin) {
      const prompt = builtins[Number(builtin.dataset.builtinIndex)];
      if (prompt) {
        ctx.closeModal();
        insertPromptContent(prompt.content);
      }
      return;
    }
    const savedItem = event.target.closest('[data-saved-id]');
    if (savedItem) {
      const prompt = saved.find((item) => item.id === savedItem.dataset.savedId);
      if (prompt) {
        ctx.closeModal();
        insertPromptContent(prompt.content);
      }
      return;
    }
    const editBtn = event.target.closest('[data-edit-prompt]');
    if (editBtn) {
      const prompt = saved.find((item) => item.id === editBtn.dataset.editPrompt);
      if (prompt) beginPromptEdit(prompt);
      return;
    }
    if (event.target.closest('#chat-prompt-cancel-edit')) {
      resetPromptForm();
      contentInput.focus();
      return;
    }
    const deleteBtn = event.target.closest('[data-delete-prompt]');
    if (deleteBtn) {
      event.stopPropagation();
      if (deleteBtn.dataset.armed !== '1') {
        container
          .querySelectorAll('[data-delete-prompt][data-armed="1"]')
          .forEach((other) => {
            other.dataset.armed = '';
            other.textContent = '×';
            other.classList.remove('armed');
            other.setAttribute('aria-label', t('chat.promptDelete'));
          });
        deleteBtn.dataset.armed = '1';
        deleteBtn.textContent = t('chat.promptConfirmDelete');
        deleteBtn.classList.add('armed');
        deleteBtn.setAttribute('aria-label', t('chat.promptConfirmDelete'));
        return;
      }
      try {
        await ctx.Api.deletePrompt(deleteBtn.dataset.deletePrompt);
        deletedIds.add(deleteBtn.dataset.deletePrompt);
        saved = saved.filter((item) => item.id !== deleteBtn.dataset.deletePrompt);
        if (editingPromptId === deleteBtn.dataset.deletePrompt) resetPromptForm();
        renderSaved();
        ctx.showToast(t('chat.promptDeleted'), 'success', 1400);
      } catch (error) {
        ctx.showToast(
          `${t('chat.promptDelete')}: ${localizeBackendError(error.message)}`,
          'error'
        );
      }
      return;
    }
    if (event.target.closest('#chat-prompt-save-btn')) {
      if (saving) return;
      const title = container.querySelector('#chat-prompt-title')?.value || '';
      const content = container.querySelector('#chat-prompt-content')?.value || '';
      if (!content.trim()) {
        ctx.showToast(t('chat.promptNoContent'), 'warning');
        return;
      }
      saving = true;
      const button = event.target.closest('#chat-prompt-save-btn');
      button.disabled = true;
      try {
        if (editingPromptId) {
          const updated = await ctx.Api.updatePrompt(editingPromptId, { title, content });
          saved = saved
            .map((item) => (item.id === updated.id ? updated : item))
            .sort((a, b) => (Number(b.updated_at) || 0) - (Number(a.updated_at) || 0));
          renderSaved();
          resetPromptForm();
          ctx.showToast(t('chat.promptUpdated'), 'success', 1400);
        } else {
          const created = await ctx.Api.createPrompt({ title, content });
          saved.unshift(created);
          renderSaved();
          if (titleInput) titleInput.value = '';
          ctx.showToast(t('chat.promptSavedToast'), 'success', 1400);
        }
      } catch (error) {
        ctx.showToast(
          `${t('chat.promptSaveFailed')}: ${localizeBackendError(error.message)}`,
          'error'
        );
      } finally {
        saving = false;
        button.disabled = false;
      }
    }
  });

  ctx.showModal(t('chat.prompts'), container, [
    { label: t('app.close'), class: 'btn-secondary' },
  ]);

  ctx.Api.listPrompts()
    .then((data) => {
      const remote = Array.isArray(data.prompts) ? data.prompts : [];
      // A prompt saved while this GET was in flight must not be lost when the
      // response lands with the pre-save snapshot.
      const merged = new Map(
        remote
          .filter((prompt) => !deletedIds.has(prompt.id))
          .map((prompt) => [prompt.id, prompt])
      );
      for (const prompt of saved) merged.set(prompt.id, prompt);
      saved = [...merged.values()].sort(
        (a, b) => (Number(b.updated_at) || 0) - (Number(a.updated_at) || 0)
      );
      renderSaved();
    })
    .catch((error) => {
      saved = [];
      renderSaved();
      ctx.showToast(
        `${t('chat.promptLoadFailed')}: ${localizeBackendError(error.message)}`,
        'error'
      );
    });
}

// ---------- conversation settings ----------

function openSettings() {
  const params = { ...DEFAULT_PARAMS, ...(activeConversation?.params || draftParams) };
  const body = `
    ${
      activeConversation
        ? `<div class="param-field">
      <label class="param-field-label" for="chat-setting-title">${ctx.escapeHtml(
        t('chat.titleLabel')
      )}</label>
      <div class="param-field-control">
        <input id="chat-setting-title" class="input" type="text" maxlength="120" value="${ctx.escapeHtml(
          activeConversation.title || ''
        )}" placeholder="${ctx.escapeHtml(t('chat.untitled'))}" />
      </div>
      <div class="param-field-help">${ctx.escapeHtml(t('chat.titleHelp'))}</div>
    </div>`
        : ''
    }
    <div class="param-field">
      <label class="param-field-label" for="chat-setting-system">${ctx.escapeHtml(
        t('chat.systemPrompt')
      )}</label>
      <div class="param-field-control">
        <textarea id="chat-setting-system" class="input chat-setting-system" rows="4">${ctx.escapeHtml(
          params.system || ''
        )}</textarea>
      </div>
      <div class="param-field-help">${ctx.escapeHtml(t('chat.systemPromptHelp'))}</div>
    </div>
    <div class="chat-setting-grid">
      <div class="param-field">
        <label class="param-field-label" for="chat-setting-temperature">${ctx.escapeHtml(
          t('chat.temperature')
        )}</label>
        <div class="param-field-control">
          <input id="chat-setting-temperature" class="input" type="number" min="0" max="2" step="0.1" value="${ctx.escapeHtml(
            String(params.temperature ?? 0.7)
          )}" />
        </div>
      </div>
      <div class="param-field">
        <label class="param-field-label" for="chat-setting-max">${ctx.escapeHtml(
          t('chat.maxTokens')
        )}</label>
        <div class="param-field-control">
          <input id="chat-setting-max" class="input" type="number" min="-1" step="1" value="${ctx.escapeHtml(
            String(params.max_tokens ?? -1)
          )}" />
        </div>
        <div class="param-field-help">${ctx.escapeHtml(t('chat.maxTokensHelp'))}</div>
      </div>
    </div>`;
  ctx.showModal(t('chat.settings'), body, [
    { label: t('app.cancel'), class: 'btn-secondary' },
    {
      label: t('app.save'),
      class: 'btn-primary',
      onClick: async (overlay) => {
        const system = overlay.querySelector('#chat-setting-system').value;
        const titleInput = overlay.querySelector('#chat-setting-title');
        const title = titleInput ? titleInput.value.trim() : '';
        const temperature = Number(overlay.querySelector('#chat-setting-temperature').value);
        const maxTokens = Number(overlay.querySelector('#chat-setting-max').value);
        if (!Number.isFinite(temperature) || temperature < 0 || temperature > 2) {
          ctx.showToast(t('chat.settings'), 'warning');
          return false;
        }
        if (!Number.isFinite(maxTokens) || maxTokens < -1) {
          ctx.showToast(t('chat.settings'), 'warning');
          return false;
        }
        const nextParams = { ...draftParams, system, temperature, max_tokens: maxTokens };
        if (activeConversation) {
          const conversationId = activeConversation.id;
          try {
            const updated = await ctx.Api.updateChat(conversationId, {
              params: nextParams,
              title,
            });
            if (activeConversationId !== conversationId) return false;
            // Commit local state only after the server accepted it; a failed
            // save must not leave the UI looking as if it succeeded.
            activeConversation = { ...updated, title };
            draftParams = nextParams;
            const listed = conversations.find((item) => item.id === conversationId);
            if (listed) listed.title = title;
            renderConversationList();
          } catch (error) {
            ctx.showToast(
              `${t('chat.settingsFailed')}: ${localizeBackendError(error.message)}`,
              'error'
            );
            return false;
          }
        } else {
          draftParams = nextParams;
        }
        ctx.showToast(t('chat.settingsSaved'), 'success');
        return true;
      },
    },
  ]);
}

// ---------- webui fallback ----------

function loadWebUI() {
  const inst = currentInstance();
  if (!inst) {
    showEmpty();
    return;
  }
  if (!inst.loopback) {
    showExternalOnly(inst);
    return;
  }
  const container = root.querySelector('#chat-frame-container');
  const safeUrl = normalizeHttpUrl(inst.url);
  if (!safeUrl) {
    showEmpty();
    ctx.showToast(t('chat.invalidUrl'), 'error');
    return;
  }
  const key = `${inst.id}:${safeUrl}`;
  if (key === lastWebUiKey && container.querySelector('#chat-iframe')) return;
  lastWebUiKey = key;
  container.setAttribute('aria-busy', 'true');
  const placeholder = document.createElement('div');
  placeholder.className = 'chat-loading';
  placeholder.innerHTML = `<span class="loading"></span> <span>${ctx.escapeHtml(
    t('chat.loadingFrame')
  )}</span>`;
  container.replaceChildren(placeholder);

  const iframe = document.createElement('iframe');
  iframe.id = 'chat-iframe';
  iframe.src = safeUrl;
  iframe.className = 'chat-iframe';
  iframe.title = t('chat.frameTitle');
  iframe.setAttribute('frameborder', '0');
  iframe.style.opacity = '0';
  iframe.setAttribute('sandbox', 'allow-scripts allow-forms allow-same-origin');
  iframe.setAttribute('allow', 'clipboard-write');
  iframe.referrerPolicy = 'no-referrer';
  iframe.addEventListener(
    'load',
    () => {
      if (!iframe.isConnected) return;
      iframe.style.opacity = '1';
      placeholder.remove();
      container.removeAttribute('aria-busy');
    },
    { once: true }
  );
  container.appendChild(iframe);
}

function emptyHtml() {
  return `
    <div class="chat-empty" id="chat-empty">
      <div class="chat-empty-title">${ctx.escapeHtml(t('chat.emptyTitle'))}</div>
      <div class="chat-empty-hint">${ctx.escapeHtml(t('chat.emptyHint'))}</div>
      <div class="chat-empty-actions">
        <button class="btn btn-primary btn-sm" id="chat-goto-dashboard">${ctx.escapeHtml(
          t('chat.gotoDashboard')
        )}</button>
        <button class="btn btn-secondary btn-sm" id="chat-reload-last">${ctx.escapeHtml(
          t('chat.reloadLast')
        )} <span class="kbd">Ctrl+R</span></button>
      </div>
    </div>`;
}

function externalOnlyHtml(inst) {
  return `
    <div class="chat-empty" id="chat-external-only">
      <div class="chat-empty-title">${ctx.escapeHtml(t('chat.externalOnlyTitle'))}</div>
      <div class="chat-empty-hint">${ctx.escapeHtml(
        t('chat.externalOnlyReason', { host: inst.host })
      )}</div>
      <button class="btn btn-primary btn-sm" id="chat-open-external">${ctx.escapeHtml(
        t('chat.openInBrowser')
      )}</button>
    </div>`;
}

function showEmpty() {
  const container = root.querySelector('#chat-frame-container');
  if (!container) return;
  container.removeAttribute('aria-busy');
  container.innerHTML = emptyHtml();
  container.querySelector('#chat-goto-dashboard')?.addEventListener('click', () => {
    ctx.navigate('dashboard');
  });
  container.querySelector('#chat-reload-last')?.addEventListener('click', relaunchLastModel);
}

function showExternalOnly(inst) {
  const container = root.querySelector('#chat-frame-container');
  if (!container) return;
  container.removeAttribute('aria-busy');
  container.innerHTML = externalOnlyHtml(inst);
  container.querySelector('#chat-open-external')?.addEventListener('click', () => {
    const safeUrl = normalizeHttpUrl(inst.url);
    if (safeUrl) window.open(safeUrl, '_blank', 'noopener,noreferrer');
    else ctx.showToast(t('chat.invalidUrl'), 'error');
  });
}

async function relaunchLastModel() {
  let records = [];
  try {
    records = await ctx.Api.listHistory(1, 0);
  } catch (error) {
    ctx.showToast(`${t('chat.reloadFailed')}: ${String(error?.message || error)}`, 'error');
    return;
  }
  const last = Array.isArray(records) ? records[0] : null;
  if (!last) {
    ctx.showToast(t('chat.reloadNone'), 'info');
    return;
  }
  const label = String(last.model_path || '').split(/[\\/]/).pop() || last.preset_name || '';
  try {
    await ctx.Api.relaunchHistory(last.id);
    ctx.showToast(t('chat.reloading', { name: label }), 'success');
  } catch (error) {
    ctx.showToast(`${t('chat.reloadFailed')}: ${String(error?.message || error)}`, 'error');
  }
}

// ---------- events ----------

function bindStaticEvents() {
  root.querySelector('#chat-instance-select').addEventListener('change', (event) => {
    selectedInstanceId = event.target.value;
    updateInstanceInfo();
    updateComposerState();
    if (mode === 'webui') loadWebUI();
    if (activeConversation && activeConversation.instance_id !== selectedInstanceId) {
      ctx.showToast(t('chat.instanceChanged'), 'info', 1200);
    }
  });

  root.querySelector('#chat-kb-select').addEventListener('change', async (event) => {
    const previous = selectedKnowledgeId;
    selectedKnowledgeId = event.target.value;
    if (!activeConversation) return;
    event.target.disabled = true;
    try {
      activeConversation = await ctx.Api.updateChat(activeConversation.id, {
        knowledge_base_id: selectedKnowledgeId || null,
      });
      ctx.showToast(
        selectedKnowledgeId ? t('chat.knowledgeChanged') : t('chat.knowledgeDisabled'),
        'info',
        1800
      );
    } catch (error) {
      selectedKnowledgeId = previous;
      event.target.value = previous;
      ctx.showToast(
        `${t('chat.knowledgeChangeFailed')}: ${localizeBackendError(error.message)}`,
        'error'
      );
    } finally {
      event.target.disabled = false;
    }
  });

  root.querySelectorAll('[data-chat-mode]').forEach((button) => {
    button.addEventListener('click', () => setMode(button.dataset.chatMode));
  });

  // 记住用户对某一条思考过程的手动开合。toggle 不冒泡，所以用捕获阶段挂在
  // root 上做事件委托；状态写回消息对象，重绘时才能把它还原回来。
  root.addEventListener(
    'toggle',
    (event) => {
      const panel = event.target;
      if (!(panel instanceof HTMLDetailsElement)) return;
      const key = panel.dataset.reasoningKey;
      if (!key) return;
      // 必须查模块级的 `messages`：渲染用的就是它。`activeConversation.messages`
      // 是后端返回的原始数组，normalizeMessage 已经复制成了另一批对象，改那边
      // 界面上什么都不会发生——手动开合记忆会静默失效。
      const message = messages.find((item) => item.key === key);
      if (!message) return;
      message.reasoningTouched = true;
      message.reasoningChosen = panel.open;
    },
    true,
  );

  root.querySelector('#btn-chat-new').addEventListener('click', newChat);
  const attachBtn = root.querySelector('#btn-chat-attach');
  const fileInput = root.querySelector('#chat-file-input');
  attachBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', async () => {
    const files = Array.from(fileInput.files || []);
    fileInput.value = '';
    await addAttachmentFiles(files);
  });
  root.querySelector('#chat-pending-attachments').addEventListener('click', (event) => {
    const remove = event.target.closest('[data-remove-attachment]');
    if (!remove) return;
    pendingAttachments.splice(Number(remove.dataset.removeAttachment), 1);
    renderPendingAttachments();
    updateComposerState();
  });
  root.querySelector('#btn-chat-prompts').addEventListener('click', openPromptLibrary);
  root.querySelector('#btn-chat-settings').addEventListener('click', openSettings);
  root.querySelector('#btn-chat-export').addEventListener('click', exportConversation);
  root.querySelector('#btn-chat-stop').addEventListener('click', stopStreaming);
  root.querySelector('#btn-chat-send').addEventListener('click', sendCurrent);

  root.querySelector('#btn-chat-refresh').addEventListener('click', () => {
    if (mode !== 'webui') return;
    const iframe = root.querySelector('#chat-iframe');
    if (iframe) {
      iframe.src = iframe.src;
      ctx.showToast(t('chat.refreshed'), 'info', 1500);
    }
  });

  root.querySelector('#btn-chat-external').addEventListener('click', () => {
    const inst = currentInstance();
    if (!inst) return;
    const safeUrl = normalizeHttpUrl(inst.url);
    if (safeUrl) window.open(safeUrl, '_blank', 'noopener,noreferrer');
  });

  const input = root.querySelector('#chat-input');
  input.addEventListener('input', () => {
    autoResize();
    updateComposerState();
  });
  input.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    sendCurrent();
  });

  root.querySelector('#chat-thread').addEventListener('click', (event) => {
    const suggestion = event.target.closest('[data-suggestion]');
    if (suggestion) {
      input.value = suggestion.dataset.suggestion;
      autoResize();
      updateComposerState();
      input.focus();
      return;
    }
    if (event.target.closest('#chat-goto-dashboard')) {
      ctx.navigate('dashboard');
      return;
    }
    if (event.target.closest('#chat-reload-last')) {
      relaunchLastModel();
      return;
    }
    const copyCode = event.target.closest('[data-copy-code]');
    if (copyCode) {
      const code = copyCode.closest('.md-code-block')?.querySelector('pre code')?.textContent || '';
      copyText(code);
      return;
    }
    const previewAttachment = event.target.closest('[data-preview-message]');
    if (previewAttachment) {
      openAttachmentPreview(
        previewAttachment.dataset.previewMessage,
        previewAttachment.dataset.previewIndex
      );
      return;
    }
    const editMessage = event.target.closest('[data-edit-message]');
    if (editMessage) {
      openEditMessage(editMessage.dataset.editMessage);
      return;
    }
    const branchMessage = event.target.closest('[data-branch-message]');
    if (branchMessage) {
      branchFromMessage(branchMessage.dataset.branchMessage);
      return;
    }
    const copyMessage = event.target.closest('[data-copy-message]');
    if (copyMessage) {
      const key = copyMessage.dataset.copyMessage;
      const message = messages.find((item) => item.key === key);
      if (message) {
        copyText(
          messageCopyText(message, {
            includeReasoning: copyMessage.dataset.copyReasoning === '1',
          })
        );
      }
      return;
    }
    if (event.target.closest('[data-regenerate]')) {
      regenerateLast();
    }
  });

  const thread = root.querySelector('#chat-thread');
  thread.addEventListener('scroll', () => {
    const distance = thread.scrollHeight - thread.scrollTop - thread.clientHeight;
    autoScroll = distance < 80;
    updateScrollButton();
  });
  root.querySelector('#chat-scroll-bottom').addEventListener('click', scrollToBottom);

  const native = root.querySelector('#chat-native');
  const dropHint = root.querySelector('#chat-drop-hint');
  const hasFiles = (event) =>
    Array.from(event.dataTransfer?.types || []).includes('Files');
  const canDrop = () => mode === 'native' && !streaming && !!currentInstance();
  native.addEventListener('dragenter', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    if (canDrop() && dropHint) dropHint.hidden = false;
  });
  native.addEventListener('dragover', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  });
  native.addEventListener('dragleave', (event) => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0 && dropHint) dropHint.hidden = true;
  });
  native.addEventListener('drop', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    if (dropHint) dropHint.hidden = true;
    if (canDrop()) addAttachmentFiles(event.dataTransfer.files);
  });
}

function exportConversation() {
  if (!activeConversation || !messages.length) {
    ctx.showToast(t('chat.exportEmpty'), 'warning');
    return;
  }
  const title = activeConversation.title || t('chat.untitled');
  const model = activeConversation.model ? `\n- ${t('chat.model')}: ${activeConversation.model}` : '';
  const lines = [
    `# ${title}`,
    '',
    `- ${t('chat.exportedAt')}: ${new Date().toLocaleString()}`,
    model.trim(),
    '',
    ...messages
      .filter((message) => message.role === 'user' || message.role === 'assistant')
      .flatMap((message) =>
        messageExportLines(message, {
          speaker: message.role === 'user' ? t('chat.you') : t('chat.assistant'),
          reasoningHeading: t('chat.thinking'),
          attachmentHeading: t('chat.attachment'),
          sourcesHeading: t('chat.sourcesHeading'),
          partialMarker: t('chat.partial'),
          errorHeading: t('chat.streamFailed'),
        })
      ),
  ].filter((line) => line !== undefined);
  const markdown = lines.join('\n').trim() + '\n';
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  const safeName = title.replace(/[\\/:*?"<>|]/g, '_').slice(0, 60) || 'chat';
  link.href = url;
  link.download = `${safeName}.md`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  scheduleObjectUrlRevoke(url, (value) => URL.revokeObjectURL(value), 1000);
  ctx.showToast(t('chat.exported'), 'success', 1500);
}

async function copyText(text) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    ctx.showToast(t('chat.copied'), 'success', 1200);
  } catch {
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand('copy');
      ctx.showToast(t('chat.copied'), 'success', 1200);
    } catch {
      ctx.showToast(t('chat.copy'), 'error');
    } finally {
      area.remove();
    }
  }
}
