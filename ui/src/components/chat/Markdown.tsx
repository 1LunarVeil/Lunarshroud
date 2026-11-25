import { memo, useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import { Check, Copy, WrapText } from 'lucide-react';
import { translate } from '@/lib/i18n';
import { highlightCode } from '@/lib/highlight';
import { markdownWithinBudget } from '@/lib/markdownBudget';
import { cn } from '@/lib/utils';

/**
 * Markdown renderer for model output.
 *
 * **Model output is untrusted.** It can contain raw HTML, `javascript:` links,
 * images pointing at the network, and structures that make the parsers recurse
 * until the stack overflows. Safeguards, in order:
 *
 * 1. `markdownWithinBudget` — a structural pre-check. Character budgets do not
 *    bound nesting, and both remark and KaTeX recurse (AGENTS §2.6 is the same
 *    reasoning for GGUF parsing). Over budget renders as plain text.
 * 2. `rehype-sanitize` with an **explicit** allow-list derived from the default
 *    schema. Owning the list rather than trusting the default matters because the
 *    default changes between releases, and a silent widening is a vulnerability,
 *    not a feature.
 * 3. `img` accepts only `data:`/`blob:` **and** is re-checked here: the schema's
 *    protocol check does not fire for protocol-relative (`//host/x.png`) or
 *    relative (`/api/...`) URLs, because those have no scheme to compare
 *    (verified against hast-util-sanitize 5.x, 2026-11-23). Left alone, a model
 *    could emit `![](/api/system/config)` and turn opening a conversation into a
 *    request the user did not make.
 * 4. Links use `rel="noreferrer noopener"` and only `http`/`https`/`mailto`.
 * 5. `rehype-katex` gets `maxSize`, because `\rule{100000em}{100000em}` produces
 *    a 1.4-million-pixel-tall document from one short span.
 *
 * A `MessageBoundary` above this component catches anything that still throws.
 */

/**
 * URL policy for the renderer.
 *
 * `react-markdown` runs `defaultUrlTransform` on every URL before plugins: it
 * allows `http`/`https`/`mailto`/`irc`/`ircs`/`xmpp` plus relative URLs, and
 * strips everything else — **including `data:`**. Left in place, that silently
 * deleted every inline image attachment from a transcript, so pictures the user
 * pasted rendered as an empty `<p>`.
 *
 * This transform keeps the default behaviour and additionally passes `data:image/`
 * and `blob:` through. `rehype-sanitize` has already run with the same allow-list,
 * and the `img` component re-checks the prefix, so a `data:text/html` payload still
 * cannot reach an `<img src>`.
 */
function transformUrl(url: string): string | null {
  const lowered = url.trim().toLowerCase();
  if (lowered.startsWith('data:image/') || lowered.startsWith('blob:')) return url;
  return defaultUrlTransform(url);
}

const SCHEMA = {
  ...defaultSchema,
  // Strip `class` from everything except code (Shiki's own spans are generated
  // after sanitization, so nothing here needs classes from the model).
  attributes: {
    ...defaultSchema.attributes,
    '*': ['className'],
    code: [['className', /^language-./]],
    a: ['href', 'title'],
  },
  tagNames: [
    'p', 'br', 'strong', 'em', 'del', 'code', 'pre', 'blockquote',
    'ul', 'ol', 'li', 'a', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'img',
    'sup', 'sub', 'span', 'input',
  ],
  protocols: {
    href: ['http', 'https', 'mailto'],
    src: ['data', 'blob'],
  },
};

/**
 * KaTeX's own clamp on length arguments. 20 is the documented value: anything a
 * real formula needs is far below it, and it bounds `\rule`/`\kern`/`\raisebox`.
 */
const KATEX_OPTIONS = { maxSize: 20, strict: 'ignore' as const };

/**
 * Whether an `<img src>` may be rendered.
 *
 * `data:image/…` and `blob:` only. Explicitly **not** relative paths: in the
 * packaged build the page origin is `tauri.localhost`, so `/api/…` resolves to
 * the launcher's own API and `../../…` to the asset origin — both are requests
 * the model would be making on the user's behalf.
 */
function isRenderableImageSrc(value: unknown): value is string {
  if (typeof value !== 'string') return false;
  const trimmed = value.trim();
  const lowered = trimmed.toLowerCase();
  return lowered.startsWith('data:image/') || lowered.startsWith('blob:');
}

interface CodeBlockProps {
  language: string;
  code: string;
}

const CodeBlock = memo(function CodeBlock({ language, code }: CodeBlockProps) {
  const [html, setHtml] = useState<string | null>(null);
  const [wrap, setWrap] = useState(false);
  const [copied, setCopied] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    // A language we do not ship returns null immediately, so the plain <pre>
    // renders without waiting on anything.
    void highlightCode(code, language).then((result) => {
      if (!cancelled && mounted.current) setHtml(result);
    });
    return () => {
      cancelled = true;
    };
  }, [code, language]);

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      // Clipboard can be blocked; the button simply does not confirm.
    }
  }, [code]);

  return (
    <div className="code-block" data-wrap={wrap} data-testid="code-block">
      <div className="code-block__head">
        <span className="code-block__lang">{language || 'text'}</span>
        <div className="code-block__actions">
          <button
            type="button"
            onClick={() => setWrap((value) => !value)}
            aria-pressed={wrap}
            title={translate('chat.copyCode')}
            aria-label={wrap ? 'Unwrap lines' : 'Wrap lines'}
            className={cn(
              'grid size-6 place-items-center rounded-[var(--radius-xs)]',
              'text-[var(--code-gutter)] hover:bg-[var(--code-hover)] hover:text-[var(--code-fg)]',
              'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--code-accent)]',
            )}
          >
            <WrapText size={13} aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={copy}
            title={translate('chat.copyCode')}
            aria-label={translate('chat.copyCode')}
            className={cn(
              'flex h-6 items-center gap-1 rounded-[var(--radius-xs)] px-1.5',
              'text-[var(--code-gutter)] hover:bg-[var(--code-hover)] hover:text-[var(--code-fg)]',
              'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--code-accent)]',
            )}
          >
            {copied ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
          </button>
        </div>
      </div>
      {html ? (
        // Sanitized upstream by rehype-sanitize and then generated by Shiki from
        // the (already sanitized) text node. `html` never contains model HTML.
        <pre>
          <code dangerouslySetInnerHTML={{ __html: html }} />
        </pre>
      ) : (
        <pre>
          <code>{code}</code>
        </pre>
      )}
    </div>
  );
});

