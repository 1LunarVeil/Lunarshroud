/**
 * Errors thrown by the API layer.
 *
 * `status` is preserved so callers can branch on 401/404/409/503 instead of
 * parsing a message string. `payload` keeps the raw FastAPI body: the settings
 * UI needs `detail` arrays to render per-field errors, and the storage layer's
 * 503 carries a machine-readable `code`.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

/** FastAPI puts validation problems in `detail` as an array of objects. */
interface ValidationItem {
  loc?: unknown;
  msg?: unknown;
  type?: unknown;
}

function describeValidationItem(item: ValidationItem): string {
  const location = Array.isArray(item.loc)
    ? item.loc.filter((part) => part !== 'body').join('.')
    : '';
  const message = typeof item.msg === 'string' ? item.msg : JSON.stringify(item.msg ?? item);
  return location ? `${location}: ${message}` : message;
}

/**
 * Turn any error payload into one readable line.
 *
 * 1.0 shipped `String(detail)` on a FastAPI array and users got
 * "[object Object]". Everything here has an explicit branch, and the fallback
 * never stringifies an object blindly.
 */
export function formatApiErrorPayload(payload: unknown, fallback: string): string {
  if (payload === null || payload === undefined || payload === '') return fallback;

  if (typeof payload === 'string') return payload;

  if (Array.isArray(payload)) {
    const parts = payload
      .filter((item): item is ValidationItem => typeof item === 'object' && item !== null)
      .map(describeValidationItem)
      .filter(Boolean);
    return parts.length ? parts.join('; ') : fallback;
  }

  if (typeof payload === 'object') {
    const record = payload as Record<string, unknown>;
    // Storage layer errors: { error, code, ... }. The code is the actionable part.
    if (typeof record.error === 'string' && record.error) {
      return typeof record.code === 'string' && record.code
        ? `${record.error} (${record.code})`
        : record.error;
    }
    if (typeof record.message === 'string' && record.message) return record.message;
    if (record.detail !== undefined) return formatApiErrorPayload(record.detail, fallback);
  }

  return fallback;
}
