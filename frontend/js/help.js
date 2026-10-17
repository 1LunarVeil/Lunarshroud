// 新手指南 / 内置使用说明。
//
// 这是"第一次安装"的主入口：五步上手、GPU 构建选择、模型文件要求、
// 以及一张按症状查的启动失败排查表。页面顶部的检查清单是**动态的**：
// 它读当前配置/能力探测/模型扫描，把"你还没做哪一步"直接指出来。
//
// 长文内容刻意放在这里而不是 locale 字典：它是成段文档，不是 UI 标签；
// 两种语言各写一份，切换语言时整页重绘。

import { getLocale, t } from './i18n.mjs';

export const id = 'help';
export const title = '新手指南';

let ctx = null;
let root = null;

const GUIDE = {
  'zh-CN': {
    intro:
      'LunarShroud 只是 llama.cpp 的桌面启动器：它不打包、也不自动下载 llama-server 和模型。' +
      '第一次使用需要你准备两样东西——对应显卡的 llama-server.exe 和一个 GGUF 模型。',
    steps: [
      {
        title: '安装启动器',
        body:
          'Windows 10/11 需要 WebView2 Runtime（Win11 通常自带；Win10 若窗口空白，先装它）。' +
          '安装版从安装包安装；便携版解压后直接运行 LunarShroud.exe。不要只复制其中一个 exe——' +
          '启动器旁边的 backend\\backend.exe 必须和它配套。',
      },
      {
        title: '获取 llama-server（不要跳过）',
        body:
          '打开 <a href="https://github.com/ggml-org/llama.cpp/releases" target="_blank" rel="noopener noreferrer">llama.cpp Releases</a>，' +
          '下载 Windows 包并按显卡选择：NVIDIA 选 <code>bin-win-cuda12.x-x64</code>（推荐）；' +
          'AMD/Intel 选 <code>bin-win-vulkan-x64</code>；只有 CPU 选 <code>bin-win-cpu-x64</code>。' +
          '解压到固定目录（例如 <code>D:\\llama</code>），里面应当有 <code>llama-server.exe</code>。' +
          'NVIDIA 用户还需要 CUDA 运行库：安装 CUDA Toolkit 运行组件，或把发布包里的 ' +
          '<code>cudart64_*.dll</code> / <code>cublas64_*.dll</code> 放在 llama-server.exe 旁边。',
      },
      {
        title: '准备模型',
        body:
          '准备一个 <code>.gguf</code> 主模型。视觉模型还需要配套的 <code>mmproj-*.gguf</code>；' +
          '推测解码需要与主模型词表一致的草稿模型。量化越低越省显存、质量越低：8GB 显卡通常' +
          '从 Q4_K_M 起步。下载没完成、文件只有几百字节、或者把 mmproj 当成主模型选中，都会启动失败。',
      },
      {
        title: '在设置里指定路径',
        body:
          '设置 → 路径：选择 llama-server.exe 和模型目录。启动器**故意不给 llama-server 默认路径**——' +
          '有默认值就等于每次启动都自动执行安装目录里一个用户可写的文件（安全原因）。' +
          '选完后回到模型库点一次「重新扫描」，确认模型能被识别。',
      },
      {
        title: '第一次启动',
        body:
          '模型库选中模型 → 直接启动；或「启动新实例」选择预设/模型。端口留空会自动分配。' +
          '第一次加载大模型可能几十秒到几分钟，期间状态是「启动中」。就绪后顶部会显示运行中，' +
          'Chat 页会自动嵌入 llama-server 自带 WebUI。失败时卡片上会出现「为什么没启动？」。',
      },
    ],
    gpuTable: {
      head: ['显卡', '选择文件', '备注'],
      rows: [
        ['NVIDIA RTX 30/40/50', 'bin-win-cuda12.x-x64', '需要 CUDA 运行库；不要选旧 CUDA 版本'],
        ['AMD / Intel 独显/核显', 'bin-win-vulkan-x64', '驱动保持较新'],
        ['只有 CPU', 'bin-win-cpu-x64', '速度慢，但兼容性最好'],
      ],
    },
    mistakes: [
      '把 <b>mmproj</b>（视觉投影文件）当成主模型选中——它是配套文件，不是主模型。',
      '只选中了一个 <b>目录</b>，或选中了 <b>LunarShroud.exe</b> 而不是 llama-server.exe。',
      'NVIDIA 装了 CUDA 版 llama.cpp，却没有 CUDA 运行库（cudart/cublas 缺失 → 0xC0000135）。',
      '模型还在下载/复制中就开始启动；<code>.gguf</code> 只有几 KB 或尾部缺失。',
      '上下文/并行槽位开得太大：8GB 显卡上 32K 上下文 + 多槽位很容易在首次推理时 OOM。',
      '嵌入模型（如 nomic-embed）不开「启用嵌入端点」，且并行槽位为 1——部分 llama.cpp 版本会断言崩溃。',
      '在「附加命令行参数」里写 --host / --port / --api-key / --path / --log-file；这些由启动器代管，会被拒绝。',
      '把监听地址改成 0.0.0.0 却不设 API 密钥——启动器会拒绝（安全边界）。',
      '沿用旧参数 --n-gpu-layers 99：新版本建议留空（自动），显存不足时 99 会让 llama.cpp 停止自适应。',
      '端口已被别的程序占用，或同时启动两个实例却手动指定了同一个端口。',
      '命令预览里的 --n-cpu-moe 0 / --n-cpu-ffn 0 是显式表示“不卸载”，不是错误；上游 --help 没声明默认值，启动器不替它猜。',
    ],
    failTable: {
      head: ['现象', '最可能的原因', '怎么办'],
      rows: [
        ['窗口空白 / 打不开', '缺 WebView2 Runtime', '安装 Microsoft Edge WebView2 Runtime 后重启'],
        ['提示找不到 llama-server', '路径为空或文件被移动', '设置 → 路径重新选择；便携版确认 backend\\backend.exe 在旁边'],
        ['0xC0000135 / 缺少 DLL', 'CUDA 运行库缺失，或选错 GPU 构建', '装 CUDA 运行组件；或换 Vulkan/CPU 构建'],
        ['CUDA error / no CUDA-capable device', '驱动过旧、显卡不受支持、构建不匹配', '更新 NVIDIA 驱动；确认下载的是对应 CUDA 版本的包'],
        ['out of memory / failed to allocate', '显存或内存不足', '降低上下文、并行槽位、GPU 层数；关闭其他占显存程序'],
        ['error loading model / invalid magic', '不是 GGUF、下载不完整、构建太旧', '重新下载完整文件；升级 llama.cpp；确认架构被支持'],
        ['GGML_ASSERT(n_outputs_max ...)', '嵌入模型 + 单槽位 + 未开嵌入端点', '勾选「启用嵌入端点」，或把并行槽位设为 2 以上'],
        ['草稿模型相关崩溃/警告', '草稿模型与主模型词表不一致', '换同系列词表的草稿模型，或不启用推测解码'],
        ['启动超时但日志在走', '模型太大/磁盘慢/首次加载', '等待；把启动超时调大；检查端口与日志'],
        ['端口被占用', '别的进程或另一个实例占着', '端口留空自动分配，或换一个端口'],
        ['unknown argument / invalid argument', '本机构建不支持该参数，或 extra_args 写错', '看参数面板的「已忽略」提示；清空 extra_args 重试'],
        ['能运行但第一次对话就崩', '显存余量不足（计算缓冲到推理时才申请）', '降上下文/槽位/层数；看显存预估是否标红'],
      ],
    },
    dataTitle: '日志与数据在哪',
    dataBody:
      '安装版：<code>%APPDATA%\\LunarShroud\\</code>（config.json、history.db、logs\\backend.log、logs\\launcher.log）。' +
      '源码运行：仓库下的 <code>data\\</code>。反馈问题时请附上 backend.log 的最后几十行——' +
      '启动器已经把退出码翻译与最后致命日志行写进实例卡片和日志页。',
    securityTitle: '安全边界（请勿绕过）',
    securityBody:
      '管理 API 只监听 127.0.0.1；非回环监听必须配 API 密钥；会话令牌不会写入日志，也不会传给 llama-server。' +
      '不要把启动器暴露到公网。',
  },
  'en-US': {
    intro:
      'LunarShroud is a desktop launcher for llama.cpp. It does not bundle or download llama-server or models. ' +
      'You need two things: a llama-server.exe built for your GPU, and a GGUF model.',
    steps: [
      {
        title: 'Install the launcher',
        body:
          'Windows 10/11 needs the WebView2 Runtime (usually present on Win11). The installer sets up the app; ' +
          'the portable build runs LunarShroud.exe directly. Never copy only one executable: the adjacent ' +
          'backend\\backend.exe must match the launcher.',
      },
      {
        title: 'Get llama-server (do not skip)',
        body:
          'Open the <a href="https://github.com/ggml-org/llama.cpp/releases" target="_blank" rel="noopener noreferrer">llama.cpp releases</a> ' +
          'and pick the Windows package for your GPU: NVIDIA → <code>bin-win-cuda12.x-x64</code>; ' +
          'AMD/Intel → <code>bin-win-vulkan-x64</code>; CPU only → <code>bin-win-cpu-x64</code>. ' +
          'Extract it to a fixed folder (e.g. <code>D:\\llama</code>). NVIDIA also needs the CUDA runtime: ' +
          'install the CUDA Toolkit runtime or copy <code>cudart64_*.dll</code> / <code>cublas64_*.dll</code> next to llama-server.exe.',
      },
      {
        title: 'Prepare a model',
        body:
          'Get a <code>.gguf</code> main model. Vision models also need a matching <code>mmproj-*.gguf</code>; ' +
          'speculative decoding needs a draft model with the same vocabulary. Lower quantization saves VRAM and ' +
          'costs quality; on an 8 GB card start with Q4_K_M. Incomplete downloads, a few-hundred-byte file, or ' +
          'selecting mmproj as the main model will fail to load.',
      },
      {
        title: 'Set the paths in Settings',
        body:
          'Settings → Paths: choose llama-server.exe and a model folder. The launcher deliberately has no default ' +
          'llama-server path: a default would auto-execute a user-writable file in the install directory. ' +
          'After choosing, press Rescan in the model library and confirm the model is recognised.',
      },
      {
        title: 'First launch',
        body:
          'Select a model in the library and press Launch, or use “New instance”. Leave the port empty to auto-assign. ' +
          'The first load of a large model can take seconds to minutes; the status stays “starting”. Once ready, ' +
          'the Chat page embeds the llama-server WebUI. If it fails, the card offers “Why did it fail?”.',
      },
    ],
    gpuTable: {
      head: ['GPU', 'Download', 'Notes'],
      rows: [
        ['NVIDIA RTX 30/40/50', 'bin-win-cuda12.x-x64', 'Needs the CUDA runtime; avoid old CUDA builds'],
        ['AMD / Intel GPU', 'bin-win-vulkan-x64', 'Keep the driver reasonably new'],
        ['CPU only', 'bin-win-cpu-x64', 'Slow but most compatible'],
      ],
    },
    mistakes: [
      'Selecting <b>mmproj</b> (the vision projector) as the main model — it is a companion file.',
      'Selecting a <b>folder</b>, or LunarShroud.exe instead of llama-server.exe.',
      'A CUDA llama.cpp build without the CUDA runtime (missing cudart/cublas → 0xC0000135).',
      'Launching while the model is still downloading/copying; the .gguf is a few KB or truncated.',
      'Context/parallel slots too large: 32K context plus multiple slots easily OOMs on 8 GB at first inference.',
      'An embedding model (e.g. nomic-embed) without “Enable embeddings” and with one slot — some builds assert.',
      'Putting --host / --port / --api-key / --path / --log-file in “Extra CLI arguments”; the launcher rejects them.',
      'Listening on 0.0.0.0 without an API key — the launcher refuses it (security boundary).',
      'Carrying over the old --n-gpu-layers 99; leave it empty (auto) so llama.cpp can spill layers on VRAM pressure.',
      'A port already in use, or two instances explicitly pinned to the same port.',
      '--n-cpu-moe 0 / --n-cpu-ffn 0 in the preview explicitly mean “do not offload”; upstream --help declares no default, so the launcher does not guess one.',
    ],
    failTable: {
      head: ['Symptom', 'Likely cause', 'What to do'],
      rows: [
        ['Blank window / does not open', 'WebView2 Runtime missing', 'Install the Microsoft Edge WebView2 Runtime and restart'],
        ['llama-server not found', 'Empty path or file moved', 'Re-select it in Settings → Paths; portable builds need backend\\backend.exe'],
        ['0xC0000135 / missing DLL', 'CUDA runtime missing or wrong GPU build', 'Install the CUDA runtime, or switch to the Vulkan/CPU build'],
        ['CUDA error / no CUDA-capable device', 'Old driver, unsupported GPU, wrong build', 'Update the NVIDIA driver; use the matching CUDA package'],
        ['out of memory / failed to allocate', 'Not enough VRAM or RAM', 'Lower context, parallel slots and GPU layers; close other GPU apps'],
        ['error loading model / invalid magic', 'Not GGUF, incomplete download, old build', 'Re-download the model; update llama.cpp; check the architecture'],
        ['GGML_ASSERT(n_outputs_max ...)', 'Embedding model + 1 slot + no embeddings endpoint', 'Enable embeddings, or set parallel slots ≥ 2'],
        ['Draft-model crash/warning', 'Draft vocabulary differs from the main model', 'Use a matching draft model or disable speculative decoding'],
        ['Startup timeout while logs continue', 'Large model / slow disk / first load', 'Wait, raise the startup timeout, verify the port and log'],
        ['Port already in use', 'Another process or instance owns it', 'Leave the port empty for auto-assignment, or choose another'],
        ['unknown argument / invalid argument', 'Flag unsupported by this build, or bad extra_args', 'Check suppressed-parameter hints; clear extra_args and retry'],
        ['Loads, then crashes on first message', 'Not enough VRAM headroom (compute buffer allocated later)', 'Lower context/slots/layers; check the VRAM estimate warning'],
      ],
    },
    dataTitle: 'Logs and data',
    dataBody:
      'Installed: <code>%APPDATA%\\LunarShroud\\</code> (config.json, history.db, logs\\backend.log, logs\\launcher.log). ' +
      'Source runs: <code>data\\</code> in the repository. When reporting a problem, include the last lines of backend.log — ' +
      'the launcher already writes exit-code translation and the last fatal log line into the instance card and log page.',
    securityTitle: 'Security boundaries (do not bypass)',
    securityBody:
      'The management API only listens on 127.0.0.1; non-loopback listeners require an API key; session tokens are never ' +
      'written to logs and never passed to llama-server. Do not expose the launcher to the internet.',
  },
};

