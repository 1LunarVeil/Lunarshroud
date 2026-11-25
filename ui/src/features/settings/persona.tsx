import { useRef, useState } from 'react';
import { ImagePlus, Trash2 } from 'lucide-react';
import { translate } from '@/lib/i18n';
import { Button } from '@/components/ui/Button';
import { Field } from '@/components/ui/Input';
import type { AppConfig } from '@/lib/api/types';

/**
 * Chat identity + background image editors.
 *
 * Both are stored as `data:` URLs in `config.json` — the backend validates that
 * (only `data:image/*`, size caps, control-character rejection) and nothing here
 * invents a second channel. There is no upload endpoint and no file on disk,
 * which is why a rejection here is final: the alternative would be a file
 * lifecycle (orphans, cleanup, permission) for a decoration.
 *
 * The encoding parameters mirror 1.0's `avatar.mjs` / `chat_background.mjs`.
 * They are re-derived here rather than imported because those modules were
 * deleted with the 1.0 frontend; the values are pinned by the backend limits
 * (`CHAT_AVATAR_MAX_BYTES` = 64 KiB, `CHAT_BACKGROUND_MAX_BYTES` = 400 KiB).
 */

const AVATAR_MAX_EDGE = 128;
const AVATAR_MAX_BYTES = 64 * 1024;
const BACKGROUND_MAX_WIDTH = 1920;
const BACKGROUND_MAX_HEIGHT = 1080;
const BACKGROUND_MAX_BYTES = 400 * 1024;
const BACKGROUND_QUALITY = 0.82;

interface Identity {
  userName: string;
  assistantName: string;
  userAvatar: string;
  assistantAvatar: string;
}

export function identityFromConfig(config: AppConfig): Identity {
  return {
    userName: config.chat_user_name ?? '',
    assistantName: config.chat_assistant_name ?? '',
    userAvatar: config.chat_user_avatar ?? '',
    assistantAvatar: config.chat_assistant_avatar ?? '',
  };
}

/** Approximate decoded byte length of a data URL (base64 → 3 bytes per 4 chars). */
function dataUrlBytes(value: string): number {
  const comma = value.indexOf(',');
  if (comma < 0) return value.length;
  const payload = value.slice(comma + 1);
  return Math.floor((payload.length * 3) / 4);
}

function loadImage(file: File): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
      URL.revokeObjectURL(url);
      resolve(image);
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('decode failed'));
    };
    image.src = url;
  });
}

/** Scale to fit a box without ever upscaling. */
function fitSize(width: number, height: number, maxWidth: number, maxHeight: number) {
  const scale = Math.min(1, maxWidth / width, maxHeight / height);
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

/**
 * Encode an image, trying progressively lower quality until it fits the budget.
 *
 * `canvas.toDataURL` can return the literal string `"data:,"` on some WebView2
 * builds when the requested type is unsupported; that is treated as a failure
 * rather than stored, because a blank avatar looks like a bug in the app.
 */
function encodeFitting(
  image: HTMLImageElement,
  size: { width: number; height: number },
  maxBytes: number,
  qualities: number[],
  type: string,
): string | null {
  const canvas = document.createElement('canvas');
  canvas.width = size.width;
  canvas.height = size.height;
  const context = canvas.getContext('2d');
  if (!context) return null;
  context.drawImage(image, 0, 0, size.width, size.height);
  for (const quality of qualities) {
    const encoded = canvas.toDataURL(type, quality);
    if (encoded.length <= 5) continue; // "data:,"
    if (dataUrlBytes(encoded) <= maxBytes) return encoded;
  }
  return null;
}

export async function fileToAvatarDataUrl(file: File): Promise<string | null> {
  if (!file.type.startsWith('image/')) return null;
  const image = await loadImage(file);
  const size = fitSize(image.naturalWidth, image.naturalHeight, AVATAR_MAX_EDGE, AVATAR_MAX_EDGE);
  for (const type of ['image/webp', 'image/png']) {
    const encoded = encodeFitting(image, size, AVATAR_MAX_BYTES, [0.9, 0.75, 0.6, 0.45], type);
    if (encoded) return encoded;
  }
  return null;
}

export async function fileToBackgroundDataUrl(file: File): Promise<string | null> {
  if (!file.type.startsWith('image/')) return null;
  const image = await loadImage(file);
  const size = fitSize(
    image.naturalWidth,
    image.naturalHeight,
    BACKGROUND_MAX_WIDTH,
    BACKGROUND_MAX_HEIGHT,
  );
  return encodeFitting(
    image,
    size,
    BACKGROUND_MAX_BYTES,
    [BACKGROUND_QUALITY, 0.7, 0.55, 0.4],
    'image/jpeg',
  );
}

/** Renders an avatar value: an uploaded image, an emoji, or nothing. */
export function AvatarPreview({ value, label }: { value: string; label: string }) {
  if (value.startsWith('data:image/')) {
    return (
      <img
        src={value}
        alt={label}
        className="size-10 shrink-0 rounded-full object-cover"
        draggable={false}
      />
    );
  }
  return (
    <span
      aria-hidden="true"
      className="grid size-10 shrink-0 place-items-center rounded-full bg-[var(--surface-sunken)] text-[length:var(--text-sm)]"
    >
      {value || translate('settings.chatAvatarEmpty')}
    </span>
  );
}

export function AvatarField({
  label,
  value,
  onChange,
  onError,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  onError: (message: string) => void;
}) {
  const input = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);

  const pick = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    try {
      const encoded = await fileToAvatarDataUrl(file);
      if (!encoded) {
        onError(translate('settings.chatAvatarEncodeFailed'));
        return;
      }
      onChange(encoded);
    } finally {
      setBusy(false);
      if (input.current) input.current.value = '';
    }
  };

  return (
    <Field label={label} hint={translate('settings.chatAvatarHelp')}>
      <div className="flex items-center gap-2">
        <AvatarPreview value={value} label={label} />
        <Button
          type="button"
          variant="secondary"
          size="sm"
          loading={busy}
          onClick={() => input.current?.click()}
        >
          <ImagePlus size={13} aria-hidden="true" />
          {translate('settings.chatAvatarUpload')}
        </Button>
        {value ? (
          <Button type="button" variant="ghost" size="sm" onClick={() => onChange('')}>
            <Trash2 size={13} aria-hidden="true" />
            {translate('settings.chatAvatarClear')}
          </Button>
        ) : null}
        <input
          ref={input}
          type="file"
          accept="image/png,image/jpeg,image/webp"
          className="sr-only"
          onChange={(event) => void pick(event.target.files?.[0])}
        />
      </div>
    </Field>
  );
}

