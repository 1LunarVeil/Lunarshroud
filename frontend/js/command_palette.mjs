// Ctrl+K command palette: navigation and a few global actions.
//
// It uses the shell's modal stack so focus trapping, ESC and background inert
// behave exactly like every other dialog.

import { t } from './i18n.mjs';
import { tauriInvoke } from './api.js?v=1';
import { open as openModelPicker } from './model_picker.mjs';

const PAGES = ['dashboard', 'chat', 'workspace', 'knowledge', 'observability', 'help'];

export function openCommandPalette(ctx) {
  const commands = PAGES.map((page) => ({
    label: t(`nav.${page}`),
    hint: t('command.navigate'),
    run: () => ctx.navigate(page),
  }));
  commands.push(
    {
      label: t('command.newChat'),
      hint: t('command.action'),
      run: () => {
        window.__pendingChatNew = true;
        ctx.navigate('chat');
      },
    },
    {
      label: t('command.pickModel'),
      hint: 'Ctrl+L',
      run: () => openModelPicker(ctx),
    },
    {
      label: t('nav.param_editor'),
      hint: t('command.action'),
      run: () => ctx.navigate('param_editor'),
    },
    {
      label: t('nav.settings'),
      hint: t('command.action'),
      run: () => ctx.navigate('settings'),
    },
    {
      label: t('command.toggleTheme'),
      hint: t('command.action'),
      run: () => ctx.setTheme(ctx.getTheme() === 'dark' ? 'light' : 'dark'),
    }
  );
  if (window.__TAURI__) {
    commands.push({
      label: t('command.quit'),
      hint: t('command.action'),
      run: async () => {
        if (!(await ctx.showConfirm(t('command.quitConfirm'), t('command.quit')))) return;
        await tauriInvoke('quit_app');
      },
    });
  }

  const body = document.createElement('div');
  body.className = 'cmd-palette';
  body.innerHTML = `
    <input id="cmd-palette-input" class="input cmd-input" type="text" autocomplete="off"
      placeholder="${ctx.escapeHtml(t('command.placeholder'))}"
      role="combobox" aria-expanded="true" aria-controls="cmd-palette-list"
      aria-autocomplete="list" aria-label="${ctx.escapeHtml(t('command.placeholder'))}" />
    <div id="cmd-palette-list" class="cmd-list" role="listbox" aria-label="${ctx.escapeHtml(
      t('command.title')
    )}"></div>
    <div class="cmd-foot">${ctx.escapeHtml(t('command.hint'))}</div>
  `;
  const overlay = ctx.showModal(t('command.title'), body, []);
  overlay.querySelector('.modal').classList.add('cmd-modal');
  const input = body.querySelector('#cmd-palette-input');
  const list = body.querySelector('#cmd-palette-list');
  let filtered = commands.slice();
  let active = 0;

  const execute = (command) => {
    if (!command) return;
    ctx.closeModal();
    // Let the palette leave the DOM before a command opens another modal.
    requestAnimationFrame(() => command.run());
  };

  const render = () => {
    if (!filtered.length) {
      list.innerHTML = `<div class="cmd-empty">${ctx.escapeHtml(t('command.empty'))}</div>`;
      return;
    }
    active = Math.max(0, Math.min(active, filtered.length - 1));
    list.innerHTML = filtered
      .map(
        (command, index) => `
        <button type="button" class="cmd-item ${index === active ? 'active' : ''}"
          role="option" aria-selected="${index === active ? 'true' : 'false'}" data-cmd-index="${index}">
          <span class="cmd-item-label">${ctx.escapeHtml(command.label)}</span>
          <span class="cmd-item-hint">${ctx.escapeHtml(command.hint || '')}</span>
        </button>`
      )
      .join('');
    const current = list.querySelector('.cmd-item.active');
    current?.scrollIntoView({ block: 'nearest' });
  };

  const filter = () => {
    const query = input.value.trim().toLowerCase();
    filtered = query
      ? commands.filter((command) => command.label.toLowerCase().includes(query))
      : commands.slice();
    active = 0;
    render();
  };

  input.addEventListener('input', filter);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      active = Math.min(active + 1, Math.max(0, filtered.length - 1));
      render();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      active = Math.max(active - 1, 0);
      render();
    } else if (event.key === 'Enter') {
      event.preventDefault();
      execute(filtered[active]);
    }
  });
  list.addEventListener('click', (event) => {
    const item = event.target.closest('[data-cmd-index]');
    if (!item) return;
    execute(filtered[Number(item.dataset.cmdIndex)]);
  });
  list.addEventListener('mousemove', (event) => {
    const item = event.target.closest('[data-cmd-index]');
    if (!item) return;
    const index = Number(item.dataset.cmdIndex);
    if (index === active) return;
    active = index;
    render();
  });

  render();
  input.focus();
}
