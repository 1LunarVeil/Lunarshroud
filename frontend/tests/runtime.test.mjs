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
