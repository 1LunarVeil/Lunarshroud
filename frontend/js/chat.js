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
import { classifyAvatar } from './avatar.mjs';
import { describeAgentState } from './chat_agent.mjs';
import { markdownPlainText, renderMarkdown } from './markdown.mjs';
import {
  accumulateToolCalls,
  buildChatStats,
  contextUsage,
  createSseParser,
  extractChunk,
  parseToolArguments,
  planReasoningUpdate,
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
  classifyAttachmentFile,
  formatAttachmentSize,
  looksBinaryText,
  messageHasImages,
} from './chat_attachments.mjs';
import { highlightParts } from './chat_search.mjs';
import { backgroundLayerStyle } from './chat_background.mjs';
import {
  READ_ONLY_TOOLS,
  buildToolResultMessage,
  describeToolExecution,
  parseToolCallArguments,
  pendingToolCalls,
  selectToolDeclarations,
} from './chat_tools.mjs';
// 右栏宽度拖拽与左侧导航共用同一套实现（夹取/键盘/防抖都在那里）。
import { attachResizer, clampWidth, PANEL_LIMITS } from './resizable.mjs';
import {
  MAX_CHAT_TITLE_CHARS,
  buildConversationExport,
  normalizeChatTitle,
  parseConversationImport,
  sortConversations,
} from './chat_conversations.mjs';

export const id = 'chat';
export const title = 'Chat';

const MODE_KEY = 'lunarshroud_chat_mode';
const ACTIVE_KEY = 'lunarshroud_chat_active';
// 会话参数的兜底值。必须与后端 chat_store.PARAM_KEYS 的字段一一对应 ——
// 少一个键（此前缺 thinking）会让对应的 <select> 匹配不到任何 option，
// 下拉框显示为空白，用户会以为"这个设置坏了"。
const DEFAULT_PARAMS = {
  system: '',
  temperature: 0.7,
  max_tokens: -1,
  top_p: 0.95,
  top_k: 40,
  repeat_penalty: 1.1,
  seed: null,
  thinking: 'off',
};

/**
 * 去掉"没设置"的草稿值，只把真正有内容的参数发出去。
 *
 * `seed: null` 是 `DEFAULT_PARAMS` 里的合法状态（"随机"），但把它原样塞进
 * `POST /api/chats` 会让后端把它当成类型错误。2026-11-03 实测：新建会话
 * 100% 失败，而前端把后端那句 `seed must be a number` 误译成
 * "知识库设置无效" —— 用户根本没有知识库。
 *
 * 规则：`null` / `undefined` / 空串 一律不发（= 用后端默认）；
 * `false` 与 `0` **必须保留** —— 它们是有意义的值，不是"空"。
 * 这正是 `if (!value)` 这种写法会犯的错，所以这里显式判 `=== null` 等。
 */
function cleanDraftParams(params) {
  const clean = {};
  for (const [key, value] of Object.entries(params || {})) {
    if (value === null || value === undefined) continue;
    if (typeof value === 'string' && value === '') continue;
    clean[key] = value;
  }
  return clean;
}

// 导出给单元测试：`export function` 在页面脚本里同样可用。
export { cleanDraftParams };

// 会话列表里的小图标。内联 SVG 而不是引用字体/图片：CSP 是 script-src 'self'，
// 而且这几个图形小到手写比多一个资源请求更划算。
const PIN_ICON =
  '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 17v5"/><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 1 1 0 0 0 0-2H8a1 1 0 0 0 0 2 1 1 0 0 1 1 1z"/></svg>';
const PENCIL_ICON =
  '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
const IMPORT_ICON =
  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
const PURGE_ICON =
  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>';
const CLEAR_ICON =
  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><line x1="9" y1="9" x2="15" y2="15"/><line x1="15" y1="9" x2="9" y2="15"/></svg>';

let ctx = null;
let root = null;
let panelEl = null;

let instances = [];
let selectedInstanceId = '';
/**
 * 绑定实例消失、且找不到同模型继任者时留下的"换模型"待确认记录。
 *
 * 形状：`{fromModel, fromInstanceId, toModel, toInstanceId} | null`。
 * 存在时发送路径必须**先让用户确认**，然后再改写会话绑定；不允许悄悄把
 * 一个模型的历史发给另一个模型（见 updateInstances 的说明）。
 */
let pendingModelHandoff = null;
let knowledgeBases = [];
let knowledgeLoaded = false;
let selectedKnowledgeId = '';
let unsubscribe = null;
let mode = readMode();
// 思考过程面板的默认开合偏好，来自 config.chat_reasoning。
// 取不到配置时按 'auto' 走，绝不因为读失败就永久隐藏面板。
let reasoningMode = 'auto';
// 显示身份：空串表示"用 i18n 默认文案 / 首字母头像"。这里只存用户配置的
// 覆盖值，回落发生在渲染时（语言切换要让默认文案跟着变）。
const chatIdentity = {
  assistantName: '',
  userName: '',
  assistantAvatar: '',
  userAvatar: '',
};

// 整页背景图的三项配置。空 image = 无背景（默认）。
const backgroundConfig = {
  image: '',
  opacity: 0.35,
  blur: 0,
};

/**
 * 工具执行被授权的根目录。**默认空**，即"任何工具都不能执行"。
 *
 * 不是从配置读、不是从工作目录猜：授权范围必须由用户在这一轮里显式选定，
 * 否则上次授权的目录会在下一次对话里继续有效（见 AGENTS §2.3 的同一条理由）。
 * 空串时批准条只显示"未选择目录"，不出现"允许"按钮。
 */
let toolRootDirectory = '';

/** 气泡里显示的名字。配置优先，否则用当前语言的默认称呼。 */
function speakerLabel(role) {
  if (role === 'user') return chatIdentity.userName || t('chat.you');
  return chatIdentity.assistantName || t('chat.assistant');
}

/**
 * 气泡头像的文字。配置优先（支持 emoji 或单字），否则退回首字母。
 *
 * 返回值一律截到 8 个码位：配置层已经限长，但历史配置或手工改过的
 * config.json 可能绕过校验，而头像框只有一个字的宽度。
 */
/**
 * 气泡头像的内部 HTML。图片走 <img>，文本/emoji 走文字。
 *
 * 用 `classifyAvatar` 而不是直接判断是否以 `data:` 开头：配置是可被手工编辑
 * 的文件，一个 `javascript:` 或超长值塞进 `<img src>` / 文本节点都会出问题。
 * 分类函数同时负责长度与协议白名单，两处逻辑只有一份。
 */
function speakerAvatarHtml(role) {
  const configured = role === 'user' ? chatIdentity.userAvatar : chatIdentity.assistantAvatar;
  const classified = classifyAvatar(configured);
  if (classified.kind === 'image') {
    return `<img src="${ctx.escapeHtml(classified.value)}" alt="" class="chat-avatar-img"/>`;
  }
  // 头像只用图片。旧配置里可能存着 emoji/首字母（那个形态已废弃），
  // 读取时按"未设置"处理 —— 渲染内置默认头像，而不是把 emoji 当头像文字。
  const glyph =
    role === 'user'
      ? '<circle cx="12" cy="9" r="3.4"/><path d="M5.2 19.2a6.8 6.8 0 0 1 13.6 0"/>'
      : '<circle cx="12" cy="9" r="3.4"/><path d="M5.2 19.2a6.8 6.8 0 0 1 13.6 0"/><circle cx="19" cy="6" r="1.5"/>';
  return `<svg viewBox="0 0 24 24" width="16" height="16" fill="none"
    stroke="currentColor" stroke-width="1.6" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true">${glyph}</svg>`;
}

let conversations = [];
let conversationQuery = '';
let searchResults = [];
let searchLoading = false;
let searchError = '';
let searchTimer = null;
let searchRequestId = 0;
/** 正在内联改名的会话 id（空串 = 没有）。渲染时据此换成输入行。 */
let renamingConversationId = '';
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
/** 右侧检查器是否展开。与其它布局偏好一样存 localStorage。 */
const INSPECTOR_KEY = 'lunarshroud_chat_inspector';
let inspectorOpen = true;
try {
  inspectorOpen = localStorage.getItem(INSPECTOR_KEY) !== '0';
} catch {
  inspectorOpen = true;
}

