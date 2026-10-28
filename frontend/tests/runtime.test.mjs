import test from 'node:test';
import assert from 'node:assert/strict';

import {
  boundedAppend,
  createReconnectDelays,
  createRequestGate,
  createSingleFlight,
  diffModelDefaults,
  formatApiErrorPayload,
  groupModelsByKey,
  instanceNameFromFilename,
  isLoopbackHost,
  mergeUpstreamOptions,
  normalizeHttpUrl,
  parseNumericInput,
  scheduleObjectUrlRevoke,
  setButtonBusy,
} from '../js/runtime.mjs';
import { classifyLogFrame } from '../js/log_stream.mjs';
import { settleModalAction, shouldCloseAfterAction } from '../js/modal.mjs';
import { escapeHtml } from '../js/html.mjs';
import { createParamView, isWideField } from '../js/param_editor_view.mjs';

test('mergeUpstreamOptions keeps schema order and appends new upstream values', () => {
  // 上游新增了 draft-dspark（实测 llama.cpp build 10960）——必须能被选到
  const merged = mergeUpstreamOptions(
    ['draft-mtp', 'ngram-mod'],
    ['draft-mtp', 'draft-dspark'],
  );
  assert.deepEqual(merged, ['draft-mtp', 'ngram-mod', 'draft-dspark']);
});

test('mergeUpstreamOptions falls back to the snapshot when nothing was probed', () => {
  // 没探测到就说没探测到，不能用猜的去凑
  assert.deepEqual(mergeUpstreamOptions(['a', 'b'], null), ['a', 'b']);
  assert.deepEqual(mergeUpstreamOptions(['a', 'b'], []), ['a', 'b']);
  assert.deepEqual(mergeUpstreamOptions(undefined, undefined), []);
});

test('mergeUpstreamOptions does not duplicate values across types', () => {
  assert.deepEqual(mergeUpstreamOptions([3, '3'], ['3']), [3, '3']);
});

test('isLoopbackHost recognises the loopback spellings', () => {
  for (const host of ['127.0.0.1', '127.1.2.3', 'localhost', 'LOCALHOST', '::1', '[::1]', '']) {
    assert.equal(isLoopbackHost(host), true, host);
  }
});

test('isLoopbackHost rejects anything that the iframe CSP would block', () => {
  // 这些地址嵌进 iframe 必然空白（CSP 只允许 127.0.0.1），必须走"外部打开"
  for (const host of ['192.168.1.10', '10.0.0.5', '0.0.0.0', 'example.com', '172.16.4.2']) {
    assert.equal(isLoopbackHost(host), false, host);
  }
  // 前缀匹配会把下面两个当成回环；必须是完整的 IPv4 字面量 + 合法字节。
  for (const host of ['127.0.0.1.evil.com', '127.0.0.256', '127.example.com']) {
    assert.equal(isLoopbackHost(host), false, host);
  }
});

test('createSingleFlight coalesces overlapping polling calls', async () => {
  let calls = 0;
  let release;
  const deferred = new Promise((resolve) => { release = resolve; });
  const run = createSingleFlight(async () => {
    calls += 1;
    await deferred;
    return calls;
  });

  const first = run();
  const second = run();

  assert.strictEqual(first, second);
  assert.equal(calls, 0);
  await Promise.resolve();
  assert.equal(calls, 1);
  release();
  assert.equal(await first, 1);
});

test('boundedAppend removes old log nodes beyond the hard limit', () => {
  const container = {
    children: [],
    appendChild(node) { this.children.push(node); },
    removeChild(node) { this.children.splice(this.children.indexOf(node), 1); },
    get firstElementChild() { return this.children[0] ?? null; },
  };

  for (let index = 0; index < 5; index += 1) {
    boundedAppend(container, { index }, 3);
  }

  assert.deepEqual(container.children.map((item) => item.index), [2, 3, 4]);
});

test('normalizeHttpUrl accepts HTTP origins and rejects script URLs', () => {
  assert.equal(normalizeHttpUrl('http://127.0.0.1:8080'), 'http://127.0.0.1:8080/');
  assert.equal(normalizeHttpUrl('https://example.test/chat'), 'https://example.test/chat');
  assert.equal(normalizeHttpUrl('javascript:alert(1)'), null);
  assert.equal(normalizeHttpUrl('file:///C:/secret.txt'), null);
  assert.equal(normalizeHttpUrl('not a url'), null);
});

