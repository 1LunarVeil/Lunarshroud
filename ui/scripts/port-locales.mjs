/**
 * One-time migration: port the 1.0 locale dictionaries to TypeScript modules.
 *
 * Why a script instead of a hand copy: the two dictionaries hold 1355 keys each,
 * and a hand transcription would silently drop or mistype entries. Reading the
 * source module and re-emitting it guarantees key-for-key fidelity, which is the
 * whole point of the port (the 2.0 rewrite must not lose translated strings).
 *
 * Run from the repository root:
 *   node ui/scripts/port-locales.mjs
 *
 * This script is temporary. It reads `frontend/js/locales/`, which Phase 8
 * deletes; remove this file in the same change that removes the old frontend.
 */
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const root = path.resolve(import.meta.dirname, '..', '..');
const source = path.join(root, 'frontend', 'js', 'locales');
const target = path.join(root, 'ui', 'src', 'lib', 'i18n');

const SOURCES = [
  {
    file: 'zh-CN.mjs',
    exported: 'ZH_CN',
    name: 'ZH_CN',
    header: [
      '/**',
      ' * Simplified Chinese dictionary — the base locale.',
      ' *',
      ' * Ported from the 1.0 frontend (`frontend/js/locales/zh-CN.mjs`) so no',
      ' * translated string is lost in the rewrite. `tests/i18n.test.ts` asserts this',
      ' * key set stays exactly equal to `en-US.ts`.',
      ' *',
      ' * Namespace convention: app.* global / nav.* navigation / badge.* status /',
      ' * dash.* dashboard / chat.* / params.* / presets.* / logs.* / monitor.* /',
      ' * history.* / settings.* / err.* backend-error mapping.',
      ' */',
    ],
  },
  {
    file: 'en-US.mjs',
    exported: 'EN_US',
    name: 'EN_US',
    header: [
      '/**',
      ' * English dictionary.',
      ' *',
      ' * Ported from the 1.0 frontend (`frontend/js/locales/en-US.mjs`): the same',
      ' * 1355 keys as `zh-CN.ts`.',
      ' */',
    ],
  },
];

function emitModule({ name, header }, entries) {
  const lines = [...header, '', `export const ${name}: Record<string, string> = {`];
  for (const [key, value] of entries) {
    if (typeof value !== 'string') {
      throw new TypeError(`locale value for ${key} is not a string`);
    }
    lines.push(`  ${JSON.stringify(key)}: ${JSON.stringify(value)},`);
  }
  lines.push('};', '');
  return lines.join('\n');
}

await mkdir(target, { recursive: true });

for (const spec of SOURCES) {
  const modulePath = path.join(source, spec.file);
  const module = await import(pathToFileURL(modulePath).href);
  const dictionary = module[spec.exported];
  if (!dictionary || typeof dictionary !== 'object') {
    throw new Error(`could not read ${spec.exported} from ${spec.file}`);
  }
  const entries = Object.entries(dictionary);
  const contents = emitModule(spec, entries);
  await writeFile(path.join(target, `${spec.name === 'ZH_CN' ? 'zh-CN' : 'en-US'}.ts`), contents, 'utf8');
  console.log(`ported ${entries.length} keys from ${spec.file}`);
}

// Parity must hold at the source too; if 1.0 shipped a mismatch, surface it now
// rather than passing it forward.
const zh = (await import(pathToFileURL(path.join(source, 'zh-CN.mjs')).href)).ZH_CN;
const en = (await import(pathToFileURL(path.join(source, 'en-US.mjs')).href)).EN_US;
const zhKeys = new Set(Object.keys(zh));
const enKeys = new Set(Object.keys(en));
const onlyZh = [...zhKeys].filter((key) => !enKeys.has(key));
const onlyEn = [...enKeys].filter((key) => !zhKeys.has(key));
if (onlyZh.length || onlyEn.length) {
  console.error(`key mismatch: only-zh=${onlyZh.length} only-en=${onlyEn.length}`);
  process.exitCode = 1;
} else {
  console.log(`parity ok: ${zhKeys.size} keys in both dictionaries`);
}