export function init(el, appCtx) {
  root = el;
  ctx = appCtx;
  // 骨架按 LM Studio 的四段式重排（docs/DESIGN_1.0.md §"Chat 骨架"）：
  //
  //   ┌──────────────────────────────────────────────┬────────────┐
  //   │ 会话头（标题 / 实例胶囊 / 动作）              │            │
  //   ├──────────────────────────────────────────────┤  检查器    │
  //   │ 对话流（可读列宽）                            │ (可收起)   │
  //   ├──────────────────────────────────────────────┤            │
  //   │ composer（输入 + 一行控制条）                 │            │
  //   └──────────────────────────────────────────────┴────────────┘
  //
  // 旧骨架是"页内工具栏 + 内容 + 底部大盒"，工具栏把所有控件（实例、知识库、
  // 模式、六个图标）挤在一行 —— 那是"启动器控制台"的形态，不是聊天 App 的。
  // 现在：实例/知识库/模式/工具这些**设置**移到右侧检查器，会话头只留标题与
  // 最常用的几个动作。所有既有 DOM id / data-* 契约保持不变（见
  // FRONTEND_CONTRACT.md §4.2），所以事件绑定与测试无需改动。
  root.innerHTML = `
    <div class="chat-page-v2">
      <div class="chat-shell">
        <div class="chat-column">
          <header class="chat-conversation-head">
            <div class="chat-conversation-title-wrap">
              <h1 class="chat-conversation-title" id="chat-conversation-title"></h1>
              <span class="chat-agent-badge" id="chat-agent-badge" hidden></span>
            </div>
            <div class="chat-head-actions">
              <button class="btn btn-ghost btn-icon" id="btn-chat-new" title="${ctx.escapeHtml(
                t('chat.newChat')
              )}" aria-label="${ctx.escapeHtml(t('chat.newChat'))}">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>
              </button>
              <button class="btn btn-ghost btn-icon" id="btn-chat-prompts" title="${ctx.escapeHtml(
                t('chat.prompts')
              )}" aria-label="${ctx.escapeHtml(t('chat.prompts'))}">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
              </button>
              <button class="btn btn-ghost btn-icon" id="btn-chat-export" title="${ctx.escapeHtml(
                t('chat.export')
              )}" aria-label="${ctx.escapeHtml(t('chat.export'))}">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              </button>
              <span class="chat-head-divider" aria-hidden="true"></span>
              <button class="btn btn-ghost btn-icon" id="btn-chat-inspector" aria-pressed="true"
                title="${ctx.escapeHtml(t('chat.inspector'))}" aria-label="${ctx.escapeHtml(t('chat.inspector'))}">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="15" y1="3" x2="15" y2="21"/></svg>
              </button>
            </div>
          </header>

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
                      <span class="chat-stream-state" id="chat-stream-state" aria-live="polite"></span>
                    </div>
                    <div class="chat-composer-actions">
                      <button class="btn btn-ghost btn-icon" id="btn-chat-attach" title="${ctx.escapeHtml(
                        t('chat.attachFiles')
                      )}" aria-label="${ctx.escapeHtml(t('chat.attachFiles'))}">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
                      </button>
                      <input id="chat-file-input" class="visually-hidden" type="file" multiple accept="${ctx.escapeHtml(
                        ATTACHMENT_ACCEPT
                      )}" tabindex="-1" aria-hidden="true" />
                      <button class="btn btn-secondary btn-sm" id="btn-chat-stop" hidden>${ctx.escapeHtml(
                        t('chat.stop')
                      )}</button>
                      <button class="btn btn-primary btn-icon" id="btn-chat-send" aria-label="${ctx.escapeHtml(
                        t('chat.send')
                      )}" title="${ctx.escapeHtml(t('chat.send'))}">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>
                      </button>
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
        </div>

        <!-- 右栏宽度拖拽手柄。放在 aside 之前（视觉上在两者之间）。
             右侧面板越往左拖越宽，所以 attachResizer 要传 invert: true。
             role="separator" 可聚焦时必须带 aria-valuenow/min/max。 -->
        <div class="chat-inspector-resizer-wrap" role="region" aria-label="调整参数面板宽度">
          <div class="chat-inspector-resizer" id="chat-inspector-resizer" role="separator"
            aria-orientation="vertical" aria-label="调整参数面板宽度"
            aria-controls="chat-inspector" aria-valuemin="260" aria-valuemax="420"
            aria-valuenow="288" tabindex="0"></div>
        </div>

        <aside class="chat-inspector" id="chat-inspector">
          <!-- 双视图切换（对标 LM Studio 的 Integrations / Configuration）。
               同一块宽度放两套内容：配置（这条对话怎么生成）与集成（模型能用
               什么工具/库/服务）。没有这个切换时，两套内容要么挤在一起、要么
               得再开一列。 -->
          <div class="chat-inspector-switch segmented-control" role="group"
            aria-label="${ctx.escapeHtml(t('chat.inspectorViews'))}">
            <button type="button" class="segmented-btn" data-inspector-view="config"
              aria-pressed="true">${ctx.escapeHtml(t('chat.viewConfig'))}</button>
            <button type="button" class="segmented-btn" data-inspector-view="integrations"
              aria-pressed="false">${ctx.escapeHtml(t('chat.viewIntegrations'))}</button>
          </div>

          <!-- ===== Configuration 视图 ===== -->
          <div class="chat-inspector-view" id="chat-inspector-config">
          <div class="chat-inspector-section">
            <label class="chat-inspector-label" for="chat-instance-select">${ctx.escapeHtml(
              t('chat.instance')
            )}</label>
            <select id="chat-instance-select" class="form-control form-control-sm">
              <option value="">${ctx.escapeHtml(t('chat.loading'))}</option>
            </select>
            <div class="chat-instance-info" id="chat-instance-info"></div>
          </div>
          <div class="chat-inspector-section">
            <label class="chat-inspector-label" for="chat-kb-select">${ctx.escapeHtml(
              t('chat.knowledge')
            )}</label>
            <select id="chat-kb-select" class="form-control form-control-sm">
              <option value="">${ctx.escapeHtml(t('chat.knowledgeNone'))}</option>
            </select>
          </div>

          <!-- ===== 对话参数（常驻，取代旧的"对话设置"弹窗） =====
               旧实现把这些字段藏进模态框：改一次温度要点两次、还会遮住正文。
               LM Studio 的做法是常驻右侧面板。字段与后端 chat_store.PARAM_KEYS
               一一对应 —— 没有后端支持的字段（min_p / grammar / 结构化输出）
               这里刻意不给入口，避免做成点了没反应的假控件。 -->
          <div class="chat-inspector-section">
            <label class="chat-inspector-label" for="chat-param-system">${ctx.escapeHtml(
              t('chat.systemPrompt')
            )}</label>
            <textarea id="chat-param-system" class="chat-param-textarea" rows="3"
              placeholder="${ctx.escapeHtml(t('chat.systemPromptHelp'))}"></textarea>
          </div>

          <details class="chat-inspector-group" id="chat-param-sampling" open>
            <summary>${ctx.escapeHtml(t('chat.sampling'))}</summary>
            <div class="chat-param-grid">
              <label class="chat-param-field">
                <span>${ctx.escapeHtml(t('chat.temperature'))}</span>
                <input id="chat-param-temperature" class="input" type="number"
                  min="0" max="2" step="0.05" />
              </label>
              <label class="chat-param-field">
                <span>${ctx.escapeHtml(t('chat.maxTokens'))}</span>
                <input id="chat-param-max" class="input" type="number" min="-1" step="1" />
              </label>
              <label class="chat-param-field">
                <span>Top P</span>
                <input id="chat-param-top-p" class="input" type="number"
                  min="0" max="1" step="0.05" />
              </label>
              <label class="chat-param-field">
                <span>Top K</span>
                <input id="chat-param-top-k" class="input" type="number" min="0" step="1" />
              </label>
              <label class="chat-param-field">
                <span>${ctx.escapeHtml(t('chat.repeatPenalty'))}</span>
                <input id="chat-param-repeat" class="input" type="number"
                  min="0" max="4" step="0.05" />
              </label>
              <label class="chat-param-field">
                <span>${ctx.escapeHtml(t('chat.seed'))}</span>
                <input id="chat-param-seed" class="input" type="number" step="1"
                  placeholder="${ctx.escapeHtml(t('chat.seedRandom'))}" />
              </label>
            </div>
          </details>

          <div class="chat-inspector-section">
            <label class="chat-inspector-label" for="chat-param-thinking">${ctx.escapeHtml(
              t('chat.thinkingMode')
            )}</label>
            <select id="chat-param-thinking" class="form-control form-control-sm">
              <option value="off">${ctx.escapeHtml(t('chat.thinkingOff'))}</option>
              <option value="on">${ctx.escapeHtml(t('chat.thinkingOn'))}</option>
              <option value="auto">${ctx.escapeHtml(t('chat.thinkingAuto'))}</option>
            </select>
          </div>

          <div class="chat-inspector-section">
            <div class="chat-inspector-label">${ctx.escapeHtml(t('chat.modeLabel'))}</div>
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
          </div>
          <div class="chat-inspector-section">
            <div class="chat-inspector-label">${ctx.escapeHtml(t('chat.contextSection'))}</div>
            <div class="chat-context-meter" id="chat-context-meter" hidden>
              <span class="chat-context-label"></span>
              <span class="chat-context-track" aria-hidden="true"><span class="chat-context-fill"></span></span>
            </div>
            <div class="chat-inspector-note" id="chat-inspector-hint">${ctx.escapeHtml(
              t('chat.inspectorHint')
            )}</div>
          </div>
          <div class="chat-inspector-section chat-inspector-section-last">
            <button type="button" class="btn btn-ghost btn-sm" id="btn-chat-refresh" title="${ctx.escapeHtml(
              t('chat.refresh')
            )}" aria-label="${ctx.escapeHtml(t('chat.refresh'))}">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
            </button>
            <button class="btn btn-ghost btn-sm" id="btn-chat-external" title="${ctx.escapeHtml(
              t('chat.openExternal')
            )}" aria-label="${ctx.escapeHtml(t('chat.openExternal'))}" disabled>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
            </button>
          </div>
          </div><!-- /config view -->

          <!-- ===== Integrations 视图 =====
               模型能"用"什么：工具、工具目录、知识库服务、本地网关。
               全部是已存在的后端能力，只是此前没有一处集中展示。 -->
          <div class="chat-inspector-view" id="chat-inspector-integrations" hidden>
            <div class="chat-inspector-section">
              <div class="chat-inspector-label">${ctx.escapeHtml(t('chat.agentStatus'))}</div>
              <div id="chat-integration-agent" class="chat-integration-body"></div>
            </div>
            <div class="chat-inspector-section">
              <div class="chat-inspector-label">${ctx.escapeHtml(t('chat.toolRoot'))}</div>
              <div id="chat-integration-toolroot" class="chat-integration-body"></div>
            </div>
            <div class="chat-inspector-section">
              <div class="chat-inspector-label">${ctx.escapeHtml(t('chat.kbServices'))}</div>
              <div id="chat-integration-kbservices" class="chat-integration-body"></div>
            </div>
            <div class="chat-inspector-section">
              <div class="chat-inspector-label">${ctx.escapeHtml(t('chat.apiGateway'))}</div>
              <div id="chat-integration-gateway" class="chat-integration-body"></div>
            </div>
          </div>
        </aside>
      </div>
      <div class="visually-hidden" id="chat-live" aria-live="polite"></div>
    </div>
  `;
  bindStaticEvents();
  setMode(mode, { render: false });
  applyInspectorState();
  initInspectorResizer();
  // 恢复上次停留的右栏视图（Configuration / Integrations）。
  inspectorView = readInspectorView();
  setInspectorView(inspectorView);
  renderConversationTitle();
  updateComposerState();
  // 先按默认值把图层准备好（异步读配置回来后再覆盖）：否则首帧会先闪一下
  // 纯色背景再跳到图片。
  applyChatBackground();
  loadChatPreferences();
}

/**
 * 读取"思考过程"开合偏好与显示身份。失败时保持默认值：
 * 读不到配置不该把面板永久藏起来，也不该让气泡失去标签 —— 那比默认值更糟。
 *
 * 身份三项（名字 / 头像）空串表示"用默认"，所以在渲染侧回落而不是在这里
 * 填默认文案：语言切换时默认文案要跟着变，而配置是持久的。
 */
async function loadChatPreferences() {
  let cfg = null;
  try {
    cfg = await ctx.Api.getSystemConfig();
  } catch {
    reasoningMode = 'auto';
    return;
  }
  // 只有取配置这一步允许失败后降级。下面这些是对**已取得的值**做赋值与渲染，
  // 让它们落在同一个 try 里，等于把"代码写错"也吞成"读不到配置" —— 本轮实测
  // 就这么把一个 const 重新赋值（TypeError）吞掉，背景图层静默不生效，且
  // console 里一条错都没有。
  const value = String((cfg && cfg.chat_reasoning) || 'auto');
  reasoningMode = ['auto', 'expand', 'collapse'].includes(value) ? value : 'auto';
  chatIdentity.assistantName = String(cfg?.chat_assistant_name || '').trim();
  chatIdentity.userName = String(cfg?.chat_user_name || '').trim();
  chatIdentity.assistantAvatar = String(cfg?.chat_assistant_avatar || '').trim();
  chatIdentity.userAvatar = String(cfg?.chat_user_avatar || '').trim();
  backgroundConfig.image = String(cfg?.chat_background_image || '');
  backgroundConfig.opacity = cfg?.chat_background_opacity;
  backgroundConfig.blur = cfg?.chat_background_blur;
  applyChatBackground();
  // 右栏宽度：`chat_panel_width` 此前是个**没有任何运行时消费者的配置项**
  // （设置页能改、后端能存，但界面从不读它）—— 拖动面板边缘也没有效果。
  // 这里把它接上：0 = 未设置，用 PANEL_LIMITS.default。
  applyInspectorWidth(cfg?.chat_panel_width);
}

/**
 * 把配置里的宽度应用到右侧参数面板。
 *
 * 用内联 `style.width` 而不是改 `--panel-width` 变量：后者会让所有使用该
 * 变量的地方一起变（包括别处的面板），而用户只想调这一个。
 */
function applyInspectorWidth(value) {
  const panel = root?.querySelector('#chat-inspector');
  const handle = root?.querySelector('#chat-inspector-resizer');
  if (!panel) return;
  const width = clampWidth(value === 0 || value == null || value === '' ? PANEL_LIMITS.default : value, PANEL_LIMITS);
  panel.style.width = `${width}px`;
  panel.style.flexBasis = `${width}px`;
  if (handle) handle.setAttribute('aria-valuenow', String(width));
}

/**
 * 接上右栏拖拽手柄。
 *
 * 与左侧导航的 resizer 共用 `attachResizer`，只有两点不同：
 *   - `invert: true`（往左拖才变宽）
 *   - 提交到 `chat_panel_width`（幂等：拖动过程中防抖，松手才发一次 PUT）
 */
function initInspectorResizer() {
  const panel = root?.querySelector('#chat-inspector');
  const handle = root?.querySelector('#chat-inspector-resizer');
  if (!panel || !handle) return;
  let timer = null;
  let pending = null;
  attachResizer({
    handle,
    target: panel,
    startWidth: PANEL_LIMITS.default,
    limits: PANEL_LIMITS,
    invert: true,
    onCommit: (width) => {
      pending = width;
      if (timer) clearTimeout(timer);
      // 复用侧栏那条防抖的间隔：拖动中每一帧都 PUT 既浪费又会让写入互相覆盖。
      timer = setTimeout(async () => {
        const next = pending;
        pending = null;
        timer = null;
        if (next === null) return;
        try {
          await ctx.Api.updateSystemConfig({ chat_panel_width: next });
        } catch (error) {
          // 保存失败不回滚视觉：宽度立即生效是用户的操作预期，
          // "重新打开回到旧值"才是没保存的真实表现。
          console.warn('[LunarShroud] 保存参数面板宽度失败:', error && error.message);
        }
      }, 500);
    },
  });
}

/**
 * 把配置里的背景图落到 CSS 变量上。
 *
 * 为什么是 CSS 变量而不是每条消息重绘时内联 style：背景属于**整页**，
 * 而消息列表每来一个 chunk 就整块重绘（见 streaming 路径）。写内联 style
 * 等于每次重绘都重新解析一遍几百 KB 的 data URL；变量只设一次。
 *
 * 没有背景时必须**移除**变量而不是设成空 url()：空 url() 在部分 WebView 上
 * 会被当成"当前页面的相对路径"去请求，产生一条无意义的 404。
 */
function applyChatBackground() {
  if (!root) return;
  const layer = root.querySelector('.chat-page-v2');
  if (!layer) return;
  const style = backgroundLayerStyle(
    backgroundConfig.image,
    backgroundConfig.opacity,
    backgroundConfig.blur
  );
  if (!style) {
    layer.classList.remove('has-chat-background');
    layer.style.removeProperty('--chat-bg-image');
    layer.style.removeProperty('--chat-bg-opacity');
    layer.style.removeProperty('--chat-bg-blur');
    return;
  }
  layer.classList.add('has-chat-background');
  layer.style.setProperty('--chat-bg-image', style.imageCss);
  layer.style.setProperty('--chat-bg-opacity', style.opacity);
  layer.style.setProperty('--chat-bg-blur', style.blur);
}

/**
 * 重读配置并重绘当前会话。
 *
 * 为什么要单独一个函数：`onShow` 与"设置已保存"事件都要做同一件事，
 * 而且都必须**重绘**才算数 —— 身份与思考偏好只在渲染时读取，光把值存进
 * 模块变量，屏幕上那条气泡不会变。
 *
 * 用 `stick: false` 重绘：设置面板打开时用户往往滚到某条消息在看，
 * 重绘顺手跳到底部会让他丢失位置。
 */
async function reloadPreferencesAndRepaint() {
  await loadChatPreferences();
  if (!root || mode !== 'native' || !messages.length) return;
  renderThread({ stick: false });
  updateComposerState();
}

/**
 * 会话头标题：当前会话的名字，没有名字时用占位文案。
 *
 * 这是 LM Studio 式的"你在哪个对话里"常驻提示。旧版把会话标题只放在左栏
 * 列表里，主区完全没有 —— 用户滚到长对话中间时无法确认自己在哪条会话。
 * 它同时也是 `chat.untitled` 的落点（改名前的空标题）。
 */
function renderConversationTitle() {
  const el = root?.querySelector('#chat-conversation-title');
  if (!el) return;
  const title = String(activeConversation?.title || '').trim();
  el.textContent = title || t('chat.untitledConversation');
  el.classList.toggle('is-placeholder', !title);
  el.title = title || '';
  // 标题是所有"当前会话变了"的共同出口（选择、新建、分支、改名、自动命名），
  // 参数面板与工具授权目录也在这里同步 —— 逐个调用点加一次必然漏掉某一处。
  renderInspectorParams();
  toolRootDirectory = String(activeConversation?.tool_root || '');
}

