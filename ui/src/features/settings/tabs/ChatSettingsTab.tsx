import { translate } from '@/lib/i18n';
import { useConfigForm, SaveRow } from '@/features/settings/useConfigForm';
import { Field, Input, Switch } from '@/components/ui/Input';
import { SectionHeader } from '@/components/ui/Feedback';
import { MemoryEditor } from '@/features/settings/tabs/MemoryEditor';
import { SkillSettings } from '@/features/settings/tabs/SkillSettings';

/**
 * Chat preferences.
 *
 * The three-state `chat_reasoning` control is the one that changed behaviour in
 * this round: it was already a config key and already persisted, but `ChatThread`
 * hard-coded `"auto"` for every message, so changing it did nothing.
 */
export function ChatSettingsTab() {
  const { draft, patch, save, saving, dirty, config } = useConfigForm();
  if (config.isPending || !draft) {
    return <SectionHeader title={translate('settings.catChat')} />;
  }

  return (
    <div className="flex flex-col gap-5">
      <SectionHeader
        title={translate('settings.catChat')}
        description={translate('settings.chatSubtitle')}
      />

      <Field
        label={translate('settings.chatReasoning')}
        hint={translate('settings.chatReasoningHelp')}
        htmlFor="settings-chat-reasoning"
      >
        <div className="flex gap-2">
          {(
            [
              ['auto', 'settings.chatReasoningAuto'],
              ['expand', 'settings.chatReasoningExpand'],
              ['collapse', 'settings.chatReasoningCollapse'],
            ] as const
          ).map(([value, key]) => {
            const active = (draft.chat_reasoning ?? 'auto') === value;
            return (
              <button
                key={value}
                type="button"
                id={value === 'auto' ? 'settings-chat-reasoning' : undefined}
                aria-pressed={active}
                onClick={() => patch({ chat_reasoning: value })}
                className={
                  'min-h-[var(--control-h)] flex-1 rounded-[var(--radius-md)] border px-2 text-[length:var(--text-xs)] ' +
                  (active
                    ? 'border-[var(--accent)] bg-[var(--accent-subtle)] font-medium text-[var(--accent-text)]'
                    : 'border-[var(--border-control)] text-[var(--text-secondary)] hover:bg-[var(--hover)]')
                }
              >
                {translate(key)}
              </button>
            );
          })}
        </div>
      </Field>

      {/*
        Display preferences for the composer and the transcript. Both are stored in
        the backend config rather than localStorage: a layout habit should not be
        lost by clearing the webview cache, and the same reasoning applies to the
        sidebar widths stored a few fields above.
      */}
      <Field
        label={translate('settings.chatSendKey')}
        hint={translate('settings.chatSendKeyHelp')}
        htmlFor="settings-chat-send-key"
      >
        <div className="flex gap-2">
          {(
            [
              ['enter', 'settings.chatSendKeyEnter'],
              ['ctrl-enter', 'settings.chatSendKeyCtrlEnter'],
            ] as const
          ).map(([value, key]) => {
            const active = (draft.chat_send_key ?? 'enter') === value;
            return (
              <button
                key={value}
                type="button"
                id={value === 'enter' ? 'settings-chat-send-key' : undefined}
                aria-pressed={active}
                onClick={() => patch({ chat_send_key: value })}
                className={
                  'min-h-[var(--control-h)] flex-1 rounded-[var(--radius-md)] border px-2 text-[length:var(--text-xs)] ' +
                  (active
                    ? 'border-[var(--accent)] bg-[var(--accent-subtle)] font-medium text-[var(--accent-text)]'
                    : 'border-[var(--border-control)] text-[var(--text-secondary)] hover:bg-[var(--hover)]')
                }
              >
                {translate(key)}
              </button>
            );
          })}
        </div>
      </Field>

      <Field
        label={translate('settings.chatShowStats')}
        hint={translate('settings.chatShowStatsHelp')}
        htmlFor="settings-chat-show-stats"
      >
        <div className="flex gap-2">
          {(
            [
              ['always', 'settings.chatShowStatsAlways'],
              ['never', 'settings.chatShowStatsNever'],
            ] as const
          ).map(([value, key]) => {
            const active = (draft.chat_show_stats ?? 'always') === value;
            return (
              <button
                key={value}
                type="button"
                id={value === 'always' ? 'settings-chat-show-stats' : undefined}
                aria-pressed={active}
                onClick={() => patch({ chat_show_stats: value })}
                className={
                  'min-h-[var(--control-h)] flex-1 rounded-[var(--radius-md)] border px-2 text-[length:var(--text-xs)] ' +
                  (active
                    ? 'border-[var(--accent)] bg-[var(--accent-subtle)] font-medium text-[var(--accent-text)]'
                    : 'border-[var(--border-control)] text-[var(--text-secondary)] hover:bg-[var(--hover)]')
                }
              >
                {translate(key)}
              </button>
            );
          })}
        </div>
      </Field>

      <Switch
        checked={Boolean(draft.chat_show_timestamps)}
        onCheckedChange={(next) => patch({ chat_show_timestamps: next })}
        label={translate('settings.chatShowTimestamps')}
        description={translate('settings.chatShowTimestampsHelp')}
      />

      <div className="grid gap-3 sm:grid-cols-2">
        <Field
          label={translate('settings.startupTimeout')}
          hint={translate('settings.startupTimeoutHelp')}
          htmlFor="settings-startup-timeout"
        >
          <Input
            id="settings-startup-timeout"
            type="number"
            min={5}
            value={draft.startup_timeout}
            onChange={(event) => patch({ startup_timeout: Number(event.target.value) })}
          />
        </Field>
        <Field
          label={translate('settings.healthInterval')}
          hint={translate('settings.healthIntervalHelp')}
          htmlFor="settings-health-interval"
        >
          <Input
            id="settings-health-interval"
            type="number"
            min={100}
            value={draft.health_check_interval_ms}
            onChange={(event) => patch({ health_check_interval_ms: Number(event.target.value) })}
          />
        </Field>
        <Field
          label={translate('settings.logBuffer')}
          hint={translate('settings.logBufferHelp')}
          htmlFor="settings-log-buffer"
        >
          <Input
            id="settings-log-buffer"
            type="number"
            min={100}
            value={draft.log_buffer_lines}
            onChange={(event) => patch({ log_buffer_lines: Number(event.target.value) })}
          />
        </Field>
        <Field
          label={translate('settings.historyMax')}
          hint={translate('settings.historyMaxHelp')}
          htmlFor="settings-history-max"
        >
          <Input
            id="settings-history-max"
            type="number"
            min={100}
            value={draft.history_max_records}
            onChange={(event) => patch({ history_max_records: Number(event.target.value) })}
          />
        </Field>
        <Field
          label={translate('settings.monitorInterval')}
          hint={translate('settings.monitorIntervalHelp')}
          htmlFor="settings-monitor-interval"
        >
          <Input
            id="settings-monitor-interval"
            type="number"
            min={1}
            value={draft.monitor_interval_seconds}
            onChange={(event) => patch({ monitor_interval_seconds: Number(event.target.value) })}
          />
        </Field>
        <Field
          label={translate('settings.monitorHistory')}
          hint={translate('settings.monitorHistoryHelp')}
          htmlFor="settings-monitor-history"
        >
          <Input
            id="settings-monitor-history"
            type="number"
            min={10}
            value={draft.monitor_history_seconds}
            onChange={(event) => patch({ monitor_history_seconds: Number(event.target.value) })}
          />
        </Field>
      </div>

      <p className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
        {translate('settings.restartHint')}
      </p>

      <div className="border-t border-[var(--border)] pt-4">
        <MemoryEditor />
      </div>

      <div className="border-t border-[var(--border)] pt-4">
        <SkillSettings />
      </div>

      <SaveRow onSave={() => void save()} saving={saving} dirty={dirty} />
    </div>
  );
}
