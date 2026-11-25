import { describe, expect, it } from 'vitest';
import {
  MAX_BLOCKQUOTE_DEPTH,
  MAX_MATH_BRACE_DEPTH,
  markdownWithinBudget,
  maxBlockquoteDepth,
  maxMathBraceDepth,
} from '@/lib/markdownBudget';

/**
 * Regression tests for the structural budget added after the 2026-11-23 frontend
 * audit found that deeply nested untrusted text throws `RangeError` inside
 * remark/KaTeX and unmounts the whole React tree.
 *
 * The payloads here are the measured ones: 2 500 nested blockquotes overflowed the
 * stack, 500 nested `\frac` overflowed inside KaTeX.
 */
describe('markdownBudget', () => {
  it('accepts ordinary prose that contains arrow-like ">" characters', () => {
    // The check only looks at the line prefix; `a > b > c` must not count.
    expect(markdownWithinBudget('if a > b > c then swap').ok).toBe(true);
    expect(maxBlockquoteDepth('if a > b > c then swap')).toBe(0);
  });

  it('accepts a normal three-level quote and a bulleted list', () => {
    const text = '> one\n> > two\n> > > three\n\n- a\n- b\n- c\n';
    expect(maxBlockquoteDepth(text)).toBe(3);
    expect(markdownWithinBudget(text).ok).toBe(true);
  });

  it('rejects the measured stack-overflow payload (2 500 nested quotes)', () => {
    const text = '> '.repeat(2500) + 'boom';
    expect(maxBlockquoteDepth(text)).toBe(2500);
    const verdict = markdownWithinBudget(text);
    expect(verdict.ok).toBe(false);
    expect(verdict.reason).toBe('blockquote-depth');
  });

  it('rejects at the limit rather than only far past it', () => {
    const atLimit = '> '.repeat(MAX_BLOCKQUOTE_DEPTH) + 'x';
    const overLimit = '> '.repeat(MAX_BLOCKQUOTE_DEPTH + 1) + 'x';
    expect(markdownWithinBudget(atLimit).ok).toBe(true);
    expect(markdownWithinBudget(overLimit).ok).toBe(false);
  });

  it('rejects the measured KaTeX recursion payload (500 nested \\frac)', () => {
    const text = '$$' + '\\frac{1}{'.repeat(500) + '1' + '}'.repeat(500) + '$$';
    expect(maxMathBraceDepth(text)).toBe(500);
    const verdict = markdownWithinBudget(text);
    expect(verdict.ok).toBe(false);
    expect(verdict.reason).toBe('math-depth');
  });

  it('does not count braces outside math segments', () => {
    // Plain code braces are everywhere in a chat transcript; counting them would
    // render half of all code answers as unformatted text.
    const text = 'function f() { if (x) { return { a: 1 }; } }';
    expect(maxMathBraceDepth(text)).toBe(0);
    expect(markdownWithinBudget(text).ok).toBe(true);
  });

  it('ignores escaped braces so `\\{` does not open a group', () => {
    const text = '$$' + '\\{'.repeat(100) + '$$';
    expect(maxMathBraceDepth(text)).toBe(0);
  });

  it('accepts realistic display math', () => {
    const text = '$$\\int_0^1 \\frac{x^2}{1+x^2}\\,dx = \\frac{\\pi}{4} - \\frac{1}{2}$$';
    expect(maxMathBraceDepth(text)).toBeLessThanOrEqual(4);
    expect(markdownWithinBudget(text).ok).toBe(true);
  });

  it('keeps the limits well above real documents and well below the crash depth', () => {
    // Crash measured at 2 500 quote levels and ~500 braces; a person writes <10.
    expect(MAX_BLOCKQUOTE_DEPTH).toBeGreaterThanOrEqual(32);
    expect(MAX_BLOCKQUOTE_DEPTH).toBeLessThan(2000);
    expect(MAX_MATH_BRACE_DEPTH).toBeGreaterThanOrEqual(8);
    expect(MAX_MATH_BRACE_DEPTH).toBeLessThan(200);
  });
});