/** 展开/收起右侧检查器；偏好持久化，首帧按存储恢复。 */
function applyInspectorState() {
  const page = root?.querySelector('.chat-page-v2');
  const btn = root?.querySelector('#btn-chat-inspector');
  if (!page) return;
  page.classList.toggle('inspector-collapsed', !inspectorOpen);
  if (btn) btn.setAttribute('aria-pressed', inspectorOpen ? 'true' : 'false');
}

/** 右栏当前视图：'config' | 'integrations'。跨会话保持。 */
const INSPECTOR_VIEW_KEY = 'lunarshroud_chat_inspector_view';
let inspectorView = 'config';

function readInspectorView() {
  try {
    return localStorage.getItem(INSPECTOR_VIEW_KEY) === 'integrations' ? 'integrations' : 'config';
  } catch {
    return 'config';
  }
}

function setInspectorView(view) {
  inspectorView = view === 'integrations' ? 'integrations' : 'config';
  const configEl = root?.querySelector('#chat-inspector-config');
  const intEl = root?.querySelector('#chat-inspector-integrations');
  if (configEl) configEl.hidden = inspectorView !== 'config';
  if (intEl) intEl.hidden = inspectorView !== 'integrations';
  root?.querySelectorAll('[data-inspector-view]').forEach((btn) => {
    const active = btn.dataset.inspectorView === inspectorView;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', String(active));
  });
  try {
    localStorage.setItem(INSPECTOR_VIEW_KEY, inspectorView);
  } catch {
    /* storage unavailable */
  }
  // Integrations 的内容是异步拉的（服务状态/网关状态），切过去才拉。
  if (inspectorView === 'integrations') void renderIntegrations();
}

/**
 * 渲染 Integrations 视图。
 *
 * 内容全部来自已有后端能力，没有一个是"占位"：
 *   - Agent 状态：实例的 declared_tools / argv 推导（chat_agent.mjs）
 *   - 工具目录：会话级 tool_root（PATCH /api/chats）
 *   - 知识库服务：GET /api/knowledge/services/status
 *   - 本地网关：GET /api/api-server/status
 *
 * 失败时逐块降级，不让一个接口挂掉整块面板 —— 集成视图是观察面，
 * 不是操作必经路径，缺数据时应当显示"读不到"而不是空白。
 */
async function renderIntegrations() {
  renderIntegrationAgent();
  renderIntegrationToolRoot();

  const kbEl = root?.querySelector('#chat-integration-kbservices');
  if (kbEl) {
    kbEl.innerHTML = `<span class="chat-integration-muted">${ctx.escapeHtml(t('chat.loading'))}</span>`;
    try {
      const data = await ctx.Api.getKnowledgeServicesStatus();
      const services = Array.isArray(data && data.services) ? data.services : [];
      kbEl.innerHTML = services.length
        ? services
            .map(
              (svc) => `<div class="chat-integration-row">
                <span class="badge badge-running">${ctx.escapeHtml(svc.kind)}</span>
                <span class="chat-integration-name">${ctx.escapeHtml(svc.name)}</span>
                <span class="chat-integration-muted">:${ctx.escapeHtml(
                  String(svc.port ?? '-')
                )}</span>
              </div>`
            )
            .join('')
        : `<span class="chat-integration-muted">${ctx.escapeHtml(t('chat.noKbServices'))}</span>`;
    } catch {
      kbEl.innerHTML = `<span class="chat-integration-muted">${ctx.escapeHtml(
        t('chat.integrationUnavailable')
      )}</span>`;
    }
  }

  const gwEl = root?.querySelector('#chat-integration-gateway');
  if (gwEl) {
    try {
      const status = await ctx.Api.getApiServerStatus();
      const running = status && status.running;
      gwEl.innerHTML = `
        <div class="chat-integration-row">
          <span class="badge ${running ? 'badge-running' : 'badge-stopped'}">${ctx.escapeHtml(
            t(running ? 'apiServer.stateRunning' : 'apiServer.stateStopped')
          )}</span>
          <span class="chat-integration-name text-mono">${ctx.escapeHtml(
            status && status.base_url ? status.base_url : '—'
          )}</span>
        </div>
        <div class="chat-integration-row chat-integration-muted">
          ${ctx.escapeHtml(t('apiServer.modelsServed', { count: (status && status.models ? status.models.length : 0) }))}
        </div>`;
    } catch {
      gwEl.innerHTML = `<span class="chat-integration-muted">${ctx.escapeHtml(
        t('chat.integrationUnavailable')
      )}</span>`;
    }
  }
}

/** Agent / 工具 / MCP 徽标（只读，数据来自当前实例）。 */
function renderIntegrationAgent() {
  const el = root?.querySelector('#chat-integration-agent');
  if (!el) return;
  const inst = currentInstance();
  if (!inst) {
    el.innerHTML = `<span class="chat-integration-muted">${ctx.escapeHtml(
      t('chat.integrationNoInstance')
    )}</span>`;
    return;
  }
  const tools = Array.isArray(inst.declared_tools) ? inst.declared_tools : [];
  const rows = [];
  if (tools.length) {
    rows.push(
      `<div class="chat-integration-row">
        <span class="badge badge-running">${ctx.escapeHtml(t('chat.toolNativeBadge', { count: tools.length }))}</span>
      </div>
      <div class="chat-integration-muted text-mono">${ctx.escapeHtml(tools.join(', '))}</div>`
    );
  } else {
    rows.push(
      `<div class="chat-integration-muted">${ctx.escapeHtml(t('chat.toolNoneDeclared'))}</div>`
    );
  }
  el.innerHTML = rows.join('');
}

/** 会话级工具目录授权：显示当前值，提供"更换/撤销"。 */
function renderIntegrationToolRoot() {
  const el = root?.querySelector('#chat-integration-toolroot');
  if (!el) return;
  const current = toolRoot();
  el.innerHTML = `
    <div class="chat-integration-row">
      <span class="chat-integration-name text-mono">${ctx.escapeHtml(
        current || t('chat.toolRootNone')
      )}</span>
    </div>
    <div class="chat-integration-actions">
      <button type="button" class="btn btn-secondary btn-sm" id="btn-integration-toolroot">
        ${ctx.escapeHtml(t('chat.toolRootChoose'))}
      </button>
      ${
        current
          ? `<button type="button" class="btn btn-ghost btn-sm" id="btn-integration-toolroot-clear">
               ${ctx.escapeHtml(t('chat.toolRootClear'))}
             </button>`
          : ''
      }
    </div>`;
  el.querySelector('#btn-integration-toolroot')?.addEventListener('click', async () => {
    await chooseToolRoot();
    renderIntegrationToolRoot();
    renderThread({ stick: false });
  });
  el.querySelector('#btn-integration-toolroot-clear')?.addEventListener('click', async () => {
    toolRootDirectory = '';
    if (activeConversationId) {
      try {
        const updated = await ctx.Api.updateChat(activeConversationId, { tool_root: '' });
        if (activeConversation && activeConversation.id === activeConversationId) {
          activeConversation = { ...activeConversation, ...updated };
        }
      } catch (error) {
        ctx.showToast(
          `${t('chat.toolRootChooseFailed')}: ${localizeBackendError(error.message)}`,
          'error'
        );
      }
    }
    renderIntegrationToolRoot();
    renderThread({ stick: false });
  });
}

function setInspectorOpen(open) {
  inspectorOpen = Boolean(open);
  try {
    localStorage.setItem(INSPECTOR_KEY, inspectorOpen ? '1' : '0');
  } catch {
    /* storage unavailable */
  }
  applyInspectorState();
}

/** 侧栏列表的标题与主行动（由 app.js 统一渲染到列表区标题行）。 */
export function sidebarListTitle() {
  return t('chat.conversations');
}

export function sidebarListAction() {
  return {
    label: '+',
    title: t('chat.newChat'),
    onClick: () => newChat(),
  };
}

export function renderSidebarList(el) {
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
  // 每次进入页面都重读显示身份与思考偏好。
  //
  // 只在 init() 读会让"设置里改完 → 回聊天页"完全无效：页面模块是常驻的，
  // init 一辈子只跑一次。实测就是这么暴露的（用户上传头像、保存、开新对话，
  // 头像和名字都没变）。
  reloadPreferencesAndRepaint();
  // 设置浮层保存后立即重读，不用等用户切页。
  window.addEventListener('lunarshroud:config-saved', reloadPreferencesAndRepaint);  const pendingInstance = window.__pendingChatInstanceId;
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
  // onShow 每次进入都会注册；不在这里摘掉，切页 N 次就有 N 个监听器，
  // 一次保存会触发 N 次重绘。
  window.removeEventListener('lunarshroud:config-saved', reloadPreferencesAndRepaint);
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

/**
 * 会话绑定的实例已经不存在时，决定"接下来跟谁说话"。
 *
 * 三种结果：
 *  - **同模型继任者**（`model_key` 相同）：直接接上，`handoff = null`。
 *    重启会换发新实例 id，但"还是那个模型"，用户不需要为这件事做决定。
 *  - **跨模型**：选一个可用实例，并返回 handoff 让调用方在发送前确认。
 *  - 当前实例列表是一个也挑不出（调用方已提前处理空列表），返回空。
 *
 * 为什么按 `model_key` 而不是文件名：`model_key` 由后端
 * `config_resolver.model_identity()` 规范化（同一模型的不同量化/路径写法会
 * 归一到同一个身份），而文件名会因目录不同而不同 —— 用文件名重绑会把
 * "换过一个目录的同一个模型"误判成换模型。
 */
function rebindConversationInstance(list) {
  const conversation = activeConversation;
  if (!conversation) return { instanceId: '', handoff: null };
  const previousModelKey = String(conversation.model_key || '');
  const previousModel = String(conversation.model || '');
  if (!previousModel && !previousModelKey) {
    // 会话从未绑定过模型（刚建、还没发消息）：随便选一个不算"换模型"。
    return { instanceId: list[0]?.id || '', handoff: null };
  }
  const successor = previousModelKey
    ? list.find((item) => String(item.model_key || '') === previousModelKey)
    : list.find((item) => String(item.model_path || '') === previousModel);
  if (successor) {
    return { instanceId: successor.id, handoff: null };
  }
  const chosen = list[0];
  return {
    instanceId: chosen?.id || '',
    handoff: chosen
      ? {
          fromModel: previousModel,
          fromInstanceId: String(conversation.instance_id || ''),
          toModel: String(chosen.model_path || ''),
          toInstanceId: chosen.id,
        }
      : null,
  };
}

/**
 * 换模型前的确认。返回 `true` 表示"继续用新模型回答"。
 *
 * 这是 F1 的收口点：以前这一步不存在，历史会被静默发给另一个模型。
 * 文案必须说清**从哪个模型换到哪个模型**，而不是泛泛一句"实例已改变" ——
 * 用户要能判断"这条对话接下来还是不是我原来那个模型在答"。
 */
async function confirmModelHandoff() {
  if (!pendingModelHandoff) return true;
  const { fromModel, toModel } = pendingModelHandoff;
  const from = String(fromModel || '').split(/[\\/]/).pop() || t('chat.untitledConversation');
  const to = String(toModel || '').split(/[\\/]/).pop() || t('chat.noRunning');
  const confirmed = await ctx.showConfirm(
    t('chat.modelHandoffConfirm', { from, to }),
    t('chat.modelHandoffTitle')
  );
  if (confirmed) {
    pendingModelHandoff = null;
    return true;
  }
  // 拒绝：不改写会话绑定，也不发送。让用户自己去选择要用的实例。
  return false;
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
    // 知识库的服务也是 llama-server 进程，但答不了对话 —— 让用户选中它们
    // 只会收到一条看不懂的上游错误。
    //
    // 判据用后端给的 `serves_chat`（权威，由 params 的 embeddings/rerank
    // 与 `[KB] ` 前缀共同判定）。旧代码在这里自己看 `item.params?.embeddings`
    // —— 但 `Instance.to_dict()` **从来不返回 params**，那个判断永远是 false，
    // 于是手动启动的 embedding 实例照样出现在下拉里（只有 `[KB] ` 前缀的
    // 一半被挡住）。`serves_chat` 是 2026-11-02 补上的字段。
    .filter((item) => item.serves_chat !== false)
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
    // 绑定实例已经不见了（停止 / 重启换代 —— 重启会换发新 id）。
    //
    // 旧实现无条件落到 instances[0]，于是：会话 A 的历史会被整段发给完全
    // 不相干的模型 B，界面上没有任何提示，会话的 model 字段还会被静默改写。
    // 那是"自制 Chat 里的模型与 WebUI 里的不是同一个东西"的最直接形态。
    //
    // 现在的顺序：
    //   1. **同模型**（model_key 相同）的下一代 → 直接接上，这是重启后最符合
    //      直觉的行为，不需要打扰用户；
    //   2. 找不到同模型 → 选一个，但**记下"换模型了"**，并在发送前拦截确认
    //      （见 confirmModelHandoff），绝不静默把历史发给另一个模型。
    const rebind = rebindConversationInstance(instances);
    selectedInstanceId = rebind.instanceId || instances[0].id;
    pendingModelHandoff = rebind.handoff;
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
    updateAgentBadge(inst);
    updateToolCapabilityBadge(inst);
  } else {
    infoEl.textContent = '';
    if (badge) badge.textContent = '';
    updateToolCapabilityBadge(null);
    updateAgentBadge(null);
  }
}

/**
 * 显示当前实例的 Agent / MCP 状态。
 *
 * 为什么从**启动参数**读而不是查端点：本机实测 llama-server b11698 没有暴露
 * 任何 MCP 的 REST 端点（`/api/mcp`、`/mcp/servers`、`/props` 里的 mcp 字段
 * 全部不存在，`/props` 只有 `cors_proxy_enabled`）。MCP 服务器是在它进程内
 * 由 WebUI 管理、或经 `--mcp-servers-json` / `--mcp-servers-config` 在启动时
 * 传入的。后者是我们唯一能如实反映的部分 —— 与其编一个不存在的接口，不如
 * 把用户真正配了什么显示出来。
 *
 * 数据来自 `/api/instances/{id}/command`，那里已经过 redact_argv，所以 MCP
 * JSON 里的凭据在到达前端之前就是 `[REDACTED]`（见 param_builder 的同名测试）。
 */