test('createReconnectDelays grows exponentially and caps at the maximum', () => {
  const delays = createReconnectDelays();
  assert.equal(delays.recordFailure(), 1000);
  assert.equal(delays.recordFailure(), 2000);
  assert.equal(delays.recordFailure(), 4000);
  assert.equal(delays.recordFailure(), 8000);
  assert.equal(delays.recordFailure(), 15000);
  assert.equal(delays.recordFailure(), 15000);
  assert.equal(delays.failures, 6);
});

test('createReconnectDelays resets after a successful connection', () => {
  const delays = createReconnectDelays();
  delays.recordFailure();
  delays.recordFailure();
  delays.recordSuccess();
  assert.equal(delays.failures, 0);
  assert.equal(delays.recordFailure(), 1000);
});

test('createReconnectDelays honors custom bounds', () => {
  const delays = createReconnectDelays({ baseMs: 500, maxMs: 4000 });
  assert.equal(delays.recordFailure(), 500);
  assert.equal(delays.recordFailure(), 1000);
  assert.equal(delays.recordFailure(), 2000);
  assert.equal(delays.recordFailure(), 4000);
  assert.equal(delays.recordFailure(), 4000);
});

test('inside Tauri, a missing verified session never falls back to a hardcoded port', async () => {
  // 后端重启窗口里 get_backend_session 返回 null；旧实现会回落到
  // 127.0.0.1:17520，并把缓存里的令牌发给那个端口上恰好存在的任意进程。
  // 新实现必须在发出任何请求之前就抛错（fetch 调用数必须为 0）。
  const calls = [];
  globalThis.window = {
    location: {
      protocol: 'http:',
      hostname: 'tauri.localhost',
      origin: 'http://tauri.localhost',
    },
    __TAURI__: { core: { invoke: async () => null } },
  };
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return new Response('{}', {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  };
  try {
    const { pathToFileURL } = await import('node:url');
    const { join } = await import('node:path');
    const jsDir = join(import.meta.dirname, '..', 'js');
    // 独立模块实例：前面的用例可能已经把 _backendBase 缓存成兜底端口。
    const api = (await import(pathToFileURL(join(jsDir, 'api.js')).href + '?no-verified-session'))
      .default;
    await assert.rejects(() => api.getSystemConfig(), /后端|backend|restart/i);
    assert.equal(calls.length, 0, `不应发出请求: ${calls}`);
  } finally {
    delete globalThis.window;
    delete globalThis.fetch;
  }
});

test('tauri.localhost never hijacks the backend-URL fallback (F24)', async () => {
  // Tauri webview 的地址也是 http(s)://tauri.localhost，它满足
  // protocol.startsWith('http') 却不是后端 —— 不排除的话默认端口兜底永远
  // 不可达。用 stub fetch 观察真实请求 URL，而不是测内部函数。
  const calls = [];
  globalThis.window = {
    location: {
      protocol: 'http:',
      hostname: 'tauri.localhost',
      origin: 'http://tauri.localhost',
    },
  };
  // Node 24 自带只读的 globalThis.navigator（有 language），i18n 直接可用，
  // 不要尝试覆写它。
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return new Response('{}', {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  };
  try {
    const { pathToFileURL } = await import('node:url');
    const { join } = await import('node:path');
    const jsDir = join(import.meta.dirname, '..', 'js');
    const api = (await import(pathToFileURL(join(jsDir, 'api.js')).href)).default;

    await api.getSystemConfig();

    assert.equal(calls.length, 1);
    assert.ok(
      calls[0].startsWith('http://127.0.0.1:17520/'),
      `请求没有落到默认端口兜底: ${calls[0]}`,
    );
  } finally {
    delete globalThis.window;
    delete globalThis.fetch;
  }
});

test('a failed non-idempotent request is not retried automatically', async () => {
  // POST/PUT/DELETE 可能已经在后端执行过；网络错误后自动重试会创建重复
  // 实例/预设。只允许 GET/HEAD 自动重试。
  const calls = [];
  globalThis.window = {
    location: {
      protocol: 'http:',
      hostname: 'tauri.localhost',
      origin: 'http://tauri.localhost',
    },
    __TAURI__: {
      core: {
        invoke: async () => ({ url: 'http://127.0.0.1:23456', token: 'test-token' }),
      },
    },
  };
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), method: options && options.method });
    throw new Error('connection reset after send');
  };
  try {
    const { pathToFileURL } = await import('node:url');
    const { join } = await import('node:path');
    const jsDir = join(import.meta.dirname, '..', 'js');
    const api = (
      await import(pathToFileURL(join(jsDir, 'api.js')).href + '?no-idempotent-retry')
    ).default;

    await assert.rejects(() => api.createInstance({ params: { model: 'm.gguf' } }));
    assert.equal(calls.length, 1, `POST 被自动重试了: ${JSON.stringify(calls)}`);
    assert.equal(calls[0].method, 'POST');
  } finally {
    delete globalThis.window;
    delete globalThis.fetch;
  }
});

