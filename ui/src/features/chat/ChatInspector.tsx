import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { toast } from 'sonner';
import { BookOpen, Cpu, Search, Sliders, Sparkles, Wrench } from 'lucide-react';
import { chatsApi, knowledgeApi, skillsApi, systemApi } from '@/lib/api';
import { pickPath } from '@/lib/api/client';
import type { Conversation, Instance } from '@/lib/api/types';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/Button';
import { Field, Input } from '@/components/ui/Input';
import { resolveChatSettings, type ResolvedChatSettings, type ThinkingMode } from '@/features/chat/settings';

/**
 * The chat inspector: how **this conversation** generates, and what it may use.
 *
 * 1.0 had this panel; 2.0 lost it while keeping every part of the mechanism —
 * the store's `inspectorOpen`/`inspectorWidth`, the CSS tokens, and all the i18n
 * copy. Restoring the panel is therefore wiring, not new design.
 *
 * Two rules are load-bearing:
 *
 * - The panel edits a **draft**. Nothing is sent until Save, so a half-typed
 *   system prompt cannot leak into the next message. On failure the draft is
 *   kept and the error is shown (§2.20: never report success for a failed save).
 * - The system prompt is stored under `params.system`, which is the key
 *   `chat_store.PARAM_KEYS` accepts. The previous build wrote `system_prompt`,
 *   which the allow-list dropped silently — the panel said "saved" and the prompt
 *   never reached the model.
 */