async function updateAgentBadge(inst) {
  const el = root.querySelector('#chat-agent-badge');
  if (!el) return;
  if (!inst) {
    el.hidden = true;
    el.textContent = '';
    el.removeAttribute('title');
    return;
  }
  let argv = [];
  try {
    const command = await ctx.Api.getInstanceCommand(inst.id);
    argv = Array.isArray(command?.argv) ? command.argv : [];
  } catch {
    el.hidden = true;
    return;
  }
  const state = describeAgentState(argv, {
    badge: t('chat.agentBadge'),
    agentMode: t('chat.agentModeOn'),
    tools: (list) => t('chat.agentTools', { list }),
    mcpProxy: t('chat.agentMcpProxy'),
    mcpServers: (count) => t('chat.agentMcpServers', { count }),
    mcpFromFile: t('chat.agentMcpFromFile'),
  });
  if (!state.enabled) {
    el.hidden = true;
    el.textContent = '';
    el.removeAttribute('title');
    return;
  }
  el.hidden = false;
  el.textContent = state.label;
  el.title = state.title;
  el.classList.toggle('chat-agent-badge-on', state.hasMcp);
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
  const pinned = Boolean(conversation.pinned);
  // 重命名走内联输入框而不是弹窗：列表项本来就在手边，弹一层浮层会遮住
  // 用户正在对照的其它会话，而且 §2.20 那类"弹窗挂起"的坑能少踩一次。
  // 输入框默认 hidden，点"改名"才换成可编辑行 —— 常驻输入框会让整张列表
  // 读起来像表单，也会抢走方向键。
  return `
    <div class="panel-chat-item${pinned ? ' is-pinned' : ''}" role="listitem" data-chat-row="${ctx.escapeHtml(
      conversation.id
    )}">
      <button type="button" class="panel-list-item ${active ? 'active' : ''}" data-chat-id="${ctx.escapeHtml(
        conversation.id
      )}"${active ? ' aria-current="true"' : ''}>
        <span class="panel-list-row"><span class="panel-list-label">${
          pinned
            ? `<span class="panel-chat-pin-mark" aria-hidden="true">${PIN_ICON}</span>`
            : ''
        }${ctx.escapeHtml(title)}</span></span>
        <span class="panel-list-meta">${ctx.escapeHtml(t('chat.messageCount', { count }))}</span>
      </button>
      <span class="panel-chat-tools">
        <button type="button" class="panel-chat-tool" data-pin-chat="${ctx.escapeHtml(
          conversation.id
        )}" data-pinned="${pinned ? '1' : '0'}"
          aria-pressed="${pinned ? 'true' : 'false'}"
          aria-label="${ctx.escapeHtml(pinned ? t('chat.unpin') : t('chat.pin'))}"
          title="${ctx.escapeHtml(pinned ? t('chat.unpin') : t('chat.pin'))}">${PIN_ICON}</button>
        <button type="button" class="panel-chat-tool" data-rename-chat="${ctx.escapeHtml(
          conversation.id
        )}" aria-label="${ctx.escapeHtml(t('chat.rename'))}" title="${ctx.escapeHtml(
          t('chat.rename')
        )}">${PENCIL_ICON}</button>
        <button type="button" class="panel-chat-delete" data-delete-chat="${ctx.escapeHtml(
          conversation.id
        )}" aria-label="${ctx.escapeHtml(t('chat.deleteChat'))}" title="${ctx.escapeHtml(
          t('chat.deleteChat')
        )}">&times;</button>
      </span>
    </div>`;
}

/** 内联改名行。确认/取消都要能键盘操作，Esc 必须还原而不是提交。 */
function renameRowHtml(conversation) {
  const title = conversation.title || '';
  return `
    <form class="panel-chat-rename" data-rename-form="${ctx.escapeHtml(
      conversation.id
    )}" autocomplete="off">
      <input type="text" class="panel-chat-rename-input" data-rename-input="${ctx.escapeHtml(
        conversation.id
      )}" value="${ctx.escapeHtml(title)}" maxlength="${MAX_CHAT_TITLE_CHARS}"
        aria-label="${ctx.escapeHtml(t('chat.rename'))}"
        placeholder="${ctx.escapeHtml(t('chat.renamePlaceholder'))}" />
      <button type="submit" class="btn btn-primary btn-sm">${ctx.escapeHtml(
        t('app.save')
      )}</button>
      <button type="button" class="btn btn-ghost btn-sm" data-rename-cancel="${ctx.escapeHtml(
        conversation.id
      )}">${ctx.escapeHtml(t('app.cancel'))}</button>
    </form>`;
}

