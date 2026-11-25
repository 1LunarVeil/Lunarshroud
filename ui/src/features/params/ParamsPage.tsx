import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { toast } from 'sonner';
import { ChevronRight, Play, Terminal } from 'lucide-react';
import { useHandoffStore } from '@/lib/stores/handoff';
import { instancesApi, modelsApi, paramsApi, runtimesApi } from '@/lib/api';
import type { PathFilter } from '@/lib/api/client';
import type { ParamMeta } from '@/lib/api/types';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn, formatBytes } from '@/lib/utils';
import { Badge, ErrorState, LoadingState, SectionHeader } from '@/components/ui/Feedback';
import { Button } from '@/components/ui/Button';
import { Field, Input, PathInput, Switch } from '@/components/ui/Input';

/**
 * Parameter editor: the schema-driven launch form.
 *
 * Four behaviours are carried over from 1.0 because each was learned the hard way, and
 * each is a comment on the code below:
 *
 * 1. **Capability probes are debounced.** `model` is a text field; without a debounce,
 *    every keystroke fires a request that may run `llama-server --help`.
 * 2. **`ctx_size` is clamped to the model's native context** after every render — not as
 *    help text, as an actual `max`. Exceeding it is still allowed (RoPE scaling is a real
 *    use), so the note warns rather than blocks.
 * 3. **Static help and dynamic state stay separate**: explanations live in a tooltip;
 *    "this build does not support the flag" and "context above native" stay inline
 *    because they change with the value.
 * 4. **Field width follows the control type**: text/path/select controls take a full row;
 *    switches and numbers share a grid.
 *
 * Values live in one `values` object. Switching groups re-renders, so every input writes
 * back immediately — a draft-per-group scheme would silently drop edits on navigation.
 */
