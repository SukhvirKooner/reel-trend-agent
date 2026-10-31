/* Dashboard behaviour. No dependencies.
   - tabs (hash based, keys 1-5), filters, search, table sorting, copy buttons, light/dark toggle, guide drawer
   - live mode: re-fetches the page every 30 s and swaps the content in place, keeping tab, filters,
     open/closed cards and scroll position. */
(function () {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const S = { paused: false, feed: 'all', trk: 'all', aq: '', tq: '', det: {} };

  const rel = (d) => {
    const s = (Date.now() - d) / 1000;
    if (s < 45) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  };
  const times = () => $$('time.when').forEach((t) => {
    const d = new Date(t.getAttribute('datetime'));
    if (!(d.getTime() > 0)) return;
    t.textContent = rel(d);
    t.title = d.toLocaleString();
  });

  const pick = (h) => {
    const want = (h || location.hash || '#overview').replace('#', '');
    const id = $('#' + CSS.escape(want) + '.tab') ? want : 'overview';
    $$('.tab').forEach((s) => s.classList.toggle('on', s.id === id));
    $$('#tabs a').forEach((a) => a.classList.toggle('on', a.dataset.tab === id));
  };

  const applyFilters = () => {
    $$('[data-fchip]').forEach((b) => b.classList.toggle('on', b.dataset.val === S[b.dataset.fchip]));
    $$('input[data-q]').forEach((i) => { if (i.value !== S[i.dataset.q]) i.value = S[i.dataset.q]; });
    const aq = S.aq.trim().toLowerCase(), tq = S.tq.trim().toLowerCase();
    $$('.fi').forEach((r) => { r.hidden = !(S.feed === 'all' || r.dataset.k === S.feed); });
    $$('.ac').forEach((c) => { c.hidden = !c.dataset.text.includes(aq); });
    $$('#trk tbody tr').forEach((r) => {
      r.hidden = !((S.trk === 'all' || r.dataset.st === S.trk) && r.dataset.text.includes(tq));
    });
    $$('[data-list]').forEach((l) => {
      const any = $$('[data-item]', l).some((x) => !x.hidden);
      const n = l.parentElement.querySelector('.nores');
      if (n) n.hidden = any;
    });
  };

  const post = () => {
    $$('details[data-id]').forEach((d) => { if (d.dataset.id in S.det) d.open = S.det[d.dataset.id]; });
    applyFilters(); times(); pick();
  };

  /* ---- clicks (delegated, so they survive the in-place refresh) */
  document.addEventListener('click', (e) => {
    const t = e.target;
    const tab = t.closest('[data-tab]');
    if (tab) { e.preventDefault(); history.replaceState(null, '', '#' + tab.dataset.tab); pick(tab.dataset.tab); scrollTo(0, 0); return; }
    const chip = t.closest('[data-fchip]');
    if (chip) { S[chip.dataset.fchip] = chip.dataset.val; applyFilters(); return; }
    const th = t.closest('table.sortable th');
    if (th) {
      const table = th.closest('table'), i = th.cellIndex, asc = !th.classList.contains('asc');
      $$('th', table).forEach((x) => x.classList.remove('asc', 'desc'));
      th.classList.add(asc ? 'asc' : 'desc');
      const tb = table.tBodies[0];
      [...tb.rows].sort((x, y) => {
        const a = x.cells[i], b = y.cells[i];
        const va = a.dataset.v !== undefined ? parseFloat(a.dataset.v) : a.innerText.toLowerCase();
        const vb = b.dataset.v !== undefined ? parseFloat(b.dataset.v) : b.innerText.toLowerCase();
        return (va > vb ? 1 : va < vb ? -1 : 0) * (asc ? 1 : -1);
      }).forEach((r) => tb.appendChild(r));
      return;
    }
    const cp = t.closest('[data-copy]');
    if (cp) {
      const txt = cp.dataset.copy, done = () => { const o = cp.dataset.label || cp.textContent; cp.dataset.label = o; cp.textContent = 'Copied ✓'; setTimeout(() => { cp.textContent = o; }, 1400); };
      (navigator.clipboard ? navigator.clipboard.writeText(txt) : Promise.reject()).then(done, () => {
        const ta = document.createElement('textarea'); ta.value = txt; document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); done(); } catch (_) {} ta.remove();
      });
      return;
    }
    if (t.closest('#pause')) {
      S.paused = !S.paused; const b = $('#pause');
      b.querySelector('span').textContent = S.paused ? 'Resume refresh' : 'Pause refresh';
      return;
    }
    if (t.closest('#theme')) {
      const dark = document.documentElement.dataset.theme === 'dark' ||
        (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
      const next = dark ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem('rt-theme', next); } catch (_) {}
      return;
    }
    if (t.closest('[data-guide]')) { document.body.classList.toggle('guide-open'); return; }
    if (t.closest('.scrim')) { document.body.classList.remove('guide-open'); }
  });
  document.addEventListener('input', (e) => {
    const q = e.target.dataset && e.target.dataset.q;
    if (q) { S[q] = e.target.value; applyFilters(); }
  });
  document.addEventListener('toggle', (e) => {
    if (e.target.matches && e.target.matches('details[data-id]')) S.det[e.target.dataset.id] = e.target.open;
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input,textarea') || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'Escape') document.body.classList.remove('guide-open');
    if (e.key === '?') document.body.classList.toggle('guide-open');
    const links = $$('#tabs a'), n = parseInt(e.key, 10);
    if (n >= 1 && n <= links.length) { history.replaceState(null, '', '#' + links[n - 1].dataset.tab); pick(); scrollTo(0, 0); }
  });
  window.addEventListener('hashchange', () => pick());

  post();
  setInterval(times, 20000);

  /* ---- live refresh */
  if (document.body.dataset.live !== '1') return;
  const refresh = async () => {
    if (S.paused || document.hidden) return;
    try {
      const r = await fetch(location.pathname + location.search, { cache: 'no-store' });
      if (!r.ok) return;
      const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
      const y = window.scrollY, feed = $('.feed'), fy = feed ? feed.scrollTop : 0;
      ['#app', '#tabs', '#live-txt'].forEach((sel) => {
        const n = doc.querySelector(sel), o = $(sel);
        if (n && o) o.innerHTML = n.innerHTML;
      });
      post();
      window.scrollTo(0, y);
      const f2 = $('.feed'); if (f2) f2.scrollTop = fy;
    } catch (_) { /* server restarting: try again next tick */ }
  };
  setInterval(refresh, 30000);
})();