/** 置顶项单独成组，其余按时间。分组标签只在真有条目时出现。 */
function conversationGroupsHtml(list) {
  const pinned = list.filter((item) => item && item.pinned);
  const rest = list.filter((item) => !(item && item.pinned));
  const section = (labelKey, items) =>
    items.length
      ? `<div class="panel-chat-group-label">${ctx.escapeHtml(t(labelKey))}</div>${items
          .map(conversationItemHtml)
          .join('')}`
      : '';
  return `${section('chat.pinnedSection', pinned)}${section(
    'chat.recentSection',
    rest
  )}`;
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
    const sorted = sortConversations(conversations);
    // 管理动作放**列表之上**，而且视觉上必须是独立一类。
    //
    // 放底部的问题（实测反馈）：用户滚到列表末尾才看见它们，而列表通常很长；
    // 更糟的是它们与"会话条目"同为 ghost 小按钮，一眼看不出这是"对整批数据
    // 的操作"而不是"另一个会话"。所以：置顶 + 容器带边框/底色 + 加图标，
    // 与下面的会话列表形成明确的分区。
    const manageBar = `<div class="panel-chat-manage" role="toolbar" aria-label="${ctx.escapeHtml(
      t('chat.manageActions')
    )}">
      <button type="button" class="panel-chat-manage-btn" id="btn-chat-import"
        title="${ctx.escapeHtml(t('chat.importChatHint'))}">${IMPORT_ICON}<span>${ctx.escapeHtml(
          t('chat.importChat')
        )}</span></button>
      <button type="button" class="panel-chat-manage-btn" id="btn-chat-purge"
        title="${ctx.escapeHtml(t('chat.purgeChatsHint'))}">${PURGE_ICON}<span>${ctx.escapeHtml(
          t('chat.purgeChats')
        )}</span></button>
      <button type="button" class="panel-chat-manage-btn is-danger" id="btn-chat-clear"
        title="${ctx.escapeHtml(t('chat.clearChatsConfirm'))}">${CLEAR_ICON}<span>${ctx.escapeHtml(
          t('chat.clearChats')
        )}</span></button>
    </div>`;
    body.innerHTML = sorted.length
      ? `${manageBar}<div class="panel-chat-list" role="list" aria-label="${ctx.escapeHtml(
          t('chat.conversationList')
        )}">${conversationGroupsHtml(sorted)}</div>`
      : `${manageBar}<div class="panel-chat-empty"><div class="panel-chat-empty-title">${ctx.escapeHtml(
          t('chat.noConversations')
        )}</div><div class="panel-chat-empty-hint">${ctx.escapeHtml(
          t('chat.noConversationsHint')
        )}</div></div>`;
    // 改名输入行插在列表之后而不是替换列表项：用户正在改名的会话可能同时
    // 是当前打开的那条，替换掉会让"我到底在改哪一条"失去参照。
    if (renamingConversationId) {
      const target = conversations.find((item) => item.id === renamingConversationId);
      if (target) body.insertAdjacentHTML('beforeend', renameRowHtml(target));
      else renamingConversationId = '';
    }
    focusRenameInput();
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
  // 头部（"对话 / 新对话"）不在这里渲染：侧栏列表区已经有统一的标题行与
  // 主行动按钮（由 app.js 按 sidebarListTitle/sidebarListAction 渲染）。
  // 两处都画一遍正是"同一功能拆得到处都是"的来源。
  panelEl.innerHTML = `
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
    const pinBtn = event.target.closest('[data-pin-chat]');
    if (pinBtn) {
      event.stopPropagation();
      await togglePinned(pinBtn.dataset.pinChat, pinBtn.dataset.pinned !== '1');
      return;
    }
    const renameBtn = event.target.closest('[data-rename-chat]');
    if (renameBtn) {
      event.stopPropagation();
      startRename(renameBtn.dataset.renameChat);
      return;
    }
    const renameCancel = event.target.closest('[data-rename-cancel]');
    if (renameCancel) {
      event.preventDefault();
      renamingConversationId = '';
      renderConversationBody();
      return;
    }
    const item = event.target.closest('[data-chat-id]');
    if (item) {
      await selectConversation(item.dataset.chatId);
      return;
    }
    if (event.target.closest('#chat-search-clear')) {
      clearConversationSearch({ focus: true });
      return;
    }
    if (event.target.closest('#btn-chat-import')) {
      await importConversationFromFile();
      return;
    }
    if (event.target.closest('#btn-chat-purge')) {
      await openPurgeDialog();
      return;
    }
    if (event.target.closest('#btn-chat-clear')) {
      await clearConversations();
    }
  };
  // 改名是 <form>：回车提交、Esc 取消。用 submit 而不是监听 input 的 keydown，
  // 因为表单的默认行为已经处理了"点保存按钮也提交"这一半。
  panelEl.onsubmit = async (event) => {
    const form = event.target.closest('[data-rename-form]');
    if (!form) return;
    event.preventDefault();
    const conversationId = form.dataset.renameForm;
    const input = form.querySelector('[data-rename-input]');
    await commitRename(conversationId, input ? input.value : '');
  };
  panelEl.oninput = (event) => {
    if (event.target.id === 'chat-conversation-search') {
      scheduleConversationSearch(event.target.value);
    }
  };
  panelEl.onkeydown = (event) => {
    if (event.isComposing) return;
    // 改名输入框里的 Esc 只取消改名，不能连带清空搜索词。
    if (event.target.closest('[data-rename-input]') && event.key === 'Escape') {
      event.preventDefault();
      renamingConversationId = '';
      renderConversationBody();
      return;
    }
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
    messages = hydrateToolResults((Array.isArray(detail.messages) ? detail.messages : []).map(normalizeMessage));
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
      // 模型身份要在建会话时就记下来：日后实例重启换发新 id 时，靠它判断
      // "接手的还是不是同一个模型"（同 key 静默续上，不同 key 必须确认）。
      model_key: inst.model_key || '',
      // 只提交**真正设了**的参数。`seed: null` 这类"留空"值不该进请求体：
      // 后端把它当类型错误（`seed must be a number`），新建会话 100% 失败。
      // 即使后端已经放行 `None`（见 chat_store._normalize_params），
      // 也不该把无意义的 null 发出去 —— 两处都做，是因为它们防的是不同的
      // 漂移方向：前端不该发垃圾，后端不该因垃圾崩。
      params: cleanDraftParams(draftParams),
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

// ---------- conversation management (rename / pin / purge / import) ----------

function startRename(conversationId) {
  if (!conversationId) return;
  renamingConversationId = conversationId;
  renderConversationBody();
  focusRenameInput();
}

/**
 * 把光标放进改名输入框。
 *
 * 必须在这里单独聚焦：``renderConversationBody`` 会整块替换 ``#chat-panel-body``，
 * 刚插进去的 ``<input>`` 不自动获得焦点。用户点了"改名"却要再点一次输入框，
 * 会读作"这个按钮坏了"（AGENTS §2.27 同一类问题：控件存在但没活着）。
 */
function focusRenameInput() {
  if (!renamingConversationId || !panelEl) return;
  const input = panelEl.querySelector(
    `[data-rename-input="${CSS.escape(renamingConversationId)}"]`
  );
  if (!input) return;
  input.focus();
  input.select();
}

async function commitRename(conversationId, value) {
  const verdict = normalizeChatTitle(value);
  if (!verdict.ok) {
    // 空标题明确拒绝而不是"留空即恢复自动标题"：后端会真的存成空串，
    // 列表随后显示"新对话"，用户以为改名没生效。
    ctx.showToast(t('chat.renameEmpty'), 'warning');
    focusRenameInput();
    return;
  }
  const previous = conversations.find((item) => item.id === conversationId);
  const previousTitle = previous ? previous.title : '';
  if (previous) previous.title = verdict.title;
  renamingConversationId = '';
  renderConversationList();
  try {
    const updated = await ctx.Api.updateChat(conversationId, { title: verdict.title });
    const found = conversations.find((item) => item.id === conversationId);
    if (found) found.title = updated.title;
    if (activeConversation && activeConversation.id === conversationId) {
      activeConversation.title = updated.title;
    }
    if (verdict.reason === 'too-long') ctx.showToast(t('chat.renameTruncated'), 'info');
    else ctx.showToast(t('chat.renamed'), 'success', 1500);
    renderConversationList();
  } catch (error) {
    if (previous) previous.title = previousTitle;
    ctx.showToast(`${t('chat.renameFailed')}: ${localizeBackendError(error.message)}`, 'error');
    renderConversationList();
  }
}

async function togglePinned(conversationId, pinned) {
  const target = conversations.find((item) => item.id === conversationId);
  if (!target) return;
  const previous = Boolean(target.pinned);
  target.pinned = pinned;
  renderConversationBody();
  try {
    const updated = await ctx.Api.updateChat(conversationId, { pinned });
    target.pinned = Boolean(updated.pinned);
    if (activeConversation && activeConversation.id === conversationId) {
      activeConversation.pinned = target.pinned;
    }
    ctx.showToast(t(pinned ? 'chat.pinned' : 'chat.unpinned'), 'success', 1200);
  } catch (error) {
    target.pinned = previous;
    ctx.showToast(
      `${t('chat.pinFailed')}: ${localizeBackendError(error.message)}`,
      'error'
    );
  } finally {
    renderConversationBody();
  }
}

/** 触发隐藏的 <input type=file> 读一个 JSON，然后交给后端导入。 */
async function importConversationFromFile() {
  if (streaming || sending) {
    ctx.showToast(t(streaming ? 'chat.stopping' : 'chat.sending'), 'info');
    return;
  }
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'application/json,.json';
  input.style.display = 'none';
  document.body.appendChild(input);
  const file = await new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      input.remove();
      resolve(value);
    };
    input.addEventListener('change', () => done(input.files && input.files[0]));
    // 用户点"取消"时部分 WebView 不发 change —— 靠 focus 回来兜底，
    // 否则这个 Promise 永远不 resolve，文件输入框就留在 DOM 里。
    window.addEventListener(
      'focus',
      () => setTimeout(() => done(null), 500),
      { once: true }
    );
    input.click();
  });
  if (!file) return;
  let payload;
  try {
    const text = await file.text();
    payload = JSON.parse(text);
  } catch {
    ctx.showToast(t('chat.importInvalidJson'), 'error');
    return;
  }
  const verdict = parseConversationImport(payload);
  if (!verdict.ok) {
    ctx.showToast(`${t('chat.importInvalid')} (${t(`chat.importReason_${verdict.reason}`)})`, 'error');
    return;
  }
  try {
    const imported = await ctx.Api.importChat(payload);
    ctx.showToast(t('chat.imported'), 'success');
    await loadConversations();
    await selectConversation(imported.id, { quiet: true });
  } catch (error) {
    ctx.showToast(`${t('chat.importFailed')}: ${localizeBackendError(error.message)}`, 'error');
  }
}

/** 批量清理：天数与保留数量两个可选条件，都不填就只给提示、不发请求。 */
async function openPurgeDialog() {
  if (streaming || sending) {
    ctx.showToast(t(streaming ? 'chat.stopping' : 'chat.sending'), 'info');
    return;
  }
  const body = document.createElement('div');
  body.className = 'chat-purge-form';
  body.innerHTML = `
    <div class="diag-intro">${ctx.escapeHtml(t('chat.purgeIntro'))}</div>
    <label class="param-field-label" for="chat-purge-days">
      <span>${ctx.escapeHtml(t('chat.purgeDays'))}</span>
    </label>
    <input type="number" min="1" step="1" class="input" id="chat-purge-days"
      placeholder="${ctx.escapeHtml(t('chat.purgeDaysPlaceholder'))}" />
    <label class="param-field-label" for="chat-purge-keep">
      <span>${ctx.escapeHtml(t('chat.purgeKeep'))}</span>
    </label>
    <input type="number" min="0" step="1" class="input" id="chat-purge-keep"
      placeholder="${ctx.escapeHtml(t('chat.purgeKeepPlaceholder'))}" />
    <div class="param-field-help">${ctx.escapeHtml(t('chat.purgePinnedNote'))}</div>`;
  ctx.showModal(
    t('chat.purgeChats'),
    body,
    [
      { label: t('app.cancel'), class: 'btn-secondary', onClick: () => true },
      {
        label: t('chat.purgeConfirm'),
        class: 'btn-danger',
        onClick: async () => {
          const daysRaw = body.querySelector('#chat-purge-days')?.value || '';
          const keepRaw = body.querySelector('#chat-purge-keep')?.value || '';
          const payload = {};
          if (daysRaw.trim() !== '') {
            const days = Number(daysRaw);
            if (!Number.isFinite(days) || days <= 0) {
              ctx.showToast(t('chat.purgeInvalidDays'), 'warning');
              return false;
            }
            payload.older_than_days = days;
          }
          if (keepRaw.trim() !== '') {
            const keep = Number(keepRaw);
            if (!Number.isInteger(keep) || keep < 0) {
              ctx.showToast(t('chat.purgeInvalidKeep'), 'warning');
              return false;
            }
            payload.keep = keep;
          }
          if (Object.keys(payload).length === 0) {
            ctx.showToast(t('chat.purgeNothingSelected'), 'warning');
            return false;
          }
          try {
            const result = await ctx.Api.purgeChats(payload);
            const deleted = Number(result?.deleted) || 0;
            ctx.showToast(t('chat.purgeDone', { count: deleted }), 'success');
            await loadConversations();
            // 当前会话可能正好被删掉：它的详情再拉一次会 404，直接清空更诚实。
            if (
              activeConversationId &&
              !conversations.some((item) => item.id === activeConversationId)
            ) {
              resetActiveConversation();
            }
            return true;
          } catch (error) {
            ctx.showToast(
              `${t('chat.purgeFailed')}: ${localizeBackendError(error.message)}`,
              'error'
            );
            return false;
          }
        },
      },
    ],
    { onClose: () => {} }
  );
}

/** 当前会话已不存在（被删除/清理）时把界面恢复到空态。 */
function resetActiveConversation() {
  activeConversation = null;
  activeConversationId = '';
  messages = [];
  pendingAttachments = [];
  renderPendingAttachments();
  renderThread();
  updateComposerState();
  renderConversationList();
}

// ---------- messages ----------

/**
 * 把 tool 消息的结果回填到发起它的 assistant 上。
 *
 * 为什么必须做：`pendingToolCalls()` 靠 `message.toolResults` 判断"这个调用
 * 还没有结果"。而 `toolResults` 只活在内存里，刷新后 `normalizeMessage` 不恢复
 * 它 —— 于是**已经执行过**的调用会被当成待批准，批准条又弹一次，用户点了
 * 就会重复执行同一个只读工具。
 *
 * 数据来源就是已落库的 tool 消息：它有 `tool_call_id`，正好是配对的钥匙。
 */
function hydrateToolResults(list) {
  const byCallId = new Map();
  for (const message of list) {
    if (message.role !== 'tool') continue;
    const callId = String(message.tool_call_id || '');
    if (callId) byCallId.set(callId, message);
  }
  if (!byCallId.size) return list;
  for (const message of list) {
    if (message.role !== 'assistant' || !Array.isArray(message.toolCalls)) continue;
    for (const call of message.toolCalls) {
      const callId = String(call.id || call.tool_call_id || '');
      const result = byCallId.get(callId);
      if (!result) continue;
      message.toolResults = {
        ...(message.toolResults || {}),
        [callId]: { recovered: true, content: String(result.content || '') },
      };
    }
  }
  return list;
}

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
    // 工具调用同样从 stats 恢复。不恢复的话，刷新后那条 assistant 就成了一条
    // 空回复，"模型请求过什么"整段消失，用户也没法再批准。
    toolCalls: Array.isArray(stats.tool_calls)
      ? stats.tool_calls
      : Array.isArray(message.toolCalls)
        ? message.toolCalls
        : [],
    // tool 消息的配对 id。重载后要靠它把"结果已存在"回填到发起调用的那条
    // assistant 上 —— 否则待批准判定会把**已经执行过**的调用再弹一次批准条。
    tool_call_id:
      typeof message.tool_call_id === 'string' && message.tool_call_id
        ? message.tool_call_id
        : null,
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
      <div class="chat-native-empty-mark" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
      </div>
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
  // 会话头标题跟着一起刷新。放在这里而不是每个调用点：renderThread 是所有
  // "当前会话变了"路径的共同出口（选择、新建、分支、改名、发送后自动命名），
  // 逐个调用点加一次必然漏掉某一处。
  renderConversationTitle();
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
  const label = speakerLabel(role);
  const avatar = speakerAvatarHtml(role);
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
  const toolCalls = toolCallsHtml(message);
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
  const canDelete = persisted && !streaming;
  return `
    <article class="chat-msg chat-msg-${role}" data-msg-key="${ctx.escapeHtml(key)}">
      <div class="chat-avatar" aria-hidden="true">${avatar}</div>
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
            ${
              canDelete
                ? `<button type="button" class="chat-text-btn" data-delete-message="${ctx.escapeHtml(
                    key
                  )}">${ctx.escapeHtml(t('chat.deleteMessage'))}</button>`
                : ''
            }
          </span>
        </div>
        ${reasoning}
        ${toolCalls}
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
        ${
          // 思考吃光预算：有思考、无正文。不提示的话用户只看到一段"思考过程"，
          // 会以为模型卡死或变笨，而真实原因是 max_tokens 被思考用完。
          message.stats && message.stats.thinkingExhausted
            ? `<div class="chat-msg-error" role="alert">${ctx.escapeHtml(
                t('chat.thinkingExhausted')
              )}</div>`
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

/**
 * 渲染模型发起的工具调用。
 *
 * 为什么值得单独一块：llama.cpp 只在请求里带了 `tools` schema 时才会产生
 * `tool_calls`（实测 b11698：不带 tools 时模型只会把工具名当普通文本念出来）。
 * 而我们**不自动注入 schema** —— 工具是服务端能力（`--tools all`），
 * 模型只是请求调用，真正执行与否由 llama-server 决定。所以这里做的是
 * "如实展示模型想调用什么"，不是"替它执行"。
 *
 * 流式期间 `arguments` 一定是不完整的 JSON，解析会失败 —— 那时显示原始
 * 片段而不是"参数无效"，因为用户此刻要看的是"它在准备调什么"。
 */
function toolCallsHtml(message) {
  const calls = Array.isArray(message.toolCalls) ? message.toolCalls : [];
  if (!calls.length) return '';
  const settled = !message.streaming;
  const items = calls
    .map((call) => {
      const name = (call && call.function && call.function.name) || t('chat.toolUnknown');
      const rawArgs = (call && call.function && call.function.arguments) || '';
      const parsed = parseToolArguments(rawArgs);
      // 完成后仍解析不了 → 参数确实坏了，原样展示让用户能看出问题；
      // 流式中 → 本来就还没写完，不报错。
      const body = parsed !== null
        ? JSON.stringify(parsed, null, 2)
        : rawArgs || (settled ? t('chat.toolInvalidArgs') : '…');
      return `<div class="chat-tool-call">
        <div class="chat-tool-call-head">
          <span class="chat-tool-call-label">${ctx.escapeHtml(t('chat.toolCall'))}</span>
          <code class="chat-tool-call-name">${ctx.escapeHtml(name)}</code>
        </div>
        <pre class="chat-tool-call-args">${ctx.escapeHtml(body)}</pre>
      </div>`;
    })
    .join('');
  // 批准条只在流式结束后出现：流式期间参数还在增长，此时给出的"允许"可能对应
  // 一份还没写完的 JSON，批准了也执行不了。
  const approval = settled ? toolApprovalHtml(message) : '';
  return `<div class="chat-tool-calls" data-tool-calls="${ctx.escapeHtml(
    String(calls.reduce((sum, call) => sum + ((call?.function?.arguments || '').length), 0))
  )}">${items}${approval}</div>`;
}

/**
 * 逐条工具调用的批准条。
 *
 * 只读工具才给"允许"；其余一律显示"该工具不被允许执行"并**不提供按钮** ——
 * 一个点不出效果的按钮比没有按钮更糟。可执行项也必须先把实际参数摊开给用户
 * 看（AGENTS §2.3：能力不是越权入口），而不是只写"允许执行 get_weather"。
 */
function toolApprovalHtml(message) {
  const calls = pendingToolCalls(message);
  if (!calls.length) {
    const done = message.toolResults && Object.keys(message.toolResults).length;
    return done
      ? `<div class="chat-tool-approved">${ctx.escapeHtml(
          t('chat.toolExecuted', { count: done })
        )}</div>`
      : '';
  }
  const root = toolRoot();
  const rows = calls
    .map((call) => {
      const id = call.id || call.tool_call_id;
      const name = (call?.function?.name || '').trim();
      const rawArgs = call?.function?.arguments || '';
      const preview = describeToolExecution(name, rawArgs, root);
      const label = `<code class="chat-tool-call-name">${ctx.escapeHtml(
        name || t('chat.toolUnknown')
      )}</code>`;
      if (!preview.ok) {
        return `<div class="chat-tool-approval-row is-blocked">
          ${label}
          <span class="chat-tool-approval-reason">${ctx.escapeHtml(
            toolReasonText(preview.reason)
          )}</span>
        </div>`;
      }
      return `<div class="chat-tool-approval-row">
        ${label}
        <code class="chat-tool-approval-target">${ctx.escapeHtml(preview.summary)}</code>
        <button type="button" class="btn btn-primary btn-sm" data-allow-tool="${ctx.escapeHtml(
          String(id)
        )}" data-allow-message="${ctx.escapeHtml(message.key || '')}">${ctx.escapeHtml(
          t('chat.toolAllow')
        )}</button>
        <button type="button" class="btn btn-ghost btn-sm" data-deny-tool="${ctx.escapeHtml(
          String(id)
        )}" data-allow-message="${ctx.escapeHtml(message.key || '')}">${ctx.escapeHtml(
          t('chat.toolDeny')
        )}</button>
      </div>`;
    })
    .join('');
  const note = root
    ? t('chat.toolRootNote', { root })
    : t('chat.toolNoRoot');
  return `<div class="chat-tool-approval" data-tool-approval="${ctx.escapeHtml(
    message.key || ''
  )}"><div class="chat-tool-approval-title">${ctx.escapeHtml(
    t('chat.toolPending', { count: calls.length })
  )}</div>${rows}<div class="chat-tool-approval-note">${ctx.escapeHtml(note)}</div></div>`;
}

/** 批准原因 → 可读文案。未知原因原样给出，便于排查而不是显示空白。 */
function toolReasonText(reason) {
  const key = `chat.toolReason_${reason}`;
  const text = t(key);
  return text === key ? String(reason || '') : text;
}

/**
 * 工具能力的状态灯（**不是**授权按钮）。
 *
 * 为什么这里不再提供"选择工具目录"：llama-server 的 `--tools` 是**服务端自己
 * 执行**的（模型返回 tool_call → 服务端读文件 → 把 `<tool_response>` 拼回上下文
 * 再生成），它完全不经过 OpenAI 兼容 API 的 tool_calls 字段，chat 页既看不到
 * 也无需介入。实测 b11929 确认：带 `--tools read_file,...` 启动时服务端日志打印
 * "server tools (experimental) enabled"，而 `/v1/chat/completions` 里看不到任何
 * 工具往来。
 *
 * 所以自建一套"目录授权 + 逐条批准"是在重复实现 llama-server 已经有的东西，
 * 而且它与用户从 WebUI 看到的行为对不上（WebUI 是服务端自动跑的）。
 * 正确做法是**映射**：语言模型该用工具时由 llama-server 执行，chat 页只显示
 * "这台实例启用了哪些工具"，需要细致控制时引导用户切到 WebUI 模式。
 */
/**
 * 侧栏检查器底部的工具按钮。
 *
 * 两种语义，同一枚按钮：
 *  - 实例带 `--tools` 启动 → 服务端已启用工具，这里只做**状态说明**
 *    （点它给一句"这由 llama-server 自己执行"的提示）。
 *  - 实例没带 → 这里是**入口**：让用户授权一个目录，授权后 chat 页才把只读
 *    工具声明发给模型、走需要人批准的 tool_calls 往返。
 *
 * 旧实现只有前一种：没有服务端工具时把按钮 `hidden = true`。于是后一种路径
 * 的**前提（已授权目录）在界面上永远制造不出来**，加上
 * `activeToolDeclarations()` 的过滤条件写反（用必然为空的 `enabled` 去过滤
 * 工具清单），整条"客户端只读工具"链路是死代码 —— 后端 220 行执行器、
 * 批准条、`/api/chat/tools/execute` 全都不可达。
 */
function updateToolCapabilityBadge(inst) {
  const btn = root.querySelector('#btn-chat-tool-root');
  if (!btn) return;
  if (!inst) {
    btn.hidden = true;
    btn.textContent = '';
    btn.removeAttribute('title');
    return;
  }
  const tools = Array.isArray(inst.declared_tools) ? inst.declared_tools : [];
  btn.hidden = false;
  if (tools.length) {
    btn.textContent = t('chat.toolNativeBadge', { count: tools.length });
    btn.title = t('chat.toolNativeHint', { list: tools.join(', ') });
    btn.classList.add('is-server-tools');
    return;
  }
  // 没有服务端工具：这是"授权目录"入口，必须可见，否则下面那条链路无从开始。
  btn.classList.remove('is-server-tools');
  const authorized = toolRoot();
  btn.textContent = authorized
    ? t('chat.toolRootChosenShort')
    : t('chat.toolRoot');
  btn.title = authorized
    ? t('chat.toolRootChosenTitle', { root: authorized })
    : t('chat.toolRootTitle');
}

/**
 * 本次请求是否附工具声明。规则与实现在 `chat_tools.mjs::selectToolDeclarations`
 * —— 那里是纯函数，可以穷举测试；这里只负责取当前界面状态。
 *
 * 为什么不在本文件里内联判断：旧实现就内联在这里，并且把过滤条件写反了
 * （用必然为空的 `enabled` 去过滤工具清单），于是整条客户端只读工具链路
 * 静默失效，而所有纯函数测试照旧全绿 —— "纯函数对、调用点错"这类缺陷只有
 * 把决策搬到可测的纯函数里才防得住。
 */
function activeToolDeclarations() {
  return selectToolDeclarations(
    currentInstance()?.declared_tools,
    toolRoot()
  );
}

/**
 * 本次请求要不要让模型先思考。
 *
 * @returns {boolean|null} true = 开启思考，false = 关闭，null = 不下发（交给模板）
 *
 * **默认 false**，这与"设置页里 reasoning 的默认值 auto"不冲突：
 * `reasoning` 是**启动参数**（模型进程级），这里是**单次请求**的模板开关。
 * 实测表明不下发这个开关时，Qwen3.5 系模板会默认开启思考，而思考与正文共享
 * `max_tokens` 预算 —— 预算被吃光后正文为空，界面上就是"卡住/变笨"。
 * 所以默认显式关闭；用户在对话设置里选"开启思考"时才发 true。
 *
 * 为什么单独一个函数而不是内联：这条路径要被打包态门禁和单测同时断言，
 * 内联在 1300 行的 generateAssistant 里没法单独验证。
 */
function chatReasoningMode() {
  const value = String(draftParams.thinking ?? '').toLowerCase();
  if (value === 'on') return true;
  if (value === 'off') return false;
  if (value === 'auto') return null;
  // 未设置 = 默认行为：关闭思考，保证正文永远有预算
  return false;
}

/** 当前授权的工具根目录（空串 = 未授权，任何工具都不能执行）。 */
function toolRoot() {
  return String(toolRootDirectory || '').trim();
}

/**
 * 让用户选一个工具可访问的目录（仅在实例**没有**内置工具时才会用到）。
 *
 * 选目录必须走外壳的原生对话框：WebView 侧没有 fs 权限（Tauri capability 只
 * 放行几个自定义命令），而 `<input webkitdirectory>` 只能上传文件副本、拿不到
 * 可读的绝对路径。
 */
async function chooseToolRoot() {
  const enabled = Array.isArray(currentInstance()?.declared_tools)
    ? currentInstance().declared_tools
    : [];
  if (enabled.length) {
    // 服务端已有工具：直接说清楚"这不是我要管的事"，而不是弹一个没用的目录框
    ctx.showToast(
      t('chat.toolNativeActive', { list: enabled.join(', ') }),
      'info',
      5000
    );
    return '';
  }
  if (!window.__TAURI__) {
    ctx.showToast(t('chat.toolRootUnavailable'), 'warning', 3000);
    return '';
  }
  try {
    const chosen = await window.__TAURI__.core.invoke('open_directory_dialog');
    if (chosen) {
      toolRootDirectory = String(chosen);
      // 授权范围落库到**会话**：刷新后不用重选，也不会跨会话继承。
      // 不落库的话，每次重载工具调用都会要求重新选目录 —— 多轮工具链
      // 事实上不可用。
      if (activeConversationId) {
        try {
          const updated = await ctx.Api.updateChat(activeConversationId, {
            tool_root: toolRootDirectory,
          });
          if (activeConversation && activeConversation.id === activeConversationId) {
            activeConversation = { ...activeConversation, ...updated };
          }
        } catch {
          /* 落库失败不阻塞本次授权：本次仍可用，只是刷新后要重选 */
        }
      }
      renderThread({ stick: false });
      ctx.showToast(t('chat.toolRootChosen', { root: toolRootDirectory }), 'success', 2500);
      return toolRootDirectory;
    }
  } catch (error) {
    ctx.showToast(`${t('chat.toolRootChooseFailed')}: ${String(error?.message || error)}`, 'error');
  }
  return '';
}

/**
 * 执行一条**用户已批准**的工具调用，并把结果作为 tool 消息落库。
 *
 * 结果必须落库而不是只留在内存里：否则刷新后历史里那条 assistant 的
 * tool_calls 就永远没有配对的结果，下一轮请求会让模型以为"我调用过但没回音"。
 */
async function runApprovedTool(messageKey, callId) {
  const message = messages.find((item) => item.key === messageKey);
  if (!message) return;
  const call = (message.toolCalls || []).find(
    (item) => String(item.id || item.tool_call_id) === String(callId)
  );
  if (!call) return;
  const root = toolRoot();
  if (!root) {
    ctx.showToast(t('chat.toolNoRoot'), 'warning', 3000);
    return;
  }
  const name = call?.function?.name || '';
  const rawArgs = call?.function?.arguments || '';
  const preview = describeToolExecution(name, rawArgs, root);
  if (!preview.ok) {
    ctx.showToast(toolReasonText(preview.reason), 'error', 4000);
    return;
  }
  const args = parseToolCallArguments(rawArgs) || {};
  try {
    const result = await ctx.Api.executeChatTool({ name, arguments: args, root });
    message.toolResults = { ...(message.toolResults || {}), [String(callId)]: result };
    const toolMessage = buildToolResultMessage(call, {
      ok: true,
      content: renderToolResultText(result),
    });
    if (toolMessage && activeConversationId) {
      // 落库与执行分开 catch：落库失败不能伪装成"执行失败"。旧实现把两者
      // 放同一个 try 里，于是工具明明读到了文件、界面却报"执行失败"
      // （后端曾漏传 tool_call_id，写库必 400）—— 用户会以为工具没跑。
      try {
        const saved = await ctx.Api.appendChatMessage(activeConversationId, {
          role: 'tool',
          content: toolMessage.content,
          tool_call_id: toolMessage.tool_call_id,
        });
        messages.push(normalizeMessage(saved));
      } catch (saveError) {
        ctx.showToast(
          `${t('chat.toolSaveFailed')}: ${localizeBackendError(saveError.message)}`,
          'error',
          4000
        );
      }
    }
    renderThread({ stick: false });
    ctx.showToast(t('chat.toolExecuted', { count: 1 }), 'success', 1800);
    // 批准后自动让模型接着说完。不续跑的话用户得再点一次发送，而多数人
    // 不知道要这么做 —— 看起来就像"点了批准之后什么也没发生"。
    await generateAssistant();
  } catch (error) {
    ctx.showToast(
      `${t('chat.toolExecuteFailed')}: ${localizeBackendError(error.message)}`,
      'error',
      4000
    );
  }
}

/**
 * 用户拒绝一次工具调用。
 *
 * 也是**要落库**的：拒绝之后如果什么都不说，模型下一轮还会再要一次同一个工具；
 * 把"用户拒绝了"作为 tool 结果告诉它，它才会换个方式回答。
 */
async function denyTool(messageKey, callId) {
  const message = messages.find((item) => item.key === messageKey);
  if (!message) return;
  const call = (message.toolCalls || []).find(
    (item) => String(item.id || item.tool_call_id) === String(callId)
  );
  if (!call) return;
  const content = t('chat.toolDeniedResult');
  message.toolResults = { ...(message.toolResults || {}), [String(callId)]: { denied: true } };
  try {
    if (activeConversationId) {
      const saved = await ctx.Api.appendChatMessage(activeConversationId, {
        role: 'tool',
        content,
        tool_call_id: String(callId),
      });
      messages.push(normalizeMessage(saved));
    }
  } catch {
    /* 落库失败不阻塞界面：至少本地状态已经显示"已拒绝" */
  }
  renderThread({ stick: false });
  ctx.showToast(t('chat.toolDenied'), 'info', 1800);
}

/** 把工具结果压成回填给模型的文本。截断要写明，否则模型以为读完了。 */
function renderToolResultText(result) {
  if (!result || typeof result !== 'object') return String(result || '');
  if (result.tool === 'read_file') {
    const note = result.truncated ? `\n${t('chat.toolTruncated')}` : '';
    return `${result.path}\n---\n${result.content}${note}`;
  }
  if (result.tool === 'file_glob_search') {
    return (result.matches || []).join('\n') || t('chat.toolNoMatches');
  }
  if (result.tool === 'grep_search') {
    const lines = (result.matches || []).map(
      (hit) => `${hit.path}:${hit.line}: ${hit.text}`
    );
    return lines.join('\n') || t('chat.toolNoMatches');
  }
  return JSON.stringify(result);
}

function statsLine(stats) {  if (!stats || !Object.keys(stats).length) return '';
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
  syncStreamingReasoning(element, message);
  syncStreamingToolCalls(element, message);
  const content = element.querySelector('[data-msg-content]');
  if (!content) return;
  if (!message.content && !message.reasoning) {
    content.innerHTML = `<span class="chat-typing" aria-label="${ctx.escapeHtml(
      t('chat.generating')
    )}"><span></span><span></span><span></span></span>`;
  } else if (!message.content) {
    // 还在思考阶段：正文区只留光标，思考正文由上面的面板实时展开。
    // 以前这里显示一句固定的"思考过程…"，而面板本身从不更新，于是整个
    // 思考期间界面上只有一个空壳 —— 用户既看不到进展，也以为卡死了。
    content.innerHTML = '<span class="chat-cursor" aria-hidden="true"></span>';
  } else {
    content.innerHTML =
      renderMarkdown(message.content, { copy: t('chat.copyCode') }) +
      '<span class="chat-cursor" aria-hidden="true"></span>';
  }
  if (autoScroll) scrollToBottom();
}

/**
 * 流式期间同步刷新工具调用块。
 *
 * 和思考面板同一个道理：`renderMessageHtml` 只在整条消息插入时跑一次，
 * 之后每一帧只更新正文节点。工具调用如果不在这一帧刷新，用户会看到
 * "模型已经跑完了、工具调用却一直不出现"，直到消息重绘。
 *
 * 用参数总长度做廉价短路 —— 长参数时每帧重排 JSON 很贵。
 */
function syncStreamingToolCalls(element, message) {
  const calls = Array.isArray(message.toolCalls) ? message.toolCalls : [];
  const existing = element.querySelector('[data-tool-calls]');
  if (!calls.length) {
    if (existing) existing.remove();
    return;
  }
  const signature = String(
    calls.reduce((sum, call) => sum + ((call?.function?.arguments || '').length), 0)
  );
  if (existing && existing.dataset.toolCalls === signature) return;
  const html = toolCallsHtml(message);
  if (!html) return;
  if (existing) {
    existing.outerHTML = html;
  } else {
    // 插入位置与 renderMessageHtml 一致：在思考面板之后、正文之前。
    const reasoning = element.querySelector('[data-reasoning-key]');
    if (reasoning) reasoning.insertAdjacentHTML('afterend', html);
    else {
      const head = element.querySelector('.chat-msg-head');
      if (head) head.insertAdjacentHTML('afterend', html);
    }
  }
  const inserted = element.querySelector('[data-tool-calls]');
  if (inserted) inserted.dataset.toolCalls = signature;
}
/**
 * 让思考面板在流式期间实时展开。
 *
 * 三条约束，缺一条都会让这个功能退化回"思考期间一片空白"：
 *
 * 1. **面板节点可能还不存在。** 首帧渲染时 `message.reasoning` 还是空串，
 *    `renderMessageHtml` 就不会输出 `<details>`。思考正文是随后才到齐的，
 *    所以这里要能补建节点。
 * 2. **不碰 `.open`。** 给 `details.open` 赋值会**异步触发 `toggle` 事件**，
 *    而上面的委托监听器会把任何 toggle 当成"用户手动操作过"，于是把它记成
 *    `reasoningTouched = true` 并冻结当时的开合状态 —— 之后"思考时自动展开、
 *    结束后自动收起"就永久失效了。建节点时用 `open` 属性（不触发事件），
 *    已存在时只改正文。
 * 3. **正文没变就不重绘。** 这个函数挂在 requestAnimationFrame 上，每帧都对
 *    整段思考跑一次 markdown 渲染没有必要（长思考时很贵）。用 dataset 记长度
 *    做廉价短路。
 */
function syncStreamingReasoning(element, message) {  const text = message.reasoning || '';
  const existing = element.querySelector('[data-reasoning-key]');
  const plan = planReasoningUpdate({
    hasPanel: Boolean(existing),
    shouldOpen: reasoningPanelOpen({
      mode: reasoningMode,
      streaming: Boolean(message.streaming),
      touched: Boolean(message.reasoningTouched),
      chosen: Boolean(message.reasoningChosen),
      hasReasoning: Boolean(text),
    }),
    textLength: text.length,
    renderedLength: existing ? existing.dataset.reasoningLen : null,
  });
  if (plan.action === 'none') {
    if (!text && existing) existing.remove();
    return;
  }
  let panel = existing;
  if (plan.action === 'create') {
    // 插入位置必须与 renderMessageHtml 一致：思考面板在正文之前。
    const anchor = element.querySelector('.chat-msg-head');
    if (!anchor) return;
    panel = document.createElement('details');
    panel.className = 'chat-reasoning';
    panel.dataset.reasoningKey = message.key;
    // 用属性而不是 .open，避免触发 toggle 被误记成用户操作。
    if (plan.open) panel.setAttribute('open', '');
    panel.innerHTML = `<summary>${ctx.escapeHtml(t('chat.thinking'))}</summary>
      <div class="chat-reasoning-body"></div>`;
    anchor.insertAdjacentElement('afterend', panel);
  }
  const body = panel.querySelector('.chat-reasoning-body');
  if (!body) return;
  panel.dataset.reasoningLen = String(text.length);
  // 思考进行中就贴底显示最新进展；用户往上翻时不打扰。
  const stick =
    !panel.scrollHeight || panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 24;
  body.innerHTML = renderMarkdown(text, { copy: t('chat.copyCode') });
  if (stick) panel.scrollTop = panel.scrollHeight;
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
  // 图片准入取决于实例能力，不取决于文件本身。没有视觉编码器的实例
  // 收到图片只会 400 或让模型胡说 —— 在入口就拦住并说明原因。
  const vision = Boolean(currentInstance()?.supports_vision);
  let totalChars = pendingAttachments.reduce(
    (sum, attachment) => sum + (attachment.content || '').length,
    0
  );
  for (const file of files) {
    if (pendingAttachments.length >= MAX_ATTACHMENTS) {
      ctx.showToast(t('chat.attachTooMany', { count: MAX_ATTACHMENTS }), 'warning');
      break;
    }
    const name = attachmentBaseName(file.name);
    const verdict = classifyAttachmentFile({
      name,
      size: file.size,
      supportsVision: vision,
    });
    if (!verdict.ok) {
      if (verdict.reason === 'no-vision') {
        ctx.showToast(t('chat.attachNoVision', { name }), 'warning', 6000);
      } else if (verdict.reason === 'too-large') {
        ctx.showToast(t('chat.attachImageTooLarge', { name }), 'warning');
      } else {
        ctx.showToast(t('chat.attachUnsupported', { name }), 'warning');
      }
      continue;
    }
    if (verdict.kind === 'image') {
      let dataUrl = '';
      try {
        dataUrl = await fileToImageDataUrl(file);
      } catch {
        ctx.showToast(t('chat.attachReadFailed', { name }), 'error');
        continue;
      }
      pendingAttachments.push({
        name,
        // 图片走 data URL：既能让后端按"文本"存进同一条 column，
        // 又能在消息里重建出来（见 buildMessagePayloadContent 的 image 分支）。
        content: dataUrl,
        kind: 'image',
        size: file.size || 0,
      });
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
      kind: 'text',
      size: file.size || new Blob([content]).size,
    });
  }
  renderPendingAttachments();
  updateComposerState();
  root.querySelector('#chat-input')?.focus();
}

/**
 * 读图片文件并转成 data URL（原样，不缩放 —— 图片要看清内容）。
 *
 * 与头像的区别：头像缩到 128×128 是为了省配置空间，图片附件缩了就看不清了。
 * 上限由 classifyAttachmentFile 的 4 MiB 兜住。
 */
function fileToImageDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('read failed'));
    reader.readAsDataURL(file);
  });
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
    // 绑定实例消失了、且接手的不是同一个模型时必须先确认。放在落库用户消息
    // **之前**：拒绝时这条消息一个字都不该写进会话（否则用户会得到一条
    // "发出去但没有回复、也没报错"的记录）。
    if (!(await confirmModelHandoff())) {
      input.value = content;
      autoResize();
      updateComposerState();
      return;
    }
    if (
      activeConversation.instance_id !== targetInstanceId ||
      activeConversation.model !== (inst.model_path || '') ||
      activeConversation.model_key !== (inst.model_key || '')
    ) {
      // 绑定改写是**元数据维护**，不是发送的前置条件。
      //
      // 2026-11-03 实测缺陷：这里原本是裸 `await`，PATCH 一旦失败（当时是
      // 数据目录只读 → 500）整条发送就中止，`/chat/completions` 一次都没发
      // 出去。日志实证：PATCH 500 连续 14 次，而 completions 与 messages
      // 的请求数都是 0。用户看到"发送失败：网络请求失败"。
      //
      // 会话绑定只影响"下次打开时记不记得用的是哪台实例"；这一轮该发给谁，
      // 已经由 `targetInstanceId` 决定了。所以：改写失败只记一条警告，
      // **继续发送**。真正该拦的跨模型交接已经在上面的
      // `confirmModelHandoff()` 里拦过了，不该靠 PATCH 抛错来兜底。
      try {
        const updated = await ctx.Api.updateChat(conversationId, {
          instance_id: targetInstanceId,
          model: inst.model_path || '',
          model_key: inst.model_key || '',
        });
        if (activeConversationId !== conversationId) return;
        activeConversation = updated;
      } catch (bindError) {
        console.warn(
          'conversation binding update failed; sending anyway',
          bindError && bindError.message ? bindError.message : bindError,
        );
        // 内存里先跟上，避免下一次发送又重复三次失败请求。
        activeConversation = {
          ...activeConversation,
          instance_id: targetInstanceId,
          model: inst.model_path || '',
          model_key: inst.model_key || '',
        };
      }
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
        // tool 消息必须一起进上下文：它是模型那次调用的结果。丢掉它，模型会
        // 看到"我调用过但没有回音"，于是下一轮重复发起同一个调用 —— 死循环。
        (message.role === 'user' ||
          message.role === 'assistant' ||
          message.role === 'tool')
    )
    .map((message) => {
      if (message.role === 'tool') {
        // tool 消息不需要组装附件/多模态：它就是一段纯文本结果 + 对应 id。
        return {
          role: 'tool',
          content: String(message.content || ''),
          tool_call_id: String(message.tool_call_id || message.id || ''),
        };
      }
      const entry = {
        role: message.role,
        // 含图片时 content 是 OpenAI 多模态数组（`[{type:'text'},…,
        // {type:'image_url'}]`），纯文本时仍是字符串。两条路径都必须保留 ——
        // 把数组当字符串 trim 会直接抛异常。
        content: buildMessagePayloadContent(message),
      };
      // assistant 的 tool_calls 必须回传：模型下一轮靠它认出"我发起过哪个
      // 调用"，随后的 tool 消息靠 id 与它配对。不带这一段，工具结果就是一条
      // 没人认领的消息，模型会重复发起同一个调用。
      if (message.role === 'assistant' && Array.isArray(message.toolCalls) && message.toolCalls.length) {
        entry.tool_calls = message.toolCalls;
      }
      return entry;
    })
    .filter((message) => {
      if (typeof message.content === 'string') return message.content.trim() !== '';
      return Array.isArray(message.content) && message.content.length > 0;
    });
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
  // 服务端给出的结束原因（stop / length / tool_calls…）。要单独留着：诊断
  // "思考吃光预算导致正文为空"必须区分 length 与 stop，只记一个布尔做不到。
  let finishReason = '';
  let failure = null;
  let knowledgeSources = [];

  try {
    const body = { messages: payloadMessages, stream: true };
    const tools = activeToolDeclarations();
    if (tools.length) body.tools = tools;
    // 参数取"会话 + 草稿"的合并视图：面板里改的值要么已落库
    // （activeConversation.params），要么还在草稿里（无会话时）。
    const effective = currentParams();
    if (Number.isFinite(effective.temperature)) body.temperature = effective.temperature;
    if (Number.isFinite(effective.max_tokens) && effective.max_tokens >= 0) {
      body.max_tokens = effective.max_tokens;
    }
    if (Number.isFinite(effective.top_p)) body.top_p = effective.top_p;
    // 这三个后端一直能存也能透传，但此前前端从未发送（"后端已备、前端空置"）。
    if (Number.isFinite(effective.top_k) && effective.top_k > 0) body.top_k = effective.top_k;
    if (Number.isFinite(effective.repeat_penalty) && effective.repeat_penalty > 0) {
      body.repeat_penalty = effective.repeat_penalty;
    }
    if (Number.isFinite(effective.seed)) body.seed = effective.seed;
    // 思考控制必须显式下发。不下发时 Qwen3.5 这类模板会把"未定义 enable_thinking"
    // 渲染成"思考段已开启"，模型于是先写几千 token 的思考；而思考与正文共享
    // `max_tokens` 预算，预算被思考吃光后 `finish_reason=length` 且 **content 为空**
    // —— 界面上看起来就是"卡住 / 死循环 / 变笨"。
    //
    // 实测（Qwen3.5-4B，同一问题"9.11 和 9.9 哪个大"）：
    //   不下发            → 1630 token 思考 + 0 字正文（finish=length）
    //   enable_thinking=false → 0 token 思考 + 164 字正常答案（finish=stop）
    const thinking = chatReasoningMode();
    if (thinking !== null) {
      body.chat_template_kwargs = { enable_thinking: thinking };
    }
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
        // 工具调用是增量的：同一 index 的多段 arguments 要累加，
        // 不能覆盖（覆盖会让参数只剩最后一段）。
        if (chunk.toolCalls && chunk.toolCalls.length) {
          assistant.toolCalls = accumulateToolCalls(assistant.toolCalls, chunk.toolCalls);
          scheduleStreamRender();
        }
        if (chunk.usage) usage = chunk.usage;
        if (chunk.timings) timings = chunk.timings;
        if (chunk.finishReason) {
          finished = true;
          finishReason = chunk.finishReason;
        }
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
  // 思考吃光预算的诊断：有思考、无正文、且生成是被长度截断的。
  //
  // 这是用户实际看到"卡住/死循环"的形态 —— 模型把预算全花在思考上，
  // 正文一个字都没写。界面上如果只显示一段思考过程，用户会以为模型坏了。
  // 必须显式说出来，并指向可操作的修法（关思考 / 调大 max_tokens）。
  const exhaustedBudget =
    !assistant.content.trim() &&
    assistant.reasoning.trim().length > 0 &&
    String(finishReason || '') === 'length';
  if (exhaustedBudget) {
    finalStats.thinkingExhausted = true;
  }
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
      // 工具调用必须落库：它只活在内存里的话，刷新后那条 assistant 就变成
      // 一条空回复，而模型"请求过什么"整段消失 —— 用户既回看不到、也没法再
      // 批准。存进 stats（已有 16 KiB 预算），与 stats.error 同一个通道。
      const stats = { ...assistant.stats };
      if (Array.isArray(assistant.toolCalls) && assistant.toolCalls.length) {
        stats.tool_calls = assistant.toolCalls;
      }
      const saved = await ctx.Api.appendChatMessage(conversationId, {
        role: 'assistant',
        content: assistant.content,
        // 完整思考过程走自己的一等列。以前它只存在于 stats 里、写库前被截到
        // 4000 字，所以"流式时看得见、重载就少一截"。
        reasoning: assistant.reasoning,
        stats,
      });
      assistant.id = saved.id;
      assistant.key = String(saved.id);
      assistant.created_at = saved.created_at;
      assistant.stats = stats;
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

/**
 * 删除单条消息。
 *
 * 与"编辑"不同，删除**不**截断后续消息：编辑一条用户消息会让后面的回答自相
 * 矛盾，而删掉一条独立的消息（比如一段失败的回复、一句贴错的附件）不应该
 * 顺手带走它后面的正常对话。要让后续失效请用编辑。
 */
async function deleteMessageFromConversation(key) {
  if (streaming || sending) {
    ctx.showToast(t(streaming ? 'chat.stopping' : 'chat.sending'), 'info');
    return;
  }
  const message = findMessageByKey(key);
  if (!message || !message.id || !activeConversationId) return;
  const confirmed = await ctx.showConfirm(
    t('chat.deleteMessageConfirm'),
    t('chat.deleteMessage')
  );
  if (!confirmed) return;
  const index = messages.indexOf(message);
  try {
    await ctx.Api.deleteChatMessage(activeConversationId, message.id);
    messages.splice(index, 1);
    // 顺带把列表里的消息数改掉：等下一次轮询才变会让人觉得"删了但没删"。
    const listed = conversations.find((item) => item.id === activeConversationId);
    if (listed) {
      listed.message_count = Math.max(0, (Number(listed.message_count) || 1) - 1);
    }
    if (activeConversation) {
      activeConversation.message_count = listed
        ? listed.message_count
        : activeConversation.message_count;
    }
    ctx.showToast(t('chat.deletedMessage'), 'success', 1500);
    renderThread({ stick: false });
    updateComposerState();
    renderConversationList();
  } catch (error) {
    ctx.showToast(
      `${t('chat.deleteMessageFailed')}: ${localizeBackendError(error.message)}`,
      'error'
    );
  }
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
    messages = hydrateToolResults((Array.isArray(detail.messages) ? detail.messages : []).map(normalizeMessage));
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

// ---------- 对话参数（常驻右侧面板） ----------
//
// 旧实现是"对话设置"模态框：改一次温度要点两次、弹窗还会遮住正文，而且它的
// 字段（系统提示/温度/最大生成/思考）与检查器里的字段（实例/知识库/模式）
// 同属"这条对话的属性"，却被拆成两个地方 —— 这就是"每个功能都拆得到处都是"。
//
// 现在合成一块常驻面板，结构与后端 chat_store.PARAM_KEYS 对齐：
//   system / temperature / max_tokens / top_p / top_k / repeat_penalty / seed / thinking
// 其中 top_k、repeat_penalty、seed 此前后端能存也能透传，但前端从未发送 ——
// 属于"后端已备、前端空置"，这里补齐。

/** 面板字段 → PARAM_KEYS 的映射（也是"哪些字段会被保存"的唯一清单）。 */
const INSPECTOR_PARAM_FIELDS = [
  ['chat-param-system', 'system', 'text'],
  ['chat-param-temperature', 'temperature', 'number'],
  ['chat-param-max', 'max_tokens', 'number'],
  ['chat-param-top-p', 'top_p', 'number'],
  ['chat-param-top-k', 'top_k', 'number'],
  ['chat-param-repeat', 'repeat_penalty', 'number'],
  ['chat-param-seed', 'seed', 'number'],
  ['chat-param-thinking', 'thinking', 'text'],
];

/** 当前生效参数：会话自己的 params 优先，其次是未落库的草稿。 */
function currentParams() {
  return { ...DEFAULT_PARAMS, ...(activeConversation?.params || draftParams) };
}

/**
 * 把当前参数写进面板控件。
 *
 * 只在"当前会话变了"或"用户从外部改了参数"时调用；用户正在输入时不要调，
 * 否则光标会被重置。
 */
function renderInspectorParams() {
  const params = currentParams();
  for (const [id, key, kind] of INSPECTOR_PARAM_FIELDS) {
    const el = root.querySelector(`#${id}`);
    if (!el) continue;
    const value = params[key];
    if (kind === 'number') {
      // 空值留空而不是填 0：0 是合法温度，用户会分不清"我设了 0"与"没设"。
      el.value = value === undefined || value === null || value === '' ? '' : String(value);
      continue;
    }
    const text = value === undefined || value === null ? '' : String(value);
    // <select> 的值必须命中某个 option，否则显示为空白（看起来像坏了）。
    // thinking 的合法值只有三档，空串回落到默认项。
    if (el.tagName === 'SELECT') {
      const hasMatch = Array.from(el.options || []).some((opt) => opt.value === text);
      el.value = hasMatch ? text : el.options?.[0]?.value || '';
    } else {
      el.value = text;
    }
  }
}

/**
 * 从面板读回参数并落库。
 *
 * 防抖保存：这些都是滑块/数字框，每敲一个字符就发一次 PATCH 既浪费又会让
 * 服务端写入互相覆盖。600ms 足够，且用户切走会话前会 flush 一次。
 */
let paramsSaveTimer = null;

function scheduleInspectorSave() {
  if (paramsSaveTimer) clearTimeout(paramsSaveTimer);
  paramsSaveTimer = setTimeout(() => {
    paramsSaveTimer = null;
    void saveInspectorParams();
  }, 600);
}

async function saveInspectorParams() {
  const next = { ...currentParams() };
  for (const [id, key, kind] of INSPECTOR_PARAM_FIELDS) {
    const el = root.querySelector(`#${id}`);
    if (!el) continue;
    if (kind === 'number') {
      const raw = String(el.value ?? '').trim();
      // 留空 = 不设置（而不是 0）。后端 _normalize_params 会丢弃未知/空值。
      if (raw === '') {
        delete next[key];
        continue;
      }
      const num = Number(raw);
      if (!Number.isFinite(num)) {
        ctx.showToast(t('chat.settingsFailed'), 'warning');
        renderInspectorParams();
        return;
      }
      next[key] = num;
    } else {
      next[key] = String(el.value ?? '');
    }
  }
  if (next.temperature !== undefined && (next.temperature < 0 || next.temperature > 2)) {
    ctx.showToast(t('chat.settingsFailed'), 'warning');
    renderInspectorParams();
    return;
  }

  if (!activeConversation) {
    // 还没有会话：只更新草稿，新建会话时会带上。
    draftParams = next;
    return;
  }
  const conversationId = activeConversation.id;
  try {
    const updated = await ctx.Api.updateChat(conversationId, { params: next });
    if (activeConversationId !== conversationId) return;
    // 服务端接受后才提交本地状态：保存失败不能让界面看起来成功了。
    activeConversation = { ...updated };
    draftParams = next;
  } catch (error) {
    ctx.showToast(`${t('chat.settingsFailed')}: ${localizeBackendError(error.message)}`, 'error');
    renderInspectorParams();
  }
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
  // 右侧检查器开合。它替代了旧版顶部那一排常驻控件 —— 设置项收进检查器，
  // 主区只留对话。
  root.querySelector('#btn-chat-inspector')?.addEventListener('click', () => {
    setInspectorOpen(!inspectorOpen);
  });
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
  const toolRootBtn = root.querySelector('#btn-chat-tool-root');
  if (toolRootBtn) {
    toolRootBtn.addEventListener('click', () => chooseToolRoot());
  }
  // 参数面板：所有可编辑字段共用一个防抖保存（600ms）。用 input 而不是 change，
  // 因为数字框的 change 要等失焦才触发，用户会以为"改了没生效"。
  for (const [id] of INSPECTOR_PARAM_FIELDS) {
    const el = root.querySelector(`#${id}`);
    if (!el) continue;
    el.addEventListener('input', scheduleInspectorSave);
    el.addEventListener('change', scheduleInspectorSave);
  }
  root.querySelector('#btn-chat-export').addEventListener('click', exportConversation);
  root.querySelector('#btn-chat-stop').addEventListener('click', stopStreaming);
  root.querySelector('#btn-chat-send').addEventListener('click', sendCurrent);

  // 右栏双视图切换：Configuration（生成参数）↔ Integrations（模型能用什么）。
  root.querySelectorAll('[data-inspector-view]').forEach((btn) => {
    btn.addEventListener('click', () => setInspectorView(btn.dataset.inspectorView));
  });

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
    const deleteMessage = event.target.closest('[data-delete-message]');
    if (deleteMessage) {
      deleteMessageFromConversation(deleteMessage.dataset.deleteMessage);
      return;
    }
    const allowTool = event.target.closest('[data-allow-tool]');
    if (allowTool) {
      runApprovedTool(allowTool.dataset.allowMessage, allowTool.dataset.allowTool);
      return;
    }
    const denyToolBtn = event.target.closest('[data-deny-tool]');
    if (denyToolBtn) {
      denyTool(denyToolBtn.dataset.allowMessage, denyToolBtn.dataset.denyTool);
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

  const dropHint = root.querySelector('#chat-drop-hint');
  const hasFiles = (event) =>
    Array.from(event.dataTransfer?.types || []).includes('Files');
  const canDrop = () => mode === 'native' && !streaming && !!currentInstance();

  // 投放到整个 Chat 页而不是只有 #chat-native：用户拖文件进来时手可能停在
  // 会话列表或工具栏上，只有正文区响应会让"拖进去没反应"。不可投放时也必须
  // 有反馈 —— 否则同样读作"坏了"。
  const dropTarget = root;
  const showHint = (ok) => {
    if (!dropHint) return;
    dropHint.hidden = false;
    dropHint.textContent = ok ? t('chat.attachDrop') : t('chat.attachDropBlocked');
    dropHint.classList.toggle('is-blocked', !ok);
  };
  dropTarget.addEventListener('dragenter', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    showHint(canDrop());
  });
  dropTarget.addEventListener('dragover', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = canDrop() ? 'copy' : 'none';
    }
  });
  dropTarget.addEventListener('dragleave', (event) => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0 && dropHint) dropHint.hidden = true;
  });
  dropTarget.addEventListener('drop', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    if (dropHint) dropHint.hidden = true;
    if (!canDrop()) {
      ctx.showToast(t('chat.attachDropBlocked'), 'warning', 3000);
      return;
    }
    addAttachmentFiles(event.dataTransfer.files);
  });

  // 粘贴：截图后直接 Ctrl+V 是最常见的"把图给模型看"的动作。
  // 只处理含文件的粘贴，纯文本粘贴仍走浏览器默认行为（不能抢走）。
  root.addEventListener('paste', (event) => {
    const items = Array.from(event.clipboardData?.items || []);
    const files = items
      .filter((item) => item.kind === 'file')
      .map((item) => item.getAsFile())
      .filter(Boolean);
    if (!files.length) return;
    if (!canDrop()) {
      ctx.showToast(t('chat.attachDropBlocked'), 'warning', 3000);
      return;
    }
    event.preventDefault();
    addAttachmentFiles(files);
  });
}

