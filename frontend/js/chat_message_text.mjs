// Pure text assembly for one chat message: what "copy" puts on the clipboard
// and what the Markdown export writes for it.
//
// These live here rather than inline in chat.js for two reasons: they are the
// two places where the model's reasoning was being **silently dropped**, and a
// silent drop is only caught by a test that asserts on the produced text.
// chat.js is DOM-coupled and cannot be imported by `node --test`.

/**
 * Clipboard text for one message.
 *
 * `includeReasoning: false` keeps the historical behaviour (answer only). The
 * caller must not use it as a silent default though -- when a message has
 * reasoning the UI offers both actions explicitly, so "the copy was missing
 * half the reply" stops being invisible.
 */
export function messageCopyText(message, { includeReasoning = false } = {}) {
  const content = String(message?.content || '');
  const reasoning = String(message?.reasoning || '');
  if (!includeReasoning || !reasoning) return content;
  const label = String(message?.reasoningLabel || 'Reasoning');
  return `${label}\n\n${reasoning}\n\n${content}`;
}

/**
 * Markdown lines for one message, reasoning included.
 *
 * Export is an archive, so dropping the thinking process there is data loss
 * rather than a display choice. The reasoning section is emitted **before** the
 * answer to match the order the model produced them in.
 *
 * @param {object} message
 * @param {object} labels resolved i18n strings from the caller
 * @returns {string[]}
 */
export function messageExportLines(message, labels) {
  const { speaker, reasoningHeading, attachmentHeading, sourcesHeading, partialMarker, errorHeading } = labels;
  const content = String(message.content || '');
  const reasoning = String(message.reasoning || '');
  const lines = [`## ${speaker}`, ''];
  if (reasoning) {
    lines.push(`### ${reasoningHeading}`, '', reasoning, '');
  }
  // An answer can be legitimately empty when the user stopped mid-stream and
  // only the thinking was produced; say so instead of exporting a blank section.
  lines.push(content || (reasoning ? `_${partialMarker}_` : ''), '');
  const failure = messageError(message);
  if (failure) {
    lines.push(`### ${errorHeading || 'Error'}`, '', failure, '');
  }
  for (const attachment of message.attachments || []) {
    lines.push(
      `### ${attachmentHeading}: ${attachment.name}`,
      '',
      attachment.content,
      '',
    );
  }
  const sources = Array.isArray(message?.stats?.sources) ? message.stats.sources : [];
  if (sources.length) {
    lines.push(`### ${sourcesHeading || 'Sources'}`, '');
    for (const source of sources) {
      const page = source.page ? ` (p. ${source.page})` : '';
      const snippet = String(source.snippet || '').trim();
      lines.push(
        `- [${source.number}] ${source.title || ''}${page}${snippet ? ` - ${snippet}` : ''}`
      );
    }
    lines.push('');
  }
  return lines;
}


/**
 * The persisted or in-memory failure attached to a message.
 *
 * Streaming failures are stored in ``stats.error`` because the chat table has
 * no dedicated error column. Reading only the transient ``error`` field makes a
 * truncated reply look complete after a reload.
 */
export function messageError(message) {
  const direct = message?.error;
  if (typeof direct === 'string' && direct.trim()) return direct;
  const persisted = message?.stats?.error;
  return typeof persisted === 'string' ? persisted.trim() : '';
}

/** Whether this message may be sent to the model as completed history. */
export function messageUsableForContext(message) {
  return !messageError(message);
}


/** Whether an assistant turn should be written even with empty content. */
export function shouldPersistAssistantMessage({ content, reasoning, failure } = {}) {
  return Boolean(
    String(content || '').trim() || String(reasoning || '').trim() || failure
  );
}
