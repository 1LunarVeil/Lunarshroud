// 首帧主题引导：必须在 CSS 已加载、页面内容绘制之前执行，避免浅色用户
// 每次启动先看到一帧深色。它不是 ES Module，由 index.html 以经典脚本同步加载。
(function () {
  var KEY = 'lunarshroud_theme';
  var pref = 'system';
  try {
    pref = localStorage.getItem(KEY) || 'system';
  } catch (e) {
    pref = 'system';
  }
  if (pref !== 'light' && pref !== 'dark') {
    try {
      pref =
        window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
          ? 'dark'
          : 'light';
    } catch (e) {
      pref = 'dark';
    }
  }
  if (document.body) {
    document.body.className = 'theme-' + pref;
  }
  document.documentElement.style.colorScheme = pref;
})();
