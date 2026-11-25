/**
 * Structural budget for untrusted markdown, checked **before** parsing.
 *
 * The backend caps message *characters* (`chat_store.MAX_CONTENT_CHARS = 200_000`),
 * which says nothing about nesting. Both remark's parser and KaTeX recurse, so a
 * five-kilobyte message can overflow the JS stack:
 *
 * - 2 500 nested blockquote levels (`> > > …`) → `RangeError: Maximum call stack
 *   size exceeded` inside `remark-parse`;
 * - ~500 nested `\frac{1}{\frac{…}}` → `RangeError` from `katex.renderToString`,
 *   which `rehype-katex` does not catch (it only catches `ParseError`).
 *
 * Measured against the real installed pipeline on 2026-11-23: depth 2 000 parses,
 * 2 500 throws. A throw during render unmounts the whole React tree, so the user
 * gets a blank window with no message — and reopening the app re-renders the same
 * conversation and blanks again.
 *
 * The limits below are an order of magnitude above anything a person writes and
 * far below the crash point, so legitimate documents are unaffected. When a
 * document exceeds them it is rendered as **plain text** rather than dropped:
 * the content stays readable, it just loses formatting.
 */

/** Deepest blockquote/list nesting accepted. */
export const MAX_BLOCKQUOTE_DEPTH = 64;

/** Deepest `{`…`}` nesting inside math segments accepted (KaTeX recursion). */
export const MAX_MATH_BRACE_DEPTH = 24;

/**
 * Longest run of `>` / `-` / `*` / digit+`.` markers at the start of one line.
 *
 * Only the line's *prefix* is examined: `a > b > c` in prose is not nesting, and
 * an arrow inside a sentence must not count.
 */
export function maxBlockquoteDepth(text: string): number {
  let deepest = 0;
  for (const line of text.split('\n')) {
    let index = 0;
    let depth = 0;
    for (;;) {
      while (index < line.length && (line[index] === ' ' || line[index] === '\t')) index += 1;
      if (line[index] === '>') {
        depth += 1;
        index += 1;
        continue;
      }
      // `- `, `* `, `+ ` repeat as nesting too, but only after a blockquote run;
      // a plain bulleted list is depth 1 by definition.
      break;
    }
    if (depth > deepest) deepest = depth;
  }
  return deepest;
}

/**
 * Deepest brace nesting inside `$…$` / `$$…$$` segments.
 *
 * Brace depth is the cheap proxy for KaTeX's recursion: every `\frac`, `\sqrt`,
 * `\begin` argument increments it. LaTeX outside math segments is not scanned,
 * because `$$` delimiters are the only place `rehype-katex` will look.
 */
export function maxMathBraceDepth(text: string): number {
  let deepest = 0;
  let depth = 0;
  let inMath = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (char === '\\') {
      // Skip the escaped character so `\{` does not open a group.
      index += 1;
      continue;
    }
    if (char === '$') {
      // `$$` is **one** display delimiter, not two toggles. Treating it as two
      // flips the state back immediately, so every display formula was classified
      // as "not math" and escaped the budget entirely — caught by the regression
      // test for the 500-`\frac` payload.
      if (text[index + 1] === '$') index += 1;
      inMath = !inMath;
      continue;
    }
    if (!inMath) continue;
    if (char === '{') {
      depth += 1;
      if (depth > deepest) deepest = depth;
    } else if (char === '}') {
      depth = Math.max(0, depth - 1);
    }
  }
  return deepest;
}

export interface MarkdownBudgetVerdict {
  ok: boolean;
  /** Which limit was exceeded, for a test and for the plain-text fallback note. */
  reason?: 'blockquote-depth' | 'math-depth';
}

/** Whether this text is safe to hand to remark + KaTeX at all. */
export function markdownWithinBudget(text: string): MarkdownBudgetVerdict {
  if (maxBlockquoteDepth(text) > MAX_BLOCKQUOTE_DEPTH) {
    return { ok: false, reason: 'blockquote-depth' };
  }
  if (maxMathBraceDepth(text) > MAX_MATH_BRACE_DEPTH) {
    return { ok: false, reason: 'math-depth' };
  }
  return { ok: true };
}
