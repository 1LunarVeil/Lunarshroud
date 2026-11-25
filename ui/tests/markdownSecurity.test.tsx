import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { Markdown } from '@/components/chat/Markdown';

/**
 * Sanitizer and image-policy regression tests.
 *
 * The 2026-11-23 frontend audit noted that **no test in the repository rendered
 * adversarial markdown through the real pipeline**, so every safety property the
 * module comment claimed was unmeasured. These tests use the real plugin stack
 * (remark-gfm, remark-math, rehype-sanitize with the app's schema, rehype-katex)
 * and assert on the produced HTML, not on intermediate ASTs.
 *
 * Rendered with `renderToStaticMarkup` rather than a DOM harness: no
 * `@testing-library/react` is installed, and the property under test is "what
 * markup does this produce", which SSR answers exactly.
 */
function render(text: string): string {
  return renderToStaticMarkup(createElement(Markdown, { text }));
}

describe('Markdown sanitizer', () => {
  it('drops every raw HTML vector the audit listed', () => {
    const payloads = [
      '<script>alert(1)</script>',
      '<img src=x onerror="alert(1)">',
      '<iframe src="https://evil.example"></iframe>',
      '<svg onload="alert(1)"></svg>',
      '<style>body{display:none}</style>',
      '<meta http-equiv="refresh" content="0;url=https://evil.example">',
      '<base href="https://evil.example/">',
      '<form action="https://evil.example"><input name="a"></form>',
      '<object data="x"></object>',
      '<embed src="x">',
      '<video autoplay src="x"></video>',
    ];
    for (const payload of payloads) {
      const html = render(payload);
      expect(html, payload).not.toContain('<script');
      expect(html, payload).not.toContain('onerror');
      expect(html, payload).not.toContain('onload');
      expect(html, payload).not.toContain('<iframe');
      expect(html, payload).not.toContain('<style');
      expect(html, payload).not.toContain('<meta');
      expect(html, payload).not.toContain('<base');
      expect(html, payload).not.toContain('<form');
      expect(html, payload).not.toContain('<object');
      expect(html, payload).not.toContain('<embed');
      expect(html, payload).not.toContain('<video');
    }
  });

  it('strips javascript: hrefs in every syntactic form', () => {
    const payloads = [
      '[x](javascript:alert(1))',
      '[x](JaVaScRiPt:alert(1))',
      '[x](&#106;avascript:alert(1))',
      '[x][ref]\n\n[ref]: javascript:alert(1)',
    ];
    for (const payload of payloads) {
      const html = render(payload);
      expect(html.toLowerCase(), payload).not.toContain('href="javascript:');
    }
  });

  it('renders an autolink with a javascript scheme as inert text, not a link', () => {
    // `<javascript:alert(1)>` is GFM autolink syntax: the renderer drops the href
    // but keeps the text. The property that matters is "no clickable javascript
    // URL", not "the string javascript: does not appear".
    const html = render('<javascript:alert(1)>');
    expect(html).not.toContain('href="javascript:');
    expect(html).not.toContain('href=');
  });

  it('strips data:text/html hrefs', () => {
    const html = render('[x](data:text/html,<script>alert(1)</script>)');
    expect(html).not.toContain('data:text/html');
  });

  it('keeps http/https links and marks them noopener', () => {
    const html = render('[x](https://example.com/a)');
    expect(html).toContain('href="https://example.com/a"');
    expect(html).toContain('rel="noreferrer noopener"');
    expect(html).toContain('target="_blank"');
  });

  /**
   * The audit's finding #3: `hast-util-sanitize` only applies the protocol list to
   * URLs that **have** a scheme, so relative and protocol-relative image sources
   * survived and were emitted — turning "open this conversation" into a request to
   * `/api/system/config` or to an attacker's origin. The renderer now refuses
   * anything that is not `data:image/…` or `blob:`.
   */
  it('refuses relative, absolute-path and protocol-relative image sources', () => {
    for (const src of ['/api/system/config', '../../../../Windows/win.ini', '//evil.example/x.png']) {
      const html = render(`![x](${src})`);
      expect(html, src).not.toContain('<img');
    }
  });

  it('refuses an https image source too (offline contract)', () => {
    const html = render('![x](https://evil.example/x.png)');
    expect(html).not.toContain('<img');
  });

  it('still renders an inline data:image attachment', () => {
    const pixel =
      'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==';
    const html = render(`![x](${pixel})`);
    expect(html).toContain('<img');
    expect(html).toContain('data:image/png;base64,');
  });

  it('does not render a data: URL of a non-image type as an image', () => {
    // `data:` of any other type is rejected by the pattern in `isRenderableImageSrc`.
    const html = render('![x](data:text/html;base64,PHNjcmlwdD4=)');
    expect(html).not.toContain('<img');
  });
});

describe('Markdown adversarial input', () => {
  /**
   * Finding #1: a 5 KB message could overflow the JS stack. Both payloads are the
   * measured ones; the component must render *something* and must not throw.
   */
  it('renders the 2 500-level blockquote payload as plain text instead of throwing', () => {
    const text = '> '.repeat(2500) + 'boom';
    let html = '';
    expect(() => {
      html = render(text);
    }).not.toThrow();
    expect(html).toContain('boom');
    // The plain-text branch, not a 2 500-deep <blockquote> tree.
    expect(html).not.toContain('<blockquote>');
  });

  it('renders the 500-nested-frac payload as plain text instead of throwing', () => {
    const text = '$$' + '\\frac{1}{'.repeat(500) + '1' + '}'.repeat(500) + '$$';
    let html = '';
    expect(() => {
      html = render(text);
    }).not.toThrow();
    expect(html).not.toContain('katex');
  });

  it('clamps KaTeX length arguments (finding #4)', () => {
    // Without `maxSize`, this produced a 100 000em-tall element. The original
    // source text still appears inside KaTeX's `<annotation>`, so the assertion is
    // on the rendered CSS lengths, not on the string.
    const html = render('$\\rule{100000em}{100000em}$');
    expect(html).toContain('katex');
    expect(html).not.toContain('100000em;');
    expect(html).toContain('20em');
  });

  it('still renders ordinary markdown', () => {
    const html = render('# Title\n\n**bold** and `code`\n\n- one\n- two');
    expect(html).toContain('<h1>');
    expect(html).toContain('<strong>bold</strong>');
    expect(html).toContain('<li>one</li>');
  });

  it('renders a fenced code block through the code chrome', () => {
    const html = render('```ts\nconst a = 1;\n```');
    expect(html).toContain('code-block');
    expect(html).toContain('const a = 1;');
  });
});
