// Pure rendering for the parameter editor.
// State (schema, currentValues, runtime) stays in param_editor.js; this module
// turns one parameter/group into the same HTML the editor has always emitted.

import { mergeUpstreamOptions } from './runtime.mjs';

export function isUnsupported(p) {
  return p && p.supported === false;
}

export function isWideField(p) {
  return (
    p.multiple ||
    p.type === 'file' ||
    p.type === 'dir' ||
    p.type === 'password' ||
    p.type === 'text'
  );
}

function splitOptionLabel(label) {
  const m = String(label).match(/^([^(（]+)[（(]([^）)]*)[）)]\s*$/);
  if (!m) return { title: String(label), desc: '' };
  return { title: m[1].trim(), desc: m[2].trim() };
}

export function createParamView({ escapeHtml, t, locale, getValues }) {
  const paramLabel = (p) => (locale === 'en-US' && p.label_en ? p.label_en : p.label);
  const paramHelp = (p) => (locale === 'en-US' && p.help_en ? p.help_en : p.help);
  const paramControlId = (p) => `param-${p.key}`;
  const optionLabel = (p, opt) => {
    const labels =
      locale === 'en-US' && p.option_labels_en ? p.option_labels_en : p.option_labels;
    if (labels && labels[opt] !== undefined) return labels[opt];
    return opt;
  };
  const groupLabel = (g) =>
    locale === 'en-US' && g.label_en ? g.label_en : g.label;

  function multiSelectHtml(p, value) {
    const selected = Array.isArray(value) ? value.map(String) : [];
    const controlId = paramControlId(p);
    const describedBy = paramHelp(p) ? ` aria-describedby="${controlId}-help"` : '';
    const options = mergeUpstreamOptions(p.options, p.upstream_options);
    const disabled = isUnsupported(p) ? ' disabled' : '';
    const cards = options
      .map((opt) => {
        const raw = String(opt);
        const { title, desc } = splitOptionLabel(optionLabel(p, opt));
        const checked = selected.includes(raw) ? 'checked' : '';
        return `
      <label class="option-card${checked ? ' selected' : ''}">
        <input type="checkbox" data-opt="${escapeHtml(raw)}" ${checked}${disabled}/>
        <span class="option-card-text">
          <span class="option-card-title">${escapeHtml(title)}</span>
          ${desc ? `<span class="option-card-desc">${escapeHtml(desc)}</span>` : ''}
        </span>
        <span class="option-card-check" aria-hidden="true">✓</span>
      </label>`;
      })
      .join('');
    return `
    <div class="option-cards" id="${controlId}" data-key="${p.key}" data-multiple="1"
         role="group" aria-labelledby="${controlId}-label"${describedBy}>
      <div class="option-cards-head">
        <span data-count aria-live="polite"></span>
        <button type="button" class="btn btn-sm btn-ghost" data-clear>${escapeHtml(
          t('params.multiClear')
        )}</button>
      </div>
      ${cards}
    </div>`;
  }

  function controlHtml(p) {
    const v = getValues()[p.key];
    const controlId = paramControlId(p);
    const describedBy = paramHelp(p) ? ` aria-describedby="${controlId}-help"` : '';
    const disabled = isUnsupported(p) ? ' disabled' : '';
    switch (p.type) {
      case 'number':
      case 'float': {
        const step = p.step || (p.type === 'float' ? 'any' : '1');
        const input = `<input type="number" class="input" id="${controlId}" data-key="${
          p.key
        }" value="${escapeHtml(v ?? '')}" min="${p.min ?? ''}" max="${
          p.max ?? ''
        }" step="${step}" placeholder="${escapeHtml(p.placeholder || '')}"${describedBy}${disabled} />`;
        return p.unit
          ? `<span class="input-unit-wrap">${input}<span class="input-unit">${escapeHtml(
              String(p.unit)
            )}</span></span>`
          : input;
      }
      case 'select': {
        if (p.multiple) return multiSelectHtml(p, v);
        const merged = mergeUpstreamOptions(p.options, p.upstream_options);
        const opts = merged
          .map((opt) => {
            const label = optionLabel(p, opt);
            const sel = String(opt) === String(v) ? 'selected' : '';
            return `<option value="${escapeHtml(String(opt))}" ${sel}>${escapeHtml(
              String(label)
            )}</option>`;
          })
          .join('');
        return `<select class="select" id="${controlId}" data-key="${p.key}"${describedBy}${disabled}>${opts}</select>`;
      }
      case 'switch': {
        const checked = v ? 'checked' : '';
        return `<label class="switch"><input type="checkbox" id="${controlId}" data-key="${
          p.key
        }" aria-labelledby="${controlId}-label"${describedBy} ${checked}${disabled}/><span class="switch-slider"></span></label>`;
      }
      case 'file':
      case 'dir': {
        const val = v || '';
        return `<input type="text" class="input" id="${controlId}" data-key="${
          p.key
        }" value="${escapeHtml(val)}" placeholder="${escapeHtml(
          p.placeholder || ''
        )}"${describedBy}${disabled} /><button class="btn btn-secondary btn-sm" data-browse="${
          p.key
        }" data-browse-type="${p.type}" aria-label="${escapeHtml(
          `${t('params.browse')} ${paramLabel(p)}`
        )}"${disabled}>${escapeHtml(t('params.browse'))}</button>`;
      }
      case 'password':
        return `<input type="password" class="input" id="${controlId}" data-key="${
          p.key
        }" value="${escapeHtml(v ?? '')}" placeholder="${escapeHtml(
          p.placeholder || ''
        )}" autocomplete="off"${describedBy}${disabled} />`;
      case 'text':
      default:
        return `<input type="text" class="input" id="${controlId}" data-key="${
          p.key
        }" value="${escapeHtml(v ?? '')}" placeholder="${escapeHtml(
          p.placeholder || ''
        )}"${describedBy}${disabled} />`;
    }
  }

  function paramHtml(p) {
    const requiredMark = p.required ? `<span class="required">*</span>` : '';
    const unsupported = isUnsupported(p);
    const badge = unsupported
      ? `<span class="tag tag-warn" style="margin-left:6px;" title="${escapeHtml(
          t('params.unsupportedHint')
        )}">${escapeHtml(t('params.unsupported'))}</span>`
      : '';
    const hint = unsupported
      ? `<div class="param-field-warn">${escapeHtml(t('params.unsupportedHint'))}</div>`
      : '';
    const helpText = paramHelp(p);
    const tipContent = helpText ? `${paramLabel(p)}\n${helpText}` : '';
    const tip = tipContent
      ? `<button type="button" class="param-help-tip" data-tip="${escapeHtml(
          tipContent
        )}" aria-label="${escapeHtml(
          `${paramLabel(p)} ${t('params.helpTipAria')}`
        )}">?</button>`
      : '';
    const isSwitch = p.type === 'switch';
    const controlId = paramControlId(p);
    const labelHtml = isSwitch
      ? `<span class="param-field-name" id="${controlId}-label">${escapeHtml(
          paramLabel(p)
        )}</span>`
      : `<label class="param-field-name" id="${controlId}-label" for="${controlId}">${escapeHtml(
          paramLabel(p)
        )}</label>`;
    const control = controlHtml(p);
    const classes = [
      'param-field',
      unsupported ? 'param-field-unsupported' : '',
      isWideField(p) ? 'param-field-wide' : '',
      isSwitch ? 'param-field-switch' : '',
    ]
      .filter(Boolean)
      .join(' ');
    return `
    <div class="${classes}" data-param="${p.key}" data-key="${p.key}">
      <div class="param-field-label">
        ${labelHtml}${requiredMark}
        ${p.cli ? `<span class="tag tag-default" style="margin-left:6px;">${escapeHtml(p.cli)}</span>` : ''}
        ${badge}${tip}${isSwitch ? control : ''}
      </div>
      ${isSwitch ? '' : `<div class="param-field-control">${control}</div>`}
      ${hint}
      ${
        helpText
          ? `<span class="visually-hidden" id="${controlId}-help">${escapeHtml(helpText)}</span>`
          : ''
      }
    </div>
  `;
  }

  function groupHtml(g) {
    return `
    <div class="collapse open" data-group="${g.key}">
      <div class="collapse-header">
        <span class="collapse-arrow">▶</span>
        <span class="collapse-title">${escapeHtml(groupLabel(g))}</span>
      </div>
      <div class="collapse-body" data-group-body="${g.key}"></div>
    </div>
  `;
  }

  return {
    paramLabel,
    paramHelp,
    paramControlId,
    optionLabel,
    groupLabel,
    paramHtml,
    groupHtml,
    multiSelectHtml,
    controlHtml,
  };
}
