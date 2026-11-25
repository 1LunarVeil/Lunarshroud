/**
 * Shiki highlighter, loaded on demand.
 *
 * **Why the core API and explicit grammar imports.** Importing the `shiki` barrel
 * pulls in *every* bundled grammar: a measured build produced 406 chunks / 14 MB in
 * `ui/dist`, all of it embedded into the Tauri binary. Only the languages we
 * actually highlight are imported here, so the bundle stays in proportion to the
 * feature.
 *
 * Grammars are dynamic imports so they land in their own chunks and are fetched
 * only when a code fence of that language first appears.
 *
 * `createJavaScriptRegexEngine` avoids shipping the Oniguruma WASM blob (~600 KB)
 * at the cost of a regex dialect that handles all the grammars listed below.
 */
import { createHighlighterCore, type HighlighterCore } from 'shiki/core';
import { createJavaScriptRegexEngine } from 'shiki/engine/javascript';

type LoadedLanguage = Parameters<HighlighterCore['loadLanguage']>[0];

/**
 * Grammars worth shipping: the ones models actually emit.
 *
 * Ordered by how often they appear in practice. The big C-family grammars
 * (cpp ≈ 780 KB, csharp, java) are imported on demand like everything else, but
 * they are listed here so the set is explicit and auditable — a stray import of
 * the `shiki` barrel would silently re-add every grammar in the package
 * (measured: 406 chunks / 14 MB).
 */
const GRAMMARS: Record<string, () => Promise<{ default: unknown }>> = {
  javascript: () => import('shiki/langs/javascript.mjs'),
  typescript: () => import('shiki/langs/typescript.mjs'),
  tsx: () => import('shiki/langs/tsx.mjs'),
  jsx: () => import('shiki/langs/jsx.mjs'),
  json: () => import('shiki/langs/json.mjs'),
  python: () => import('shiki/langs/python.mjs'),
  bash: () => import('shiki/langs/bash.mjs'),
  diff: () => import('shiki/langs/diff.mjs'),
  markdown: () => import('shiki/langs/markdown.mjs'),
  yaml: () => import('shiki/langs/yaml.mjs'),
  toml: () => import('shiki/langs/toml.mjs'),
  sql: () => import('shiki/langs/sql.mjs'),
  html: () => import('shiki/langs/html.mjs'),
  css: () => import('shiki/langs/css.mjs'),
  ini: () => import('shiki/langs/ini.mjs'),
  powershell: () => import('shiki/langs/powershell.mjs'),
  rust: () => import('shiki/langs/rust.mjs'),
  go: () => import('shiki/langs/go.mjs'),
  c: () => import('shiki/langs/c.mjs'),
  cpp: () => import('shiki/langs/cpp.mjs'),
  csharp: () => import('shiki/langs/csharp.mjs'),
  java: () => import('shiki/langs/java.mjs'),
};

/** Aliases the model commonly writes that map onto a grammar above. */
const ALIASES: Record<string, string> = {
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  ts: 'typescript',
  py: 'python',
  rs: 'rust',
  sh: 'bash',
  shell: 'bash',
  ps1: 'powershell',
  cxx: 'cpp',
  h: 'c',
  hpp: 'cpp',
  cs: 'csharp',
  yml: 'yaml',
  md: 'markdown',
  ps: 'powershell',
};

export const SUPPORTED_LANGUAGES = Object.keys(GRAMMARS);

let highlighterPromise: Promise<HighlighterCore> | null = null;
const loaded = new Set<string>();

async function getHighlighter(): Promise<HighlighterCore> {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighterCore({
      themes: [import('shiki/themes/github-light.mjs'), import('shiki/themes/github-dark.mjs')],
      langs: [],
      engine: createJavaScriptRegexEngine(),
    });
  }
  return highlighterPromise;
}

/** Normalize a fence label to a grammar we ship. Unknown labels return null. */
export function normalizeLanguage(raw: string | null | undefined): string | null {
  const label = String(raw ?? '').trim().toLowerCase().split(/\s+/)[0] ?? '';
  if (!label) return null;
  const resolved = ALIASES[label] ?? label;
  return resolved in GRAMMARS ? resolved : null;
}

/**
 * Highlight `code`. Returns `null` when the language is unknown or loading fails,
 * in which case the caller renders the plain `<pre>` — a failed highlight must
 * never blank the block.
 */
export async function highlightCode(
  code: string,
  language: string | null | undefined,
): Promise<string | null> {
  const lang = normalizeLanguage(language);
  if (!lang) return null;
  try {
    const highlighter = await getHighlighter();
    if (!loaded.has(lang)) {
      const loader = GRAMMARS[lang];
      if (!loader) return null;
      const grammar = await loader();
      await highlighter.loadLanguage(grammar.default as LoadedLanguage);
      loaded.add(lang);
    }
    return highlighter.codeToHtml(code, {
      lang,
      themes: { light: 'github-light', dark: 'github-dark' },
      defaultColor: false,
    });
  } catch {
    return null;
  }
}
