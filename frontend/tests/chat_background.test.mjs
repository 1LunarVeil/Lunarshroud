import test from 'node:test';
import assert from 'node:assert/strict';

import {
  BG_BLUR_RANGE,
  BG_MAX_BYTES,
  BG_MAX_HEIGHT,
  BG_MAX_WIDTH,
  BG_OPACITY_DEFAULT,
  backgroundByteLength,
  backgroundLayerStyle,
  clampBackgroundBlur,
  clampBackgroundOpacity,
  classifyBackground,
  fitBackgroundSize,
  pickBackgroundEncoding,
} from '../js/chat_background.mjs';

/** 一个体积可控的合法 JPEG data URL。 */
function jpegOfLength(chars) {
  return 'data:image/jpeg;base64,' + 'A'.repeat(Math.max(0, chars - 23));
}

test('fitBackgroundSize only shrinks and honours both edges', () => {
  // 不放大
  assert.deepEqual(fitBackgroundSize(800, 600), {
    width: 800,
    height: 600,
    scaled: false,
  });
  // 宽超限：按宽缩，高同比
  const wide = fitBackgroundSize(3840, 1080);
  assert.equal(wide.scaled, true);
  assert.equal(wide.width, BG_MAX_WIDTH);
  assert.equal(wide.height, 540);
  // 高超限：按高缩
  const tall = fitBackgroundSize(1000, 4000);
  assert.equal(tall.height, BG_MAX_HEIGHT);
  assert.equal(tall.width, 270);
  // 两轴都超的正方形：被更严格的高度上限约束，落到 1080×1080
  // （不是 1920 —— 宽上限更宽松，但等比缩放不能两轴各取一个上限）
  const both = fitBackgroundSize(4000, 4000);
  assert.equal(both.width, 1080);
  assert.equal(both.height, BG_MAX_HEIGHT);
  // 非法输入回落到上限尺寸，而不是 NaN 画布
  for (const value of [0, -5, NaN, 'x', null, undefined]) {
    const size = fitBackgroundSize(value, 100);
    assert.equal(Number.isFinite(size.width), true);
    assert.equal(Number.isFinite(size.height), true);
    assert.equal(size.scaled, false);
  }
});

test('pickBackgroundEncoding takes the first candidate that is small enough', () => {
  const big = { type: 'image/jpeg', quality: 0.82, dataUrl: jpegOfLength(BG_MAX_BYTES + 500) };
  const small = { type: 'image/jpeg', quality: 0.6, dataUrl: jpegOfLength(5000) };
  assert.equal(pickBackgroundEncoding([big, small]).ok, true);
  assert.equal(pickBackgroundEncoding([big, small]).quality, 0.6);

  // 全部超预算
  assert.equal(pickBackgroundEncoding([big]).ok, false);
  assert.equal(pickBackgroundEncoding([big]).reason, 'cannot-encode-within-limit');

  // canvas 编码失败的特征：data:, 或过短
  assert.equal(pickBackgroundEncoding([{ dataUrl: 'data:,' }]).ok, false);
  assert.equal(pickBackgroundEncoding([{ dataUrl: jpegOfLength(20) }]).ok, false);
  // 非图片 data URL
  assert.equal(pickBackgroundEncoding([{ dataUrl: 'data:text/plain;base64,' + 'A'.repeat(200) }]).ok, false);
  // 非数组
  assert.equal(pickBackgroundEncoding(null).ok, false);
});

test('classifyBackground accepts only bounded local raster data URLs', () => {
  assert.deepEqual(classifyBackground(''), { kind: 'empty', value: '', reason: '' });
  assert.equal(classifyBackground('   ').kind, 'empty');
  assert.equal(classifyBackground(jpegOfLength(5000)).kind, 'image');
  assert.equal(classifyBackground('data:image/png;base64,' + 'A'.repeat(100)).kind, 'image');
  assert.equal(classifyBackground('data:image/webp;base64,' + 'A'.repeat(100)).kind, 'image');

  // 远程地址、伪协议、SVG（可带脚本）、非图片 data URL 全部拒绝
  for (const bad of [
    'https://example.com/bg.jpg',
    'javascript:alert(1)',
    'data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=',
    'data:text/html;base64,AAA',
  ]) {
    const verdict = classifyBackground(bad);
    assert.equal(verdict.kind, 'invalid', `expected rejection for ${bad}`);
    assert.ok(verdict.reason);
  }
  assert.equal(classifyBackground(jpegOfLength(BG_MAX_BYTES + 100)).reason, 'too-large');
});

test('clampBackgroundOpacity and clampBackgroundBlur keep values in range', () => {
  assert.equal(clampBackgroundOpacity(0), 0);
  assert.equal(clampBackgroundOpacity(1), 1);
  assert.equal(clampBackgroundOpacity(-1), 0);
  assert.equal(clampBackgroundOpacity(5), 1);
  assert.equal(clampBackgroundOpacity('0.5'), 0.5);
  // 非数字回落默认值，而不是 NaN（NaN 会让 CSS 变量整条失效）
  for (const bad of [NaN, undefined, null, 'x', {}]) {
    assert.equal(clampBackgroundOpacity(bad), BG_OPACITY_DEFAULT);
  }

  assert.equal(clampBackgroundBlur(0), 0);
  assert.equal(clampBackgroundBlur(40), 40);
  assert.equal(clampBackgroundBlur(-3), 0);
  assert.equal(clampBackgroundBlur(999), BG_BLUR_RANGE[1]);
  assert.equal(clampBackgroundBlur(3.7), 4);
  for (const bad of [NaN, undefined, 'x']) {
    assert.equal(clampBackgroundBlur(bad), 0);
  }
});

test('backgroundLayerStyle returns null without an image and CSS values with one', () => {
  assert.equal(backgroundLayerStyle('', 0.35, 0), null);
  assert.equal(backgroundLayerStyle('https://example.com/x.jpg', 0.35, 0), null);

  const layer = backgroundLayerStyle(jpegOfLength(5000), 0.5, 8);
  assert.ok(layer);
  assert.match(layer.imageCss, /^url\("data:image\/jpeg;base64,/);
  assert.equal(layer.opacity, '0.5');
  assert.equal(layer.blur, '8px');

  // 越界值被夹取而不是原样写进 CSS
  const clamped = backgroundLayerStyle(jpegOfLength(5000), 9, 500);
  assert.equal(clamped.opacity, '1');
  assert.equal(clamped.blur, '40px');
});

test('backgroundByteLength counts UTF-8 bytes', () => {
  assert.equal(backgroundByteLength('abc'), 3);
  assert.equal(backgroundByteLength('中文'), 6);
  assert.equal(backgroundByteLength(''), 0);
  assert.equal(backgroundByteLength(null), 0);
});