test('createParamView escapes values and marks unsupported/wide controls', () => {
  const view = createParamView({
    escapeHtml,
    t: (key) => key,
    locale: 'en-US',
    getValues: () => ({ model: 'a"<b', ctx_size: 4096 }),
  });
  const text = view.paramHtml({
    key: 'model',
    label: '模型文件路径',
    label_en: 'Model file',
    type: 'text',
    supported: true,
    cli: '-m',
  });
  assert.match(text, /Model file/);
  assert.match(text, /a&quot;&lt;b/);
  assert.match(text, /param-field-wide/);
  assert.match(text, /data-key="model"/);

  const broken = view.paramHtml({
    key: 'broken',
    label: 'Broken',
    type: 'text',
    supported: false,
  });
  assert.match(broken, /param-field-unsupported/);
  assert.match(broken, /disabled/);

  const multiple = view.controlHtml({
    key: 'spec_type',
    label: 'Spec',
    type: 'select',
    multiple: true,
    options: ['a', 'b'],
    option_labels: { a: 'A（desc）' },
    supported: true,
  });
  assert.match(multiple, /option-card/);
  assert.match(multiple, /data-opt="a"/);
  assert.match(multiple, /A/);
});

test('isWideField reserves full rows for path/text/multi controls', () => {
  assert.equal(isWideField({ type: 'text' }), true);
  assert.equal(isWideField({ type: 'file' }), true);
  assert.equal(isWideField({ type: 'select', multiple: true }), true);
  assert.equal(isWideField({ type: 'number' }), false);
  assert.equal(isWideField({ type: 'switch' }), false);
});

test('groupModelsByKey keeps groups in first-seen order and buckets unkeyed files', () => {
  const models = [
    { path: 'a', manifest: { model_key: 'org/a' } },
    { path: 'b', manifest: { model_key: 'org/a' } },
    { path: 'c', manifest: null },
    { path: 'd', manifest: { model_key: 'org/d' } },
  ];
  const groups = groupModelsByKey(models, (m) => m.manifest && m.manifest.model_key);
  assert.deepEqual(
    groups.map((g) => [g.key, g.models.map((m) => m.path)]),
    [
      ['org/a', ['a', 'b']],
      ['', ['c']],
      ['org/d', ['d']],
    ]
  );
});

test('diffModelDefaults stores only explicit changes and never managed keys', () => {
  const defaults = { ctx_size: 8192, temperature: 0.8, threads: -1, port: 8080 };
  const current = { ctx_size: 16384, temperature: 0.8, threads: -1, port: 9000, model: 'x.gguf' };

  const changed = diffModelDefaults(current, defaults, ['port', 'model']);

  assert.deepEqual(changed, { ctx_size: 16384 });
});

test('parseNumericInput keeps intermediate typing states out of the value model', () => {
  // 关键回归：参数编辑器不能把 "-" / "0." 这类输入中间态规范化后再写回输入框，
  // 否则键入 0.75 会变成 750，键入 -1 会变成 1。
  assert.deepEqual(parseNumericInput('', 'number'), { value: null, valid: true });
  assert.deepEqual(parseNumericInput('0.75', 'float'), { value: 0.75, valid: true });
  assert.deepEqual(parseNumericInput('0.', 'float'), { value: 0, valid: true });
  assert.deepEqual(parseNumericInput('-', 'number'), { value: null, valid: false });
  assert.deepEqual(parseNumericInput('-1', 'number'), { value: -1, valid: true });
  assert.deepEqual(parseNumericInput('1e', 'number'), { value: null, valid: false });
  assert.deepEqual(parseNumericInput(' 8192 ', 'number'), { value: 8192, valid: true });
});