/** 触发一次本地下载。WebView2 下 revoke 必须延迟（见 runtime.mjs）。 */
function downloadTextFile(text, filename, mime) {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  scheduleObjectUrlRevoke(url, (value) => URL.revokeObjectURL(value), 1000);
}

function safeFileStem(title) {
  return String(title || '').replace(/[\\/:*?"<>|]/g, '_').slice(0, 60) || 'chat';
}

function exportConversationMarkdown() {
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
          speaker: speakerLabel(message.role),
          reasoningHeading: t('chat.thinking'),
          attachmentHeading: t('chat.attachment'),
          sourcesHeading: t('chat.sourcesHeading'),
          partialMarker: t('chat.partial'),
          errorHeading: t('chat.streamFailed'),
        })
      ),
  ].filter((line) => line !== undefined);
  const markdown = lines.join('\n').trim() + '\n';
  downloadTextFile(markdown, `${safeFileStem(title)}.md`, 'text/markdown;charset=utf-8');
  ctx.showToast(t('chat.exported'), 'success', 1500);
}

/**
 * JSON 导出：可被"导入对话"再读回来。
 *
 * 与 Markdown 并列而不是替代：Markdown 是给人看的存档，丢掉了 createdAt 与
 * 部分结构；JSON 是给本程序的备份，为了能完整往返，它带上思考过程、引用与
 * 附件内容。两者都由同一份 messages 生成，避免"导出两种格式内容不一致"。
 */
