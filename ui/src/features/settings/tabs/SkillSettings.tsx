import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Plus, Trash2 } from 'lucide-react';
import { skillsApi, type Skill } from '@/lib/api';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/Button';
import { Field, Input } from '@/components/ui/Input';
import { SectionHeader } from '@/components/ui/Feedback';

/**
 * Skill library.
 *
 * A skill is a named instruction block plus an optional tool allow-list. The editor
 * makes the two distinctions a user has to get right explicit:
 *
 * - **Allowed tools: all / none / some.** `[]` ("none") and `null` ("all") are
 *   different answers and the UI offers both as buttons rather than a checkbox that
 *   cannot express "none".
 * - **The name is the `@mention` token.** It is validated lowercase on the server,
 *   so the field states the rule instead of failing after a save.
 *
 * `@name` triggers are resolved server-side (only the latest user message counts, and
 * never inside code or quotes); this page does not duplicate that logic.
 */
const TOOL_CHOICES = [
  'read_file',
  'file_glob_search',
  'grep_search',
  'write_file',
  'edit_file',
  'run_command',
  'web_search',
] as const;

export function SkillSettings() {
  const client = useQueryClient();
  const skills = useQuery({ queryKey: ['skills'], queryFn: () => skillsApi.list() });
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({
    name: '',
    description: '',
    instructions: '',
    mode: 'all' as 'all' | 'none' | 'some',
    tools: [] as string[],
  });

  const invalidate = () => void client.invalidateQueries({ queryKey: ['skills'] });

  const create = useMutation({
    mutationFn: () =>
      skillsApi.create({
        name: form.name.trim(),
        description: form.description.trim(),
        instructions: form.instructions,
        // `all` → null (no restriction); `none` → []; `some` → the picked subset.
        allowed_tools: form.mode === 'all' ? null : form.mode === 'none' ? [] : form.tools,
      }),
    onSuccess: () => {
      setCreating(false);
      setForm({ name: '', description: '', instructions: '', mode: 'all', tools: [] });
      invalidate();
      toast.success(translate('settings.skillCreated'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      skillsApi.update(id, { enabled }),
    onSuccess: invalidate,
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const remove = useMutation({
    mutationFn: (id: string) => skillsApi.remove(id),
    onSuccess: invalidate,
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const list: Skill[] = skills.data?.skills ?? [];

  return (
    <div className="flex flex-col gap-3">
      <SectionHeader
        title={translate('settings.skillsTitle')}
        description={translate('settings.skillsHelp')}
        action={
          <Button variant="secondary" size="sm" onClick={() => setCreating((value) => !value)}>
            <Plus size={12} aria-hidden="true" />
            {translate('settings.skillCreate')}
          </Button>
        }
      />

      {list.length === 0 && !creating ? (
        <p className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
          {translate('settings.skillsEmpty')}
        </p>
      ) : null}

      {list.length ? (
        <ul className="flex flex-col gap-1">
          {list.map((skill) => (
            <li
              key={skill.id}
              className="flex items-start gap-2 rounded-[var(--radius-sm)] border border-[var(--border)] px-2 py-1.5"
            >
              <label className="mt-0.5 flex items-center">
                <input
                  type="checkbox"
                  checked={skill.enabled}
                  aria-label={translate('settings.skillEnabled')}
                  onChange={(event) =>
                    toggle.mutate({ id: skill.id, enabled: event.target.checked })
                  }
                  className="accent-[var(--accent)]"
                />
              </label>
              <div className="min-w-0 flex-1">
                <p className="flex items-center gap-1.5">
                  <span className="truncate text-[length:var(--text-xs)] font-medium">
                    {skill.name}
                  </span>
                  <code className="shrink-0 rounded-[var(--radius-xs)] bg-[var(--surface-sunken)] px-1 text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                    @{skill.name}
                  </code>
                </p>
                {skill.description ? (
                  <p className="truncate text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                    {skill.description}
                  </p>
                ) : null}
                <p className="text-[length:var(--text-2xs)] text-[var(--text-quaternary)]">
                  {skill.allowed_tools === null
                    ? translate('settings.skillToolsAll')
                    : skill.allowed_tools.length === 0
                      ? translate('settings.skillToolsNone')
                      : skill.allowed_tools.join(', ')}
                </p>
              </div>
              <button
                type="button"
                aria-label={translate('common.delete')}
                onClick={() => {
                  if (window.confirm(translate('settings.skillDeleteConfirm', { name: skill.name }))) {
                    remove.mutate(skill.id);
                  }
                }}
                className="mt-0.5 grid size-5 place-items-center rounded-[var(--radius-xs)] text-[var(--text-tertiary)] hover:bg-[var(--danger-subtle)] hover:text-[var(--danger)]"
              >
                <Trash2 size={11} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      {creating ? (
        <div className="flex flex-col gap-2 rounded-[var(--radius-md)] border border-[var(--border)] p-2.5">
          <Field label={translate('settings.skillName')} hint={translate('settings.skillNameHelp')} htmlFor="skill-name">
            <Input
              id="skill-name"
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              placeholder="reviewer"
              className="font-mono"
            />
          </Field>
          <Field label={translate('settings.skillDescription')} htmlFor="skill-description">
            <Input
              id="skill-description"
              value={form.description}
              onChange={(event) => setForm({ ...form, description: event.target.value })}
            />
          </Field>
          <Field
            label={translate('settings.skillInstructions')}
            hint={translate('settings.skillInstructionsHelp')}
            htmlFor="skill-instructions"
          >
            <textarea
              id="skill-instructions"
              value={form.instructions}
              onChange={(event) => setForm({ ...form, instructions: event.target.value })}
              rows={5}
              className="w-full resize-y rounded-[var(--radius-sm)] border border-[var(--border-control)] bg-[var(--surface)] px-2 py-1.5 text-[length:var(--text-xs)]"
            />
          </Field>

          <fieldset className="flex flex-col gap-1.5">
            <legend className="text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
              {translate('settings.skillTools')}
            </legend>
            <div className="flex gap-1">
              {(['all', 'none', 'some'] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  aria-pressed={form.mode === mode}
                  onClick={() => setForm({ ...form, mode })}
                  className={cn(
                    'h-[var(--control-h-sm)] flex-1 rounded-[var(--radius-sm)] border text-[length:var(--text-xs)]',
                    form.mode === mode
                      ? 'border-[var(--accent)] bg-[var(--accent-subtle)] font-medium text-[var(--accent-text)]'
                      : 'border-[var(--border-control)] text-[var(--text-secondary)] hover:bg-[var(--hover)]',
                  )}
                >
                  {translate(`settings.skillTools_${mode}`)}
                </button>
              ))}
            </div>
            {form.mode === 'some' ? (
              <div className="flex flex-wrap gap-1.5 pt-1">
                {TOOL_CHOICES.map((tool) => {
                  const active = form.tools.includes(tool);
                  return (
                    <button
                      key={tool}
                      type="button"
                      aria-pressed={active}
                      onClick={() =>
                        setForm({
                          ...form,
                          tools: active
                            ? form.tools.filter((item) => item !== tool)
                            : [...form.tools, tool],
                        })
                      }
                      className={cn(
                        'rounded-[var(--radius-pill)] px-2 py-0.5 font-mono text-[length:var(--text-2xs)]',
                        active
                          ? 'bg-[var(--accent-subtle)] text-[var(--accent-text)]'
                          : 'bg-[var(--surface-sunken)] text-[var(--text-tertiary)]',
                      )}
                    >
                      {tool}
                    </button>
                  );
                })}
              </div>
            ) : null}
          </fieldset>

          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setCreating(false)}>
              {translate('common.cancel')}
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={!form.name.trim()}
              loading={create.isPending}
              onClick={() => create.mutate()}
            >
              {translate('common.save')}
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
