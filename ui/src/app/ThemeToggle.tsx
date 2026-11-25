import { Monitor, Moon, Sun } from 'lucide-react';
import { useUiStore, type ThemePreference } from '@/lib/stores/ui';
import { translate } from '@/lib/i18n';
import { cn } from '@/lib/utils';

const ORDER: ThemePreference[] = ['system', 'light', 'dark'];

const ICONS = {
  system: Monitor,
  light: Sun,
  dark: Moon,
} as const;

/**
 * Cycles system → light → dark.
 *
 * A three-state cycle instead of a two-state toggle: "follow the OS" is the
 * default and must remain reachable. A plain light/dark switch silently opts the
 * user out of the OS setting and there is no way back without opening settings.
 */
export function ThemeToggle() {
  const preference = useUiStore((state) => state.themePreference);
  const setPreference = useUiStore((state) => state.setThemePreference);
  const resolved = useUiStore((state) => state.resolvedTheme);

  const next = ORDER[(ORDER.indexOf(preference) + 1) % ORDER.length] ?? 'system';
  const Icon = ICONS[preference];

  return (
    <button
      type="button"
      data-testid="theme-toggle"
      onClick={() => setPreference(next)}
      aria-label={translate('app.themeToggle', { current: translate(`app.theme.${preference}`) })}
      title={`${translate('app.theme.' + preference)} · ${translate(`app.theme.${preference}`)}`}
      className={cn(
        'grid size-8 place-items-center rounded-[var(--radius-md)]',
        'text-[length:var(--text-sm)] text-[var(--text-secondary)] hover:bg-[var(--hover)] hover:text-[var(--text)]',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--border-focus)]',
      )}
    >
      <Icon size={16} aria-hidden="true" />
      <span className="sr-only">{resolved}</span>
    </button>
  );
}
