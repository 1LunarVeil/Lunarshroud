import test from 'node:test';
import assert from 'node:assert/strict';

import {
  boundedAppend,
  createReconnectDelays,
  createSingleFlight,
  isLoopbackHost,
  mergeUpstreamOptions,
  normalizeHttpUrl,
} from '../js/runtime.mjs';

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
  // 不可达（审计 F24）。用 stub fetch 观察真实请求 URL，而不是测内部函数。
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
