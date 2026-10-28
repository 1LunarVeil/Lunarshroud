// 资源监控页面
// Canvas 绘制 60 秒滚动折线图（CPU/内存/GPU 利用率）+ GPU 表格 + 进程关联
// 纯原生 Canvas API，不引第三方库

import { createSingleFlight } from './runtime.mjs';
import { t } from './i18n.mjs';
import { emptyStateHtml } from './panel_views.mjs';

export const id = 'monitor';
export const title = '资源监控';

let ctx = null;       // app 上下文
let root = null;
let canvas = null;
let c2d = null;       // Canvas 2D 上下文
let watcher = null;
let metrics = [];     // 数据点列表：{t, cpu, mem, gpuUtil}
const MAX_HISTORY_SECONDS = 60;
// 数据点条数硬上限：时间戳乱序会让时间裁剪失效，防无界增长
const MAX_METRIC_POINTS = 1200;
let overviewTimer = null;  // 进程关联轮询
let connToastShown = false; // 每次断线只提示一次

// 线条颜色（运行时根据当前主题从 CSS 变量读取）
function getChartColors() {
  const style = getComputedStyle(document.body);
  const value = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
  return {
    cpu: value('--chart-cpu', '#EF4444'),
    mem: value('--chart-memory', '#10B981'),
    gpu: value('--chart-gpu', '#F59E0B'),
    grid: value('--chart-grid', '#E5E7EB'),
    text: value('--color-text-secondary', '#6B7280'),
    axis: value('--chart-axis', '#9CA3AF'),
  };
}
let COLORS = getChartColors();

export function init(el, appCtx) {
  ctx = appCtx;
  root = el;
  root.innerHTML = `
    <div class="page-header">
      <div>
        <h1 class="page-title">${ctx.escapeHtml(t('monitor.title'))}</h1>
        <div class="page-subtitle" id="monitor-subtitle">${ctx.escapeHtml(t('monitor.subtitle'))}</div>
      </div>
      <div class="page-actions">
        <button class="btn btn-secondary" id="monitor-refresh">${ctx.escapeHtml(t('monitor.refreshGpu'))}</button>
      </div>
    </div>
    <div class="monitor-grid">
      <div class="metric-chart-card">
        <div class="card-header">
          <span class="card-title">${ctx.escapeHtml(t('monitor.usageChart'))}</span>
        </div>
        <canvas id="metric-canvas"></canvas>
        <div class="metric-legend">
          <span class="item"><span class="dot dot-cpu"></span>${ctx.escapeHtml(t('monitor.cpu'))}</span>
          <span class="item"><span class="dot dot-memory"></span>${ctx.escapeHtml(t('monitor.memory'))}</span>
          <span class="item"><span class="dot dot-gpu"></span>${ctx.escapeHtml(t('monitor.gpuUtil'))}</span>
        </div>
      </div>
      <div class="card">
        <div class="card-header">
          <span class="card-title">${ctx.escapeHtml(t('monitor.gpuList'))}</span>
        </div>
        <div class="table-wrap" id="gpu-table-wrap">
          <div class="empty-state">${ctx.escapeHtml(t('monitor.waiting'))}</div>
        </div>
      </div>
      <div class="card">
        <div class="card-header">
          <span class="card-title">${ctx.escapeHtml(t('monitor.procTitle'))}</span>
        </div>
        <div class="table-wrap" id="proc-table-wrap">
          <div class="empty-state">${ctx.escapeHtml(t('monitor.waiting'))}</div>
        </div>
      </div>
    </div>
  `;
  canvas = root.querySelector('#metric-canvas');
  c2d = canvas.getContext('2d');
  root.querySelector('#monitor-refresh').addEventListener('click', refreshOverview);
  // 主题变更时重绘并更新图例颜色
  document.addEventListener('theme-changed', () => {
    COLORS = getChartColors();
    updateLegendColors();
    drawChart();
  });
}

