import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useUiStore, resolveTheme } from '@/lib/stores/ui';
import { useInstanceFeed } from '@/lib/stores/instances';
import { systemApi } from '@/lib/api';
import { translate } from '@/lib/i18n';
import { AppShell } from '@/app/AppShell';

/**
 * App root: owns the global effects that must exist exactly once.
 *
 * - applies the resolved theme to `<html>` (so `tokens.css`'s `:root.theme-dark`
 *   block matches) and keeps it in sync with the OS when the preference is
 *   `system`
 * - keeps `<html lang>` in step with the locale, which is what screen readers use
 *   to pick a pronunciation for CJK text
 * - starts the instance feed (WebSocket + polling fallback)
 */
export function App() {
  const resolvedTheme = useUiStore((state) => state.resolvedTheme);
  const themePreference = useUiStore((state) => state.themePreference);
  const syncResolvedTheme = useUiStore((state) => state.syncResolvedTheme);
  const locale = useUiStore((state) => state.locale);
  const config = useQuery({ queryKey: ['system', 'config'], queryFn: systemApi.getConfig });

  useInstanceFeed();

  // Apply the theme. `theme-boot.js` did this before first paint; this keeps it
  // correct after the user changes the preference or the OS changes.
  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle('theme-dark', resolvedTheme === 'dark');
    root.style.colorScheme = resolvedTheme;
  }, [resolvedTheme]);

  /**
   * Apply the accent preset.
   *
   * A `data-accent` attribute rather than an inline custom property: the presets in
   * `tokens.css` are per-theme (a value that reads on white is wrong on black), and
   * an inline `--accent` would have to be recomputed on every theme change in JS.
   * With the attribute, the CSS cascade picks the right pair for the current theme.
   *
   * The value is validated on the backend (closed set) and again here by omission:
   * an unknown preset simply matches no rule, which leaves the defaults in place —
   * a bad stored value degrades to the shipped accent instead of to an unstyled
   * window.
   */
  useEffect(() => {
    const preset = config.data?.accent_preset;
    if (preset) document.documentElement.dataset.accent = preset;
  }, [config.data?.accent_preset]);

  // Seed layout widths from backend config where this machine has no stored value.
  // The keys exist for "move to another machine and keep your layout"; they were
  // never read before, so that promise was empty.
  const hydrateLayout = useUiStore((state) => state.hydrateLayoutFromConfig);
  useEffect(() => {
    if (!config.data) return;
    hydrateLayout({
      sidebar: config.data.chat_sidebar_width,
      panel: config.data.chat_panel_width,
    });
  }, [config.data, hydrateLayout]);

  // Same mechanism for the body font: a closed set in tokens.css, applied as an
  // attribute so the cascade (not JS) decides the stack.
  useEffect(() => {
    const preset = config.data?.font_preset;
    if (preset) document.documentElement.dataset.font = preset;
  }, [config.data?.font_preset]);

  /**
   * Whole-window wallpaper.
   *
   * Applied to `<body>` through two CSS custom properties rather than an inline
   * `background-image` on a React node: the wallpaper must sit **behind** every
   * surface (sidebar, toolbar, panels) without any of them having to know it exists.
   * `app.css` uses these to paint a fixed, aria-hidden layer under the app.
   *
   * Clearing the properties when the feature is off is deliberate — leaving a stale
   * `--wallpaper-image` behind would keep the image in memory and could paint it on
   * the next mount that forgot to reset.
   */
  useEffect(() => {
    const style = document.body.style;
    const image = config.data?.chat_background_image ?? '';
    const enabled = config.data?.background_whole_window === true && Boolean(image);
    if (!enabled) {
      style.removeProperty('--wallpaper-image');
      style.removeProperty('--wallpaper-opacity');
      style.removeProperty('--wallpaper-blur');
      style.removeProperty('--wallpaper-background');
      return;
    }
    const opacity = config.data?.chat_background_opacity ?? 0.35;
    const blur = config.data?.chat_background_blur ?? 0;
    style.setProperty('--wallpaper-image', `url("${image}")`);
    // The overlay opacity in the settings sheet means "how much theme colour covers
    // the picture", so the picture's own visibility is its complement.
    style.setProperty('--wallpaper-opacity', String(Math.max(0, Math.min(1, 1 - opacity))));
    style.setProperty('--wallpaper-blur', `${blur}px`);
    // Let the fixed layer show through the body ground.
    style.setProperty('--wallpaper-background', 'transparent');
  }, [
    config.data?.background_whole_window,
    config.data?.chat_background_image,
    config.data?.chat_background_opacity,
    config.data?.chat_background_blur,
  ]);

  // Follow the OS preference while the user is on "system".
  useEffect(() => {
    if (themePreference !== 'system') return;
    let media: MediaQueryList;
    try {
      media = window.matchMedia('(prefers-color-scheme: dark)');
    } catch {
      return;
    }
    const onChange = () => syncResolvedTheme();
    media.addEventListener('change', onChange);
    // Re-resolve on subscribe: the preference may have changed while a listener
    // from a previous mount was still installed.
    syncResolvedTheme();
    return () => media.removeEventListener('change', onChange);
  }, [themePreference, syncResolvedTheme]);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  /**
   * Storage health, polled once per mount.
   *
   * `backend/api/lifecycle.py` documents this endpoint as the thing that lets the UI
   * show a persistent warning at startup, and `system.py` says the same — but no
   * consumer was ever built, so "every save fails with a 500" was still invisible
   * until the user lost a conversation. The Developer tab was the only reader.
   */
  const storage = useQuery({
    queryKey: ['system', 'storage', 'banner'],
    queryFn: systemApi.storage,
    retry: false,
    staleTime: 60_000,
  });
  const storageBroken = storage.data?.writable === false;
  const storageReasons = (storage.data?.reasons ?? []).join('; ');

  return (
    <>
      {storageBroken ? (
        <div
          role="alert"
          data-testid="storage-warning"
          className="flex items-center gap-2 border-b border-[var(--danger)]/40 bg-[var(--danger-subtle)] px-3 py-1.5 text-[length:var(--text-xs)] text-[var(--danger)]"
        >
          {translate('settings.storageWarning', { reasons: storageReasons || '—' })}
        </div>
      ) : null}
      <AppShell />
    </>
  );
}

export { resolveTheme };
