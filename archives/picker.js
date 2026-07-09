// Dynamic nav picker v3 — loaded externally so all archive pages stay current
(async function() {
  const BASE = '/agency-funnel-dashboard';
  try {
    const r = await fetch(BASE + '/archives/nav.json?t=' + Date.now());
    if (!r.ok) return;
    const nav = await r.json();
    const path = window.location.pathname;

    let curMonth = nav.live_month;
    let curWeek  = null;
    const mMatch = path.match(/archives\/(\d{4}-\d{2})\.html/);
    const wMatch = path.match(/archives\/(week-[\d-]+)\.html/);
    const wCur   = path.includes('week-current.html');

    if (mMatch)      { curMonth = mMatch[1]; }
    else if (wMatch) { curWeek = wMatch[1]; curMonth = wMatch[1].replace('week-','').substring(0,7); }
    else if (wCur)   { curWeek = 'week-current'; curMonth = nav.live_month; }

    const mSel = document.querySelector('.month-picker select');
    if (mSel) {
      const curLabel = (nav.months.find(m => m.key === curMonth) || {}).label || 'Select month';
      let opts = `<option value="" disabled selected>${curLabel}</option>`;
      opts += nav.months.map(m => {
        const href = m.is_live ? BASE+'/index.html' : BASE+'/archives/'+m.key+'.html';
        return `<option value="${href}">${m.label}</option>`;
      }).join('');
      mSel.innerHTML = opts;
      mSel.onchange = function() { if (this.value) window.location.href = this.value; };
    }

    const wSel = document.querySelector('.week-picker select');
    if (wSel) {
      const weeks  = nav.weeks[curMonth] || [];
      const isLive = curMonth === nav.live_month;
      const fullHref = isLive ? BASE+'/index.html' : BASE+'/archives/'+curMonth+'.html';

      const opts = [`<option value="${fullHref}">Full Month</option>`];
      weeks.forEach(w => {
        opts.push(`<option value="${BASE+'/archives/'+w.key+'.html'}">${w.label}</option>`);
      });
      wSel.innerHTML = opts.join('');

      const curWkLabel = curWeek
        ? (weeks.find(w => w.key === curWeek) || {}).label || 'This week'
        : 'Full Month';
      wSel.insertAdjacentHTML('afterbegin', `<option value="" disabled selected>${curWkLabel}</option>`);
      wSel.querySelectorAll('option:not([disabled])').forEach(o => o.removeAttribute('selected'));
      wSel.onchange = function() { if (this.value) window.location.href = this.value; };

      if (weeks.length === 0) {
        const wp  = document.querySelector('.week-picker');
        const div = document.querySelector('.picker-divider');
        if (wp)  wp.style.display  = 'none';
        if (div) div.style.display = 'none';
      }
    }
  } catch(e) {
    // Silently fail — baked-in picker remains as fallback
  }
})();