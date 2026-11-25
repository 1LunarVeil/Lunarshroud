import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { EN_US } from '@/lib/i18n/en-US';
import { ZH_CN } from '@/lib/i18n/zh-CN';
import { EN_US_EXTRA, ZH_CN_EXTRA } from '@/lib/i18n/extra';
import { localizeBackendError, translate, translator } from '@/lib/i18n';

/**
 * Dictionary parity and the error-mapping contract.
 *
 * The parity test is the important one: the two dictionaries are generated from
 * the 1.0 sources and merged with a hand-written `extra.ts`. Nothing else stops a
 * new key from being added to one language only, which ships a UI with raw keys in
 * one locale.
 */

describe('dictionary parity', () => {
  it('ported dictionaries have identical key sets', () => {
    const zh = new Set(Object.keys(ZH_CN));
    const en = new Set(Object.keys(EN_US));
    const onlyZh = [...zh].filter((key) => !en.has(key));
    const onlyEn = [...en].filter((key) => !zh.has(key));
    expect(onlyZh).toEqual([]);
    expect(onlyEn).toEqual([]);
  });

  it('the 2.0 additions have identical key sets', () => {
    const zh = new Set(Object.keys(ZH_CN_EXTRA));
    const en = new Set(Object.keys(EN_US_EXTRA));
    expect([...zh].filter((key) => !en.has(key))).toEqual([]);
    expect([...en].filter((key) => !zh.has(key))).toEqual([]);
  });

  it('the merged dictionaries have identical key sets', () => {
    const zh = new Set([...Object.keys(ZH_CN), ...Object.keys(ZH_CN_EXTRA)]);
    const en = new Set([...Object.keys(EN_US), ...Object.keys(EN_US_EXTRA)]);
    expect([...zh].filter((key) => !en.has(key))).toEqual([]);
    expect([...en].filter((key) => !zh.has(key))).toEqual([]);
  });

  it('no translation contains a raw angle bracket', () => {
    // Values are rendered as text, but a `<` in a dictionary almost always means
    // someone pasted markup into a label.
    for (const [key, value] of Object.entries({ ...ZH_CN, ...EN_US })) {
      expect(value, `${key} contains '<'`).not.toContain('<');
    }
  });
});

describe('translate', () => {
  it('interpolates named placeholders', () => {
    expect(translate('chat.workedFor', { duration: '12s' })).toContain('12s');
  });

  it('leaves unknown placeholders untouched rather than printing "undefined"', () => {
    // A missing param must not become the string "undefined" in the UI.
    const result = translate('chat.workedFor', {});
    expect(result).not.toContain('undefined');
  });

  it('falls back to the key itself so a missing translation is visible', () => {
    expect(translate('this.key.does.not.exist')).toBe('this.key.does.not.exist');
  });

  it('the curried translator behaves like translate', () => {
    const t = translator('en-US');
    expect(t('nav.chat')).toBe(translate('nav.chat'));
  });
});

describe('localizeBackendError', () => {
  it('localizes a known backend message', () => {
    const localized = localizeBackendError('model file not found: a.gguf');
    expect(localized).not.toBe('model file not found: a.gguf');
  });

  it('returns the raw message when nothing matches', () => {
    expect(localizeBackendError('some brand new failure')).toBe('some brand new failure');
  });

  it('returns an empty string for empty input', () => {
    expect(localizeBackendError('')).toBe('');
    expect(localizeBackendError(null)).toBe('');
  });

  it('keeps the field name for a generic type error', () => {
    // Regression: 1.0 folded `must be a number` into the knowledge-base pattern,
    // so `seed must be a number` was reported as "invalid knowledge base settings"
    // and the real field name never appeared.
    const localized = localizeBackendError('seed must be a number');
    expect(localized).toContain('seed must be a number');
  });

  it('does NOT treat a generic type error as a knowledge-base problem', () => {
    const localized = localizeBackendError('seed must be a number');
    const knowledgeLabel = translate('err.knowledgeSetting');
    expect(localized).not.toBe(knowledgeLabel);
  });

  it('does localize a genuine knowledge-base error', () => {
    const localized = localizeBackendError('top_k must be between 1 and 100');
    expect(localized).toBe(translate('err.knowledgeSetting'));
  });

  it('distinguishes storage-unwritable from other failures', () => {
    expect(localizeBackendError('database is locked')).toBe(translate('err.storageBusy'));
    expect(localizeBackendError('readonly database')).toBe(translate('err.storageReadonly'));
  });

  it('localizes both attachment limits', () => {
    expect(localizeBackendError('attachment photo.png is too large')).toBe(
      translate('err.attachmentTooLarge'),
    );
    expect(localizeBackendError('conversation attachments are too large')).toBe(
      translate('err.attachmentTooLarge'),
    );
  });
});

