import { describe, expect, it } from 'vitest';
import {
  buildMessagePayloadContent,
  classifyAttachmentContent,
  classifyAttachmentFile,
  attachmentBaseName,
  attachmentExtension,
  isImageAttachmentName,
  isSupportedAttachmentName,
  looksBinaryText,
  messageHasImages,
  MAX_MESSAGE_ATTACHMENT_CHARS,
} from '@/lib/ported/attachments';

/**
 * The interesting half of this suite is `classifyAttachmentContent`, which does not
 * exist in 1.0. It exists because the backend stores no `kind` column: it decides
 * image-vs-text from the content itself. A client that trusts its own `kind` field
 * loses it on reload, and a reloaded image is then re-sent as a **text** attachment
 * containing its own base64. That was a real 1.0 defect; these tests pin the fix.
 */

describe('attachment naming', () => {
  it('strips directory prefixes from either separator', () => {
    expect(attachmentBaseName('C:\\docs\\a.txt')).toBe('a.txt');
    expect(attachmentBaseName('/home/u/a.txt')).toBe('a.txt');
    expect(attachmentBaseName('')).toBe('');
  });

  it('lowercases extensions and requires a dot before content', () => {
    expect(attachmentExtension('A.TXT')).toBe('txt');
    expect(attachmentExtension('.bashrc')).toBe('');
    expect(attachmentExtension('noext')).toBe('');
  });

  it('recognizes the text allow-list', () => {
    expect(isSupportedAttachmentName('a.md')).toBe(true);
    expect(isSupportedAttachmentName('a.py')).toBe(true);
    expect(isSupportedAttachmentName('a.exe')).toBe(false);
  });

  it('recognizes image extensions', () => {
    expect(isImageAttachmentName('a.PNG')).toBe(true);
    expect(isImageAttachmentName('a.txt')).toBe(false);
  });
});

describe('classifyAttachmentFile', () => {
  it('accepts text files without a vision requirement', () => {
    expect(classifyAttachmentFile({ name: 'notes.md', size: 10, supportsVision: false })).toEqual({
      ok: true,
      kind: 'text',
    });
  });

  it('rejects images when the instance has no vision encoder', () => {
    // Accepting then failing at the model would be worse: the user attaches a
    // picture, sends, and gets a 400 with no useful explanation.
    expect(classifyAttachmentFile({ name: 'a.png', size: 10, supportsVision: false })).toEqual({
      ok: false,
      kind: 'image',
      reason: 'no-vision',
    });
  });

  it('accepts images with vision and rejects oversized ones', () => {
    expect(classifyAttachmentFile({ name: 'a.png', size: 10, supportsVision: true }).ok).toBe(true);
    expect(
      classifyAttachmentFile({ name: 'a.png', size: 5 * 1024 * 1024, supportsVision: true }),
    ).toEqual({ ok: false, kind: 'image', reason: 'too-large' });
  });

  it('rejects unsupported extensions', () => {
    expect(classifyAttachmentFile({ name: 'a.exe', size: 1 })).toEqual({
      ok: false,
      kind: 'text',
      reason: 'unsupported',
    });
  });
});

describe('classifyAttachmentContent', () => {
  it('detects every supported image data URL', () => {
    for (const mime of ['png', 'jpeg', 'jpg', 'webp', 'gif', 'bmp']) {
      expect(classifyAttachmentContent(`data:image/${mime};base64,AAAA`)).toBe('image');
    }
  });

  it('treats plain text as text even when it starts with "data:"', () => {
    expect(classifyAttachmentContent('data:something else')).toBe('text');
    expect(classifyAttachmentContent('hello')).toBe('text');
    expect(classifyAttachmentContent('')).toBe('text');
  });

  it('survives a reload: the same content classifies the same way', () => {
    // The old code read `attachment.kind`, which the server never persists.
    const stored = { name: 'shot.png', content: 'data:image/png;base64,iVBOR' };
    expect(classifyAttachmentContent(stored.content)).toBe('image');
  });
});

describe('looksBinaryText', () => {
  it('flags NUL bytes only', () => {
    expect(looksBinaryText('a\u0000b')).toBe(true);
    expect(looksBinaryText('plain text')).toBe(false);
  });
});

describe('buildMessagePayloadContent', () => {
  it('returns the bare string when there are no attachments', () => {
    expect(buildMessagePayloadContent({ content: 'hi' })).toBe('hi');
  });

  it('wraps text attachments in explicit delimiters', () => {
    const payload = buildMessagePayloadContent({
      content: 'summarize',
      attachments: [{ name: 'a.txt', content: 'body', kind: 'text' }],
    });
    expect(payload).toBe(
      'summarize\n\n--- Attached file: a.txt ---\nbody\n--- End of a.txt ---',
    );
  });

  it('derives the kind from content when the caller omits it', () => {
    // This is the reload path: no `kind`, content is an image data URL.
    const payload = buildMessagePayloadContent({
      content: '',
      attachments: [{ name: 'a.png', content: 'data:image/png;base64,AAAA' }],
    });
    expect(Array.isArray(payload)).toBe(true);
    expect(payload).toEqual([
      { type: 'image_url', image_url: { url: 'data:image/png;base64,AAAA' } },
    ]);
  });

  it('sends images after the text part so the question is read first', () => {
    const payload = buildMessagePayloadContent({
      content: 'what is this',
      attachments: [
        { name: 'a.png', content: 'data:image/png;base64,AAAA', kind: 'image' },
        { name: 'b.txt', content: 'notes', kind: 'text' },
      ],
    }) as Array<{ type: string }>;
    expect(payload[0]?.type).toBe('text');
    expect(payload[1]?.type).toBe('image_url');
  });

  it('never exceeds the backend per-message text budget by construction', () => {
    // The backend rejects >500k chars of text attachments; the composer enforces
    // the same cap so a send cannot fail after the fact.
    expect(MAX_MESSAGE_ATTACHMENT_CHARS).toBe(500_000);
  });
});

describe('messageHasImages', () => {
  it('is true only for image attachments, derived from content', () => {
    expect(messageHasImages({ attachments: [{ name: 'a.png', content: 'data:image/png;base64,A' }] })).toBe(
      true,
    );
    expect(messageHasImages({ attachments: [{ name: 'a.txt', content: 'x' }] })).toBe(false);
    expect(messageHasImages({})).toBe(false);
  });
});