export function BackgroundField({
  image,
  opacity,
  blur,
  onChange,
  onError,
}: {
  image: string;
  opacity: number;
  blur: number;
  onChange: (changes: Partial<AppConfig>) => void;
  onError: (message: string) => void;
}) {
  const input = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);

  const pick = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    try {
      const encoded = await fileToBackgroundDataUrl(file);
      if (!encoded) {
        onError(translate('settings.chatBackgroundEncodeFailed'));
        return;
      }
      onChange({ chat_background_image: encoded });
    } finally {
      setBusy(false);
      if (input.current) input.current.value = '';
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <Field label={translate('settings.chatBackground')} hint={translate('settings.chatBackgroundHelp')}>
        <div className="flex flex-wrap items-center gap-2">
          {image ? (
            <img
              src={image}
              alt={translate('settings.chatBackgroundPreview')}
              className="h-12 w-20 rounded-[var(--radius-sm)] border border-[var(--border)] object-cover"
            />
          ) : (
            <span className="grid h-12 w-20 place-items-center rounded-[var(--radius-sm)] border border-dashed border-[var(--border-control)] text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
              {translate('settings.chatBackgroundNone')}
            </span>
          )}
          <Button type="button" variant="secondary" size="sm" loading={busy} onClick={() => input.current?.click()}>
            <ImagePlus size={13} aria-hidden="true" />
            {translate('settings.chatBackgroundUpload')}
          </Button>
          {image ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => onChange({ chat_background_image: '' })}
            >
              <Trash2 size={13} aria-hidden="true" />
              {translate('settings.chatAvatarClear')}
            </Button>
          ) : null}
          <input
            ref={input}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            className="sr-only"
            onChange={(event) => void pick(event.target.files?.[0])}
          />
        </div>
      </Field>

      <Field
        label={translate('settings.chatBackgroundOpacity')}
        hint={translate('settings.chatBackgroundOpacityHelp')}
        htmlFor="settings-background-opacity"
      >
        <div className="flex items-center gap-3">
          <input
            id="settings-background-opacity"
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={opacity}
            onChange={(event) => onChange({ chat_background_opacity: Number(event.target.value) })}
            className="flex-1 accent-[var(--accent)]"
          />
          <span className="w-10 text-end text-[length:var(--text-xs)] tnum text-[var(--text-secondary)]">
            {opacity.toFixed(2)}
          </span>
        </div>
      </Field>

      <Field
        label={translate('settings.chatBackgroundBlur')}
        hint={translate('settings.chatBackgroundBlurHelp')}
        htmlFor="settings-background-blur"
      >
        <div className="flex items-center gap-3">
          <input
            id="settings-background-blur"
            type="range"
            min={0}
            max={40}
            step={1}
            value={blur}
            onChange={(event) => onChange({ chat_background_blur: Number(event.target.value) })}
            className="flex-1 accent-[var(--accent)]"
          />
          <span className="w-10 text-end text-[length:var(--text-xs)] tnum text-[var(--text-secondary)]">
            {blur}px
          </span>
        </div>
      </Field>
    </div>
  );
}