export function onShow() {
  updateSubtitle();
  resizeCanvas();
  drawChart();
  // 启动 WebSocket 监听（断线自动重连）
  if (watcher) watcher.close();
  connToastShown = false;
  watcher = ctx.Api.watchSystemMetrics({
    onMessage: (msg) => {
      pushMetric(msg);
      updateGpuTable(msg);
      drawChart();
    },
    onStatus: renderConnState,
  });
  // 启动进程关联轮询（10s 一次）
  refreshOverviewOnce();
  overviewTimer = setInterval(refreshOverviewOnce, 10000);
  // 窗口尺寸变化重绘
  window.addEventListener('resize', onResize);
}

export function onHide() {
  if (watcher) {
    watcher.close();
    watcher = null;
  }
  if (overviewTimer) clearInterval(overviewTimer);
  overviewTimer = null;
  window.removeEventListener('resize', onResize);
}

// 副标题显示真实的推送间隔（来自后端配置，避免文案与实际不符）
async function updateSubtitle() {
  const el = root.querySelector('#monitor-subtitle');
  if (!el) return;
  try {
    const cfg = await ctx.Api.getSystemConfig();
    const interval = Number(cfg && cfg.monitor_interval_seconds) || 5;
    el.textContent = t('monitor.subtitleWithInterval', { seconds: interval });
  } catch {
    el.textContent = t('monitor.subtitle');
  }
}

// 连接状态提示：每次断线只弹一次 toast，重连成功后重置
function renderConnState(state) {
  if (state === 'reconnecting' && !connToastShown) {
    connToastShown = true;
    ctx.showToast(t('monitor.reconnectingToast'), 'warning');
  } else if (state === 'connected') {
    connToastShown = false;
  }
}

function onResize() {
  resizeCanvas();
  drawChart();
}

