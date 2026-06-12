/* System Clearer — frontend logic */
const S = {
  token: localStorage.getItem('sc_token') || '',
  view: 'dashboard',
  latestScanId: null,
  selectedTargets: new Set(),
  items: [],
  charts: {},
  vizType: 'treemap',
};

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const human = (n) => {
  n = Number(n || 0); const u = ['B','KB','MB','GB','TB','PB']; let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n.toFixed(0) : n.toFixed(2)) + ' ' + u[i];
};
const CAT_COLORS = {
  xcode_derived_data:'#6366f1', xcode_device_support:'#8b5cf6', core_simulator:'#a855f7',
  xcode_archives:'#ec4899', xcode_misc:'#818cf8', ios_backups:'#f43f5e', user_caches:'#22d3ee',
  container_caches:'#06b6d4', logs:'#14b8a6', saved_state:'#10b981', trash:'#84cc16',
  homebrew_cache:'#eab308', package_caches:'#f59e0b', docker:'#0ea5e9', node_modules:'#f97316',
  mail_downloads:'#d946ef', vm_swap:'#64748b', downloads:'#fb7185', uncategorized:'#475569', '':'#475569',
};
const catColor = (k) => CAT_COLORS[k] || '#6366f1';

/* ---------------- API ---------------- */
async function api(path, opts = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  if (S.token) headers['Authorization'] = 'Bearer ' + S.token;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401) { showAuth(); throw new Error('unauthorized'); }
  let data = null; try { data = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error((data && data.detail) || res.statusText);
  return data;
}

/* ---------------- toast ---------------- */
let toastT;
function toast(msg, kind = '') {
  const el = $('#toast'); el.textContent = msg; el.className = 'toast ' + kind;
  clearTimeout(toastT); toastT = setTimeout(() => el.classList.add('hidden'), 3200);
}

