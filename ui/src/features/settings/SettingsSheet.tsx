import { Check, Clock, Code2, Cpu, Download, HardDrive, Info, Laptop, MessageSquare, Moon, Server, Settings as SettingsIcon, Sun } from 'lucide-react';
import { toast } from 'sonner';
import { useUiStore } from '@/lib/stores/ui';
import { translate } from '@/lib/i18n';
import { apiServerApi } from '@/lib/api';
import { cn } from '@/lib/utils';
import { Dialog, DialogContent } from '@/components/ui/Dialog';
import { Button } from '@/components/ui/Button';
import { LoadingState, SectionHeader } from '@/components/ui/Feedback';
import { Field, Input, PathInput, Switch } from '@/components/ui/Input';
import { useConfigForm, SaveRow } from '@/features/settings/useConfigForm';
import { AppearanceTab } from '@/features/settings/tabs/AppearanceTab';
import { ChatSettingsTab } from '@/features/settings/tabs/ChatSettingsTab';
import { SystemTab } from '@/features/settings/tabs/SystemTab';
import { ScheduledTasksTab } from '@/features/settings/tabs/ScheduledTasksTab';
import { UsageTab } from '@/features/settings/tabs/UsageTab';
import { DataTab } from '@/features/settings/tabs/DataTab';
import { DiagnosticsTab } from '@/features/settings/tabs/DiagnosticsTab';
import { AboutTab } from '@/features/settings/tabs/AboutTab';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { localizeBackendError } from '@/lib/i18n';

/**
 * Settings sheet.
 *
 * A wide two-pane dialog rather than a route: settings are a detour, and closing
 * them must return to the conversation exactly as it was. `navigate('settings')`
 * opens it through the UI store (see `stores/ui.ts`), so every "open settings"
 * button in the app works — before that branch existed those buttons changed a
 * route that rendered the chat page underneath and appeared to do nothing.
 *
 * The tab set mirrors what the backend actually offers; a tab that had no backend
 * behind it would be a stub, and a stub in a settings page reads as a broken
 * feature. Sections without a backend yet (voice, browser, computer control) are
 * deliberately absent rather than rendered disabled.
 */
type SettingsTab =
  | 'general'
  | 'appearance'
  | 'chat'
  | 'system'
  | 'ports'
  | 'serving'
  | 'usage'
  | 'scheduled'
  | 'data'
  | 'diagnostics'
  | 'about';

const TABS: Array<{ id: SettingsTab; labelKey: string; icon: typeof Sun }> = [
  { id: 'general', labelKey: 'settings.catGeneral', icon: SettingsIcon },
  { id: 'appearance', labelKey: 'settings.catAppearance', icon: Sun },
  { id: 'chat', labelKey: 'settings.catChat', icon: MessageSquare },
  { id: 'system', labelKey: 'settings.catSystem', icon: Cpu },
  { id: 'ports', labelKey: 'settings.catServer', icon: Server },
  { id: 'serving', labelKey: 'settings.catApiServer', icon: Code2 },
  { id: 'scheduled', labelKey: 'settings.catScheduled', icon: Clock },
  { id: 'usage', labelKey: 'settings.catUsage', icon: Info },
  { id: 'data', labelKey: 'settings.catData', icon: HardDrive },
  { id: 'diagnostics', labelKey: 'settings.catDeveloper', icon: Cpu },
  { id: 'about', labelKey: 'settings.catAbout', icon: Info },
];

