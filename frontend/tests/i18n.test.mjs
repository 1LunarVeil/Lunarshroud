// i18n 与参数联动纯逻辑的单元测试（node --test）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const jsDir = join(import.meta.dirname, '..', 'js');
const { t, setLocale, getLocale, localizeBackendError } = await import(
  pathToFileURL(join(jsDir, 'i18n.mjs')).href
);
const { ZH_CN } = await import(pathToFileURL(join(jsDir, 'locales', 'zh-CN.mjs')).href);
const { EN_US } = await import(pathToFileURL(join(jsDir, 'locales', 'en-US.mjs')).href);
const { isParamEnabled, shouldAutoAssignPort } = await import(
  pathToFileURL(join(jsDir, 'runtime.mjs')).href
);

test('zh-CN and en-US dictionaries expose identical key sets', () => {
  const zh = Object.keys(ZH_CN).sort();
  const en = Object.keys(EN_US).sort();
  assert.deepEqual(zh, en, 'locale dictionaries must stay in sync');
});

test('t() translates per locale, interpolates params, and falls back to the key', () => {
  setLocale('en-US');
  assert.equal(getLocale(), 'en-US');
  assert.equal(t('app.cancel'), 'Cancel');
  assert.equal(t('dash.launched', { name: 'demo', port: 8080 }), 'Instance started: demo (port 8080)');

  setLocale('zh-CN');
  assert.equal(t('app.cancel'), '取消');
  assert.equal(t('dash.launched', { name: 'demo', port: 8080 }), '实例已启动: demo（端口 8080）');

  assert.equal(t('definitely.missing.key'), 'definitely.missing.key');
});

test('setLocale rejects unknown or identical locales', () => {
  setLocale('zh-CN');
  assert.equal(setLocale('zh-CN'), false);
  assert.equal(setLocale('fr-FR'), false);
  assert.equal(getLocale(), 'zh-CN');
});

test('localizeBackendError maps known backend messages and passes unknowns through', () => {
  setLocale('zh-CN');
  assert.equal(
    localizeBackendError('port 127.0.0.1:8080 is already in use'),
    '端口已被占用'
  );
  setLocale('en-US');
  assert.equal(
    localizeBackendError('no available port in range 8080-8090'),
    'No free port within the configured range'
  );
  // 未匹配的原样返回
  assert.equal(localizeBackendError('something unexpected'), 'something unexpected');
});

test('isParamEnabled honours depends_on and depends_on_nonempty (incl. none)', () => {
  const chatTemplate = { key: 'chat_template', depends_on: { jinja: false } };
  assert.equal(isParamEnabled(chatTemplate, { jinja: false }), true);
  assert.equal(isParamEnabled(chatTemplate, { jinja: true }), false);
  assert.equal(isParamEnabled(chatTemplate, {}), false); // 缺失键视为不满足

  // 多值依赖：--spec-type 是逗号分隔的多值参数，命中 allowed 中任一值即启用
  const draft = {
    key: 'model_draft',
    depends_on: { spec_default: false },
    depends_on_any: { spec_type: ['draft-simple', 'draft-eagle3', 'draft-mtp', 'draft-dflash'] },
  };
  assert.equal(isParamEnabled(draft, { spec_default: false, spec_type: ['draft-mtp'] }), true);
  assert.equal(isParamEnabled(draft, { spec_default: false, spec_type: ['draft-simple'] }), true);
  assert.equal(isParamEnabled(draft, { spec_default: false, spec_type: ['ngram-mod'] }), false);
  assert.equal(isParamEnabled(draft, { spec_default: false, spec_type: [] }), false);
  // 开启 --spec-default 后手动选择被忽略
  assert.equal(isParamEnabled(draft, { spec_default: true, spec_type: ['draft-mtp'] }), false);

  const ropeScale = { key: 'rope_scale', depends_on_nonempty: 'rope_scaling' };
  assert.equal(isParamEnabled(ropeScale, { rope_scaling: 'yarn' }), true);
  assert.equal(isParamEnabled(ropeScale, { rope_scaling: 'none' }), false);
  assert.equal(isParamEnabled(ropeScale, { rope_scaling: '' }), false);
  assert.equal(isParamEnabled(ropeScale, {}), false);

  const plain = { key: 'model' };
  assert.equal(isParamEnabled(plain, {}), true);
});

test('shouldAutoAssignPort only treats the schema-default port as unspecified', () => {
  assert.equal(shouldAutoAssignPort({ port: 8080 }, { port: 8080 }), true);
  assert.equal(shouldAutoAssignPort({ port: 9000 }, { port: 8080 }), false);
  assert.equal(shouldAutoAssignPort({ port: '8080' }, { port: 8080 }), false); // 类型不同视为显式
  assert.equal(shouldAutoAssignPort({}, { port: 8080 }), true); // 未填 = 自动分配
});
