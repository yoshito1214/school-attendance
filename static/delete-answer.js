    (() => {
      'use strict';
      const clock = new Intl.DateTimeFormat('en-GB', {
        timeZone: 'Asia/Tokyo', hour: '2-digit', hourCycle: 'h23'
      });
      const themes = {
        morning: { label: '朝', icon: '☀', color: '#fff6e6' },
        day: { label: '昼', icon: '☀', color: '#edf8fe' },
        evening: { label: '夕', icon: '◒', color: '#fff0eb' },
        night: { label: '夜', icon: '☾', color: '#101b30' }
      };
      function updateTheme() {
        const hour = Number(clock.formatToParts(new Date()).find(part => part.type === 'hour').value);
        const key = hour >= 5 && hour < 8 ? 'morning'
          : hour >= 8 && hour < 14 ? 'day'
          : hour >= 14 && hour < 18 ? 'evening' : 'night';
        const theme = themes[key];
        document.documentElement.dataset.theme = key;
        document.querySelector('meta[name="theme-color"]').content = theme.color;
        const date = document.querySelector('[data-japan-date]');
        if (date) date.textContent = new Intl.DateTimeFormat('ja-JP', {timeZone:'Asia/Tokyo', year:'numeric',month:'long',day:'numeric',weekday:'short'}).format(new Date());
        const label = document.querySelector('[data-theme-label]');
        if (label) label.textContent = theme.label + 'の時間 · 日本時間';
        const logo = document.querySelector('.logo');
        if (logo) logo.textContent = theme.icon;
      }
      updateTheme();
      document.addEventListener('DOMContentLoaded', updateTheme);
      window.setInterval(updateTheme, 1000);
      window.addEventListener('pageshow', updateTheme);
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) updateTheme();
      });
    })();

// Keep deletion as a CSRF-protected form POST, with confirmation.
(() => {
  function initializeDeletion() {
    document.querySelectorAll('form[data-delete-answer]').forEach(form => {
      const button = form.querySelector('[data-delete-button]');
      if (!button) return;
      form.addEventListener('submit', event => {
        if (!window.confirm('今日の回答を削除しますか？\n削除すると、みんなの回答も見られなくなります。締切後・休止日は再回答できません。')) {
          event.preventDefault();
        }
      });
      button.disabled = false;
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initializeDeletion);
  else initializeDeletion();
})();