function guide() {
  return GUIDE[getLocale()] || GUIDE['zh-CN'];
}

export function init(el, appCtx) {
  root = el;
  ctx = appCtx;
  renderShell();
  refreshChecklist();
}

export async function onShow() {
  await refreshChecklist();
}

function renderShell() {
  const g = guide();
  const steps = g.steps
    .map(
      (step, index) => `
      <article class="help-step">
        <div class="help-step-num">${index + 1}</div>
        <div>
          <h3>${ctx.escapeHtml(step.title)}</h3>
          <p>${step.body}</p>
        </div>
      </article>`
    )
    .join('');
  const table = (spec) => `
    <div class="table-wrap help-table-wrap">
      <table class="table help-table">
        <thead><tr>${spec.head.map((h) => `<th>${ctx.escapeHtml(h)}</th>`).join('')}</tr></thead>
        <tbody>${spec.rows
          .map((row) => `<tr>${row.map((cell) => `<td>${ctx.escapeHtml(cell)}</td>`).join('')}</tr>`)
          .join('')}</tbody>
      </table>
    </div>`;
  root.innerHTML = `
    <div class="page-header">
      <div>
        <div class="page-title">${ctx.escapeHtml(t('help.title'))}</div>
        <div class="page-subtitle">${ctx.escapeHtml(t('help.subtitle'))}</div>
      </div>
      <div class="page-actions">
        <button class="btn btn-secondary" id="help-refresh">${ctx.escapeHtml(t('help.refresh'))}</button>
      </div>
    </div>
    <div class="help-layout">
      <section class="card help-card" id="help-checklist">
        <div class="panel-empty">${ctx.escapeHtml(t('help.checking'))}</div>
      </section>
      <section class="card help-card">
        <h2>${ctx.escapeHtml(t('help.quickStart'))}</h2>
        <p class="help-intro">${ctx.escapeHtml(g.intro)}</p>
        ${steps}
      </section>
      <section class="card help-card">
        <h2>${ctx.escapeHtml(t('help.gpuTitle'))}</h2>
        ${table(g.gpuTable)}
      </section>
      <section class="card help-card">
        <h2>${ctx.escapeHtml(t('help.mistakesTitle'))}</h2>
        <ul class="help-mistakes">${g.mistakes.map((item) => `<li>${item}</li>`).join('')}</ul>
      </section>
      <section class="card help-card">
        <h2>${ctx.escapeHtml(t('help.failTitle'))}</h2>
        ${table(g.failTable)}
      </section>
      <section class="card help-card">
        <h2>${ctx.escapeHtml(g.dataTitle)}</h2>
        <p>${g.dataBody}</p>
      </section>
      <section class="card help-card help-security">
        <h2>${ctx.escapeHtml(g.securityTitle)}</h2>
        <p>${g.securityBody}</p>
      </section>
    </div>
  `;
  root.querySelector('#help-refresh').addEventListener('click', refreshChecklist);
}

