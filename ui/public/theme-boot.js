/*
 * Applied before first paint so a dark-theme user never sees a white flash.
 *
 * A classic script (not a module) on purpose: modules are deferred, which runs
 * after the first paint. This file must stay tiny and dependency-free.
 *
 * The class lands on <html> synchronously, and `tokens.css` scopes dark mode with
 * `:root.theme-dark`. Putting it on <body> would need a DOMContentLoaded handler,
 * which fires *after* CSS is applied — exactly the flash this file exists to
 * prevent.
 *
 * Keep the storage key in sync with `src/lib/stores/ui.ts`.
 */
(function () {
  var STORAGE_KEY = 'lunarshroud_theme';
  var preference = 'system';
  try {
    var saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'light' || saved === 'dark' || saved === 'system') preference = saved;
  } catch (error) {
    /* localStorage unavailable: fall back to the system preference */
  }

  var dark = preference === 'dark';
  if (preference === 'system') {
    try {
      dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    } catch (error) {
      dark = false;
    }
  }

  if (dark) document.documentElement.classList.add('theme-dark');
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
})();
