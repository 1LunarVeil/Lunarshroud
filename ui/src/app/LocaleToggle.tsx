import { Languages } from 'lucide-react';
import { useUiStore } from '@/lib/stores/ui';
import { LOCALE_NAMES, translate } from '@/lib/i18n';
import { cn } from '@/lib/utils';

/**
 * Toggles between the two shipped locales.
 *
 * No page reload: 1.0 reloaded on language change, which discarded an in-flight
 * stream and any unsent draft. Here the locale is store state, so React re-renders
 * and everything that reads `translate()` updates in place.
 */
export function LocaleToggle() {
  const locale = useUiStore((state) => state.locale);
  const setLocale = useUiStore((state) => state.setLocale);
  const next = locale === 'zh-CN' ? 'en-US' : 'zh-CN';

  return (
    <button
      type="button"
      data-testid="locale-toggle"
      onClick={() => setLocale(next)}
      aria-label={translate('app.language', { language: LOCALE_NAMES[next] })}
      title={LOCALE_NAMES[next]}
      className={cn(
        'flex h-8 items-center gap-1.5 rounded-[var(--radius-md)] px-2',
        'text-[length:var(--text-xs)] font-medium text-[length:var(--text-sm)] text-[var(--text-secondary)]',
        'hover:bg-[var(--hover)] hover:text-[var(--text)]',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--border-focus)]',
      )}
    >
      <Languages size={15} aria-hidden="true" />
      {locale === 'zh-CN' ? '中' : 'EN'}
    </button>
  );
}
