// Chat 背景图：缩放参数、编码降级与 CSS 图层计算。
//
// 与头像共用"选中一张图"的思路，但**参数完全不同**：头像是 128×128 的小图标，
// 背景铺满整页。沿用头像参数会把背景糊成色块，所以这里单独一套常量，且不与
// avatar.mjs 的导出去共享 —— 两者将来任何一方调整都不该悄悄改到另一方。
//
// 真正不可单测的部分（canvas 缩放）留在设置页；这里只放能断言的判定逻辑。

/** 背景图长边上限。1920 足够覆盖常见 1080p/2.5K 屏，再大只是白占 config。 */
export const BG_MAX_WIDTH = 1920;
export const BG_MAX_HEIGHT = 1080;
/** 与 backend/config.py 的 CHAT_BACKGROUND_MAX_BYTES 保持一致（后端会再校验）。 */
export const BG_MAX_BYTES = 400 * 1024;
/** 只接受这三种：SVG 可以内嵌脚本、GIF/BMP 体积不可控。 */
export const BG_ACCEPT = 'image/png,image/jpeg,image/webp';
/** JPEG 质量 0.82 是"看得出细节、又不超预算"的实测折中。 */
export const BG_QUALITY = 0.82;
export const BG_OPACITY_DEFAULT = 0.35;
export const BG_OPACITY_RANGE = [0, 1];
export const BG_BLUR_RANGE = [0, 40];

/**
 * 等比缩放（只缩不放，且长边不超过 1920 与宽 1920/高 1080 的两个约束）。
 *
 * 只缩不放的理由同头像：把 800×600 拉到 1920 只会得到一张模糊的图，还让
 * config 变大。这里额外按 **宽高各自的上限** 计算，而不是只看长边：
 * 一张 2000×500 的横幅长边超限要缩，一张 1000×2000 的竖图下半截会被
 * `background-size: cover` 裁掉，但它本身没超预算，不该被无谓地重新编码。
 */
export function fitBackgroundSize(width, height) {
  const w = Number(width);
  const h = Number(height);
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) {
    return { width: BG_MAX_WIDTH, height: BG_MAX_HEIGHT, scaled: false };
  }
  const ratio = Math.min(BG_MAX_WIDTH / w, BG_MAX_HEIGHT / h, 1);
  if (ratio >= 1) {
    return { width: Math.round(w), height: Math.round(h), scaled: false };
  }
  return {
    width: Math.max(1, Math.round(w * ratio)),
    height: Math.max(1, Math.round(h * ratio)),
    scaled: true,
  };
}

/** UTF-8 字节数（data URL 是纯 ASCII，长度即字节数）。 */
export function backgroundByteLength(text) {
  const value = String(text || '');
  if (typeof TextEncoder !== 'undefined') {
    return new TextEncoder().encode(value).length;
  }
  return value.length;
}

/**
 * 从若干编码尝试里挑第一个"非空且够小"的。
 *
 * 降质顺序由调用方给（先 JPEG 0.82，再 0.7、0.6，最后 PNG 兜底）。返回失败
 * 时不抛异常而是给出 reason：调用方要把它呈现成可读的错误，而不是一个
 * 控制台里的 TypeError。
 *
 * @param {Array<{type: string, quality: number, dataUrl: string}>} attempts
 * @param {number} maxBytes
 */
export function pickBackgroundEncoding(attempts, maxBytes = BG_MAX_BYTES) {
  const list = Array.isArray(attempts) ? attempts : [];
  for (const attempt of list) {
    const dataUrl = String(attempt?.dataUrl || '');
    // `data:,` 是 canvas 编码失败的特征（空内容）
    if (!/^data:image\/(jpeg|png|webp);base64,/.test(dataUrl)) continue;
    if (dataUrl.length < 128) continue;
    if (backgroundByteLength(dataUrl) > maxBytes) continue;
    return { ok: true, dataUrl, type: attempt?.type || '', quality: attempt?.quality ?? null };
  }
  return { ok: false, dataUrl: '', reason: 'cannot-encode-within-limit' };
}

/**
 * 校验一个已存的背景值能否安全地放进 CSS `url(...)`。
 *
 * 与 avatar.mjs 的 classifyAvatar 同一套安全理由：非 `data:image/*` 的 URL
 * 会让界面去请求远程地址（隐私泄漏 + 断网即失效），`javascript:` 是注入面。
 */
export function classifyBackground(value) {
  const raw = typeof value === 'string' ? value.trim() : '';
  if (!raw) return { kind: 'empty', value: '', reason: '' };
  if (raw.startsWith('data:')) {
    if (!/^data:image\/(png|jpeg|jpg|webp);base64,/i.test(raw)) {
      return { kind: 'invalid', value: raw, reason: 'unsupported-data-url' };
    }
    if (backgroundByteLength(raw) > BG_MAX_BYTES) {
      return { kind: 'invalid', value: raw, reason: 'too-large' };
    }
    return { kind: 'image', value: raw, reason: '' };
  }
  return { kind: 'invalid', value: raw, reason: 'not-a-data-url' };
}

/**
 * 把 0–1 的不透明度夹进合法区间；缺失值回落到默认值。
 *
 * 必须显式挡掉 null/''/undefined：``Number(null)`` 是 **0**，而 0 表示"完全不
 * 遮"，也就是把正文直接压在图上。缺值本该回落到 0.35，静默变成 0 的话，界面
 * 上看不出错，只觉得"字看不清了"。
 */
export function clampBackgroundOpacity(value) {
  if (value === null || value === undefined || value === '') return BG_OPACITY_DEFAULT;
  const number = Number(value);
  if (!Number.isFinite(number)) return BG_OPACITY_DEFAULT;
  const [low, high] = BG_OPACITY_RANGE;
  return Math.min(high, Math.max(low, number));
}

/** 把模糊半径夹进 0–40 并取整；缺失值回落 0（0 与默认值一致，风险低）。 */
export function clampBackgroundBlur(value) {
  if (value === null || value === undefined || value === '') return 0;
  const number = Number(value);
  if (!Number.isFinite(number)) return 0;
  const [low, high] = BG_BLUR_RANGE;
  return Math.min(high, Math.max(low, Math.round(number)));
}

/**
 * 生成 CSS 变量值，供 style.css 里的 `::before` 图层使用。
 *
 * 返回 `null` 表示"没有背景"，调用方应移除该变量而不是给一个空 url() ——
 * 空 url() 在某些 WebView 上会被当成相对路径去请求当前页面，产生一条
 * 无意义的 404。
 *
 * @returns {{imageCss: string, opacity: string, blur: string}|null}
 */
export function backgroundLayerStyle(image, opacity, blur) {
  const classified = classifyBackground(image);
  if (classified.kind !== 'image') return null;
  const safeOpacity = clampBackgroundOpacity(opacity);
  const safeBlur = clampBackgroundBlur(blur);
  // 用双引号包裹 data URL：base64 里可能出现 `+`/`/`/`=`，但不能出现 `"`，
  // 所以不需要转义。双引号比单引号更安全 —— CSS 里两者等价，但 HTML 属性
  // 常见用双引号包裹，选双引号时内容里出现单引号也不会破坏语法。
  return {
    imageCss: `url("${classified.value}")`,
    opacity: String(safeOpacity),
    blur: `${safeBlur}px`,
  };
}
