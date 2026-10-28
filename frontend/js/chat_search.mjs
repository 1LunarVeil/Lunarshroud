// Pure highlighting for local conversation search results.
//
// The backend returns already-bounded snippets; this only splits them into
// escaped-in-the-view parts, so callers must still escape every segment before
// inserting it into HTML.

export function highlightParts(text, query) {
  const value = String(text || '');
  const needle = String(query || '').trim();
  if (!needle || !value) return [{ text: value, match: false }];
  const haystack = value.toLocaleLowerCase();
  const lower = needle.toLocaleLowerCase();
  const parts = [];
  let cursor = 0;
  while (cursor < value.length) {
    const index = haystack.indexOf(lower, cursor);
    if (index === -1) break;
    if (index > cursor) {
      parts.push({ text: value.slice(cursor, index), match: false });
    }
    parts.push({ text: value.slice(index, index + needle.length), match: true });
    cursor = index + needle.length;
  }
  if (!parts.length) return [{ text: value, match: false }];
  if (cursor < value.length) parts.push({ text: value.slice(cursor), match: false });
  return parts;
}
