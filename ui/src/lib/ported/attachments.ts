/**
 * Pure helpers for local attachments in the native chat.
 *
 * Ported from the 1.0 module `frontend/js/chat_attachments.mjs`. Attachments are
 * read in the webview (File API) and never uploaded. The allow-list and bounds
 * mirror `backend/core/chat_store.py` so a file accepted by the composer cannot
 * be rejected after sending.
 *
 * **One 1.0 defect is fixed here, not carried over.** The old module decided
 * "is this an image?" from a client-supplied `kind` field, which the backend does
 * not persist (it classifies by content shape: a `data:image/...` prefix). After
 * a reload the `kind` was gone, so a reloaded image was re-sent as a *text*
 * attachment containing its own base64. `classifyAttachmentContent()` derives the
 * kind from the content itself, so the classification survives a reload.
 */

export const ATTACHMENT_EXTENSIONS = [
  'txt', 'md', 'markdown', 'json', 'jsonl', 'yaml', 'yml', 'toml',
  'ini', 'cfg', 'conf', 'csv', 'tsv', 'log',
  'py', 'js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx', 'css', 'html', 'htm',
  'xml', 'svg', 'sql', 'ps1', 'sh', 'bat', 'cmd',
  'c', 'h', 'cc', 'cpp', 'hpp', 'cs', 'rs', 'go', 'java', 'kt', 'swift',
] as const;

export const ATTACHMENT_ACCEPT = ATTACHMENT_EXTENSIONS.map((ext) => `.${ext}`).join(',');

/** Image extensions, kept separate because their admission rule differs. */
export const IMAGE_EXTENSIONS = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'] as const;

export const MAX_ATTACHMENTS = 5;
export const MAX_ATTACHMENT_CHARS = 200_000;
export const MAX_MESSAGE_ATTACHMENT_CHARS = 500_000;
/** Encoded size cap per image; looser than avatars because content must be legible. */
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;

const EXTENSION_SET = new Set<string>(ATTACHMENT_EXTENSIONS);
const IMAGE_SET = new Set<string>(IMAGE_EXTENSIONS);

/** Matches the backend's `_IMAGE_DATA_URL_RE` intent: a supported image data URL. */
const IMAGE_DATA_URL = /^data:image\/(png|jpeg|jpg|webp|gif|bmp);base64,/i;

export type AttachmentKind = 'text' | 'image';

export function attachmentBaseName(name: string | null | undefined): string {
  const value = String(name || '').replace(/\\/g, '/');
  return (value.split('/').pop() ?? '').trim().slice(0, 200);
}

export function attachmentExtension(name: string | null | undefined): string {
  const base = attachmentBaseName(name).toLowerCase();
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(dot + 1) : '';
}

export function isSupportedAttachmentName(name: string | null | undefined): boolean {
  return EXTENSION_SET.has(attachmentExtension(name));
}

export function isImageAttachmentName(name: string | null | undefined): boolean {
  return IMAGE_SET.has(attachmentExtension(name));
}

export interface ClassifyFileResult {
  ok: boolean;
  kind: AttachmentKind;
  reason?: 'no-vision' | 'too-large' | 'unsupported';
}

/**
 * Whether a file may be attached, and why not when it may not.
 *
 * Extracted so the same verdict serves drag-drop, paste and the file picker —
 * three hand-written copies inevitably drift, and the classic symptom is "drag
 * works, the picker does not". Image admission also depends on the *instance's*
 * capability (does it have an mmproj), and that prerequisite is an explicit
 * argument rather than something the caller must remember to check.
 */
export function classifyAttachmentFile({
  name,
  size,
  supportsVision,
}: {
  name?: string | null;
  size?: number | null;
  supportsVision?: boolean;
} = {}): ClassifyFileResult {
  const base = attachmentBaseName(name);
  if (isImageAttachmentName(base)) {
    if (!supportsVision) return { ok: false, kind: 'image', reason: 'no-vision' };
    if (Number(size) > MAX_IMAGE_BYTES) return { ok: false, kind: 'image', reason: 'too-large' };
    return { ok: true, kind: 'image' };
  }
  if (isSupportedAttachmentName(base)) return { ok: true, kind: 'text' };
  return { ok: false, kind: 'text', reason: 'unsupported' };
}

/**
 * Derive an attachment's kind from its stored content.
 *
 * The backend stores no `kind` column — it classifies by content shape, because a
 * client-declared kind can lie. The UI must do the same on load, otherwise a
 * reloaded image is treated as text and its base64 reaches the model as prose.
 */
export function classifyAttachmentContent(content: string | null | undefined): AttachmentKind {
  return IMAGE_DATA_URL.test(String(content ?? '')) ? 'image' : 'text';
}

/** Text containing NUL bytes is binary; keep it out of the prompt and the database. */
export function looksBinaryText(text: string | null | undefined): boolean {
  return String(text || '').includes('\u0000');
}

export function formatAttachmentSize(bytes: number | null | undefined): string {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return '0 B';
  if (value < 1024) return `${Math.round(value)} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export interface PayloadAttachment {
  name: string;
  content: string;
  /** Derived from content when absent — see `classifyAttachmentContent`. */
  kind?: AttachmentKind;
}

export type MultimodalPart =
  | { type: 'text'; text: string }
  | { type: 'image_url'; image_url: { url: string } };

export interface PayloadMessage {
  content?: string | null;
  attachments?: PayloadAttachment[] | null;
}

function attachmentKind(attachment: PayloadAttachment): AttachmentKind {
  return attachment.kind ?? classifyAttachmentContent(attachment.content);
}

/**
 * Build the message content sent to llama-server for one conversation turn.
 *
 * Text attachments are appended inside explicit delimiters so the model can tell
 * "file content" from "the user's question".
 *
 * Images **cannot** be concatenated that way: multimodal input goes through the
 * OpenAI `content: [{type:'image_url'}]` shape, and stuffing base64 into plain
 * text only gives the model a wall of noise. So this returns either a string
 * (text-only, no call-site change needed) or an array of content parts.
 */
export function buildMessagePayloadContent(
  message: PayloadMessage | null | undefined,
): string | MultimodalPart[] {
  const content = String(message?.content || '');
  const attachments = Array.isArray(message?.attachments) ? message.attachments : [];
  if (!attachments.length) return content;

  const images = attachments.filter((a) => attachmentKind(a) === 'image' && a.content);
  const texts = attachments.filter((a) => attachmentKind(a) !== 'image' && a.content);
  const textBlocks = texts.map(
    (attachment) =>
      `--- Attached file: ${attachment.name} ---\n${attachment.content}\n--- End of ${attachment.name} ---`,
  );
  const textPart = [content, ...textBlocks]
    .filter((part) => String(part || '').trim())
    .join('\n\n');

  if (!images.length) return textPart;

  // Multimodal: one text part then one image_url per image — "read the question,
  // then look at the pictures", matching natural reading order.
  const parts: MultimodalPart[] = [];
  if (textPart.trim()) parts.push({ type: 'text', text: textPart });
  for (const image of images) {
    parts.push({ type: 'image_url', image_url: { url: image.content } });
  }
  return parts;
}

/** Whether a message carries images (decides whether the request body is multimodal). */
export function messageHasImages(message: PayloadMessage | null | undefined): boolean {
  const attachments = Array.isArray(message?.attachments) ? message.attachments : [];
  return attachments.some((a) => attachmentKind(a) === 'image' && a.content);
}