function resizeCanvas() {
  // 适配高 DPI 屏幕
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 600;
  const cssH = parseInt(getComputedStyle(canvas).height) || 220;
  canvas.width = cssW * dpr;
  canvas.height = cssH * dpr;
  c2d.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function pushMetric(msg) {
  if (!msg || msg.timestamp === undefined) return;
  // 选取第一个 GPU 的利用率作为图表的 GPU 线
  let gpuUtil = 0;
  if (Array.isArray(msg.gpus) && msg.gpus.length > 0) {
    gpuUtil = msg.gpus[0].utilization || 0;
  }
  // Date.parse 失败会得到 NaN，混进 metrics 会让裁剪失效、曲线画不出来；
  // 退回本地当前时间，保证时间轴单调可用。
  const parsed = typeof msg.timestamp === 'number' ? msg.timestamp : Date.parse(msg.timestamp) / 1000;
  const ts = Number.isFinite(parsed) ? parsed : Date.now() / 1000;
  metrics.push({
    t: ts,
    cpu: msg.cpu_percent ?? 0,
    mem: msg.memory_percent ?? 0,
    gpuUtil,
  });
  // 清理过期数据
  const cutoff = ts - MAX_HISTORY_SECONDS;
  while (metrics.length > 0 && metrics[0].t < cutoff) metrics.shift();
  // 乱序时间戳会让上面的裁剪失效（首元素不过期就整段保留），补一道条数上限
  if (metrics.length > MAX_METRIC_POINTS) {
    metrics.splice(0, metrics.length - MAX_METRIC_POINTS);
  }
}

// ========== Canvas 绘图 ==========

function updateLegendColors() {
  // The legend colors come from the same custom properties the canvas reads,
  // so a theme switch only has to repaint the chart.
}

function drawChart() {
  if (!c2d) return;
  const colors = getChartColors();
  const w = canvas.clientWidth || 600;
  const h = parseInt(getComputedStyle(canvas).height) || 220;
  const pad = { left: 40, right: 12, top: 12, bottom: 24 };
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  // 清空
  c2d.clearRect(0, 0, w, h);
  c2d.fillStyle = 'transparent';

  // 背景网格（4 条横线）
  c2d.strokeStyle = colors.grid;
  c2d.lineWidth = 1;
  c2d.fillStyle = colors.text;
  c2d.font = '11px sans-serif';
  c2d.textAlign = 'right';
  c2d.textBaseline = 'middle';
  for (let i = 0; i <= 4; i++) {
    const y = pad.top + (plotH * i) / 4;
    c2d.beginPath();
    c2d.moveTo(pad.left, y);
    c2d.lineTo(pad.left + plotW, y);
    c2d.stroke();
    const pct = 100 - (i * 25);
    c2d.fillText(`${pct}%`, pad.left - 6, y);
  }

  // X 轴刻度（-60s ~ 0s，每 15s 一个刻度）
  c2d.textAlign = 'center';
  c2d.textBaseline = 'top';
  const nowTs = metrics.length ? metrics[metrics.length - 1].t : Date.now() / 1000;
  for (let i = 0; i <= 4; i++) {
    const xRatio = i / 4;
    const x = pad.left + plotW * xRatio;
    c2d.strokeStyle = colors.grid;
    c2d.beginPath();
    c2d.moveTo(x, pad.top);
    c2d.lineTo(x, pad.top + plotH);
    c2d.stroke();
    const secAgo = Math.round(MAX_HISTORY_SECONDS * (1 - xRatio));
    const label = secAgo === 0 ? t('monitor.now') : `-${secAgo}s`;
    c2d.fillStyle = colors.text;
    c2d.fillText(label, x, pad.top + plotH + 4);
  }

  // 绘制折线
  if (metrics.length >= 1) {
    const t0 = nowTs - MAX_HISTORY_SECONDS;
    const xAt = (ts) => pad.left + ((ts - t0) / MAX_HISTORY_SECONDS) * plotW;
    const yAt = (v) => pad.top + plotH - (Math.max(0, Math.min(100, v)) / 100) * plotH;

    drawLine(metrics.map((m) => [xAt(m.t), yAt(m.cpu)]), colors.cpu);
    drawLine(metrics.map((m) => [xAt(m.t), yAt(m.mem)]), colors.mem);
    drawLine(metrics.map((m) => [xAt(m.t), yAt(m.gpuUtil)]), colors.gpu);

    // 末端数值标注
    const last = metrics[metrics.length - 1];
    drawEndLabel(pad.left + plotW, yAt(last.cpu), last.cpu.toFixed(1) + '%', colors.cpu);
    drawEndLabel(pad.left + plotW, yAt(last.mem), last.mem.toFixed(1) + '%', colors.mem);
    drawEndLabel(pad.left + plotW, yAt(last.gpuUtil), last.gpuUtil.toFixed(1) + '%', colors.gpu);
  } else {
    c2d.fillStyle = colors.text;
    c2d.textAlign = 'center';
    c2d.textBaseline = 'middle';
    c2d.fillText(t('monitor.waiting'), pad.left + plotW / 2, pad.top + plotH / 2);
  }
}

function drawLine(points, color) {
  if (points.length === 0) return;
  c2d.strokeStyle = color;
  c2d.lineWidth = 2;
  c2d.lineJoin = 'round';
  c2d.beginPath();
  for (let i = 0; i < points.length; i++) {
    const [x, y] = points[i];
    if (i === 0) c2d.moveTo(x, y);
    else c2d.lineTo(x, y);
  }
  c2d.stroke();
  // 末端圆点
  if (points.length > 0) {
    const [x, y] = points[points.length - 1];
    c2d.fillStyle = color;
    c2d.beginPath();
    c2d.arc(x, y, 3, 0, Math.PI * 2);
    c2d.fill();
  }
}

function drawEndLabel(x, y, text, color) {
  // 在线条末端右侧绘制小标签
  c2d.fillStyle = color;
  c2d.textAlign = 'left';
  c2d.textBaseline = 'middle';
  c2d.font = 'bold 11px sans-serif';
  // 限制不超出右边界
  const labelX = Math.min(x - 32, x);
  c2d.fillText(text, labelX, y - 8);
}

// ========== GPU 表格 ==========

// 部分驱动会返回 "[N/A]"，后端将无法解析的指标置为 null
const fmtGpuMem = (mb) => (mb === null || mb === undefined) ? 'N/A' : `${(mb / 1024).toFixed(1)} GB`;
const fmtGpuPct = (v) => (v === null || v === undefined) ? 'N/A' : `${v}%`;
const fmtGpuTemp = (v) => (v === null || v === undefined) ? 'N/A' : `${v}°C`;

function updateGpuTable(msg) {
  const gpus = msg.gpus || [];
  const wrap = root.querySelector('#gpu-table-wrap');
  if (!gpus.length) {
    wrap.innerHTML = emptyStateHtml(ctx, { icon: 'cpu', title: t('monitor.noGpu'), sub: t('monitor.noGpuSub') });
    return;
  }
  wrap.innerHTML = `
    <table class="table">
      <thead><tr><th>${ctx.escapeHtml(t('monitor.index'))}</th><th>${ctx.escapeHtml(t('monitor.name'))}</th><th>${ctx.escapeHtml(t('monitor.vram'))}</th><th>${ctx.escapeHtml(t('monitor.utilization'))}</th><th>${ctx.escapeHtml(t('monitor.temp'))}</th></tr></thead>
      <tbody>
        ${gpus.map((g) => `
          <tr>
            <td>${g.index}</td>
            <td>${ctx.escapeHtml(g.name)}</td>
            <td>${fmtGpuMem(g.memory_used_mb)} / ${fmtGpuMem(g.memory_total_mb)}</td>
            <td>${fmtGpuPct(g.utilization)}</td>
            <td>${fmtGpuTemp(g.temperature)}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

// ========== 进程关联 ==========

async function refreshOverview() {
  try {
    const r = await ctx.Api.getSystemOverview();
    updateProcTable(r.gpu_processes || []);
  } catch (e) {
    // 失败不能无声：进程表会一直显示旧数据却毫无提示。
    console.warn('[monitor] failed to load gpu processes', e);
    markProcTableStale();
  }
}

function markProcTableStale() {
  const wrap = root.querySelector('#proc-table-wrap');
  if (!wrap || wrap.querySelector('.proc-stale-hint')) return;
  const hint = document.createElement('div');
  hint.className = 'proc-stale-hint text-muted';
  hint.style.fontSize = '12px';
  hint.style.marginBottom = '6px';
  hint.textContent = t('monitor.procStale');
  wrap.prepend(hint);
}

const refreshOverviewOnce = createSingleFlight(refreshOverview);

function updateProcTable(procs) {
  const wrap = root.querySelector('#proc-table-wrap');
  if (!procs || !procs.length) {
    wrap.innerHTML = `<div class="empty-state">${ctx.escapeHtml(t('monitor.noGpuProcs'))}</div>`;
    return;
  }
  // 优先显示启动器管理的进程；若无管理进程则回退显示全部
  const managed = procs.filter((p) => p.managed);
  const showProcs = managed.length > 0 ? managed : procs;
  const formatMem = (mb) => (mb === null || mb === undefined) ? 'N/A' : `${(mb / 1024).toFixed(2)} GB`;
  wrap.innerHTML = `
    <table class="table">
      <thead><tr><th>PID</th><th>${ctx.escapeHtml(t('monitor.procName'))}</th><th>${ctx.escapeHtml(t('monitor.vramUsage'))}</th><th>${ctx.escapeHtml(t('monitor.type'))}</th></tr></thead>
      <tbody>
        ${showProcs.map((p) => {
          const isManaged = p.managed;
          return `<tr${isManaged ? ' class="managed-row"' : ''}>
            <td>${p.pid}</td>
            <td>${ctx.escapeHtml(p.name)}</td>
            <td>${formatMem(p.memory_used_mb)}</td>
            <td>${isManaged ? `<span class="badge badge-running">${ctx.escapeHtml(t('monitor.managed'))}</span>` : `<span class="tag tag-default">${ctx.escapeHtml(t('monitor.other'))}</span>`}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>
  `;
}
