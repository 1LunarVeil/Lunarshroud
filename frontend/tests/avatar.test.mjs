// 头像：缩放尺寸、值分类、编码挑选。
//
// 这三段是纯逻辑，能在没有 DOM/canvas 的环境里测 —— 而它们恰好是最容易
// 出安全/边界问题的地方（远程 URL、超长值、编码失败）。

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  AVATAR_MAX_BYTES,
  AVATAR_MAX_EDGE,
  byteLength,
  classifyAvatar,
  fitAvatarSize,
  pickEncodedAvatar,
} from '../js/avatar.mjs';

test('fitAvatarSize only ever shrinks, never upscales', () => {
  // 32×32 拉到 128 只会得到一张糊图，还白占空间。
  assert.deepEqual(fitAvatarSize(32, 32), { width: 32, height: 32, scaled: false });
  assert.deepEqual(fitAvatarSize(128, 128), { width: 128, height: 128, scaled: false });
});

test('fitAvatarSize keeps the aspect ratio when shrinking', () => {
  const wide = fitAvatarSize(400, 100);
  assert.equal(wide.scaled, true);
  assert.equal(Math.max(wide.width, wide.height), AVATAR_MAX_EDGE);
  assert.ok(Math.abs(wide.width / wide.height - 4) < 0.05, '4:1 must stay ~4:1');

  const tall = fitAvatarSize(100, 400);
  assert.equal(Math.max(tall.width, tall.height), AVATAR_MAX_EDGE);
  assert.ok(Math.abs(tall.height / tall.width - 4) < 0.05);
});

test('fitAvatarSize survives nonsense dimensions', () => {
  for (const [w, h] of [[0, 0], [-1, 5], [NaN, 10], [undefined, undefined]]) {
    const size = fitAvatarSize(w, h);
    assert.ok(size.width >= 1 && size.height >= 1, `bad input ${w}x${h}`);
  }
});

test('classifyAvatar accepts short text and image data URLs', () => {
  assert.equal(classifyAvatar('').kind, 'empty');
  assert.equal(classifyAvatar('🦙').kind, 'text');
  assert.equal(classifyAvatar('AI').kind, 'text');
  assert.equal(
    classifyAvatar('data:image/png;base64,iVBORw0KGgo=').kind,
    'image'
  );
  assert.equal(
    classifyAvatar('data:image/jpeg;base64,/9j/4AAQ').kind,
    'image'
  );
});

test('classifyAvatar rejects remote URLs and other schemes', () => {
  // 安全要求：http(s) 会让界面去请求远程地址（隐私泄漏 + 断网裂图），
  // javascript: 是注入面。两者都必须在数据进入渲染前就被拦下。
  for (const bad of [
    'http://example.com/a.png',
    'https://example.com/a.png',
    'javascript:alert(1)',
    'file:///C:/x.png',
    'ftp://x/y',
  ]) {
    const result = classifyAvatar(bad);
    assert.equal(result.kind, 'invalid', bad);
    assert.equal(result.reason, 'not-a-data-url', bad);
  }
});

test('classifyAvatar rejects non-image data URLs and oversized images', () => {
  assert.equal(classifyAvatar('data:text/html;base64,PHNjcmlwdD4=').kind, 'invalid');
  const huge = 'data:image/png;base64,' + 'A'.repeat(AVATAR_MAX_BYTES + 16);
  const result = classifyAvatar(huge);
  assert.equal(result.kind, 'invalid');
  assert.equal(result.reason, 'too-large');
});

test('classifyAvatar rejects over-long text and control characters', () => {
  assert.equal(classifyAvatar('abcdefghij').kind, 'invalid'); // > 8
  assert.equal(classifyAvatar('ok').kind, 'text');
  // U+202E 能把整行视觉顺序反转；换行会在导出里伪造结构
  assert.equal(classifyAvatar('\u202eevil').kind, 'invalid');
  assert.equal(classifyAvatar('a\nb').kind, 'invalid');
});

test('pickEncodedAvatar skips empty canvas output and oversized results', () => {
  // WebP 在部分 WebView2 上 canvas.toDataURL 会返回 `data:,`（空图）。
  // 直接把它当成功会存进去一个坏值，界面上是一片空白。
  const empty = pickEncodedAvatar([{ type: 'image/webp', quality: 0.9, dataUrl: 'data:,' }]);
  assert.equal(empty.ok, false);

  const good = 'data:image/webp;base64,' + 'A'.repeat(200);
  const picked = pickEncodedAvatar([
    { type: 'image/webp', quality: 0.9, dataUrl: 'data:,' },
    { type: 'image/webp', quality: 0.6, dataUrl: good },
  ]);
  assert.equal(picked.ok, true);
  assert.equal(picked.dataUrl, good);

  // 全部超限 → 失败，调用方报错而不是存坏值
  const tooBig = 'data:image/png;base64,' + 'A'.repeat(AVATAR_MAX_BYTES + 100);
  assert.equal(pickEncodedAvatar([{ dataUrl: tooBig }]).ok, false);
  assert.equal(pickEncodedAvatar([]).ok, false);
  assert.equal(pickEncodedAvatar(null).ok, false);
});

test('byteLength counts UTF-8 bytes, not code units', () => {
  assert.equal(byteLength('abc'), 3);
  // emoji 是 4 字节 / 2 个 UTF-16 code unit —— 用 .length 会低估
  assert.equal(byteLength('🦙'), 4);
});