/* ---------------- auth ---------------- */
async function boot() {
  const st = await fetch('/api/auth/status').then(r => r.json());
  if (!st.configured) return showAuth('setup');
  if (S.token) {
    try { await api('/api/settings'); return showApp(); } catch (_) {}
  }
  showAuth('login');
}
function showAuth(mode = 'login') {
  $('#app').classList.add('hidden');
  $('#auth-screen').classList.remove('hidden');
  const setup = mode === 'setup';
  $('#auth-mode-text').textContent = setup
    ? 'Create a PIN to secure this tool. You will use it to unlock.'
    : 'Enter your PIN to unlock.';
  $('#auth-pin2-wrap').classList.toggle('hidden', !setup);
  $('#auth-submit').textContent = setup ? 'Create & Unlock' : 'Unlock';
  $('#auth-form').dataset.mode = mode;
  $('#auth-error').classList.add('hidden');
}
async function showApp() {
  $('#auth-screen').classList.add('hidden');
  $('#app').classList.remove('hidden');
  await Promise.all([loadDisk(), loadScans(), loadTargets()]);
  initCharts();
}
$('#auth-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const mode = e.target.dataset.mode, pin = $('#auth-pin').value;
  const err = $('#auth-error');
  try {
    if (mode === 'setup') {
      if (pin !== $('#auth-pin2').value) throw new Error('PINs do not match.');
      const d = await fetch('/api/auth/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin }) }).then(r => r.json().then(j => { if (!r.ok) throw new Error(j.detail); return j; }));
      S.token = d.token;
    } else {
      const d = await fetch('/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin }) }).then(r => r.json().then(j => { if (!r.ok) throw new Error(j.detail); return j; }));
      S.token = d.token;
    }
    localStorage.setItem('sc_token', S.token);
    $('#auth-pin').value = ''; showApp();
  } catch (ex) { err.textContent = ex.message; err.classList.remove('hidden'); }
});
$('#logout-btn').addEventListener('click', () => { localStorage.removeItem('sc_token'); S.token = ''; showAuth('login'); });

/* ---------------- navigation ---------------- */
const TITLES = {
  dashboard: ['Dashboard', 'Overview of your disk and reclaimable space'],
  visualize: ['Visualize', 'Interactive map of where your space went'],
  items: ['Items & AI', 'Browse, score with GPT‑5, and clean up'],
  cleanup: ['Cleanup Log', 'History, restore, and quarantine'],
  settings: ['Settings', 'API key, scanning, and access'],
};
$('#nav').addEventListener('click', (e) => {
  const btn = e.target.closest('.nav-item'); if (!btn || !btn.dataset.view) return;
  switchView(btn.dataset.view);
});
function switchView(v) {
  S.view = v;
  $$('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  $$('[data-section]').forEach(s => s.classList.toggle('hidden', s.dataset.section !== v));
  $('#view-title').textContent = TITLES[v][0];
  $('#view-sub').textContent = TITLES[v][1];
  if (v === 'visualize') loadTree();
  if (v === 'items') loadItems();
  if (v === 'cleanup') loadDeletions();
  if (v === 'settings') loadSettings();
  setTimeout(() => Object.values(S.charts).forEach(c => c && c.resize()), 50);
}

/* ---------------- disk ---------------- */
async function loadDisk() {
  const d = await api('/api/disk'); const v = d.volumes || [];
  const main = v[0];
  if (main) {
    renderGauge(main.percent);
    $('#mini-disk').innerHTML = `<div class="flex justify-between"><span class="text-slate-400">Free space</span><b class="text-white">${main.free_human}</b></div>
      <div class="progress mt-2"><div class="progress-fill" style="width:${main.percent}%"></div></div>
      <div class="text-[10px] text-slate-500 mt-1">${main.used_human} of ${main.total_human} used</div>`;
  }
  $('#disk-volumes').innerHTML = v.map(x => `
    <div class="flex items-center justify-between text-xs">
      <span class="text-slate-300 truncate max-w-[55%]">${x.label}</span>
      <span class="text-slate-500">${x.free_human} free · ${x.percent}%</span>
    </div>`).join('');
}
function renderGauge(pct) {
  const c = S.charts.gauge || (S.charts.gauge = echarts.init($('#disk-gauge')));
  c.setOption({
    series: [{
      type: 'gauge', startAngle: 210, endAngle: -30, min: 0, max: 100, radius: '100%',
      progress: { show: true, width: 16, roundCap: true },
      axisLine: { lineStyle: { width: 16, color: [[.7,'#22c55e'],[.9,'#f59e0b'],[1,'#f43f5e']] } },
      pointer: { show: false }, axisTick: { show: false }, splitLine: { show: false },
      axisLabel: { show: false },
      anchor: { show: false },
      itemStyle: { color: pct > 90 ? '#f43f5e' : pct > 70 ? '#f59e0b' : '#22c55e' },
      detail: { valueAnimation: true, fontSize: 30, fontWeight: 'bold', color: '#fff',
                offsetCenter: [0, '5%'], formatter: '{value}%' },
      title: { offsetCenter: [0, '32%'], color: '#94a3b8', fontSize: 12 },
      data: [{ value: pct, name: 'Disk used' }],
    }],
  });
}

/* ---------------- scans / summary ---------------- */
async function loadScans() {
  const d = await api('/api/scans');
  S.latestScanId = d.latest ? d.latest.id : null;
  if (S.latestScanId) await loadSummary(S.latestScanId);
  else renderStats(null, []);
}
async function loadSummary(id) {
  const d = await api('/api/scan/' + id + '/summary');
  renderStats(d, d.categories);
  renderDonut(d.categories);
}
function renderStats(sum, cats) {
  const total = sum ? sum.total_bytes : 0;
  const top = cats && cats.length ? cats[0] : null;
  const cards = [
    ['Reclaimable found', sum ? sum.total_human : '—', '#6366f1'],
    ['Files scanned', sum ? Number(sum.total_files).toLocaleString() : '—', '#22d3ee'],
    ['Folders', sum ? Number(sum.total_dirs).toLocaleString() : '—', '#8b5cf6'],
    ['Biggest category', top ? top.label : '—', catColor(top && top.key)],
  ];
  $('#stat-cards').innerHTML = cards.map(([l,v,c]) => `
    <div class="glass stat" style="border-left:3px solid ${c}">
      <div class="l">${l}</div><div class="v">${v}</div>
    </div>`).join('');
}
function renderDonut(cats) {
  const c = S.charts.donut || (S.charts.donut = echarts.init($('#cat-donut')));
  const total = cats.reduce((a, b) => a + b.bytes, 0);
  $('#donut-total').textContent = human(total) + ' total';
  c.setOption({
    tooltip: { trigger: 'item', backgroundColor: '#0d1326', borderColor: 'rgba(255,255,255,.1)',
      textStyle: { color: '#e2e8f0' },
      formatter: (p) => `${p.data.name}<br/><b>${human(p.value)}</b> (${p.percent}%)` },
    legend: { type: 'scroll', orient: 'vertical', right: 0, top: 'center',
      textStyle: { color: '#94a3b8', fontSize: 11 }, itemWidth: 10, itemHeight: 10 },
    series: [{
      type: 'pie', radius: ['45%', '72%'], center: ['38%', '50%'], avoidLabelOverlap: true,
      itemStyle: { borderColor: '#0f172a', borderWidth: 2, borderRadius: 4 },
      label: { show: false }, labelLine: { show: false },
      data: cats.map(x => ({ name: x.label, value: x.bytes, itemStyle: { color: catColor(x.key) } })),
    }],
  });
}

/* ---------------- targets ---------------- */
async function loadTargets() {
  const d = await api('/api/targets'); S.targets = d.targets;
  d.targets.forEach(t => { if (t.default && t.exists) S.selectedTargets.add(t.key); });
  renderTargets();
}
function renderTargets() {
  $('#targets-grid').innerHTML = S.targets.map(t => {
    const on = S.selectedTargets.has(t.key);
    return `<div class="target ${on ? 'on' : ''} ${t.exists ? '' : 'missing'}" data-key="${t.key}">
      <div class="text-lg">${iconFor(t.icon)}</div>
      <div class="min-w-0">
        <div class="text-sm text-slate-200 font-medium flex items-center gap-2">${t.label}
          <span class="badge">safe ${t.base_score}/10</span></div>
        <div class="text-[11px] text-slate-500 truncate">${t.exists ? t.existing_paths[0] : 'not present on this Mac'}</div>
      </div>
    </div>`;
  }).join('');
}
const ICONS = { hammer:'🔨', iphone:'📱', 'device-mobile':'📲', archive:'📦', shield:'🛡️',
  stack:'🗂️', box:'📦', 'file-text':'📄', window:'🪟', trash:'🗑️', beer:'🍺', package:'📦',
  ship:'🐳', 'folder-code':'📁', mail:'✉️', cpu:'🧠', download:'⬇️' };
const iconFor = (i) => ICONS[i] || '📁';
$('#targets-grid').addEventListener('click', (e) => {
  const t = e.target.closest('.target'); if (!t) return;
  const k = t.dataset.key;
  S.selectedTargets.has(k) ? S.selectedTargets.delete(k) : S.selectedTargets.add(k);
  renderTargets();
});
$('#targets-all').addEventListener('click', () => { S.targets.forEach(t => t.exists && S.selectedTargets.add(t.key)); renderTargets(); });
$('#targets-default').addEventListener('click', () => { S.selectedTargets.clear(); S.targets.forEach(t => t.default && t.exists && S.selectedTargets.add(t.key)); renderTargets(); });

/* ---------------- scanning ---------------- */
let pollT;
$('#scan-btn').addEventListener('click', startScan);
$('#scan-cancel').addEventListener('click', () => api('/api/scan/cancel', { method: 'POST' }));
async function startScan() {
  const mode = $('#scan-mode').value;
  let body = { mode };
  if (mode === 'path') {
    const p = prompt('Folder to scan (e.g. ~/Library/Caches or /Users/you/Projects):', '~/Library/Caches');
    if (!p) return; body.path = p;
  } else {
    body.keys = [...S.selectedTargets];
    if (!body.keys.length) return toast('Select at least one target.', 'err');
  }
  try {
    await api('/api/scan', { method: 'POST', body: JSON.stringify(body) });
    $('#scan-progress').classList.remove('hidden');
    $('#scan-pill').classList.remove('hidden');
    pollStatus();
  } catch (ex) { toast(ex.message, 'err'); }
}
async function pollStatus() {
  clearTimeout(pollT);
  const st = await api('/api/scan/status');
  const pct = st.roots_total ? Math.min(99, Math.round(st.roots_done / st.roots_total * 100)) : 50;
  $('#scan-bar').style.width = (st.status === 'running' ? pct : 100) + '%';
  $('#scan-detail').textContent = `${human(st.bytes)} · ${Number(st.files).toLocaleString()} files · ${st.current || ''}`;
  $('#scan-pill').textContent = `⚡ ${human(st.bytes)} scanned`;
  if (st.status === 'running') { pollT = setTimeout(pollStatus, 700); }
  else {
    $('#scan-bar').style.width = '100%';
    setTimeout(() => { $('#scan-progress').classList.add('hidden'); $('#scan-pill').classList.add('hidden'); }, 1200);
    toast(st.status === 'done' ? 'Scan complete ✓' : 'Scan ' + st.status, st.status === 'done' ? 'ok' : 'err');
    await loadScans(); await loadDisk();
    if (S.view === 'items') loadItems();
    if (S.view === 'visualize') loadTree();
  }
}

/* ---------------- visualize ---------------- */
$('#viz-toggle').addEventListener('click', (e) => {
  const b = e.target.closest('.chip'); if (!b) return;
  $$('#viz-toggle .chip').forEach(x => x.classList.remove('active')); b.classList.add('active');
  S.vizType = b.dataset.viz; loadTree();
});
async function loadTree() {
  if (!S.latestScanId) return;
  const d = await api(`/api/scan/${S.latestScanId}/treemap?depth=4&breadth=14`);
  const c = S.charts.tree || (S.charts.tree = echarts.init($('#tree-chart')));
  const tip = { backgroundColor: '#0d1326', borderColor: 'rgba(255,255,255,.1)',
    textStyle: { color: '#e2e8f0' },
    formatter: (p) => `${p.data.path || p.name}<br/><b>${human(p.value)}</b>` };
  if (S.vizType === 'treemap') {
    c.setOption({
      tooltip: tip,
      series: [{
        type: 'treemap', roam: false, nodeClick: 'zoomToNode', data: d.tree,
        leafDepth: 2, drillDownIcon: '▸',
        levels: [
          { itemStyle: { borderColor: '#0b1020', borderWidth: 3, gapWidth: 3 } },
          { itemStyle: { borderColor: '#0f172a', borderWidth: 2, gapWidth: 2 }, colorSaturation: [.35,.6] },
          { itemStyle: { gapWidth: 1 }, colorSaturation: [.3,.5] },
        ],
        breadcrumb: { show: true, itemStyle: { color: '#1e293b' }, textStyle: { color: '#cbd5e1' } },
        label: { color: '#fff', fontSize: 12, formatter: (p) => `${p.name}\n${human(p.value)}` },
        itemStyle: { borderRadius: 4 },
        color: Object.values(CAT_COLORS),
      }],
    }, true);
  } else {
    c.setOption({
      tooltip: tip,
      series: [{
        type: 'sunburst', radius: ['12%', '95%'], data: d.tree, nodeClick: 'rootToNode',
        emphasis: { focus: 'ancestor' },
        itemStyle: { borderColor: '#0b1020', borderWidth: 1 },
        label: { color: '#fff', minAngle: 8, fontSize: 11 },
        levels: [{}, { r0: '12%', r: '42%' }, { r0: '42%', r: '68%' }, { r0: '68%', r: '85%' }, { r0: '85%', r: '95%' }],
      }],
    }, true);
  }
}

/* ---------------- items + AI ---------------- */
async function loadItems() {
  if (!S.latestScanId) { $('#items-empty').classList.remove('hidden'); return; }
  const cat = $('#item-cat').value, dirs = $('#item-dirs').checked;
  const d = await api(`/api/scan/${S.latestScanId}/items?top=200&only_dirs=${dirs}${cat ? '&category=' + cat : ''}`);
  S.items = d.items; populateCatFilter();
  renderItems();
}
function populateCatFilter() {
  const sel = $('#item-cat'); if (sel.dataset.filled) return;
  const cats = [...new Set(S.items.map(i => i.category).filter(Boolean))];
  cats.forEach(k => { const o = document.createElement('option'); o.value = k;
    o.textContent = (S.items.find(i => i.category === k) || {}).category_label || k; sel.appendChild(o); });
  sel.dataset.filled = '1';
}
function scoreClass(s) { return s == null ? 's-na' : s >= 8 ? 's-hi' : s >= 5 ? 's-mid' : 's-lo'; }
function renderItems() {
  const body = $('#items-body');
  $('#items-empty').classList.toggle('hidden', S.items.length > 0);
  body.innerHTML = S.items.map((it, idx) => {
    const a = it.analysis || {};
    const sc = a.score;
    return `<tr data-idx="${idx}">
      <td class="p-3"><input type="checkbox" class="row-check accent-brand-500" value="${encodeURIComponent(it.path)}"></td>
      <td class="p-3"><div class="path-main truncate max-w-[420px]">${it.name}</div>
        <div class="path-sub truncate max-w-[420px]">${it.path}</div></td>
      <td class="p-3"><span class="badge" style="border-color:${catColor(it.category)}55;color:${catColor(it.category)}">${it.category_label}</span></td>
      <td class="p-3 text-right text-slate-200 font-medium">${it.size_human}</td>
      <td class="p-3 text-center"><span class="score ${scoreClass(sc)}">${sc == null ? '–' : sc}</span></td>
      <td class="p-3 text-xs ${a.risk ? 'risk-' + a.risk : 'text-slate-500'}">${a.risk || '—'}</td>
      <td class="p-3 text-right"><button class="btn-ghost text-xs info-btn" data-idx="${idx}">ℹ︎</button></td>
    </tr>`;
  }).join('');
}
$('#item-cat').addEventListener('change', loadItems);
$('#item-dirs').addEventListener('change', () => { $('#item-cat').dataset.filled = ''; $('#item-cat').innerHTML = '<option value="">All categories</option>'; loadItems(); });
$('#check-all').addEventListener('change', (e) => $$('.row-check').forEach(c => c.checked = e.target.checked));
$('#items-body').addEventListener('click', (e) => {
  const b = e.target.closest('.info-btn'); if (b) openDrawer(S.items[+b.dataset.idx]);
});
function selectedPaths() {
  return $$('.row-check').filter(c => c.checked).map(c => decodeURIComponent(c.value));
}

$('#analyze-btn').addEventListener('click', async () => {
  let paths = selectedPaths();
  const btn = $('#analyze-btn'); btn.disabled = true; const old = btn.innerHTML;
  btn.innerHTML = '<span>⏳</span> Analyzing…';
  try {
    let body = paths.length ? { paths } : { scan_id: S.latestScanId, top: 40, only_dirs: $('#item-dirs').checked };
    const d = await api('/api/analyze', { method: 'POST', body: JSON.stringify(body) });
    const map = {}; d.results.forEach(r => map[r.path] = r);
    S.items.forEach(it => { if (map[it.path]) it.analysis = map[it.path]; });
    renderItems();
    const note = d.source === 'gpt-5' ? 'Scored by GPT‑5 ✓' : 'Scored by built‑in heuristic';
    $('#items-meta').textContent = note + (d.ai_error ? '  ·  AI note: ' + d.ai_error : '');
    toast(note, d.source === 'gpt-5' ? 'ok' : '');
  } catch (ex) { toast(ex.message, 'err'); }
  finally { btn.disabled = false; btn.innerHTML = old; }
});

$('#delete-btn').addEventListener('click', () => doDelete(selectedPaths(), $('#del-method').value));
async function doDelete(paths, method) {
  if (!paths.length) return toast('Select items first.', 'err');
  const verb = method === 'permanent' ? 'PERMANENTLY delete' : method === 'quarantine' ? 'quarantine' : 'move to Trash';
  if (!confirm(`${verb} ${paths.length} item(s)?\n\nProtected system paths are always refused.`)) return;
  try {
    const d = await api('/api/delete', { method: 'POST', body: JSON.stringify({ paths, method }) });
    toast(`Freed ${d.bytes_freed_human} · ${d.count} cleaned${d.errors.length ? ', ' + d.errors.length + ' skipped' : ''}`, 'ok');
    if (d.errors.length) console.warn('skipped:', d.errors);
    closeDrawer(); await loadItems(); await loadDisk(); await loadScans();
  } catch (ex) { toast(ex.message, 'err'); }
}

/* ---------------- drawer ---------------- */
function openDrawer(it) {
  const a = it.analysis || {};
  $('#drawer-title').textContent = it.name;
  $('#drawer-body').innerHTML = `
    <div class="flex items-center gap-3">
      <span class="score ${scoreClass(a.score)}" style="width:48px;height:48px;font-size:1.1rem">${a.score == null ? '–' : a.score}</span>
      <div><div class="text-white font-medium">${it.category_label}</div>
      <div class="text-xs text-slate-400">${it.size_human}${a.source ? ' · ' + a.source : ''}</div></div>
    </div>
    ${a.explanation ? `<div><div class="lbl">What is this</div><p class="text-slate-300">${a.explanation}</p></div>` : '<p class="text-slate-500 text-xs">Not analyzed yet. Click “Analyze with GPT‑5”.</p>'}
    ${a.reason ? `<div><div class="lbl">Why this score</div><p class="text-slate-300">${a.reason}</p></div>` : ''}
    ${a.recommendation ? `<div><div class="lbl">Recommendation</div><p class="text-slate-300">${a.recommendation}</p></div>` : ''}
    <div class="kv"><span class="k">Risk</span><span class="v ${a.risk ? 'risk-' + a.risk : ''}">${a.risk || '—'}</span></div>
    <div class="kv"><span class="k">Path</span><span class="v">${it.path}</span></div>
    <div class="flex gap-2 pt-2">
      <button class="btn-primary text-sm flex-1 justify-center" id="drawer-analyze">🤖 Analyze</button>
      <button class="btn-danger text-sm flex-1 justify-center" id="drawer-delete">🗑️ ${$('#del-method').value === 'permanent' ? 'Delete' : 'Trash'}</button>
    </div>`;
  $('#drawer').classList.remove('hidden');
  $('#drawer-analyze').onclick = async () => {
    const d = await api('/api/analyze', { method: 'POST', body: JSON.stringify({ paths: [it.path] }) });
    if (d.results[0]) { it.analysis = d.results[0]; openDrawer(it); renderItems(); }
  };
  $('#drawer-delete').onclick = () => doDelete([it.path], $('#del-method').value);
}
function closeDrawer() { $('#drawer').classList.add('hidden'); }
$$('[data-close-drawer]').forEach(el => el.addEventListener('click', closeDrawer));

/* ---------------- cleanup log ---------------- */
async function loadDeletions() {
  const d = await api('/api/deletions'); const rows = d.deletions;
  const freed = rows.filter(r => r.status === 'done').reduce((a, b) => a + (b.size_bytes || 0), 0);
  const quar = rows.filter(r => r.method === 'quarantine' && r.status === 'done').length;
  $('#cleanup-stats').innerHTML = [
    ['Total actions', rows.length, '#6366f1'],
    ['Space freed', human(freed), '#22c55e'],
    ['Restorable (quarantine)', quar, '#f59e0b'],
  ].map(([l,v,c]) => `<div class="glass stat" style="border-left:3px solid ${c}"><div class="l">${l}</div><div class="v">${v}</div></div>`).join('');
  $('#deletions-list').innerHTML = rows.length ? rows.map(r => `
    <div class="flex items-center justify-between glass rounded-xl px-4 py-2.5">
      <div class="min-w-0">
        <div class="path-main truncate max-w-[460px]">${r.path}</div>
        <div class="path-sub">${r.method} · ${r.status} · ${new Date(r.created_at * 1000).toLocaleString()}</div>
      </div>
      <div class="flex items-center gap-3">
        <span class="text-slate-300 text-sm">${r.size_human}</span>
        ${r.method === 'quarantine' && r.status === 'done' ? `<button class="btn-ghost text-xs" data-restore="${r.id}">Restore</button>` : ''}
      </div>
    </div>`).join('') : '<p class="text-slate-500 text-sm text-center py-8">No deletions yet.</p>';
}
$('#deletions-list').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-restore]'); if (!b) return;
  try { const d = await api('/api/deletions/' + b.dataset.restore + '/restore', { method: 'POST' });
    toast('Restored ✓', 'ok'); loadDeletions(); } catch (ex) { toast(ex.message, 'err'); }
});
$('#empty-quar').addEventListener('click', async () => {
  if (!confirm('Permanently delete all quarantined items?')) return;
  const d = await api('/api/quarantine/empty', { method: 'POST' });
  toast(`Emptied · freed ${d.bytes_freed_human}`, 'ok'); loadDeletions(); loadDisk();
});