export function ParamsPage() {
  const pendingModel = useHandoffStore((state) => state.takeModel());
  const [runtimeId, setRuntimeId] = useState('');
  const [modelPath, setModelPath] = useState(pendingModel ?? '');
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [launching, setLaunching] = useState(false);

  const runtimes = useQuery({ queryKey: ['runtimes'], queryFn: runtimesApi.list, staleTime: 30_000 });
  const profiles = runtimes.data?.runtimes ?? [];
  const effectiveRuntime = runtimeId || profiles[0]?.id || '';

  const schema = useQuery({
    queryKey: ['params', 'schema', effectiveRuntime],
    queryFn: () => paramsApi.schema(effectiveRuntime || undefined),
    staleTime: 60_000,
  });

  const defaults = useQuery({
    queryKey: ['params', 'defaults'],
    queryFn: paramsApi.defaults,
    staleTime: 60_000,
  });

  // Seed values from defaults once, then let the model path drive the rest.
  useEffect(() => {
    if (defaults.data && Object.keys(values).length === 0) {
      setValues({ ...defaults.data });
    }
  }, [defaults.data, values]);

  // Debounced capability probe: `model` is typed, so the request must not fire per key.
  const [probedPath, setProbedPath] = useState('');
  useEffect(() => {
    const handle = setTimeout(() => setProbedPath(modelPath.trim()), 350);
    return () => clearTimeout(handle);
  }, [modelPath]);

  const capabilities = useQuery({
    queryKey: ['capabilities', probedPath, effectiveRuntime],
    queryFn: () => modelsApi.vramEstimate({ model: probedPath }),
    enabled: Boolean(probedPath) && probedPath.toLowerCase().endsWith('.gguf'),
    staleTime: 60_000,
  });

  const schemaGroups = schema.data?.groups ?? [];
  const params = schema.data?.params ?? [];
  const [query, setQuery] = useState('');

  /**
   * Group sections open by default per the **backend's** `default_open`, which has
   * existed since 1.0 and was never read. The old UI instead showed one group at a
   * time behind eight tabs, so seeing every parameter meant eight clicks.
   */
  const [openGroups, setOpenGroups] = useState<Record<string, boolean> | null>(null);
  const effectiveOpen = useMemo(() => {
    if (openGroups) return openGroups;
    const initial: Record<string, boolean> = {};
    for (const entry of schemaGroups) initial[entry.key] = Boolean(entry.default_open);
    return initial;
  }, [openGroups, schemaGroups]);

  /**
   * A value is "active" when its `depends_on*` conditions hold. The backend has
   * modelled these 39 conditions all along and uses them when building the command;
   * the UI rendered every dependent parameter regardless, so the draft-model group
   * showed all 19 controls even with speculative decoding off.
   */
  const isActive = (param: ParamMeta): boolean => {
    const dependsOn = param.depends_on;
    const dependsAny = param.depends_on_any;
    const dependsNonempty = param.depends_on_nonempty;
    if (dependsOn) {
      for (const [key, expected] of Object.entries(dependsOn)) {
        if (String(values[key] ?? '') !== String(expected ?? '')) return false;
      }
    }
    if (dependsAny) {
      for (const [key, allowed] of Object.entries(dependsAny)) {
        if (!allowed.map(String).includes(String(values[key] ?? ''))) return false;
      }
    }
    if (dependsNonempty) {
      const source = values[dependsNonempty];
      if (source === undefined || source === null || String(source).trim() === '') return false;
    }
    return true;
  };

  /** Groups with the parameters that survive the search box, in backend order. */
  const sections = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matches = (param: ParamMeta) => {
      if (!needle) return true;
      return [param.key, param.label, param.label_en, param.summary, param.summary_en, param.cli]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(needle));
    };
    return schemaGroups.map((entry) => {
      const all = params.filter((param) => param.group === entry.key);
      const visible = all.filter(matches);
      return {
        ...entry,
        total: all.length,
        visible,
        // A search hit must be reachable without hunting: force the section open.
        open: needle ? visible.length > 0 : Boolean(effectiveOpen[entry.key]),
      };
    });
  }, [schemaGroups, params, query, effectiveOpen]);

  const totalHits = sections.reduce((sum, section) => sum + section.visible.length, 0);

  const command = useQuery({
    queryKey: ['params', 'command', effectiveRuntime, values, modelPath],
    queryFn: () =>
      paramsApi.buildCommand({ ...values, model: modelPath.trim() || undefined }, effectiveRuntime || undefined),
    enabled: Boolean(schema.data),
    staleTime: 5_000,
  });

  const nativeContext = Number(
    (capabilities.data?.native_context as number | undefined) ??
      (capabilities.data?.n_ctx_train as number | undefined) ??
      0,
  );
  const ctxValue = Number(values.ctx_size ?? 0);
  const ctxTooHigh = nativeContext > 0 && ctxValue > nativeContext;

  const launch = async () => {
    if (!modelPath.trim()) {
      toast.error(translate('params.modelRequired'));
      return;
    }
    setLaunching(true);
    try {
      const body = { ...values, model: modelPath.trim() };
      try {
        const preflight = await instancesApi.diagnose({
          params: body,
          runtime_id: effectiveRuntime || undefined,
        });
        const blocking = preflight.blocking ?? [];
        if (blocking.length) {
          toast.error(blocking.map((entry) => entry.message).join('; '));
          return;
        }
      } catch {
        // The start path validates again; a failed preflight must not block intent.
      }
      await instancesApi.create({ params: body, runtime_id: effectiveRuntime || undefined });
      toast.success(translate('dash.launched'));
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    } finally {
      setLaunching(false);
    }
  };

  if (schema.isPending || defaults.isPending) return <LoadingState />;
  if (schema.isError) {
    return (
      <ErrorState
        message={localizeBackendError(schema.error instanceof Error ? schema.error.message : schema.error)}
        action={
          <Button variant="secondary" onClick={() => void schema.refetch()}>
            {translate('common.retry')}
          </Button>
        }
      />
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="border-b border-[var(--border)] px-5 pb-3 pt-4">
        <SectionHeader
          title={translate('params.title')}
          description={translate('params.subtitle')}
          action={
            <Button variant="primary" onClick={() => void launch()} loading={launching}>
              <Play size={14} aria-hidden="true" />
              {translate('params.launchNow')}
            </Button>
          }
        />
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <Field label={translate('params.runtimeSelect')} hint={translate('params.runtimeSelectHint')}>
            <select
              value={effectiveRuntime}
              onChange={(event) => setRuntimeId(event.target.value)}
              className="h-[var(--control-h)] w-full rounded-[var(--radius-md)] border border-[var(--border-control)] bg-[var(--surface)] px-2 text-[length:var(--text-sm)] outline-none focus:ring-2 focus:ring-[var(--focus-ring)]"
            >
              {profiles.map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.display_name}
                </option>
              ))}
            </select>
          </Field>
          <Field label={translate('params.model')} htmlFor="param-model">
            <PathInput
              id="param-model"
              kind="file"
              value={modelPath}
              onValueChange={setModelPath}
              placeholder={translate('dash.model')}
              filters={pathFiltersFor('model')}
            />
          </Field>
        </div>
      </div>

      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/*
          One scrolling column of collapsible sections instead of eight tabs.
          The backend already marks three groups `default_open`; everything else
          stays collapsed until it is needed, and a search forces the matching
          sections open.
        */}
        <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-6 pt-3">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={translate('params.searchPlaceholder')}
              aria-label={translate('params.searchPlaceholder')}
              data-testid="params-search"
              inputSize="sm"
              className="max-w-sm"
            />
            {query.trim() ? (
              <span className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)] tnum">
                {totalHits > 0
                  ? translate('params.groupCount', { count: totalHits })
                  : translate('params.noMatch', { query: query.trim() })}
              </span>
            ) : null}
          </div>

          {ctxTooHigh ? (
            <p className="mb-3 rounded-[var(--radius-sm)] bg-[var(--warning-subtle)] px-2.5 py-1.5 text-[length:var(--text-xs)] text-[var(--warning)]">
              {translate('params.ctxAboveNative', { native: nativeContext.toLocaleString() })}
            </p>
          ) : null}

          <div className="flex flex-col gap-2">
            {sections.map((section) => {
              const active = section.visible.filter(isActive);
              const hiddenByCondition = section.visible.length - active.length;
              return (
                <section
                  key={section.key}
                  data-testid={`param-group-${section.key}`}
                  data-open={section.open ? 'true' : 'false'}
                  className="rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--panel)]"
                >
                  <button
                    type="button"
                    aria-expanded={section.open}
                    onClick={() =>
                      setOpenGroups({ ...effectiveOpen, [section.key]: !section.open })
                    }
                    className="flex w-full items-center gap-2 px-3 py-2 text-start"
                  >
                    <ChevronRight
                      size={13}
                      aria-hidden="true"
                      className={cn(
                        'shrink-0 text-[var(--text-tertiary)] transition-transform duration-100',
                        section.open && 'rotate-90',
                      )}
                    />
                    <span className="flex-1 text-[length:var(--text-sm)] font-medium text-[var(--text)]">
                      {section.label}
                    </span>
                    {hiddenByCondition > 0 ? (
                      <span className="text-[length:var(--text-2xs)] text-[var(--text-quaternary)]">
                        {translate('params.hiddenByDependency', { count: hiddenByCondition })}
                      </span>
                    ) : null}
                    <span className="tnum text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                      {translate('params.groupCount', { count: section.visible.length })}
                    </span>
                  </button>

                  {section.open ? (
                    <div className="grid grid-cols-1 gap-3 border-t border-[var(--border)] p-3 lg:grid-cols-2">
                      {active.map((param) => (
                        <ParamControl
                          key={param.key}
                          param={param}
                          value={values[param.key]}
                          onChange={(next) =>
                            setValues((current) => ({ ...current, [param.key]: next }))
                          }
                          nativeContext={param.key === 'ctx_size' ? nativeContext : 0}
                        />
                      ))}
                      {active.length === 0 ? (
                        <p className="text-[length:var(--text-xs)] text-[var(--text-tertiary)] lg:col-span-2">
                          {translate('common.none')}
                        </p>
                      ) : null}
                    </div>
                  ) : null}
                </section>
              );
            })}
          </div>
        </div>

        <aside className="w-[24rem] shrink-0 overflow-y-auto border-s border-[var(--border)] bg-[var(--panel)] p-3">
          <p className="mb-2 flex items-center gap-1.5 text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
            <Terminal size={13} aria-hidden="true" />
            {translate('params.cmdTitle')}
          </p>
          {command.data ? (
            <>
              <pre className="max-h-[18rem] overflow-auto whitespace-pre-wrap break-anywhere rounded-[var(--radius-sm)] bg-[var(--code-bg)] p-2 font-mono text-[length:var(--text-2xs)] text-[var(--code-fg)]">
                {command.data.command}
              </pre>
              {command.data.suppressed.length ? (
                <div className="mt-2">
                  <p className="text-[length:var(--text-2xs)] text-[var(--warning)]">
                    {translate('params.suppressedTitle')}
                  </p>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {command.data.suppressed.map((key) => (
                      <Badge key={key} tone="warning">
                        {key}
                      </Badge>
                    ))}
                  </div>
                </div>
              ) : null}
            </>
          ) : (
            <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
              {translate('params.cmdEmpty')}
            </p>
          )}

          {capabilities.data ? (
            <div className="mt-3 rounded-[var(--radius-sm)] border border-[var(--border)] p-2">
              <p className="text-[length:var(--text-2xs)] font-medium text-[var(--text-secondary)]">
                {translate('params.vramEstimate')}
              </p>
              <p className="mt-1 text-[length:var(--text-2xs)] tnum text-[var(--text)]">
                {typeof capabilities.data.estimated_bytes === 'number'
                  ? formatBytes(capabilities.data.estimated_bytes as number)
                  : '—'}
              </p>
              <p className="mt-1 text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                {translate('params.vramApprox')}
              </p>
            </div>
          ) : null}
        </aside>
      </div>
    </div>
  );
}

