import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/** Merge conditional class names, letting later Tailwind utilities win. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

const HTML_ESCAPES: Readonly<Record<string, string>> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

/**
 * Escape a value for interpolation into an HTML string.
 *
 * React escapes text nodes automatically, so this exists only for the few
 * places that must build markup by hand (the code-block header, and citation
 * snippets). Prefer React children everywhere else.
 */
export function escapeHtml(value: unknown): string {
  return String(value ?? '').replace(/[&<>"']/g, (char) => HTML_ESCAPES[char] ?? char);
}

/** Last path segment, accepting both separators. */
export function basename(path: string | null | undefined): string {
  if (!path) return '';
  const parts = String(path).replace(/\\/g, '/').split('/');
  return parts[parts.length - 1] ?? '';
}

/** Byte size with a fixed number of decimals per unit. `-` for missing values. */
export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let index = 0;
  let scaled = Math.abs(value);
  while (scaled >= 1024 && index < units.length - 1) {
    scaled /= 1024;
    index += 1;
  }
  const sign = value < 0 ? '-' : '';
  return `${sign}${scaled.toFixed(index === 0 ? 0 : 2)} ${units[index]}`;
}

/**
 * Compact duration. `0` is a legitimate value ("just started"), so it must not
 * collapse to a dash — 1.0 shipped that bug and the dashboard showed "-" for a
 * freshly started instance.
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '-';
  const total = Number(seconds);
  if (Number.isNaN(total) || total < 0) return '-';
  const whole = Math.floor(total);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  if (hours > 0) return `${hours}h${minutes}m`;
  if (minutes > 0) return `${minutes}m${secs}s`;
  return `${secs}s`;
}

/**
 * Normalize a timestamp to a `Date`.
 *
 * The backend returns **epoch seconds as a number** from SQLite (`chats.db`), while
 * a few older payload shapes and fixtures use ISO strings. Accepting both in one
 * place keeps every formatter honest: `new Date('1795415701.33')` is `Invalid Date`,
 * so a formatter that only understood strings printed "-" for every real record.
 */
export function toDate(value: string | number | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    // Values below ~1e11 are epoch seconds; above are milliseconds.
    const ms = value < 1e11 ? value * 1000 : value;
    const date = new Date(ms);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  const numeric = Number(value);
  if (Number.isFinite(numeric) && value.trim() !== '') return toDate(numeric);
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Locale-aware timestamp; falls back to the raw value when unparseable. */
export function formatTime(
  value: string | number | null | undefined,
  locale = 'zh-CN',
): string {
  const date = toDate(value);
  if (!date) return value === null || value === undefined || value === '' ? '-' : String(value);
  try {
    return date.toLocaleString(locale === 'zh-CN' ? 'zh-CN' : 'en-US', { hour12: false });
  } catch {
    return String(value);
  }
}

/** Relative time for list rows: "just now", "12m", "3h", "5d". */
export function formatRelative(
  value: string | number | null | undefined,
  now = Date.now(),
): string {
  const date = toDate(value);
  if (!date) return '';
  const then = date.getTime();
  const delta = Math.max(0, now - then);
  const minutes = Math.floor(delta / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d`;
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

const HEX_PATTERN = /^#?([0-9a-f]{6})$/i;

/** 0xRRGGBB → {r,g,b}. */
export function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  const matched = hex.trim().match(HEX_PATTERN);
  if (!matched || !matched[1]) return null;
  const value = matched[1];
  return {
    r: parseInt(value.slice(0, 2), 16),
    g: parseInt(value.slice(2, 4), 16),
    b: parseInt(value.slice(4, 6), 16),
  };
}

function srgbChannel(value: number): number {
  const scaled = value / 255;
  return scaled <= 0.04045 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4;
}

/** WCAG relative luminance. */
export function relativeLuminance(hex: string): number {
  const rgb = hexToRgb(hex);
  if (!rgb) return 0;
  return (
    0.2126 * srgbChannel(rgb.r) +
    0.7152 * srgbChannel(rgb.g) +
    0.0722 * srgbChannel(rgb.b)
  );
}

/** WCAG contrast ratio between two hex colours (1–21). */
export function contrastRatio(a: string, b: string): number {
  const first = relativeLuminance(a);
  const second = relativeLuminance(b);
  const lighter = Math.max(first, second);
  const darker = Math.min(first, second);
  return (lighter + 0.05) / (darker + 0.05);
}

/** Clamp a number into a range, tolerating NaN by returning the minimum. */
export function clamp(value: number, min: number, max: number): number {
  if (Number.isNaN(value)) return min;
  return Math.min(max, Math.max(min, value));
}

/** Stable, collision-free id for optimistic rows. */
export function makeId(prefix = 'id'): string {
  const random = Math.random().toString(36).slice(2, 10);
  return `${prefix}-${Date.now().toString(36)}-${random}`;
}