export function ChatInspector({
  conversation,
  instances,
  activeInstanceId,
  toolRoot,
  onClose,
  onSaved,
  width = 0,
}: {
  conversation: Conversation | null;
  instances: Instance[];
  activeInstanceId: string | null;
  toolRoot: string | null;
  onClose: () => void;
  /** Pixels; 0 means "use the CSS default". */
  width?: number;
  onSaved: (settings: ResolvedChatSettings) => void;
}) {
  const resolved = useMemo(() => resolveChatSettings(conversation), [conversation]);
  const [draft, setDraft] = useState(() => ({ ...resolved }));
  const [saving, setSaving] = useState(false);

  // Re-seed when the conversation changes; keeping the old draft would apply one
  // conversation's prompt to another.
  useEffect(() => {
    setDraft({ ...resolved });
  }, [resolved]);

  const knowledge = useQuery({ queryKey: ['knowledge'], queryFn: knowledgeApi.list, staleTime: 30_000 });
  const bases = knowledge.data?.bases ?? [];
  const config = useQuery({ queryKey: ['system', 'config'], queryFn: systemApi.getConfig, staleTime: 30_000 });
  const skillsQuery = useQuery({ queryKey: ['skills'], queryFn: () => skillsApi.list(), staleTime: 30_000 });
  const skills = skillsQuery.data?.skills ?? [];
  const webSearchAvailable = Boolean(
    config.data?.web_search_enabled && String(config.data?.web_search_endpoint ?? '').trim(),
  );

  const patchParams = (changes: Record<string, unknown>) =>
    setDraft((current) => ({ ...current, params: { ...current.params, ...changes } }));

  const save = async () => {
    if (!conversation) return;
    setSaving(true);
    try {
      const params: Record<string, unknown> = { ...draft.params, thinking: draft.thinking };
      if (draft.systemPrompt.trim()) params.system = draft.systemPrompt;
      else delete params.system;

      await chatsApi.update(conversation.id, {
        instance_id: draft.instanceId,
        knowledge_base_id: draft.knowledgeBaseId,
        params,
      });
      onSaved(draft);
      toast.success(translate('chat.settingsSaved'));
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    } finally {
      setSaving(false);
    }
  };

  const chooseRoot = async () => {
    if (!conversation) return;
    const picked = await pickPath('dir', { current: toolRoot ?? undefined });
    if (!picked) return;
    try {
      await chatsApi.update(conversation.id, { tool_root: picked });
      onSaved({ ...draft });
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  };

  return (
    <aside
      data-testid="chat-inspector"
      aria-label={translate('chat.inspector')}
      /*
        `--inspector-w` is the default; a stored/configured width overrides it inline.
        Without the override the store's `inspectorWidth` (fed from
        `chat_panel_width`) reached nothing, which is why the setting had no visible
        effect even after the config key was hydrated.
      */
      style={width > 0 ? ({ ['--inspector-w' as string]: `${width}px` } as React.CSSProperties) : undefined}
      className="flex min-h-0 w-[var(--inspector-w)] shrink-0 flex-col border-s border-[var(--border)] bg-[var(--panel)]"
    >
      <header className="flex items-center gap-2 border-b border-[var(--border)] px-3 py-2">
        <Sliders size={14} aria-hidden="true" className="text-[var(--text-tertiary)]" />
        <h2 className="flex-1 truncate text-[length:var(--text-xs)] font-medium">
          {translate('chat.inspector')}
        </h2>
        <Button variant="ghost" size="sm" onClick={onClose} aria-label={translate('common.close')}>
          ×
        </Button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {!conversation ? (
          <p className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
            {translate('chat.inspectorHint')}
          </p>
        ) : (
          <div className="flex flex-col gap-4">
            <Field label={<IconLabel icon={<Cpu size={12} />} text={translate('chat.contextSection')} />}>
              <select
                aria-label={translate('chat.contextSection')}
                value={draft.instanceId ?? activeInstanceId ?? ''}
                onChange={(event) => setDraft({ ...draft, instanceId: event.target.value || null })}
                className="h-[var(--control-h)] w-full rounded-[var(--radius-md)] border border-[var(--border-control)] bg-[var(--surface)] px-2 text-[length:var(--text-xs)]"
              >
                <option value="">{translate('chat.noInstance')}</option>
                {instances.map((instance) => (
                  <option key={instance.id} value={instance.id}>
                    {instance.name || instance.model_path}
                  </option>
                ))}
              </select>
            </Field>

            <Field label={<IconLabel icon={<BookOpen size={12} />} text={translate('nav.knowledge')} />}>
              <select
                aria-label={translate('nav.knowledge')}
                value={draft.knowledgeBaseId ?? ''}
                onChange={(event) =>
                  setDraft({ ...draft, knowledgeBaseId: event.target.value || null })
                }
                className="h-[var(--control-h)] w-full rounded-[var(--radius-md)] border border-[var(--border-control)] bg-[var(--surface)] px-2 text-[length:var(--text-xs)]"
              >
                <option value="">{translate('chat.knowledgeNone')}</option>
                {bases.map((base) => (
                  <option key={base.id} value={base.id}>
                    {base.name}
                  </option>
                ))}
              </select>
            </Field>

            <Field
              label={translate('chat.systemPrompt')}
              hint={translate('chat.systemPromptHelp')}
            >
              <textarea
                aria-label={translate('chat.systemPrompt')}
                value={draft.systemPrompt}
                onChange={(event) => setDraft({ ...draft, systemPrompt: event.target.value })}
                rows={5}
                className="w-full resize-y rounded-[var(--radius-md)] border border-[var(--border-control)] bg-[var(--surface)] px-2 py-1.5 text-[length:var(--text-xs)] leading-[var(--leading-relaxed)]"
              />
            </Field>

            <Field label={translate('chat.thinkingMode')} hint={translate('chat.thinkingHelp')}>
              <div className="flex flex-col gap-1">
                {(
                  [
                    ['off', 'chat.thinkingOff'],
                    ['on', 'chat.thinkingOn'],
                    ['auto', 'chat.thinkingAuto'],
                  ] as Array<[ThinkingMode, string]>
                ).map(([value, key]) => {
                  const active = draft.thinking === value;
                  return (
                    <button
                      key={value}
                      type="button"
                      aria-pressed={active}
                      onClick={() => setDraft({ ...draft, thinking: value })}
                      className={cn(
                        'min-h-[var(--control-h-sm)] rounded-[var(--radius-sm)] border px-2 text-start text-[length:var(--text-2xs)]',
                        active
                          ? 'border-[var(--accent)] bg-[var(--accent-subtle)] text-[var(--accent-text)]'
                          : 'border-[var(--border-control)] text-[var(--text-secondary)] hover:bg-[var(--hover)]',
                      )}
                    >
                      {translate(key)}
                    </button>
                  );
                })}
              </div>
            </Field>

            <fieldset className="flex flex-col gap-2">
              <legend className="mb-1 text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
                {translate('chat.sampling')}
              </legend>
              <NumberRow
                id="inspector-temperature"
                label={translate('chat.temperature')}
                step={0.05}
                value={Number(draft.params.temperature ?? 0.8)}
                onChange={(value) => patchParams({ temperature: value })}
              />
              <NumberRow
                id="inspector-top-p"
                label="top_p"
                step={0.05}
                value={Number(draft.params.top_p ?? 0.95)}
                onChange={(value) => patchParams({ top_p: value })}
              />
              <NumberRow
                id="inspector-max-tokens"
                label={translate('chat.maxTokens')}
                step={128}
                value={Number(draft.params.max_tokens ?? 2048)}
                onChange={(value) => patchParams({ max_tokens: value })}
              />
              <NumberRow
                id="inspector-top-k"
                label="top_k"
                step={1}
                value={Number(draft.params.top_k ?? 40)}
                onChange={(value) => patchParams({ top_k: value })}
              />
              <NumberRow
                id="inspector-repeat-penalty"
                label={translate('chat.repeatPenalty')}
                step={0.05}
                value={Number(draft.params.repeat_penalty ?? 1.1)}
                onChange={(value) => patchParams({ repeat_penalty: value })}
              />
            </fieldset>

            <Field label={<IconLabel icon={<Wrench size={12} />} text={translate('chat.toolRoot')} />}>
              <div className="flex gap-1.5">
                <Input readOnly value={toolRoot ?? ''} placeholder={translate('chat.toolNoRoot')} />
                <Button variant="secondary" size="sm" onClick={() => void chooseRoot()}>
                  {translate('settings.browse')}
                </Button>
              </div>
            </Field>

            {/*
              Agent mode is per conversation and stored in `params.agent_mode`.
              Turning it on adds write_file / edit_file / run_command to the
              declared tool set; every one of them still asks before running.
              It is off by default because "let the model write files" is a
              decision, not a convenience.
            */}
            <label className="flex items-start gap-2 rounded-[var(--radius-md)] border border-[var(--border)] p-2">
              <input
                type="checkbox"
                checked={Boolean(draft.params.agent_mode)}
                onChange={(event) => patchParams({ agent_mode: event.target.checked })}
                className="mt-0.5 accent-[var(--accent)]"
              />
              <span className="flex-1">
                <span className="block text-[length:var(--text-xs)] font-medium">
                  {translate('chat.agentMode')}
                </span>
                <span className="block text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                  {translate('chat.agentModeHelp')}
                </span>
              </span>
            </label>

            {/*
              Skills: a pinned choice wins over an `@mention`. Listing only enabled
              ones is deliberate — a disabled skill must not be selectable, and the
              server refuses to resolve one even if a stale id arrives.
            */}
            {skills.length ? (
              <Field label={<IconLabel icon={<Sparkles size={12} />} text={translate('chat.skillLabel')} />}>
                <select
                  aria-label={translate('chat.skillLabel')}
                  value={String(draft.params.skill_id ?? '')}
                  onChange={(event) => patchParams({ skill_id: event.target.value || null })}
                  className="h-[var(--control-h)] w-full rounded-[var(--radius-md)] border border-[var(--border-control)] bg-[var(--surface)] px-2 text-[length:var(--text-xs)]"
                >
                  <option value="">{translate('chat.skillNone')}</option>
                  {skills
                    .filter((skill) => skill.enabled)
                    .map((skill) => (
                      <option key={skill.id} value={skill.id}>
                        {skill.name}
                      </option>
                    ))}
                </select>
              </Field>
            ) : null}

            {/*
              Memory is per conversation, like agent mode: turning it on for a
              project chat must not silently inject notes into every other
              conversation. The editor lives in Settings → 聊天; this is the switch
              that decides whether *this* conversation reads it.
            */}
            <label className="flex items-start gap-2 rounded-[var(--radius-md)] border border-[var(--border)] p-2">
              <input
                type="checkbox"
                checked={Boolean(draft.params.memory)}
                onChange={(event) => patchParams({ memory: event.target.checked })}
                className="mt-0.5 accent-[var(--accent)]"
              />
              <span className="flex-1">
                <span className="block text-[length:var(--text-xs)] font-medium">
                  {translate('chat.useMemory')}
                </span>
                <span className="block text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                  {translate('chat.useMemoryHelp')}
                </span>
              </span>
            </label>

            {/*
              Web search: shown only when the backend has it enabled and an
              endpoint is configured, because a toggle that cannot work is worse
              than no toggle. The confirm dialog states that the query leaves the
              machine — that is the one place in this app where it does.
            */}
            {webSearchAvailable ? (
              <label className="flex items-start gap-2 rounded-[var(--radius-md)] border border-[var(--border)] p-2">
                <input
                  type="checkbox"
                  checked={Boolean(draft.params.web_search)}
                  onChange={(event) => patchParams({ web_search: event.target.checked })}
                  className="mt-0.5 accent-[var(--accent)]"
                />
                <span className="flex-1">
                  <span className="block text-[length:var(--text-xs)] font-medium">
                    {translate('chat.webSearch')}
                  </span>
                  <span className="block text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                    {translate('chat.webSearchHelp')}
                  </span>
                </span>
              </label>
            ) : null}
          </div>
        )}
      </div>

      {conversation ? (
        <footer className="border-t border-[var(--border)] p-3">
          <Button variant="primary" className="w-full" loading={saving} onClick={() => void save()}>
            {translate('chat.settings')}
          </Button>
        </footer>
      ) : null}
    </aside>
  );
}

function IconLabel({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span aria-hidden="true" className="text-[var(--text-tertiary)]">
        {icon}
      </span>
      {text}
    </span>
  );
}

function NumberRow({
  id,
  label,
  value,
  step,
  onChange,
}: {
  id: string;
  label: string;
  value: number;
  step: number;
  onChange: (value: number) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <label htmlFor={id} className="flex-1 text-[length:var(--text-2xs)] text-[var(--text-secondary)]">
        {label}
      </label>
      <input
        id={id}
        type="number"
        step={step}
        value={Number.isFinite(value) ? value : 0}
        onChange={(event) => onChange(Number(event.target.value))}
        className="h-[var(--control-h-sm)] w-20 rounded-[var(--radius-sm)] border border-[var(--border-control)] bg-[var(--surface)] px-1.5 text-end text-[length:var(--text-2xs)] tnum"
      />
    </div>
  );
}

export { Search };