function ParamControl({
  param,
  value,
  onChange,
  nativeContext,
}: {
  param: ParamMeta;
  value: unknown;
  onChange: (next: unknown) => void;
  nativeContext: number;
}) {
  const unsupported = param.supported === false;
  const wide = ['text', 'password', 'file', 'dir', 'multi', 'select'].includes(String(param.type));
  const label = param.label || param.key;

  /**
   * Control type, taken from the backend's vocabulary.
   *
   * The backend emits `switch` / `float` / `password` / `multi`; the old renderer
   * tested for `'boolean'` and fell through everything else to a text input. The
   * consequences were not cosmetic: all 14 switches rendered as a text box holding
   * `"true"`/`"false"`, all 7 floats as text, and — worst — the single `password`
   * parameter (`api_key`) rendered **in clear text**.
   */
  const kind = String(param.type);

  if (kind === 'switch' || kind === 'boolean') {
    return (
      <div
        className={cn(
          'rounded-[var(--radius-sm)] border border-[var(--border)] px-2.5 py-2',
          unsupported && 'opacity-60',
        )}
      >
        <Switch
          checked={Boolean(value)}
          onCheckedChange={onChange}
          disabled={unsupported}
          label={label}
          description={param.summary ?? undefined}
        />
        <ParamHelp help={param.help} helpEn={param.help_en} summaryShown />
        {unsupported ? (
          <p className="mt-1 text-[length:var(--text-2xs)] text-[var(--warning)]">
            {translate('params.unsupported')}
          </p>
        ) : null}
      </div>
    );
  }

  if (kind === 'select' || kind === 'multi') {
    const options = param.upstream_options ?? param.options ?? [];
    const labels = param.option_labels ?? {};
    const current = Array.isArray(value) ? value.map(String) : [String(value ?? '')];
    return (
      <Field label={label} className={wide ? 'lg:col-span-2' : undefined}>
        <select
          value={current[0] ?? ''}
          onChange={(event) => onChange(event.target.value)}
          disabled={unsupported}
          multiple={kind === 'multi'}
          className="h-[var(--control-h)] w-full rounded-[var(--radius-md)] border border-[var(--border-control)] bg-[var(--surface)] px-2 text-[length:var(--text-sm)] outline-none focus:ring-2 focus:ring-[var(--focus-ring)] disabled:opacity-50"
        >
          <option value="">{translate('common.none')}</option>
          {options.map((option) => (
            // `option_labels` carries the human wording for CLI values; showing the
            // raw flag value is what made selects read like config files.
            <option key={option} value={option}>
              {labels[option] ?? option}
            </option>
          ))}
        </select>
        <ParamHelp help={param.help} helpEn={param.help_en} summary={param.summary} />
        {unsupported ? (
          <p className="text-[length:var(--text-2xs)] text-[var(--warning)]">
            {translate('params.unsupported')}
          </p>
        ) : null}
      </Field>
    );
  }

  // Path fields get the native picker; everything else is a text/number box.
  if (kind === 'file' || kind === 'dir') {
    return (
      <Field
        label={label}
        htmlFor={`param-${param.key}`}
        className={wide ? 'lg:col-span-2' : undefined}
      >
        <PathInput
          id={`param-${param.key}`}
          kind={kind}
          value={value === null || value === undefined ? '' : String(value)}
          onValueChange={onChange}
          placeholder={param.placeholder ?? undefined}
          disabled={unsupported}
          filters={pathFiltersFor(param.key)}
        />
        <ParamHelp help={param.help} helpEn={param.help_en} summary={param.summary} />
        {unsupported ? (
          <p className="text-[length:var(--text-2xs)] text-[var(--warning)]">
            {translate('params.unsupported')}
          </p>
        ) : null}
      </Field>
    );
  }

  const numeric = kind === 'number' || kind === 'float';
  return (
    <Field
      label={label}
      htmlFor={`param-${param.key}`}
      className={wide ? 'lg:col-span-2' : undefined}
    >
      <Input
        id={`param-${param.key}`}
        // `password` was falling through to `text`, printing the API key on screen.
        type={kind === 'password' ? 'password' : numeric ? 'number' : 'text'}
        step={kind === 'float' ? (param.step ?? 'any') : undefined}
        value={value === null || value === undefined ? '' : String(value)}
        onChange={(event) => {
          const raw = event.target.value;
          if (numeric) {
            onChange(raw === '' ? '' : Number(raw));
          } else {
            onChange(raw);
          }
        }}
        // The native ceiling is a state, not a description: it belongs on the control.
        min={param.min ?? undefined}
        max={param.key === 'ctx_size' && nativeContext > 0 ? nativeContext : (param.max ?? undefined)}
        placeholder={param.placeholder ?? undefined}
        disabled={unsupported}
        trailing={
          param.unit ? <span className="text-[length:var(--text-2xs)]">{param.unit}</span> : undefined
        }
      />
      <ParamHelp help={param.help} helpEn={param.help_en} summary={param.summary} />
      {unsupported ? (
        <p className="text-[length:var(--text-2xs)] text-[var(--warning)]">
          {translate('params.unsupported')}
        </p>
      ) : null}
    </Field>
  );
}

