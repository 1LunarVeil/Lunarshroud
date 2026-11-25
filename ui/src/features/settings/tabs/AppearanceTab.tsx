import { Languages } from 'lucide-react';
import { useState } from 'react';
import { LOCALE_NAMES, type Locale } from '@/lib/i18n';
import { useUiStore, type ThemePreference } from '@/lib/stores/ui';
import { useConfigForm, SaveRow } from '@/features/settings/useConfigForm';
import { Field, Input, Switch } from '@/components/ui/Input';
import { SectionHeader } from '@/components/ui/Feedback';
import { translate } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { identityFromConfig, AvatarField, BackgroundField } from '@/features/settings/persona';
import { Laptop, Moon, Sun } from 'lucide-react';

/**
 * The accent presets, in the order the picker shows them.
 *
 * No colour values live here: each swatch sets `data-accent` on **itself**, so the
 * cascade in `tokens.css` gives it that preset's `--accent` for the current theme.
 * That is what keeps AGENTS §2.36 true (colours only in `tokens.css`) and it also
 * means the dots automatically show the dark-theme variant when the theme changes.
 *
 * A preset added here without a matching CSS block simply renders an unstyled dot,
 * and `test_ui_tokens.py` fails because it requires a block per preset name.
 */
const ACCENT_PRESETS = ['blue', 'violet', 'teal', 'amber', 'rose', 'green'] as const;
/** Body font presets; each is a full stack in tokens.css with CJK fallbacks. */
const FONT_PRESETS = ['system', 'serif', 'mono'] as const;

/**
 * Appearance: theme, language, chat identity and the chat background.
 *
 * Theme and language live in the UI store (localStorage) because they decide how
 * the shell paints before the backend answers; everything else is backend config.
 */