/* ---------------- settings ---------------- */
async function loadSettings() {
  const s = await api('/api/settings');
  $('#set-model').value = s.openai_model || 'gpt-5';
  $('#set-base').value = s.openai_base_url || '';
  $('#set-depth').value = s.scan_max_depth;
  $('#set-largest').value = s.largest_files_limit;
  $('#set-min').value = Math.round(s.min_node_bytes / (1024 * 1024));
  $('#set-method').value = s.default_delete_method;
  $('#key-status').textContent = s.openai_api_key_set ? `Key set (${s.openai_api_key_masked})` : 'No key set — using built‑in heuristic scoring.';
  $('#set-key').value = '';
}
$('#save-openai').addEventListener('click', async () => {
  const body = { openai_model: $('#set-model').value, openai_base_url: $('#set-base').value };
  if ($('#set-key').value.trim()) body.openai_api_key = $('#set-key').value.trim();
  await api('/api/settings', { method: 'POST', body: JSON.stringify(body) });
  toast('Saved ✓', 'ok'); loadSettings();
});
$('#test-openai').addEventListener('click', async () => {
  const r = $('#openai-test-result'); r.textContent = 'testing…'; r.className = 'text-xs self-center text-slate-400';
  if ($('#set-key').value.trim()) await api('/api/settings', { method: 'POST', body: JSON.stringify({ openai_api_key: $('#set-key').value.trim(), openai_model: $('#set-model').value }) });
  const d = await api('/api/settings/test-openai', { method: 'POST' });
  r.textContent = d.ok ? `✓ ${d.model} replied “${d.reply}”` : '✕ ' + d.error;
  r.className = 'text-xs self-center ' + (d.ok ? 'text-emerald-400' : 'text-rose-400');
});
$('#save-scan').addEventListener('click', async () => {
  await api('/api/settings', { method: 'POST', body: JSON.stringify({
    scan_max_depth: +$('#set-depth').value, largest_files_limit: +$('#set-largest').value,
    min_node_bytes: Math.round(+$('#set-min').value * 1024 * 1024),
    default_delete_method: $('#set-method').value }) });
  $('#del-method').value = $('#set-method').value;
  toast('Saved ✓', 'ok');
});
$('#save-pin').addEventListener('click', async () => {
  const r = $('#pin-result');
  try {
    await api('/api/auth/change', { method: 'POST', body: JSON.stringify({ old_pin: $('#old-pin').value, new_pin: $('#new-pin').value }) });
    r.textContent = 'PIN updated ✓'; r.className = 'text-xs text-emerald-400';
    $('#old-pin').value = ''; $('#new-pin').value = '';
  } catch (ex) { r.textContent = ex.message; r.className = 'text-xs text-rose-400'; }
});

/* ---------------- charts init ---------------- */
function initCharts() {
  ['gauge','donut','tree'].forEach(k => { /* lazy */ });
  window.addEventListener('resize', () => Object.values(S.charts).forEach(c => c && c.resize()));
}

boot();
