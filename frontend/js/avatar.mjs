// 头像图片：缩放参数、data URL 校验与社会形象判断。
//
// 为什么是 data URL 而不是文件上传：CSP 是 `script-src 'self'`、前端零构建，
// 接一个真正的上传通道要同时解决磁盘路径、MIME 嗅探、清理与孤儿文件回收；
// 而头像是一张 128×128 的小图，塞进 config.json 完全够用（base64 后 ~20 KB）。
//
// 缩放必须在**前端**做：用户随手截的图可能有 4000×3000、几 MB，直接存进
// config 会让每次 GET /api/system/config 都拖着一个巨大的字符串。

/** 头像边长上限。128 在 Retina 下显示 64 CSS px 仍然清晰。 */
export const AVATAR_MAX_EDGE = 128;
/** 与 backend/config.py 的 CHAT_AVATAR_MAX_BYTES 保持一致（后端会再校验一次）。 */
export const AVATAR_MAX_BYTES = 64 * 1024;
export const AVATAR_ACCEPT = 'image/png,image/jpeg,image/webp,image/gif';

/**
 * 算等比缩放后的尺寸。
 *
 * 只缩不放：把 32×32 拉到 128×128 只会得到一张糊图，还白占空间。
 */
export function fitAvatarSize(width, height, maxEdge = AVATAR_MAX_EDGE) {
  const w = Number(width);
  const h = Number(height);
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) {
    return { width: maxEdge, height: maxEdge, scaled: false };
  }
  const longest = Math.max(w, h);
  if (longest <= maxEdge) {
    return { width: Math.round(w), height: Math.round(h), scaled: false };
  }
  const ratio = maxEdge / longest;
  return {
    width: Math.max(1, Math.round(w * ratio)),
    height: Math.max(1, Math.round(h * ratio)),
    scaled: true,
  };
}

/**
 * 校验一个头像值能否安全地放进 `<img src>` / `<div>text</div>`。
 *
 * 拒绝非 `data:image/*` 的 URL 是**安全要求**不是洁癖：
 * - `http(s)://` 会让界面去请求远程地址 → 隐私泄漏（谁在看你的头像）
 *   且断网就变成裂图；
 * - `javascript:` 放进 href/src 是注入面；
 * - 裸文本过长会把气泡撑破。
 *
 * @returns {{kind: 'empty'|'image'|'text'|'invalid', value: string, reason?: string}}
 */
export function classifyAvatar(value, textMax = 8) {
  const raw = typeof value === 'string' ? value.trim() : '';
  if (!raw) return { kind: 'empty', value: '' };
  if (raw.startsWith('data:')) {
    if (!/^data:image\/(png|jpeg|jpg|webp|gif);base64,/i.test(raw)) {
      return { kind: 'invalid', value: raw, reason: 'unsupported-data-url' };
    }
    if (byteLength(raw) > AVATAR_MAX_BYTES) {
      return { kind: 'invalid', value: raw, reason: 'too-large' };
    }
    return { kind: 'image', value: raw };
  }
  // 有协议头的任何东西都不是我们要的文本头像
  if (/^[a-z][a-z0-9+.-]*:/i.test(raw)) {
    return { kind: 'invalid', value: raw, reason: 'not-a-data-url' };
  }
  if (Array.from(raw).length > textMax) {
    return { kind: 'invalid', value: raw, reason: 'too-long' };
  }
  // 控制字符 + 双向文本覆盖符。后者（U+202A-U+202E、U+2066-U+2069）不在
  // C0 控制字符区间里，但能把整行文字的**视觉顺序**反转 —— 一个头像字段
  // 没有任何理由需要它们。
  if (/[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069\u2028\u2029]/.test(raw)) {
    return { kind: 'invalid', value: raw, reason: 'control-chars' };
  }
  return { kind: 'text', value: raw };
}

/** UTF-8 字节数；data URL 是纯 ASCII，但用同一套算法避免两处不一致。 */
export function byteLength(text) {
  const value = String(text || '');
  if (typeof TextEncoder !== 'undefined') {
    return new TextEncoder().encode(value).length;
  }
  // 极老的 WebView 没有 TextEncoder：base64 是 ASCII，长度即字节数
  return value.length;
}

/**
 * 画布编码失败时的降级顺序。
 *
 * WebP 在部分 WebView2 版本上 canvas.toDataURL 会返回 `data:,`（空图）；
 * 逐级回退到 PNG —— PNG 处处可用，只是大一点。全部失败就返回空串，
 * 调用方据此报错而不是存一个坏值。
 */
export const AVATAR_ENCODE_CANDIDATES = ['image/webp', 'image/png'];

export const AVATAR_ENCODE_QUALITIES = [0.9, 0.75, 0.6, 0.45];

/**
 * 从一组候选编码里挑第一个"够小且非空"的结果。
 *
 * 抽成纯函数是因为真正的 canvas 编码在单测里跑不了（没有 DOM），
 * 而"挑选与降级"这段逻辑恰恰是最容易写错的地方。
 *
 * @param {Array<{type: string, quality: number, dataUrl: string}>} attempts
 * @returns {{ok: boolean, dataUrl: string, reason?: string}}
 */
export function pickEncodedAvatar(attempts) {
  const list = Array.isArray(attempts) ? attempts : [];
  for (const attempt of list) {
    const dataUrl = String(attempt?.dataUrl || '');
    // `data:,` 是 canvas 编码失败的特征（空内容）
    if (!dataUrl.startsWith('data:image/') || dataUrl.length < 64) continue;
    if (byteLength(dataUrl) > AVATAR_MAX_BYTES) continue;
    return { ok: true, dataUrl };
  }
  return { ok: false, dataUrl: '', reason: 'cannot-encode-within-limit' };
}