export function AppearanceTab() {
  const { draft, patch, save, saving, dirty, config } = useConfigForm();
  const preference = useUiStore((state) => state.themePreference);
  const setPreference = useUiStore((state) => state.setThemePreference);
  const locale = useUiStore((state) => state.locale);
  const setLocale = useUiStore((state) => state.setLocale);
  const [identityError, setIdentityError] = useState<string | null>(null);

  const themes: Array<{ id: ThemePreference; labelKey: string; icon: typeof Sun }> = [
    { id: 'system', labelKey: 'settings.themeSystem', icon: Laptop },
    { id: 'light', labelKey: 'settings.themeLight', icon: Sun },
    { id: 'dark', labelKey: 'settings.themeDark', icon: Moon },
  ];

  if (config.isPending || !draft) {
    return <SectionHeader title={translate('settings.catAppearance')} />;
  }

  const identity = identityFromConfig(draft);

  return (
    <div className="flex flex-col gap-6">
      <SectionHeader
        title={translate('settings.catAppearance')}
        description={translate('settings.appearanceSubtitle')}
      />

      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
          {translate('settings.theme')}
        </legend>
        <div className="flex gap-2">
          {themes.map((entry) => {
            const Icon = entry.icon;
            const active = preference === entry.id;
            return (
              <button
                key={entry.id}
                type="button"
                onClick={() => setPreference(entry.id)}
                aria-pressed={active}
                className={cn(
                  'flex h-[var(--control-h-lg)] flex-1 items-center justify-center gap-1.5 rounded-[var(--radius-md)] border',
                  'text-[length:var(--text-sm)]',
                  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--border-focus)]',
                  active
                    ? 'border-[var(--accent)] bg-[var(--accent-subtle)] font-medium text-[var(--accent-text)]'
                    : 'border-[var(--border-control)] bg-[var(--surface)] text-[var(--text-secondary)] hover:bg-[var(--hover)]',
                )}
              >
                <Icon size={15} aria-hidden="true" />
                {translate(entry.labelKey)}
              </button>
            );
          })}
        </div>
        <p className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
          {translate('settings.themeHelp')}
        </p>
      </fieldset>

      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
          {translate('settings.language')}
        </legend>
        <div className="flex gap-2">
          {(['zh-CN', 'en-US'] as Locale[]).map((id) => (
            <button
              key={id}
              type="button"
              onClick={() => setLocale(id)}
              aria-pressed={locale === id}
              className={cn(
                'flex h-[var(--control-h)] flex-1 items-center justify-center gap-1.5 rounded-[var(--radius-md)] border',
                'text-[length:var(--text-sm)]',
                locale === id
                  ? 'border-[var(--accent)] bg-[var(--accent-subtle)] font-medium text-[var(--accent-text)]'
                  : 'border-[var(--border-control)] bg-[var(--surface)] text-[var(--text-secondary)] hover:bg-[var(--hover)]',
              )}
            >
              <Languages size={14} aria-hidden="true" />
              {LOCALE_NAMES[id]}
            </button>
          ))}
        </div>
      </fieldset>

      {/*
        Accent presets, not a colour picker. `--accent` is used both as a fill (text
        on top) and as text on the page surface, and those roles need different
        lightness — hence a hand-checked pair per preset per theme, recomputed by
        `test_ui_tokens.py`. A free picker could produce a value that fails AA in one
        of the two roles.

        Each swatch sets `data-accent` on itself so its dot is painted by the same
        CSS block the UI will use, with no colour literal in this file (AGENTS §2.36).
      */}
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
          {translate('settings.accentPreset')}
        </legend>
        <div className="flex flex-wrap gap-2">
          {ACCENT_PRESETS.map((preset) => {
            const active = (draft.accent_preset ?? 'blue') === preset;
            return (
              <button
                key={preset}
                type="button"
                aria-pressed={active}
                aria-label={translate(`settings.accent_${preset}`)}
                title={translate(`settings.accent_${preset}`)}
                onClick={() => patch({ accent_preset: preset })}
                data-testid={`accent-${preset}`}
                className={cn(
                  'grid size-8 place-items-center rounded-full border-2',
                  active
                    ? 'border-[var(--text)]'
                    : 'border-transparent hover:border-[var(--border-control)]',
                )}
              >
                <span
                  aria-hidden="true"
                  data-accent={preset}
                  className="block size-5 rounded-full bg-[var(--accent)]"
                />
              </button>
            );
          })}
        </div>
        <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
          {translate('settings.accentPresetHelp')}
        </p>
      </fieldset>

      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
          {translate('settings.fontPreset')}
        </legend>
        <div className="flex gap-2">
          {FONT_PRESETS.map((preset) => {
            const active = (draft.font_preset ?? 'system') === preset;
            return (
              <button
                key={preset}
                type="button"
                aria-pressed={active}
                data-testid={`font-${preset}`}
                onClick={() => patch({ font_preset: preset })}
                className={cn(
                  'min-h-[var(--control-h)] flex-1 rounded-[var(--radius-md)] border px-2 text-[length:var(--text-xs)]',
                  active
                    ? 'border-[var(--accent)] bg-[var(--accent-subtle)] font-medium text-[var(--accent-text)]'
                    : 'border-[var(--border-control)] text-[var(--text-secondary)] hover:bg-[var(--hover)]',
                )}
              >
                {translate(`settings.font_${preset}`)}
              </button>
            );
          })}
        </div>
        <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
          {translate('settings.fontPresetHelp')}
        </p>
      </fieldset>

      <section className="flex flex-col gap-4 border-t border-[var(--border)] pt-5">
        <SectionHeader title={translate('settings.identityTitle')} />
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={translate('settings.chatUserName')} hint={translate('settings.chatUserNameHelp')} htmlFor="settings-user-name">
            <Input
              id="settings-user-name"
              maxLength={32}
              value={identity.userName}
              placeholder={translate('settings.chatUserNamePlaceholder')}
              onChange={(event) => patch({ chat_user_name: event.target.value })}
            />
          </Field>
          <Field
            label={translate('settings.chatAssistantName')}
            hint={translate('settings.chatAssistantNameHelp')}
            htmlFor="settings-assistant-name"
          >
            <Input
              id="settings-assistant-name"
              maxLength={32}
              value={identity.assistantName}
              placeholder={translate('settings.chatAssistantNamePlaceholder')}
              onChange={(event) => patch({ chat_assistant_name: event.target.value })}
            />
          </Field>
          <AvatarField
            label={translate('settings.chatUserAvatar')}
            value={identity.userAvatar}
            onChange={(next) => patch({ chat_user_avatar: next })}
            onError={setIdentityError}
          />
          <AvatarField
            label={translate('settings.chatAssistantAvatar')}
            value={identity.assistantAvatar}
            onChange={(next) => patch({ chat_assistant_avatar: next })}
            onError={setIdentityError}
          />
        </div>
        {identityError ? (
          <p role="alert" className="text-[length:var(--text-xs)] text-[var(--danger)]">
            {identityError}
          </p>
        ) : null}
      </section>

      <section className="flex flex-col gap-4 border-t border-[var(--border)] pt-5">
        <BackgroundField
          image={draft.chat_background_image}
          opacity={draft.chat_background_opacity}
          blur={draft.chat_background_blur}
          onChange={(changes) => patch(changes)}
          onError={setIdentityError}
        />
        {/*
          Reuses the same image rather than adding a second one: two wallpapers would
          double the config payload and make "change the wallpaper" a two-step action.
          Disabled while no image is chosen — the switch would otherwise promise
          something with nothing to show.
        */}
        <Switch
          checked={Boolean(draft.background_whole_window)}
          onCheckedChange={(next) => patch({ background_whole_window: next })}
          disabled={!draft.chat_background_image}
          label={translate('settings.backgroundWholeWindow')}
          description={translate('settings.backgroundWholeWindowHelp')}
        />
      </section>

      <section className="flex flex-col gap-4 border-t border-[var(--border)] pt-5">
        <Switch
          checked={draft.chat_reasoning !== 'collapse'}
          onCheckedChange={(next) =>
            patch({ chat_reasoning: next ? 'auto' : 'collapse' })
          }
          label={translate('settings.chatReasoning')}
          description={translate('settings.chatReasoningHelp')}
        />
        <Field
          label={translate('settings.chatSidebarWidth')}
          hint={translate('settings.chatSidebarWidthHelp')}
          htmlFor="settings-sidebar-width"
        >
          <Input
            id="settings-sidebar-width"
            type="number"
            min={0}
            max={420}
            value={draft.chat_sidebar_width}
            onChange={(event) => patch({ chat_sidebar_width: Number(event.target.value) })}
          />
        </Field>
        <Field
          label={translate('settings.chatPanelWidth')}
          hint={translate('settings.chatPanelWidthHelp')}
          htmlFor="settings-panel-width"
        >
          <Input
            id="settings-panel-width"
            type="number"
            min={0}
            max={420}
            value={draft.chat_panel_width}
            onChange={(event) => patch({ chat_panel_width: Number(event.target.value) })}
          />
        </Field>
      </section>

      <SaveRow onSave={() => void save()} saving={saving} dirty={dirty} />
    </div>
  );
}
