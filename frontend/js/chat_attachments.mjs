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

export const MAX_ATTACHMENTS = 5;
export const MAX_ATTACHMENT_CHARS = 200_000;
export const MAX_MESSAGE_ATTACHMENT_CHARS = 500_000;

const EXTENSION_SET = new Set(ATTACHMENT_EXTENSIONS);

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
 * The visible message stays clean; attached text is appended inside explicit
 * file delimiters so the model can distinguish it from the user's question.
 */
export function buildMessagePayloadContent(message) {
  const content = String(message?.content || '');
  const attachments = Array.isArray(message?.attachments) ? message.attachments : [];
  if (!attachments.length) return content;
  const blocks = attachments.map(
    (attachment) =>
      `--- Attached file: ${attachment.name} ---\n${attachment.content}\n--- End of ${attachment.name} ---`
  );
  return [content, ...blocks].filter((part) => String(part || '').trim()).join('\n\n');
}