export function SettingsSheet() {
  const open = useUiStore((state) => state.settingsOpen);
  const setOpen = useUiStore((state) => state.setSettingsOpen);
  const tab = useUiStore((state) => state.settingsTab) as SettingsTab;
  const setTab = useUiStore((state) => state.openSettings);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {open ? (
        <DialogContent size="settings" title={translate('settings.title')} className="p-0">
          <div className="flex h-full min-h-0">
            <nav
              aria-label={translate('settings.title')}
              className="flex w-[248px] shrink-0 flex-col gap-[2px] overflow-y-auto border-e border-[var(--border)] bg-[var(--panel)] p-2"
            >
              {TABS.map((entry) => {
                const Icon = entry.icon;
                const active = tab === entry.id;
                return (
                  <button
                    key={entry.id}
                    type="button"
                    data-testid={`settings-tab-${entry.id}`}
                    aria-current={active ? 'page' : undefined}
                    onClick={() => setTab(entry.id)}
                    className={cn(
                      'flex h-[30px] items-center gap-2 rounded-[var(--radius-md)] px-2.5 text-start text-[length:var(--text-sm)]',
                      'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--border-focus)]',
                      active
                        ? 'bg-[var(--selected)] font-medium text-[var(--text)]'
                        : 'text-[var(--text-secondary)] hover:bg-[var(--hover)] hover:text-[var(--text)]',
                    )}
                  >
                    <Icon
                      size={15}
                      aria-hidden="true"
                      className="shrink-0 text-[var(--text-tertiary)]"
                    />
                    <span className="truncate">{translate(entry.labelKey)}</span>
                  </button>
                );
              })}
            </nav>
            <div className="min-h-0 flex-1 overflow-y-auto p-5" data-testid="settings-body">
              {tab === 'general' ? <GeneralTab /> : null}
              {tab === 'appearance' ? <AppearanceTab /> : null}
              {tab === 'chat' ? <ChatSettingsTab /> : null}
              {tab === 'system' ? <SystemTab /> : null}
              {tab === 'ports' ? <PortsTab /> : null}
              {tab === 'serving' ? <ServingTab /> : null}
              {tab === 'scheduled' ? <ScheduledTasksTab /> : null}
              {tab === 'usage' ? <UsageTab /> : null}
              {tab === 'data' ? <DataTab /> : null}
              {tab === 'diagnostics' ? <DiagnosticsTab /> : null}
              {tab === 'about' ? <AboutTab /> : null}
            </div>
          </div>
        </DialogContent>
      ) : null}
    </Dialog>
  );
}

function GeneralTab() {
  const { draft, patch, save, saving, dirty, config } = useConfigForm();
  if (config.isPending || !draft) return <LoadingState label={translate('settings.loading')} />;

  return (
    <div className="flex flex-col gap-5">
      <SectionHeader title={translate('settings.catGeneral')} />
      <Field
        label={translate('settings.serverPath')}
        hint={translate('settings.serverPathHelp')}
        htmlFor="settings-server-path"
      >
        {/*
          A native picker, not a bare text box: this is the field that decides which
          executable the launcher runs, and typing an absolute path from memory is
          how users end up pointing at the wrong build.
        */}
        <PathInput
          id="settings-server-path"
          kind="file"
          value={String(draft.llama_server_path ?? '')}
          onValueChange={(next) => patch({ llama_server_path: next })}
          placeholder="C:\\llama\\llama-server.exe"
          filters={[{ name: 'Executable', extensions: ['exe'] }]}
        />
      </Field>

      <Field
        label={translate('settings.modelDir')}
        hint={translate('settings.modelDirHelp')}
        htmlFor="settings-model-dir"
      >
        {/*
          The key is `default_model_dir` — the backend's own name. Sending
          `model_directory` made every save on this tab fail with
          "unsupported config key(s)", which is the defect the user reported on
          2026-11-24. `test_frontend_contracts.py` now pins the spelling.
        */}
        <PathInput
          id="settings-model-dir"
          kind="dir"
          value={String(draft.default_model_dir ?? '')}
          onValueChange={(next) => patch({ default_model_dir: next })}
        />
      </Field>

      <Field label={translate('settings.guardrail')} hint={translate('settings.guardrailHelp')}>
        <div className="flex gap-2">
          {(
            [
              ['off', 'settings.guardrailOff'],
              ['warn', 'settings.guardrailWarn'],
              ['strict', 'settings.guardrailStrict'],
            ] as const
          ).map(([level, labelKey]) => {
            const active = String(draft.launch_guardrail ?? 'warn') === level;
            return (
              <button
                key={level}
                type="button"
                aria-pressed={active}
                onClick={() => patch({ launch_guardrail: level })}
                className={cn(
                  'min-h-[var(--control-h)] flex-1 rounded-[var(--radius-md)] border px-2 text-[length:var(--text-sm)]',
                  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--border-focus)]',
                  active
                    ? 'border-[var(--accent)] bg-[var(--accent-subtle)] font-medium text-[var(--accent-text)]'
                    : 'border-[var(--border-control)] text-[var(--text-secondary)] hover:bg-[var(--hover)]',
                )}
              >
                {translate(labelKey)}
              </button>
            );
          })}
        </div>
      </Field>

      <Switch
        checked={Boolean(draft.close_to_tray)}
        onCheckedChange={(next) => patch({ close_to_tray: next })}
        label={translate('settings.closeToTray')}
        description={translate('settings.closeToTrayHelp')}
      />

      <SaveRow onSave={() => void save()} saving={saving} dirty={dirty} />
    </div>
  );
}