function checklistItem(state, title, detail, goto) {
  const icon = { ok: '✓', warn: '!', error: '✕', info: 'i' }[state] || 'i';
  return `
    <div class="help-check help-check-${state}">
      <span class="help-check-icon">${icon}</span>
      <div class="help-check-body">
        <div class="help-check-title">${ctx.escapeHtml(title)}</div>
        ${detail ? `<div class="help-check-detail">${ctx.escapeHtml(detail)}</div>` : ''}
      </div>
      ${goto ? `<button class="btn btn-sm btn-secondary" data-goto="${goto}">${ctx.escapeHtml(t('help.openSettings'))}</button>` : ''}
    </div>`;
}

async function refreshChecklist() {
  const box = root.querySelector('#help-checklist');
  if (!box) return;
  box.innerHTML = `<div class="panel-empty">${ctx.escapeHtml(t('help.checking'))}</div>`;

  let cfg = null;
  try {
    cfg = await ctx.Api.getSystemConfig();
  } catch (error) {
    box.innerHTML = checklistItem('error', t('help.check.configFailed'), String(error.message || error));
    return;
  }

  const items = [];
  if (!cfg || !String(cfg.llama_server_path || '').trim()) {
    items.push(checklistItem('error', t('help.check.serverMissing'), '', 'settings'));
  } else {
    let server = null;
    try {
      const caps = await ctx.Api.getCapabilities();
      server = caps && caps.server;
    } catch (error) {
      server = { probed: false, error: String(error.message || error) };
    }
    if (server && server.probed) {
      const version = server.version || t('help.check.unknownVersion');
      items.push(checklistItem('ok', t('help.check.serverOk', { version })));
    } else {
      items.push(
        checklistItem(
          'warn',
          t('help.check.serverProbe'),
          (server && server.error) || t('help.check.unknownVersion'),
          'settings'
        )
      );
    }
  }

  if (!cfg || !String(cfg.default_model_dir || '').trim()) {
    items.push(checklistItem('warn', t('help.check.dirMissing'), '', 'settings'));
  } else {
    try {
      const scan = await ctx.Api.scanModels(cfg.default_model_dir, false);
      const count = scan && scan.models ? scan.models.length : 0;
      if (count > 0) {
        items.push(checklistItem('ok', t('help.check.modelsOk', { count })));
      } else {
        items.push(checklistItem('warn', t('help.check.modelsNone'), cfg.default_model_dir, 'settings'));
      }
    } catch (error) {
      items.push(checklistItem('warn', t('help.check.modelsError'), String(error.message || error)));
    }
  }

  try {
    const history = await ctx.Api.listHistory(1, 0);
    if (history && history.length) {
      items.push(checklistItem('ok', t('help.check.launched')));
    } else {
      items.push(checklistItem('info', t('help.check.notLaunched')));
    }
  } catch {
    /* 历史读不到不影响清单主体 */
  }

  box.innerHTML = `
    <h2>${ctx.escapeHtml(t('help.checkTitle'))}</h2>
    <div class="help-checks">${items.join('')}</div>
  `;
  box.querySelectorAll('[data-goto]').forEach((button) => {
    button.addEventListener('click', () => ctx.navigate(button.dataset.goto));
  });
}
