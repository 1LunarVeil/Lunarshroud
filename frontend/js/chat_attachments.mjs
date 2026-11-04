// Pure helpers for local text attachments in the native chat.
//
// Attachments are read in the webview (File API) and never uploaded anywhere.
// The allow-list and bounds mirror backend/core/chat_store.py so a valid file
// in the composer cannot be rejected after sending.

export const ATTACHMENT_EXTENSIONS = [
  'txt', 'md', 'markdown', 'json', 'jsonl', 'yaml', 'yml', 'toml',
  'ini', 'cfg', 'conf', 'csv', 'tsv', 'log',
  'py', 'js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx', 'css', 'html', 'htm',
  'xml', 'svg', 'sql', 'ps1', 'sh', 'bat', 'cmd',
  'c', 'h', 'cc', 'cpp', 'hpp', 'cs', 'rs', 'go', 'java', 'kt', 'swift',
];

export const ATTACHMENT_ACCEPT = ATTACHMENT_EXTENSIONS.map((ext) => `.${ext}`).join(',');

/** 图片附件的扩展名。与文本分开，因为它们的准入条件不同（需要视觉能力）。 */
export const IMAGE_EXTENSIONS = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'];

export const MAX_ATTACHMENTS = 5;
export const MAX_ATTACHMENT_CHARS = 200_000;
export const MAX_MESSAGE_ATTACHMENT_CHARS = 500_000;
/** 单张图片编码后的字节上限。比头像宽松（要看清内容），但仍要防爆库。 */
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;

const EXTENSION_SET = new Set(ATTACHMENT_EXTENSIONS);
const IMAGE_SET = new Set(IMAGE_EXTENSIONS);

export function attachmentBaseName(name) {
  const value = String(name || '').replace(/\\/g, '/');
  return value.split('/').pop().trim().slice(0, 200);
}

export function attachmentExtension(name) {
  const base = attachmentBaseName(name).toLowerCase();
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(dot + 1) : '';
}

export function isSupportedAttachmentName(name) {
  return EXTENSION_SET.has(attachmentExtension(name));
}

export function isImageAttachmentName(name) {
  return IMAGE_SET.has(attachmentExtension(name));
}

/**
 * 一个文件能不能作为附件进来，以及不能时的原因。
 *
 * 为什么把判定抽出来：同一个结论要在三处用（拖拽、粘贴、文件选择），
 * 三处各写一遍必然漂移 —— 典型后果是"拖拽能进、选择器不能进"。
 * 而且**图片的准入依赖实例能力**（有没有 mmproj），这个前提必须显式传入，
 * 不能靠调用方记得检查。
 *
 * @param {object} options
 * @param {string} options.name 文件名
 * @param {number} options.size 字节数
 * @param {boolean} options.supportsVision 当前实例能否接收图片
 * @returns {{ok: boolean, kind: 'text'|'image', reason?: string}}
 */
export function classifyAttachmentFile({ name, size, supportsVision } = {}) {
  const base = attachmentBaseName(name);
  if (isImageAttachmentName(base)) {
    if (!supportsVision) return { ok: false, kind: 'image', reason: 'no-vision' };
    if (Number(size) > MAX_IMAGE_BYTES) return { ok: false, kind: 'image', reason: 'too-large' };
    return { ok: true, kind: 'image' };
  }
  if (isSupportedAttachmentName(base)) {
    return { ok: true, kind: 'text' };
  }
  return { ok: false, kind: 'text', reason: 'unsupported' };
}

/** Text with NUL bytes is binary; keep it out of the prompt and database. */
export function looksBinaryText(text) {
  return String(text || '').includes('\u0000');
}

export function formatAttachmentSize(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return '0 B';
  if (value < 1024) return `${Math.round(value)} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Build the text sent to llama-server for one conversation turn.
 *
 * 文本附件追加在显式分隔符里（模型能分清"文件内容"与"用户的问题"）。
 *
 * 图片**不能**这么拼：多模态要走 OpenAI 的 `content: [{type:'image_url'}]`
 * 结构，把 base64 塞进纯文本只会让模型读到一长串乱码。所以这里返回
 * 一个"内容可能不是字符串"的结果，由调用方决定怎么组装 messages。
 * 只有文本时仍然返回字符串 —— 保持旧调用点无需改动。
 */
export function buildMessagePayloadContent(message) {
  const content = String(message?.content || '');
  const attachments = Array.isArray(message?.attachments) ? message.attachments : [];
  if (!attachments.length) return content;

  const images = attachments.filter((a) => a.kind === 'image' && a.content);
  const texts = attachments.filter((a) => a.kind !== 'image' && a.content);
  const textBlocks = texts.map(
    (attachment) =>
      `--- Attached file: ${attachment.name} ---\n${attachment.content}\n--- End of ${attachment.name} ---`
  );
  const textPart = [content, ...textBlocks].filter((part) => String(part || '').trim()).join('\n\n');

  if (!images.length) return textPart;

  // 多模态：文本块 + 每张图片一个 image_url。顺序上图片在文本之后，
  // 与"先看问题、再看附图"的自然阅读顺序一致。
  const parts = [];
  if (textPart.trim()) parts.push({ type: 'text', text: textPart });
  for (const image of images) {
    parts.push({ type: 'image_url', image_url: { url: image.content } });
  }
  return parts;
}

/** 一条消息是否含图片附件（决定请求体用不用多模态结构）。 */
export function messageHasImages(message) {
  const attachments = Array.isArray(message?.attachments) ? message.attachments : [];
  return attachments.some((a) => a.kind === 'image' && a.content);
}