function PortsTab() {
  const { draft, patch, save, saving, dirty, config } = useConfigForm();
  if (config.isPending || !draft) return <LoadingState label={translate('settings.loading')} />;

  return (
    <div className="flex flex-col gap-5">
      <SectionHeader title={translate('settings.catServer')} />
      <div className="grid grid-cols-2 gap-3">
        <Field
          label={translate('settings.launcherPort')}
          hint={translate('settings.launcherPortHelp')}
          htmlFor="settings-launcher-port"
        >
          <Input
            id="settings-launcher-port"
            type="number"
            min={1}
            max={65535}
            value={draft.launcher_port}
            onChange={(event) => patch({ launcher_port: Number(event.target.value) })}
          />
        </Field>
        <Field label={translate('settings.launcherHost')} hint={translate('settings.launcherHostHelp')}>
          <Input value={draft.launcher_host} readOnly disabled />
        </Field>
        <Field
          label={translate('settings.portStart')}
          hint={translate('settings.portStartHelp')}
          htmlFor="settings-port-start"
        >
          <Input
            id="settings-port-start"
            type="number"
            value={draft.instance_port_start}
            onChange={(event) => patch({ instance_port_start: Number(event.target.value) })}
          />
        </Field>
        <Field
          label={translate('settings.portEnd')}
          hint={translate('settings.portEndHelp')}
          htmlFor="settings-port-end"
        >
          <Input
            id="settings-port-end"
            type="number"
            value={draft.instance_port_end}
            onChange={(event) => patch({ instance_port_end: Number(event.target.value) })}
          />
        </Field>
      </div>
      <p className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
        {translate('settings.restartHint')}
      </p>
      <SaveRow onSave={() => void save()} saving={saving} dirty={dirty} />
    </div>
  );
}

function ServingTab() {
  const { draft, patch, setDraft, save, saving, dirty, config } = useConfigForm();
  const client = useQueryClient();
  const clearKey = useMutation({
    mutationFn: () => apiServerApi.clearKey(),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['system', 'config'] });
      void client.invalidateQueries({ queryKey: ['api-server'] });
      setDraft(null);
      toast.success(translate('settings.apiServerKeyCleared'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  if (config.isPending || !draft) return <LoadingState label={translate('settings.loading')} />;

  return (
    <div className="flex flex-col gap-5">
      <SectionHeader title={translate('settings.catApiServer')} />
      <Switch
        checked={Boolean(draft.api_server_enabled)}
        onCheckedChange={(next) => patch({ api_server_enabled: next })}
        label={translate('settings.apiServerEnabled')}
        description={translate('settings.apiServerEnabledHelp')}
      />
      <div className="grid grid-cols-2 gap-3">
        <Field
          label={translate('settings.apiServerPort')}
          hint={translate('settings.apiServerPortHelp')}
          htmlFor="settings-api-port"
        >
          <Input
            id="settings-api-port"
            type="number"
            value={draft.api_server_port}
            onChange={(event) => patch({ api_server_port: Number(event.target.value) })}
          />
        </Field>
        <Field label={translate('settings.apiServerKey')} hint={translate('settings.apiServerKeyHelp')}>
          <Input
            type="password"
            autoComplete="new-password"
            value={draft.api_server_key}
            onChange={(event) => patch({ api_server_key: event.target.value })}
            placeholder={translate('settings.apiServerKeyPh')}
          />
        </Field>
      </div>
      {/*
        Clearing the key has its own route on purpose: an empty string in the PUT
        body means "keep the current key" (the GET never returns it), so the only
        way to actually remove it must not be a save.
      */}
      <div className="flex justify-end">
        <Button
          variant="dangerGhost"
          size="sm"
          loading={clearKey.isPending}
          onClick={() => clearKey.mutate()}
        >
          {translate('settings.apiServerClearKey')}
        </Button>
      </div>
      <Field
        label={translate('settings.apiServerCors')}
        hint={translate('settings.apiServerCorsHelp')}
        htmlFor="settings-cors"
      >
        <Input
          id="settings-cors"
          value={draft.api_server_cors_origins}
          onChange={(event) => patch({ api_server_cors_origins: event.target.value })}
          placeholder="http://127.0.0.1:3000"
          className="font-mono"
        />
      </Field>
      <Switch
        checked={Boolean(draft.api_server_log_requests)}
        onCheckedChange={(next) => patch({ api_server_log_requests: next })}
        label={translate('settings.apiServerLogRequests')}
        description={translate('settings.apiServerLogRequestsHelp')}
      />
      <SaveRow onSave={() => void save()} saving={saving} dirty={dirty} />
    </div>
  );
}

export { Check, Download, Laptop, Moon };