function extractText(node: ReactNode): string {
  if (typeof node === 'string') return node;
  if (typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(extractText).join('');
  if (node && typeof node === 'object' && 'props' in node) {
    const props = (node as { props?: { children?: ReactNode } }).props;
    return extractText(props?.children);
  }
  return '';
}

export interface MarkdownProps {
  text: string;
  className?: string;
}

export const Markdown = memo(function Markdown({ text, className }: MarkdownProps) {
  // Structural budget first: a nesting blow-up must not reach the parser at all.
  if (!markdownWithinBudget(text).ok) {
    return (
      <div className={cn('markdown', className)} data-testid="markdown">
        <p className="meta-text">{translate('chat.renderTooComplex')}</p>
        <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-anywhere font-mono text-[length:var(--text-2xs)] text-[length:var(--text-sm)] text-[var(--text-secondary)]">
          {text}
        </pre>
      </div>
    );
  }
  return (
    <div className={cn('markdown', className)} data-testid="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[[rehypeSanitize, SCHEMA], [rehypeKatex, KATEX_OPTIONS]]}
        urlTransform={transformUrl}
        components={{
          // `node` is react-markdown's internal hast node. Spreading the rest props
          // put it on the DOM as `node="[object object]"` (caught by the sanitizer
          // tests); `skipHtml` is handled by the pipeline, so it is dropped too.
          a({ href, children, node: _node, ...rest }) {
            return (
              <a href={href} target="_blank" rel="noreferrer noopener" {...rest}>
                {children}
              </a>
            );
          },
          img({ src, alt, node: _node, ...rest }) {
            // The schema's protocol list does not cover scheme-less URLs, so the
            // real check lives here (see the module comment).
            if (!isRenderableImageSrc(src)) return null;
            return (
              <img
                {...rest}
                src={src}
                alt={alt ?? ''}
                loading="lazy"
                className="max-w-full rounded-[var(--radius-md)]"
              />
            );
          },
          pre({ children }) {
            // `react-markdown` wraps fenced code in <pre><code>. We replace the
            // whole pair with our own chrome, so unwrap it here.
            const child = Array.isArray(children) ? children[0] : children;
            if (
              child &&
              typeof child === 'object' &&
              'props' in child &&
              (child as { props?: { className?: string } }).props?.className?.startsWith('language-')
            ) {
              const props = (child as { props: { className?: string; children?: ReactNode } }).props;
              const language = props.className?.replace('language-', '') ?? '';
              return <CodeBlock language={language} code={extractText(props.children)} />;
            }
            return <pre>{children}</pre>;
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
