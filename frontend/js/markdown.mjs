// Small, dependency-free Markdown renderer for chat messages.
//
// The output is assembled only from escaped text and the tags we emit here.
// We intentionally do not implement raw HTML: model output is untrusted, and a
// remote Markdown library would also violate the offline contract.

import { escapeHtml } from './html.mjs';

function inline(text, labels) {
  const codeSpans = [];
  // Pull inline code out first so ** and _ inside it stay literal.
  let value = text.replace(/`([^`\n]+)`/g, (_match, code) => {
    const index = codeSpans.push(`<code>${escapeHtml(code)}</code>`) - 1;
    return `\u0000CODE${index}\u0000`;
  });
  value = escapeHtml(value);
  // Links: only http(s). A javascript:/data: target never matches and stays text.
  value = value.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_match, label, url) =>
      `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`
  );
  value = value.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  value = value.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
  value = value.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  value = value.replace(/(^|[^_])_([^_\n]+)_(?!_)/g, '$1<em>$2</em>');
  value = value.replace(/\n/g, '<br>');
  value = value.replace(/\u0000CODE(\d+)\u0000/g, (_match, index) => codeSpans[Number(index)] || '');
  return value;
}

function codeBlock(language, code, labels) {
  const lang = language ? escapeHtml(language) : '';
  const langClass = language ? ` class="language-${language.replace(/[^A-Za-z0-9_-]/g, '')}"` : '';
  const copy = escapeHtml(labels.copy || '');
  return (
    `<div class="md-code-block"><div class="md-code-head"><span class="md-code-lang">${lang}</span>` +
    `<button type="button" class="md-copy" data-copy-code aria-label="${copy}">${copy}</button></div>` +
    `<pre class="md-code"><code${langClass}>${escapeHtml(code)}</code></pre></div>`
  );
}

/**
 * Render a chat message as safe HTML.
 *
 * Supported: fenced code, headings, blockquotes, ordered/unordered lists,
 * horizontal rules, paragraphs, bold/italic/strikethrough, inline code and
 * http(s) links. Everything else is escaped text.
 *
 * @param {string} text
 * @param {{copy?: string}} labels UI labels for the code-copy button.
 * @returns {string}
 */
export function renderMarkdown(text, labels = {}) {
  const copyLabel = labels.copy || 'Copy';
  const lines = String(text ?? '').replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let paragraph = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    out.push(`<p>${inline(paragraph.join('\n'), { copy: copyLabel })}</p>`);
    paragraph = [];
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];

    if (/^\s*```/.test(line)) {
      flushParagraph();
      const language = line.replace(/^\s*```/, '').trim().split(/\s+/)[0] || '';
      const body = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) {
        body.push(lines[index]);
        index += 1;
      }
      out.push(codeBlock(language, body.join('\n'), { copy: copyLabel }));
      continue;
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushParagraph();
      const level = heading[1].length;
      out.push(`<h${level} class="md-h">${inline(heading[2], { copy: copyLabel })}</h${level}>`);
      continue;
    }

    if (/^\s*([-*_])\s*\1\s*\1[\s*_-]*$/.test(line)) {
      flushParagraph();
      out.push('<hr>');
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      flushParagraph();
      const quote = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s*>\s?/, ''));
        index += 1;
      }
      index -= 1;
      out.push(`<blockquote>${inline(quote.join('\n'), { copy: copyLabel })}</blockquote>`);
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      flushParagraph();
      const items = [];
      while (index < lines.length && /^\s*[-*+]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*[-*+]\s+/, ''));
        index += 1;
      }
      index -= 1;
      out.push(
        `<ul>${items.map((item) => `<li>${inline(item, { copy: copyLabel })}</li>`).join('')}</ul>`
      );
      continue;
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      flushParagraph();
      const items = [];
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*\d+[.)]\s+/, ''));
        index += 1;
      }
      index -= 1;
      out.push(
        `<ol>${items.map((item) => `<li>${inline(item, { copy: copyLabel })}</li>`).join('')}</ol>`
      );
      continue;
    }

    if (!line.trim()) {
      flushParagraph();
      continue;
    }

    paragraph.push(line);
  }
  flushParagraph();
  return out.join('');
}

/** Plain text preview used by conversation titles and accessible labels. */
export function markdownPlainText(text) {
  return String(text ?? '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/[#>*_~-]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}