/**
 * Dynamic keys reach `translate` from backend data (`agent.risk.${risk}`,
 * `nav.${route}`, `badge.${status}`). A plain object lookup walks the prototype
 * chain, so `translate('constructor')` used to return the `Object` constructor —
 * a **function** where React expects a string, which throws during render and
 * blanks the window (audit finding L8, 2026-11-23).
 */
describe('translate() is total over arbitrary keys', () => {
  it('returns the key itself for prototype properties, never a function', () => {
    for (const key of ['constructor', 'toString', 'hasOwnProperty', '__proto__', 'valueOf']) {
      const result = translate(key);
      expect(typeof result, key).toBe('string');
      expect(result, key).toBe(key);
    }
  });

  it('the curried translator has the same property', () => {
    const t = translator('zh-CN');
    for (const key of ['constructor', 'toString', '__proto__']) {
      expect(typeof t(key), key).toBe('string');
      expect(t(key), key).toBe(key);
    }
  });

  it('still resolves a real key', () => {
    expect(translate('nav.chat')).not.toBe('nav.chat');
  });
});

/**
 * Every key the source *actually uses* must exist in the dictionaries.
 *
 * The parity tests above only compare zh against en, so a key that is **absent from
 * both** passes them and renders as its literal key in the UI. That is not
 * hypothetical: 2.0.0 shipped with `nav.params` (the route id is `params`; the 1.0
 * dictionary only had `nav.param_editor`), `params.subtitle`, `params.model` and
 * eight more, so the toolbar and page headers displayed `nav.params` verbatim.
 *
 * Static keys are extracted by regex rather than by a TypeScript parser: the call
 * form is uniform (`translate('x')`), and a parser dependency for this would cost
 * more than it proves. Dynamic keys (`translate(\`nav.${route}\`)`) cannot be seen
 * here — those are covered by requiring the route ids themselves to have keys in
 * `navigation.ts`, asserted separately below.
 */
describe('translation keys used by the source', () => {
  const sourceRoot = path.resolve(__dirname, '..', 'src');

  function collectSourceFiles(dir: string, out: string[] = []): string[] {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) collectSourceFiles(full, out);
      else if (/\.(ts|tsx)$/.test(entry.name)) out.push(full);
    }
    return out;
  }

  const dictionary = { ...ZH_CN_EXTRA, ...EN_US_EXTRA, ...ZH_CN, ...EN_US };

  it('every static translate() key exists in the dictionaries', () => {
    const used = new Set<string>();
    for (const file of collectSourceFiles(sourceRoot)) {
      const text = readFileSync(file, 'utf-8');
      for (const match of text.matchAll(/translate\(\s*'([A-Za-z][\w.]*)'/g)) {
        const key = match[1];
        if (!key) continue;
        // A trailing dot means a concatenation prefix (`translate('app.theme.' + x)`),
        // not a key. Dynamic keys are covered by the route-id test below.
        if (key.endsWith('.')) continue;
        // Comments are scanned by regex too; `translate('constructor')` appears in a
        // doc comment explaining the prototype-chain guard.
        if (key === 'constructor') continue;
        used.add(key);
      }
    }
    expect(used.size).toBeGreaterThan(100);

    const missing = [...used].filter((key) => !Object.hasOwn(dictionary, key)).sort();
    expect(missing, `keys rendered as raw text in the UI: ${missing.join(', ')}`).toEqual([]);
  });

  it('every route id has a nav.* translation', () => {
    // `Toolbar.tsx` builds the title as `translate('nav.' + route)`, so a route id
    // without a matching key shows the raw string in the window header.
    const nav = readFileSync(path.join(sourceRoot, 'app', 'navigation.ts'), 'utf-8');
    const ids = new Set<string>();
    for (const match of nav.matchAll(/\{\s*id:\s*'([\w-]+)'/g)) {
      if (match[1]) ids.add(match[1]);
    }
    // The settings sheet is opened from a button, not a route, but it shares the
    // same title mechanism.
    ids.add('settings');
    expect(ids.size).toBeGreaterThan(5);

    const missing = [...ids].filter((id) => !Object.hasOwn(dictionary, `nav.${id}`)).sort();
    expect(missing, `route ids without a nav.* key: ${missing.join(', ')}`).toEqual([]);
  });
});
