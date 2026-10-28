// 启动前检查与失败归因的共享 UI。
//
// 后端只返回 {code, severity, detail}；标题与修复建议在这里按 code 从 i18n
// 字典取（`diag.<code>.title` / `diag.<code>.fix`）。缺翻译时退回 code 本身，
// 保证新后端检查项在旧前端上仍然可见，而不是空白。

import { t } from './i18n.mjs';

const SEVERITIES = ['error', 'warning', 'info'];

function translated(key) {
  const value = t(key);
  return value === key ? '' : value;
}

export function checkTitle(code) {
  return translated(`diag.${code}.title`) || code;
}

export function checkFix(code) {
  return translated(`diag.${code}.fix`);
}

export function checksHtml(ctx, checks) {
  const items = (checks || [])
    .map((check) => {
      const severity = SEVERITIES.includes(check.severity) ? check.severity : 'info';
      const fix = checkFix(check.code);
      return `
        <div class="diag-item diag-${severity}">
          <div class="diag-head">
            <span class="diag-severity">${ctx.escapeHtml(t(`diag.severity.${severity}`))}</span>
            <span class="diag-title">${ctx.escapeHtml(checkTitle(check.code))}</span>
          </div>
          <div class="diag-detail">${ctx.escapeHtml(check.detail || '')}</div>
          ${fix ? `<div class="diag-fix"><strong>${ctx.escapeHtml(t('diag.fixLabel'))}</strong> ${ctx.escapeHtml(fix)}</div>` : ''}
        </div>`;
    })
    .join('');
  return `<div class="diag-list">${items}</div>`;
}

/**
 * Run the server-side preflight for a launch body and ask the user what to do.
 * Returns true when the caller should proceed with createInstance().
 *
 * Fail-open on transport errors: a diagnostic service outage must not make the
 * launcher unusable. Hard validation still happens in the backend.
 */
export async function confirmLaunch(ctx, body) {
  let result;
  try {
    result = await ctx.Api.diagnoseLaunch(body);
  } catch (error) {
    console.warn('[preflight] skipped:', error);
    return true;
  }
  const checks = result && Array.isArray(result.checks) ? result.checks : [];
  const blocking = checks.filter((check) => check.severity === 'error');
  const notable = checks.filter(
    (check) => check.severity === 'error' || check.severity === 'warning'
  );
  if (!notable.length) return true;
  // off = 只有错误才打断；警告直接放行，不弹“仍然启动”的确认。
  // strict 由后端把 warning 也算进 blocking，前端不再自行解释策略。
  const guardrail = result && result.guardrail;
  if (guardrail === 'off' && blocking.length === 0) return true;

  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    const blocked =
      blocking.length > 0 || (result && result.blocking === true);
    const intro = blocked ? t('preflight.blockedIntro') : t('preflight.warnIntro');
    const actions = blocked
      ? [
          { label: t('app.close'), class: 'btn-secondary', onClick: () => done(false) },
          {
            label: t('preflight.openGuide'),
            class: 'btn-primary',
            onClick: () => {
              done(false);
              ctx.navigate('help');
            },
          },
        ]
      : [
          { label: t('app.cancel'), class: 'btn-secondary', onClick: () => done(false) },
          {
            label: t('preflight.launchAnyway'),
            class: 'btn-primary',
            onClick: () => done(true),
          },
        ];
    // 叠加在宿主表单之上：blocked/取消后表单仍然存在，输入不会丢失。
    // 关闭 X/遮罩同样走 onClose → done(false)，调用方不会永远挂着。
    ctx.showModal(
      t(blocked ? 'preflight.blockedTitle' : 'preflight.warnTitle'),
      `<div class="diag-intro">${ctx.escapeHtml(intro)}</div>${checksHtml(ctx, checks)}`,
      actions,
      { stack: true, onClose: () => done(false) }
    );
  });
}

/** Explain a failed instance; fetches the server diagnosis when the payload lacks it. */
export async function showDiagnosisModal(ctx, instance, checks) {
  let list = Array.isArray(checks) ? checks : [];
  if (!list.length) {
    try {
      const payload = await ctx.Api.getInstanceDiagnosis(instance.id);
      list = (payload && payload.diagnosis) || [];
    } catch (error) {
      console.warn('[diagnosis] fetch failed:', error);
    }
  }
  const fallback = instance.error_message
    ? `<div class="diag-intro">${ctx.escapeHtml(instance.error_message)}</div>`
    : '';
  const content = list.length
    ? checksHtml(ctx, list)
    : `${fallback}<div class="diag-intro">${ctx.escapeHtml(t('diag.noDetail'))}</div>`;
  ctx.showModal(
    t('diag.modalTitle', { name: instance.name || instance.id }),
    content,
    [
      { label: t('app.close'), class: 'btn-secondary' },
      {
        label: t('diag.openLogs'),
        class: 'btn-secondary',
        onClick: () => {
          window.__pendingLogsInstanceId = instance.id;
          window.__pendingObservabilityTab = 'logs';
  ctx.navigate('observability');
        },
      },
      {
        label: t('diag.openGuide'),
        class: 'btn-primary',
        onClick: () => ctx.navigate('help'),
      },
    ]
  );
}