test('hasUsableServer accepts a registered runtime when the global path is empty', async () => {
  const { hasUsableServer } = await import('../js/configured_state.mjs');
  // 全局路径填了 → 已配置，不必再看注册表
  assert.equal(hasUsableServer({ serverPath: 'D:/llama/llama-server.exe', runtimes: [] }), true);
  // 全局路径空、但注册过构建 → 也算已配置（这正是 4.1 的坑）
  assert.equal(
    hasUsableServer({ serverPath: '', runtimes: [{ executable_path: 'D:/a/llama-server.exe' }] }),
    true
  );
  // 探测失败的 profile 仍然算：探测只回答"支持哪些 flag"，不影响能否启动
  assert.equal(
    hasUsableServer({
      serverPath: '  ',
      runtimes: [{ executable_path: 'D:/a/x.exe', probe: { probed: false } }],
    }),
    true
  );
  // 两者都没有 → 才该提示未配置
  assert.equal(hasUsableServer({ serverPath: '', runtimes: [] }), false);
  assert.equal(hasUsableServer({ serverPath: undefined, runtimes: undefined }), false);
  // 列表里有空壳记录不算已配置
  assert.equal(hasUsableServer({ serverPath: '', runtimes: [{ executable_path: '' }] }), false);
});


test('createRequestGate only accepts the latest request', () => {
  const gate = createRequestGate();
  const first = gate.begin();
  const second = gate.begin();
  assert.equal(gate.isCurrent(first), false);
  assert.equal(gate.isCurrent(second), true);
  gate.cancel();
  assert.equal(gate.isCurrent(second), false);
});

test('formatApiErrorPayload turns FastAPI detail arrays into actionable text', () => {
  assert.equal(formatApiErrorPayload(null, 'fallback'), 'fallback');
  assert.equal(formatApiErrorPayload('plain', 'fallback'), 'plain');
  assert.equal(formatApiErrorPayload({ error: 'boom' }, 'fallback'), 'boom');
  assert.equal(formatApiErrorPayload({ detail: 'simple detail' }, 'fallback'), 'simple detail');
  assert.equal(
    formatApiErrorPayload(
      { detail: [{ loc: ['query', 'q'], msg: 'too long' }, { msg: 'second' }] },
      'fallback'
    ),
    'q: too long; second'
  );
});

test('instanceNameFromFilename is bounded and never empty for a filename', () => {
  assert.equal(instanceNameFromFilename('a'.repeat(120)).length, 100);
  assert.equal(instanceNameFromFilename('  model.gguf  '), 'model.gguf');
  assert.equal(instanceNameFromFilename(''), '');
});

test('scheduleObjectUrlRevoke defers revocation for WebView2 downloads', async () => {
  let revoked = '';
  scheduleObjectUrlRevoke('blob:x', (url) => { revoked = url; }, 0);
  assert.equal(revoked, '');
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(revoked, 'blob:x');
});

test('classifyLogFrame keeps control frames out of the log buffer', () => {
  assert.deepEqual(classifyLogFrame(null), { kind: 'ignore' });
  assert.deepEqual(classifyLogFrame({ type: 'ping' }), { kind: 'heartbeat' });
  assert.deepEqual(classifyLogFrame({ type: 'logs_dropped', count: 3 }), {
    kind: 'dropped',
    count: 3,
  });
  assert.deepEqual(classifyLogFrame({ type: 'logs_dropped', count: -2 }), {
    kind: 'dropped',
    count: 0,
  });
  assert.deepEqual(classifyLogFrame({ timestamp: 1, line: 'hello' }), { kind: 'line' });
});


test('modal actions await asynchronous results before deciding to close', async () => {
  let finished = false;
  const result = await settleModalAction({
    onClick: async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      finished = true;
      return false;
    },
  });
  assert.equal(finished, true);
  assert.equal(result, false);
  assert.equal(shouldCloseAfterAction(result, false), false);
  assert.equal(shouldCloseAfterAction(true, false), true);
  assert.equal(shouldCloseAfterAction(undefined, false), true);
  assert.equal(shouldCloseAfterAction(true, true), false);
});


test('setButtonBusy disables a control and exposes aria-busy', () => {
  const attrs = new Map();
  const button = {
    disabled: false,
    setAttribute(name, value) { attrs.set(name, String(value)); },
    removeAttribute(name) { attrs.delete(name); },
  };
  setButtonBusy(button, true);
  assert.equal(button.disabled, true);
  assert.equal(attrs.get('aria-busy'), 'true');
  setButtonBusy(button, false);
  assert.equal(button.disabled, false);
  assert.equal(attrs.has('aria-busy'), false);
  assert.doesNotThrow(() => setButtonBusy(null, true));
});