async function exportConversationJson() {
  if (!activeConversation || !messages.length) {
    ctx.showToast(t('chat.exportEmpty'), 'warning');
    return;
  }
  const title = activeConversation.title || t('chat.untitled');
  let payload;
  try {
    // 以服务端导出为准：它在同一个事务里读会话+消息+附件，前端手里的
    // messages 可能因为流式写入而与其不一致。
    payload = await ctx.Api.exportChat(activeConversationId);
  } catch (error) {
    // 后端不可用时退回本地组装：导出不该因为一次网络抖动而完全失败。
    payload = buildConversationExport(activeConversation, messages, {
      exportedAt: Date.now() / 1000,
    });
    console.warn('server-side export failed, using local snapshot:', error);
  }
  downloadTextFile(
    JSON.stringify(payload, null, 2),
    `${safeFileStem(title)}.json`,
    'application/json;charset=utf-8'
  );
  ctx.showToast(t('chat.exported'), 'success', 1500);
}

/** 导出入口：让用户选格式。默认按钮直接给 Markdown 会藏掉 JSON。 */
function exportConversation() {
  if (!activeConversation || !messages.length) {
    ctx.showToast(t('chat.exportEmpty'), 'warning');
    return;
  }
  const body = document.createElement('div');
  body.innerHTML = `<div class="diag-intro">${ctx.escapeHtml(t('chat.exportChoose'))}</div>`;
  ctx.showModal(
    t('chat.export'),
    body,
    [
      {
        label: t('chat.exportMarkdown'),
        class: 'btn-secondary',
        onClick: () => {
          exportConversationMarkdown();
          return true;
        },
      },
      {
        label: t('chat.exportJson'),
        class: 'btn-primary',
        onClick: async () => {
          await exportConversationJson();
          return true;
        },
      },
    ],
    { onClose: () => {} }
  );
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