/**
 * Extension filters per parameter, so the picker does not make the user hunt.
 *
 * Keyed by the parameter's own key rather than by label text, and deliberately
 * narrow: only `.gguf` for models and `.exe` for the server binary. Anything else
 * gets an unfiltered dialog rather than a wrong filter that hides the real file.
 */
function pathFiltersFor(key: string): PathFilter[] | undefined {
  if (['model', 'model_draft', 'mmproj', 'embedding_model', 'embedding_model_path'].includes(key)) {
    return [{ name: 'GGUF model', extensions: ['gguf'] }];
  }
  if (['llama_server_path', 'runtime_path', 'executable_path'].includes(key)) {
    return [{ name: 'Executable', extensions: ['exe'] }];
  }
  return undefined;
}

/**
 * One line of short description, with the full text behind a disclosure.
 *
 * The previous UI printed the entire `help` paragraph under every control. With 92
 * parameters that is a wall of prose, and the user's report was exactly that:
 * "descriptions dumped inline, bloated interface". Detail is still one click away —
 * nothing is lost, it is just not shouting.
 */
function ParamHelp({
  help,
  summary,
  helpEn,
  summaryShown = false,
}: {
  help?: string | null;
  summary?: string | null;
  helpEn?: string | null;
  /** Set when the control already renders `summary` (the switch branch). */
  summaryShown?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const hasDetail = Boolean(help && help.trim());
  if (!hasDetail) return null;
  const short = summary && summary.trim() ? summary : '';
  return (
    <div className="mt-1">
      {!summaryShown && short ? (
        <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">{short}</p>
      ) : null}
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="mt-0.5 text-[length:var(--text-2xs)] text-[var(--accent-text)] hover:underline"
      >
        {open ? translate('params.hideDetail') : translate('params.showDetail')}
      </button>
      {open ? (
        <p className="mt-1 whitespace-pre-wrap text-[length:var(--text-2xs)] leading-[var(--leading-relaxed)] text-[var(--text-tertiary)]">
          {help}
          {helpEn ? <span className="sr-only"> {helpEn}</span> : null}
        </p>
      ) : null}
    </div>
  );
}

export default ParamsPage;
