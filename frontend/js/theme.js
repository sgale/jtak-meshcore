/* jTAK Theme — Night / Day toggle, shared across all pages */
(function () {
  const KEY = 'jtak_theme';

  function _apply(theme) {
    document.documentElement.dataset.theme = theme === 'day' ? 'day' : '';
  }

  function _updateBtn(theme) {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    btn.textContent = theme === 'day' ? '☾' : '☀';
    btn.title       = theme === 'day' ? 'Switch to Night mode' : 'Switch to Day mode';
  }

  function toggle() {
    const current = localStorage.getItem(KEY) || 'night';
    const next    = current === 'day' ? 'night' : 'day';
    localStorage.setItem(KEY, next);
    _apply(next);
    _updateBtn(next);
  }

  // Apply before first paint (called from inline <script> in <head>)
  window._jtakApplyTheme = function () {
    _apply(localStorage.getItem(KEY) || 'night');
  };

  document.addEventListener('DOMContentLoaded', function () {
    const theme = localStorage.getItem(KEY) || 'night';
    _apply(theme);
    _updateBtn(theme);
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.addEventListener('click', toggle);
  });
})();
