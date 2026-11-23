'use strict';

/* 脚本构建标记：排查「按钮在、点了没反应」时先确认浏览器加载的是不是这一版 */
window.__APP_BUILD__ = '2026-11-23.3';

/* 半新半旧的页面（新 app.js 配旧缓存 HTML，或反过来）会缺元素。
   以前第一处 $('#缺的').onclick = … 就抛 TypeError，整段脚本当场中断，
   后面所有绑定（选文件夹 / 导出标题清空 / 扫描并分析 / 最近项目删除）全丢。
   缺失元素一律返回一个游离 div：属性赋值是安全的空操作，脚本继续跑完。 */
const _MISSING = new Set();
const $ = (s) => {
  const el = document.querySelector(s);
  if (el) return el;
  if (!_MISSING.has(s)) {
    _MISSING.add(s);
    try { console.warn('[枫的瞬剪] 页面缺少元素：' + s + '（强制刷新一次可恢复完整界面）'); } catch (e) { /* 忽略 */ }
  }
  return document.createElement('div');
};
const $$ = (s) => Array.from(document.querySelectorAll(s));

const S = {
  ready: false, name: '', total: 0, eps: [], beats: [], highlights: [],
  endings: [], endhook: null,
  plan: null, win: new Set(), drop: new Set(), hook: null,
  endMode: 'auto', tightness: 0,
  view: { t0: 0, t1: 1 }, hover: -1, drag: null, moved: 0,
  logo: '', overlay: '', endframe: '',
  batch: {}, batchEnd: {}, bendCands: {}, hkHide: new Set(), hkAdd: [], hkFilter: 'all',
  hkPrefs: { enabled: {}, weight: {} },
  hkShift: {}, endShift: {},
  batchAlloc: 'total', batchTotal: 20, outdir: '', outShown: '', nameTpl: '',
  scanPending: false,
  hookMeta: {},
  auto: { deep: false, plan: null, topics: [], stats: null },
  hl: { plan: null, target: 600, endMode: 'auto', endKey: '', tail: 'auto' },
  target: 360, lead: 1, tailMode: 'auto', tailFixed: 2, efVideoDur: 0, jobTimer: null, saveTimer: null,
};

/* ── 基础 ─────────────────────────────────────────── */
async function api(path, body, method = 'POST') {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  const t = await r.text();
  let d = null;
  try { d = t ? JSON.parse(t) : null; } catch (e) { d = { detail: t }; }
  if (!r.ok) throw new Error((d && d.detail) || ('请求失败 ' + r.status));
  return d;
}

/* 后端同一时间只跑一个任务。撞上"有任务在跑"不要静默失败——
   把当前任务名显示出来，等它结束后自动继续这次请求。 */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function apiStart(path, body, label) {
  let told = false;
  for (let i = 0; i < 300; i++) {
    try { return await api(path, body); }
    catch (e) {
      if (!/正在跑/.test(e.message)) throw e;
      let j = null;
      try { j = await api('/api/job', undefined, 'GET'); } catch (x) { /* 忽略 */ }
      const cur = (j && j.msg) || '处理中';
      $('#pmsg').textContent = (label || '操作') + ' 排队中 · 当前任务：' + cur + '（完成后自动继续）';
      $('#globalbar').classList.add('on');
      if (!told) { toast('正在处理：' + cur + '，完成后再' + (label || '继续'), true); told = true; }
      await sleep(2000);
    }
  }
  throw new Error('任务一直没结束，刷新页面后重试');
}

function fmt(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return h ? h + ':' + String(m).padStart(2, '0') + ':' + String(x).padStart(2, '0')
           : m + ':' + String(x).padStart(2, '0');
}
/* 带一位小数的分:秒——微调 0.5 秒时整秒显示看不出变化 */
function fmt1(s) {
  s = Math.max(0, s);
  const m = Math.floor(s / 60), x = s - m * 60;
  return m + ':' + (x < 10 ? '0' : '') + x.toFixed(1);
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function toast(msg, bad) {
  // 这里必须直接查 DOM：#toast 是按需创建的，用容错版 $() 会拿到游离节点
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div'); el.id = 'toast';
    el.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);' +
      'background:#1c2331;border:1px solid #334056;color:#dfe6f4;padding:9px 16px;border-radius:9px;' +
      'font-size:12.5px;z-index:300;box-shadow:0 10px 30px rgba(0,0,0,.5);transition:.2s;opacity:0';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.style.borderColor = bad ? '#5a2f38' : '#334056';
  el.style.opacity = 1;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.style.opacity = 0), 2600);
}

/* ── 任务轮询 ─────────────────────────────────────── */
/* 扫描统一收尾：以前任务报错时 watchJob 直接 return，onDone 不执行，
   #btnScan 一直是 disabled —— 重新导入时看着就像「按钮坏了」。
   现在不管成功 / 失败 / 排队超时，都从这里把按钮和遮罩恢复。 */
function endScan() {
  if (!S.scanPending) return;
  S.scanPending = false;
  $('#btnScan').disabled = false;
  impHide();
}
function watchJob(onDone, onFail) {
  clearInterval(S.jobTimer);
  $('#globalbar').classList.add('on');
  S.jobTimer = setInterval(async () => {
    let j;
    try { j = await api('/api/job', undefined, 'GET'); } catch (e) { return; }
    const pct = j.progress || 0;
    $('#progBar').style.width = pct + '%';
    $('#globalbar').style.width = pct + '%';
    $('#pmsg').textContent = j.msg || '';
    if (j.state === 'done' || j.state === 'error') {
      clearInterval(S.jobTimer);
      endScan();
      impHide();
      $('#globalbar').classList.remove('on');
      $('#progBar').style.width = j.state === 'done' ? '100%' : '0';
      if (j.state === 'error') {
        $('#pmsg').textContent = '出错：' + j.msg;
        toast(j.msg, true);
        if (onFail) onFail(j.msg);
        return;
      }
      $('#pmsg').textContent = '完成';
      onDone && onDone(j.result);
    }
  }, 700);
}

/* ── 导入 ─────────────────────────────────────────── */
/* 导入遮罩：扫描期间把旧剧的画面盖住。
   不然旧项目的剧集/候选还留在屏幕上，很容易被当成"新剧识别成了旧剧"。 */
function impShow(name, sub) {
  const m = document.getElementById('impMask');
  if (!m) return;
  $('#impName').textContent = name || '新剧';
  $('#impSub').textContent = sub || '扫描分集 → 配对字幕 / 识别画面台词 → 分析剧情';
  m.hidden = false;
}
function impHide() { const m = document.getElementById('impMask'); if (m) m.hidden = true; }

async function doScan() {
  const folder = $('#folder').value.trim().replace(/^["']|["']$/g, '');
  let name = $('#projName').value.trim();
  if (!folder) { toast('先填剧集文件夹路径', true); return; }
  // 换了文件夹但剧名还是上一部剧的 → 自动改用文件夹名（后端同名不同剧也会再兜一层）
  const base = (folder.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '').trim();
  const trimEnd = (s) => (s || '').replace(/[\\/]+$/, '').toLowerCase();
  if (base && (!name || (name === S.name && S.folder && trimEnd(S.folder) !== trimEnd(folder)))) {
    name = base.replace(/[（(].*$/, '').trim() || base;
    $('#projName').value = name;
    toast('这次导入记为「' + name + '」，不会覆盖上一部剧');
  }
  S.scanPending = true;
  $('#btnScan').disabled = true;
  impShow(name || base, $('#ocrChk').checked
    ? '扫描分集 → 配对字幕 / 识别画面台词 → 分析剧情'
    : '扫描分集 → 配对字幕 → 分析剧情（画面识别已关闭）');
  try {
    await apiStart('/api/scan', { folder, name, ocr: $('#ocrChk').checked }, '导入');
    $('#pmsg').textContent = '扫描中…';
    watchJob((res) => {
      endScan();
      if (res) applyProject(res);
      renderRecents();
    });
  } catch (e) {
    endScan();
    toast(e.message, true);
  }
}

async function runOcr(force) {
  if (!S.ready) { toast('先导入剧集', true); return; }
  try {
    await apiStart('/api/ocr', { force: !!force }, '识别字幕');
    $('#pmsg').textContent = '识别画面字幕…';
    toast('开始从画面识别字幕，集数多的话要跑几分钟');
    watchJob((res) => { if (res) applyProject(res); });
  } catch (e) { toast(e.message, true); }
}

function applyProject(d) {
  const prevName = S.name || '';
  if (d.notice) toast(d.notice);
  if (d.ocr && d.ocr.eps) {
    if (d.ocr.cues) toast('画面识别完成：' + d.ocr.eps + ' 集，共 ' + d.ocr.cues + ' 句台词');
    else toast('这 ' + d.ocr.eps + ' 集画面里没识别到字幕', true);
  }
  S.ready = true;
  // 换项目：旧的识别结果与混剪 / 高光方案都不能继承
  S.auto = { deep: false, plan: null, topics: [], stats: null };
  S.hl.plan = null;
  renderHl(null);
  $('#acFb').hidden = true; $('#acMetrics').hidden = true;
  $('#acList').innerHTML = ''; $('#acRender').hidden = true;
  loadAutoState();
  S.name = d.name; S.total = d.total; S.eps = d.eps || [];
  S.folder = d.folder || S.folder || '';
  S.beats = d.beats || []; S.highlights = d.highlights || [];
  S.hookMeta = d.hook_meta || {};
  S.endings = d.endings || [];
  S.plan = null; S.win = new Set(); S.drop = new Set();
  S.hook = null; S.endhook = null; S.batch = {}; S.batchEnd = {}; S.bendCands = {};
  S.lastDir = '';
  S.outShown = '';
  $('#out').innerHTML = '';
  $('#btnShowOut').hidden = true;
  S.hkHide = new Set(); S.hkAdd = []; S.hkFilter = 'all';
  S.hkShift = {}; S.endShift = {};
  loadHkEdits();
  loadHookPrefs();
  S.view = { t0: 0, t1: Math.max(1, S.total) };
  $('#projName').value = S.name;
  // 换项目时把跟着旧项目走的字段刷新掉，避免串戏
  if (!$('#title').value || $('#title').value === prevName) $('#title').value = S.name;

  $('#epCount').textContent = S.eps.length;
  const missing = S.eps.length - d.sub_episodes;
  if (missing > 0 && S.beats.length) {
    $('#subPair').innerHTML = d.sub_episodes + '/' + S.eps.length + ' 集有字幕 · ' +
      '<a href="javascript:void 0" id="lnkOcr" class="lnk">识别缺失 ' + missing + ' 集</a>';
    const lnk = $('#lnkOcr');
    if (lnk) lnk.onclick = () => runOcr();
  } else {
    $('#subPair').textContent = d.sub_episodes + '/' + S.eps.length + ' 集有字幕';
  }
  $('#subPair').className = 'hint';
  $('#meta').innerHTML =
    '<span class="chip">' + esc(S.name) + '</span>' +
    '<span class="chip">总长 ' + fmt(S.total) + '</span>' +
    '<span class="chip">' + S.eps.length + ' 集</span>' +
    '<span class="chip ' + (d.sub_episodes === S.eps.length ? 'ok' : 'warn') + '">字幕 ' +
      d.sub_episodes + '/' + S.eps.length + '</span>' +
    (d.ocr_eps ? '<span class="chip ok">画面识别 ' + d.ocr_eps + ' 集</span>' : '') +
    (d.acts ? '<span class="chip">画面动作 ' + d.acts + ' 处</span>' : '') +
    '<span class="chip">' + (S.beats.length) + ' 个节拍</span>';

  // 源视频不在本机（换机后只拷了项目存档）：明说，别让人对着黑缩略图猜
  const fw = $('#folderWarn');
  if (fw) {
    const bad = d.folder_ok === false;
    fw.hidden = !bad;
    document.body.classList.toggle('haswarn', bad);
    if (bad) {
      $('#fwPath').textContent = (S.folder || '（未记录路径）') +
        ' · 缩略图、原片播放、出片都会失败';
    }
  }

  renderEps(); renderStats(); renderBatchBar(); resize();
  fillAddEps();

  // 没读到台词：给出 OCR 入口，不进顺剪流程
  if (!S.beats.length) {
    const hint = '这个项目还没有可用台词：文件夹里没有字幕文件（srt / ass / vtt），' +
      '画面里的硬字幕也还没识别。可以直接从画面识别，或配好字幕文件后重新扫描。';
    const act = $('#ocrChk').checked
      ? '<button class="btn sm" id="btnOcrRun">从画面识别字幕</button>'
      : '<div class="tipline">「画面硬字幕识别」已关闭，勾上后再重新扫描</div>';
    $('#hooks').innerHTML = '<div class="empty warn"><div>' + esc(hint) + '</div>' + act + '</div>';
    $('#ends').innerHTML = '<div class="empty warn"><div>' + esc(hint) + '</div>' + act + '</div>';
    $('#tlEmpty').innerHTML = '<div class="ei">⬆ 没有读到台词</div><div class="ei sub">' +
      esc(hint) + '</div>';
    $('#tlEmpty').style.display = '';
    $('#tlvid').hidden = true;
    $$('#btnOcrRun').forEach((b) => { b.onclick = () => runOcr(); });
    toast('没有字幕文件，可以从画面识别台词', true);
    renderRecents();
    return;
  }

  renderHooks(); renderEnds(); draw();
  $('#tlEmpty').style.display = 'none';
  // 右侧原片栏亮起来；换项目时重置播放状态
  $('#tlvid').hidden = false;
  Vid.ep = 0; Vid.pend = null;
  vid.removeAttribute('src'); vid.load();
  vidLabel('原片');
  $('#vidPh').hidden = false;

  // 刷新 / 重启服务后，后端还留着上次的方案就照原样接回来：
  // 别自动换掉用户已经选好的开头钩子、结尾点和时长（以前一刷新钩子就被换回第 1 集）。
  const saved = d.plan || null;
  const allH = hookAll();
  if (saved && saved.hook && saved.hook.g0 != null) {
    S.hook = { g0: saved.hook.g0, g1: saved.hook.g1, ep: saved.hook.ep,
               text: saved.hook.text || '', dur: saved.hook.dur || (saved.hook.g1 - saved.hook.g0) };
    if (saved.target) { S.target = saved.target; $('#target').value = saved.target; }
    if (saved.tightness != null) {
      S.tightness = saved.tightness;
      const opt = Array.from($('#tightness').options).find((o) => Number(o.value) === saved.tightness);
      if (opt) $('#tightness').value = opt.value;
    }
    if (saved.end_forced && saved.end) {
      S.endhook = { g0: saved.end.g0, g1: saved.end.g1, ep: saved.end.ep,
                    text: saved.end.text || '', dur: saved.end.dur };
      S.endMode = 'point'; $('#endMode').value = 'point';
    }
    if (Array.isArray(saved.beats_drop)) S.drop = new Set(saved.beats_drop);
    renderHooks(); renderEnds(); renderHookBox();
  } else if (allH.length) {
    // 首次导入：自动挑一个开头钩子，优先集数靠前的（往后还有剧情可接），分数作次要依据
    const early = allH.filter((h) => !h.manual && h.ep <= 3);
    const pick = (early.length ? early : allH)
      .slice().sort((a, b) => (a.ep - b.ep) || (b.score - a.score))[0];
    selectHook(pick, true);
  }
  makePlan(true);
  renderRecents();          // 新导入/换项目后，「最近项目」跟着刷新（当前项目会高亮）
}

function renderEps() {
  const box = $('#epList');
  if (!S.eps.length) { box.innerHTML = '<div class="empty">还没有导入剧集</div>'; return; }
  box.innerHTML = S.eps.map((e) =>
    '<div class="eprow" data-ep="' + e.idx + '">' +
      '<span class="epno">' + e.idx + '</span>' +
      '<span class="epname" title="' + esc(e.file) + '">' + esc(e.file) + '</span>' +
      '<span class="epdur">' + fmt(e.dur) + '</span>' +
      '<i class="dot ' + (e.cues ? '' : 'no') + '" title="' +
        (e.cues ? (e.sub_source === 'ocr' ? '画面识别 ' : '') + e.cues + ' 句台词' : '无字幕') + '"></i>' +
    '</div>').join('');
  $$('#epList .eprow').forEach((row) => {
    row.onclick = () => zoomToEp(Number(row.dataset.ep));
  });
}

function renderStats() {
  const est = S.plan ? S.plan.estimated : 0;
  const efDur = S.endframe ? Math.max(0.5, parseFloat($('#efDur').value) || 2.5) : 0;
  const cards = efDur;   // 成片 = 正片 + 引导尾帧，没有片头/片尾卡
  const tiles = [
    ['原片总长', S.total ? fmt(S.total) : '—'],
    ['集数', S.eps.length || '—'],
    ['台词句数', S.beats.reduce((a, b) => a + (b.cues || 1), 0) || '—'],
    ['预计成片', est ? fmt(est + cards) : '—'],
  ];
  $('#stats').innerHTML = tiles.map((t, i) =>
    '<div class="stat' + (i === 3 ? ' accent' : '') + '"><label>' + t[0] + '</label><b>' + t[1] + '</b></div>').join('');
  if (S.plan) {
    const pl = S.plan;
    const pr = pl.estimated / Math.max(1, S.total);
    $('#keepRatio').textContent = (pr * 100).toFixed(1) + '%';
    $('#segCount').textContent = pl.segments.filter((s) => s.keep).length;
    const sp = pl.ep_span || [0, 0];
    $('#planEps').textContent = sp[0]
      ? ('第 ' + sp[0] + (sp[1] && sp[1] !== sp[0] ? '–' + sp[1] : '') + ' 集')
      : '—';
    $('#planSpan').textContent = pl.from
      ? ('第' + pl.from.ep + '集 ' + fmt(pl.from.t) + ' → 第' + pl.to.ep + '集 ' + fmt(pl.to.t))
      : '—';
    $('#planSpan').title = pl.from ? fmt(pl.from.t) + ' 起，顺接 ' + fmt(pl.to.t) + ' 止' : '';
    $('#planEnd').textContent = pl.end_reason || '—';
    $('#planEnd').style.color = pl.shortage ? '#ffb27a' : '';
    $('#planEnd').title = pl.shortage ? '后面剩余剧情不够目标时长，已顺到剧终/末尾' : '';
    $('#keepBar').style.width = Math.min(100, pr * 100) + '%';
    const tcuts = pl.tail_cuts || [];
    const tRow = $('#tailCutRow');
    if (tRow) {
      tRow.hidden = !tcuts.length;
      if (tcuts.length) {
        $('#tailCutVal').textContent = '共 ' + (pl.tail_total || 0).toFixed(1) + ' 秒 · ' +
          tcuts.length + ' 个集末';
        $('#tailCutVal').title = tcuts.map((c) =>
          '第' + c.ep + '集 −' + c.sec.toFixed(1) + 's' +
          (c.by === 'card-low' ? '（超上限）' : '')).join('\n');
      }
    }
  } else {
    // 方案被清掉（换了一批钩子 / 换项目）时，别把旧方案的数字留在面板上
    ['#keepRatio', '#segCount', '#planEps', '#planSpan', '#planEnd'].forEach((s) => {
      const el = $(s); if (el) { el.textContent = '—'; el.title = ''; }
    });
    const kb = $('#keepBar'); if (kb) kb.style.width = '0%';
    const pe = $('#planEnd'); if (pe) pe.style.color = '';
    const tr = $('#tailCutRow'); if (tr) tr.hidden = true;
  }
}

/* ── 钩子候选 ─────────────────────────────────────── */
function epOffset(idx) {
  const e = S.eps.find((x) => x.idx === idx);
  return e ? e.offset : 0;
}

function hookKey(h) {
  return h.manual ? ('m' + Number(h.g0).toFixed(2) + '-' + Number(h.g1).toFixed(2))
    : (h.ep + '@' + Number(h.g0).toFixed(1));
}

/* 候选的人工增删记在浏览器本地，按剧名分开存 */
function hkStoreKey() { return 'dc_hooks_' + (S.name || '_'); }

function loadHkEdits() {
  try {
    const d = JSON.parse(localStorage.getItem(hkStoreKey()) || '{}');
    S.hkHide = new Set(d.hide || []);
    S.hkAdd = (d.add || []).filter((x) => x && x.ep && isFinite(x.g0));
    S.hkShift = d.hookShift || {};
    S.endShift = d.endShift || {};
  } catch (e) { S.hkHide = new Set(); S.hkAdd = []; S.hkShift = {}; S.endShift = {}; }
}

function saveHkEdits() {
  try {
    localStorage.setItem(hkStoreKey(),
      JSON.stringify({ hide: Array.from(S.hkHide), add: S.hkAdd,
                       hookShift: S.hkShift, endShift: S.endShift }));
  } catch (e) { /* 隐私模式 / 空间满：忽略，不影响当前会话 */ }
}

/* ── 候选人工前移 / 后移 ────────────────────────────
   自动识别的高光点常常早半秒或晚半秒，差这一点成片开头就废了。
   卡片上的 ‹ › 把整段的起止一起挪，只动时间不动别的：
   缩略图跟着重取、台词按新位置重拼，方案也会立刻重算。 */
const MV = 0.5;            // 单击挪 0.5 秒
const MV_BIG = 2.0;        // Shift + 单击挪 2 秒（一次挪一大截）

/* 候选在 localStorage 里的稳定 key：平移后 g0 会变，key 必须用原始值 */
function hkId(h) { return h._k || hookKey(h); }

function epBounds(ep) {
  const e = S.eps.find((x) => x.idx === ep);
  return e ? [e.offset, e.offset + Math.max(0, e.dur)] : [0, 1e9];
}

/* 挪完的位置覆盖了哪几句台词，用来即时刷新卡片文字 */
function textAt(ep, g0, g1, tail) {
  const l = S.beats.filter((b) => b.ep === ep && b.g1 > g0 + 0.02 && b.g0 < g1 - 0.02);
  const t = l.map((b) => b.text).join(' ').trim();
  if (!t) return '';
  return tail ? t.slice(-56) : t.slice(0, 56);
}

function round3(v) { return Math.round(v * 1000) / 1000; }

/* d 被夹在这一集里，返回夹好的位移量 */
function clampShift(raw0, raw1, ep, d) {
  const [lo, hi] = epBounds(ep);
  const pad = 0.2;
  d = Math.min(d, hi - pad - raw1);       // 往后：尾巴不能出集
  d = Math.max(d, lo + pad - raw0);       // 往前：开头不能出集
  return d;
}

function shiftHook(h, delta) {
  const k = hkId(h);
  const cur = S.hkShift[k] || 0;
  const raw0 = h.g0 - cur, raw1 = h.g1 - cur;
  const d = round3(clampShift(raw0, raw1, h.ep, cur + delta));
  if (Math.abs(d - cur) < 1e-6) { toast('已经贴到这一集的边了，再挪就到别的集了', true); return; }
  if (Math.abs(d) < 0.05) delete S.hkShift[k]; else S.hkShift[k] = d;
  const on = S.hook && Math.abs(S.hook.g0 - h.g0) < 0.15;
  if (S.batch[k]) { S.batch[k].g0 = round3(raw0 + d); S.batch[k].g1 = round3(raw1 + d); }
  saveHkEdits();
  if (on) {                    // 当前开头跟着挪，方案立刻按新位置重算
    S.hook = Object.assign({}, S.hook, { g0: round3(raw0 + d), g1: round3(raw1 + d) });
    renderHooks(); renderHookBox(); makePlan();
  } else {
    renderHooks();
  }
  renderBatchBar(); draw();
  toast((d > 0 ? '后移 ' : '前移 ') + Math.abs(delta) + ' 秒 → ' +
        fmt1(raw0 + d) + ' → ' + fmt1(raw1 + d) + '（第 ' + h.ep + ' 集）');
}

function shiftEnd(h, delta) {
  const k = h._k;
  const cur = S.endShift[k] || 0;
  const raw0 = h.g0 - cur, raw1 = h.g1 - cur;
  const d = round3(clampShift(raw0, raw1, h.ep, cur + delta));
  if (Math.abs(d - cur) < 1e-6) { toast('已经贴到这一集的边了，再挪就到别的集了', true); return; }
  if (Math.abs(d) < 0.05) delete S.endShift[k]; else S.endShift[k] = d;
  const on = S.endMode === 'point' && S.endhook && Math.abs(S.endhook.g1 - h.g1) < 0.25;
  saveHkEdits();
  if (on) {
    S.endhook = Object.assign({}, S.endhook, { g0: round3(raw0 + d), g1: round3(raw1 + d) });
    renderEnds(); makePlan();
  } else {
    renderEnds();
  }
  draw();
  toast((d > 0 ? '后移 ' : '前移 ') + Math.abs(delta) + ' 秒 → 收在 ' +
        fmt1(raw1 + d) + '（第 ' + h.ep + ' 集）');
}

/* 原始候选（自动识别的 + 手动加的，没被删掉的），顺序即面板顺序 */
function hookRaw() {
  return S.hkAdd.concat(S.highlights.filter((h) => !S.hkHide.has(hookKey(h))));
}

/* 没被删掉的全部候选：手动加的排最前，人工挪过的按位移显示 */
function hookAll() {
  return hookRaw().map((h) => {
    const k = hookKey(h);
    const d = S.hkShift[k] || 0;
    const o = Object.assign({}, h, { _k: k, shift: d });
    if (d) {
      o.g0 = round3(h.g0 + d); o.g1 = round3(h.g1 + d);
      const t = textAt(h.ep, o.g0, o.g1, false);
      if (t) o.text = t;
    }
    return o;
  });
}

/* ── 钩子类型：筛选 / 开关 / 权重 / 证据 ─────────────────────────── */
const HK_TYPES = [
  { key: 'energy',      label: '高能画面', group: 'visual' },
  { key: 'action',      label: '动作冲突', group: 'visual' },
  { key: 'intimacy',    label: '亲密接触', group: 'talk' },
  { key: 'tension',     label: '情感张力', group: 'visual' },
  { key: 'performance', label: '戏点张力', group: 'visual' },
  { key: 'dialog',      label: '台词冲突', group: 'talk' },
  { key: 'reveal',      label: '身份反转', group: 'talk' },
  { key: 'suspense',    label: '悬念留扣', group: 'talk' },
];
const HK_TYPE_MAP = {};
HK_TYPES.forEach((t) => { HK_TYPE_MAP[t.key] = t; });
// 旧项目 / 老候选没有 primary_type 时按旧 kind 回退，界面不会开天窗
const HK_KIND_FALLBACK = {
  '动作冲突': 'action', '爆发点': 'action', '台词冲突': 'dialog',
  '身份反转': 'reveal', '悬念留扣': 'suspense', '剧情节点': 'performance',
};
const HK_PREF_KEY = 'dc_hookprefs';

function hookType(h) {
  if (h.manual) return 'manual';
  return HK_TYPE_MAP[h.primary_type] ? h.primary_type : (HK_KIND_FALLBACK[h.kind] || 'dialog');
}

function hookMatchFilter(h, f) {
  if (f === 'all') return true;
  const meta = HK_TYPE_MAP[hookType(h)];
  if (f === 'visual') return !!meta && meta.group === 'visual';
  if (f === 'talk') return !!meta && meta.group === 'talk';
  return hookType(h) === f;
}

function loadHookPrefs() {
  try {
    const d = JSON.parse(localStorage.getItem(HK_PREF_KEY) || '{}');
    S.hkPrefs = { enabled: d.enabled || {}, weight: d.weight || {} };
  } catch (e) { S.hkPrefs = { enabled: {}, weight: {} }; }
}
function saveHookPrefs() {
  try { localStorage.setItem(HK_PREF_KEY, JSON.stringify(S.hkPrefs)); }
  catch (e) { /* 隐私模式：只影响本次会话 */ }
}
function hookPrefEnabled(t) { return S.hkPrefs.enabled[t] !== false; }
function hookPrefWeight(t) {
  const v = Number(S.hkPrefs.weight[t]);
  return (isFinite(v) && v >= 0 ? v : 100) / 100;
}

function hookShown() {
  const kept = [];
  hookAll().forEach((h, i) => {
    if (h.manual) { kept.push({ h: h, i: i, w: 1e9 }); return; }  // 手动加的永远排最前
    const t = hookType(h);
    if (!hookPrefEnabled(t) || !hookMatchFilter(h, S.hkFilter)) return;
    const base = Number(h.rank_score) || Number(h.score) || 0;
    kept.push({ h: h, i: i, w: base * hookPrefWeight(t) });
  });
  kept.sort((a, b) => b.w - a.w || a.i - b.i);   // 同权重保持原顺序，稳定可预期
  return kept.map((x) => x.h);
}

function hookKindClass(h) {
  if (h.manual) return 'k-man';
  if (hookType(h) === 'intimacy') return 'k-int';
  const meta = HK_TYPE_MAP[hookType(h)];
  return meta && meta.group === 'visual' ? 'k-act' : 'k-talk';
}

function renderHookFilter() {
  const box = $('#hkFilter');
  if (!box) return;
  const scroll = box.scrollLeft;
  const present = {};
  hookAll().forEach((h) => { if (!h.manual) present[hookType(h)] = true; });
  const items = [{ f: 'all', label: '全部' }, { f: 'visual', label: '画面' },
                 { f: 'talk', label: '台词' }];
  const extras = HK_TYPES.filter((t) => present[t.key])
                         .map((t) => ({ f: t.key, label: t.label }));
  if (extras.length) items.push({ sep: true });
  extras.forEach((x) => items.push(x));
  if (S.hkFilter !== 'all' && !items.some((x) => x.f === S.hkFilter)) S.hkFilter = 'all';
  box.innerHTML = items.map((x) => x.sep ? '<i class="fsep"></i>'
    : '<button data-f="' + x.f + '" class="' + (S.hkFilter === x.f ? 'on' : '') + '">' +
      esc(x.label) + '</button>').join('');
  $$('#hkFilter button').forEach((b) => {
    b.onclick = () => { S.hkFilter = b.dataset.f; renderHookFilter(); renderHooks(); };
  });
  box.scrollLeft = scroll;
}

const HK_SIG_NAMES = {
  dialog: '台词', reveal: '身份', suspense: '悬念', motion: '画面运动',
  audio: '声音', intensity: '冲击强度', stillness: '静态张力', position: '位置',
};

function openHookInfo(h) {
  const body = $('#hookInfoBody');
  $('#hookInfoTitle').textContent = (h.primary_label || h.kind || '剧情节点') +
    ' · 第 ' + h.ep + ' 集 ' + fmt1(h.g0) + ' → ' + fmt1(h.g1);
  if (!h.signals || !h.evidence) {
    body.innerHTML = '<div class="hi-warn">这条候选来自旧版识别结果（或手动添加），' +
      '没有证据明细。重新导入一次即可生成。</div>';
  } else {
    const bars = Object.keys(HK_SIG_NAMES).map((k) => {
      const v = Number(h.signals[k] || 0);
      return '<div class="hi-row"><span class="hi-nm">' + HK_SIG_NAMES[k] + '</span>' +
        '<span class="hi-bar"><i style="width:' + Math.round(v * 100) + '%"></i></span>' +
        '<span class="hi-v">' + v.toFixed(2) + '</span></div>';
    }).join('');
    const ev = (h.evidence || []).map((e) =>
      '<div class="hi-ev"><b>' + esc(e.kind || '') + '</b><span>' + esc(e.note || '') +
      '（' + Number(e.strength || 0).toFixed(2) + '）</span></div>').join('');
    body.innerHTML =
      '<div class="hi-sec">判定</div>' +
      '<div class="hi-row"><span class="hi-nm">主类型</span><span>' +
        esc(h.primary_label || h.kind || '-') + '</span>' +
        '<span style="margin-left:auto;color:var(--dim)">置信度 ' +
        Math.round(Number(h.confidence || 0) * 100) + '%</span></div>' +
      '<div class="hi-row"><span class="hi-nm">全部标签</span><span>' +
        esc((h.types || []).join(' / ') || '-') + '</span></div>' +
      '<div class="hi-row"><span class="hi-nm">信号来源</span><span>' +
        esc(h.source || '-') + '　·　' + (h.has_dialog ? '有台词' : '无台词') + '</span></div>' +
      '<div class="hi-sec">信号强度</div>' + bars +
      '<div class="hi-sec">命中证据</div>' + (ev || '<div class="hi-warn">无</div>') +
      '<div class="hi-sec">排序理由</div><div class="hi-warn">' +
        esc(h.rank_reason || '-') + '　展示分 ' + Number(h.score || 0).toFixed(1) +
        ' · 排序分 ' + Number(h.rank_score || 0).toFixed(1) + '</div>';
  }
  $('#hookInfoModal').classList.add('on');
}

function renderHookPrefs() {
  const box = $('#hookPrefsList');
  const counts = {};
  hookAll().forEach((h) => {
    if (!h.manual) { const t = hookType(h); counts[t] = (counts[t] || 0) + 1; }
  });
  box.innerHTML = HK_TYPES.map((t) => {
    const on = hookPrefEnabled(t.key);
    const w = Math.round(hookPrefWeight(t.key) * 100);
    return '<div class="hi-pref" data-t="' + t.key + '">' +
      '<label><input type="checkbox" class="hi-en" ' + (on ? 'checked' : '') + '>' +
        esc(t.label) + '</label>' +
      '<input type="range" class="hi-w" min="0" max="200" step="10" value="' + w + '">' +
      '<span class="hi-pv">' + w + '%</span><span class="hi-pc">' +
        (counts[t.key] || 0) + ' 条</span></div>';
  }).join('');
  $$('#hookPrefsList .hi-pref').forEach((row) => {
    const t = row.dataset.t;
    const en = row.querySelector('.hi-en');
    const rg = row.querySelector('.hi-w');
    en.onchange = () => {
      S.hkPrefs.enabled[t] = en.checked;
      saveHookPrefs(); renderHookPrefs(); renderHooks();
    };
    rg.oninput = () => {
      S.hkPrefs.weight[t] = Number(rg.value);
      row.querySelector('.hi-pv').textContent = rg.value + '%';
      saveHookPrefs();
    };
    rg.onchange = () => renderHooks();
  });
}
function openHookPrefs() { renderHookPrefs(); $('#hookPrefsModal').classList.add('on'); }

/* 已设条数的开头，按候选面板顺序排（分数高的在前），总数不够分时好开头优先 */
function batchItems() {
  const order = new Map();
  hookAll().forEach((h, i) => { if (!order.has(hkId(h))) order.set(hkId(h), i); });
  return Object.keys(S.batch)
    .map((k) => ({ k, v: S.batch[k], i: order.has(k) ? order.get(k) : 9999 }))
    .sort((a, b) => a.i - b.i)
    .map((x) => Object.assign({ k: x.k }, x.v));
}

/* 与后端 _alloc_items 一致的分配，先在前端算给用户看 */
function planCounts(list) {
  const n = list.length;
  if (!n) return [];
  if (S.batchAlloc === 'total' && S.batchTotal > 0) {
    const t = Math.max(1, Math.min(200, Math.round(S.batchTotal)));
    if (t < n) return list.slice(0, t).map(() => 1);
    const base = Math.floor(t / n), extra = t % n;
    return list.map((_, i) => base + (i < extra ? 1 : 0));
  }
  return list.map((v) => Math.max(1, Math.min(20, v.count || 1)));
}

/* 批量状态同步到右下角出片卡：没设条数=单条顺剪，设了=批量 */
function renderBatchBar() {
  try {
  const list = batchItems();
  const btn = $('#btnRender'), line = $('#batchLine');
  const brief = $('#bPlan'), row = $('.cntrow');
  if (!list.length) {           // 单条模式：数量行淡化，手动输入仍然可用
    btn.textContent = '开始顺剪出片';
    btn.title = '按当前方案出一条成片；想批量就在候选卡片右下用 −N+ 设条数';
    line.hidden = true;
    brief.textContent = '单条';
    if (row) row.classList.add('dim');
    return;
  }
  if (row) row.classList.remove('dim');
  const counts = planCounts(list);
  const total = counts.reduce((a, b) => a + b, 0);
  const isTotal = S.batchAlloc === 'total';
  const spec = bendSpecCount();
  let s = '批量：已选 <b>' + list.length + '</b> 个开头 · 共 <b>' + total + '</b> 条' +
    (spec ? ' · 已指定结尾 <b>' + spec + '</b> 条' : '') +
    ' · <a href="javascript:void(0)" id="bendLink" title="给每条成片单独选结尾候选">指定每条的结尾…</a>';
  if (isTotal && total < list.length) s += '（总数只够前 ' + total + ' 个）';
  line.innerHTML = s;
  line.hidden = false;
  $('#bendLink').onclick = bendOpen;
  btn.textContent = '开始批量出片（共 ' + total + ' 条）';
  btn.title = '每个开头第 1 条是标准方案，第 2 条起换结尾候选、剪几处空镜做差异化';
  brief.textContent = isTotal && counts.length > 1
    ? '分配 ' + counts.slice(0, 8).join('·') + (counts.length > 8 ? '…' : '')
    : '共 ' + total + ' 条';
  $('#bTotal').disabled = !isTotal;
  } finally { syncClearPicks(); }
}

function setBatchCount(h, n) {
  const gk = hkId(h);
  n = Math.max(0, Math.min(20, n));
  if (n === 0) { delete S.batch[gk]; delete S.batchEnd[gk]; }
  else {
    S.batch[gk] = { g0: h.g0, g1: h.g1, count: n, ep: h.ep };
    const ends = S.batchEnd[gk] || [];
    ends.length = Math.min(ends.length, n);   // 条数变少时截掉多余的指定
    S.batchEnd[gk] = ends;
  }
  renderHooks();
  renderBatchBar();
  draw();
}

/* 高光候选区「全选」：当前筛选下能看到的候选一次全部选中（各出 1 条）。
   以前要一个个点卡片右下角的 +，挑 10 个开头就是 10 次点击。
   已经选过的保留原条数，不会被重置成 1。 */
function selectAllHooks() {
  const list = hookShown();
  if (!list.length) { toast('当前筛选下没有候选可以全选', true); return; }
  let added = 0;
  list.forEach((h) => {
    const k = hkId(h);
    if (S.batch[k]) return;
    S.batch[k] = { g0: h.g0, g1: h.g1, count: 1, ep: h.ep };
    added += 1;
  });
  renderHooks(); renderBatchBar(); draw();
  if (!added) {
    toast('当前筛选下的 ' + list.length + ' 个候选已经全选过了');
    return;
  }
  const n = batchItems().length;
  const totalMode = S.batchAlloc === 'total' && S.batchTotal > 0;
  let tail = '（各出 1 条）';
  if (totalMode) {
    tail = n > S.batchTotal
      ? '，当前按总数 ' + S.batchTotal + ' 条分配（只够前 ' + S.batchTotal + ' 个，可在右下角改成按张数）'
      : '，合计 ' + S.batchTotal + ' 条';
  }
  toast('已全选 ' + n + ' 个开头' + tail + '；不想要的再点卡片右下角的 −');
}

/* ── 批量结尾指定：每条成片可以单独选用哪个结尾候选 ──
   结尾候选跟着开头走：同一个候选对 A 开头够得着、对 B 开头未必，
   所以面板按每个批量开头各自算一份（/api/plan/ends，纯计算很快）。 */
function batchEndGet(gk, n) {
  const arr = (S.batchEnd[gk] || []).slice(0, n);
  while (arr.length < n) arr.push(null);
  return arr;
}

function bendCandsFor(gk) {
  return (S.bendCands && S.bendCands[gk]) || endList();
}

function endsForItem(gk, n) {
  const ends = bendCandsFor(gk);
  return batchEndGet(gk, n).map((k) => {
    if (!k) return null;
    const e = ends.find((x) => endKey(x) === k);
    return e ? { g0: e.g0, g1: e.g1, ep: e.ep } : null;
  });
}

function bendSpecCount() {
  let n = 0;
  for (const k in S.batchEnd) n += S.batchEnd[k].filter(Boolean).length;
  return n;
}

let bendLoading = false;
let bendCtx = null;   // {list, counts, errs}——重渲染时不用重新算候选

async function bendOpen() {
  if (bendLoading) return;
  try {
    await bendOpenInner();
  } catch (e) {
    console.error(e);
    toast('打开「指定结尾」面板失败：' + ((e && e.message) || e), true);
  } finally {
    bendLoading = false;
  }
}

async function bendOpenInner() {
  const list = batchItems();
  if (!list.length) { toast('先在高光开头卡片右下用 −N+ 设要出几条', true); return; }
  const counts = planCounts(list);
  bendLoading = true;
  $('#bendModal').classList.add('on');
  $('#bendSub').textContent = '正在按各开头计算可用的结尾候选…';
  $('#bendBody').innerHTML = '<div class="empty">正在计算…</div>';
  const rs = await Promise.all(list.map((v) =>
      api('/api/plan/ends', {
        hook: { g0: v.g0, g1: v.g1 },
        target: S.target, hook_lead: S.lead, tightness: S.tightness,
        tail_mode: S.tailMode, tail_fixed: S.tailFixed,
      }).catch(() => ({ ends: [], error: '算不出来' }))));
  bendCtx = { list, counts, errs: rs.map((r) => r.error || '') };
  S.bendCands = {};
  list.forEach((v, i) => { S.bendCands[v.k] = rs[i].ends || []; });
  $('#bendSub').textContent =
    '点候选画面看视频（往前 10 秒铺垫 + 停点后 1.5 秒）；点候选下面的数字把对应条指定到这个结尾';
  bendRender();
}

/* 面板重画：指定/取消指定后只重渲染，不重新算候选；滚动位置不跳 */
function bendRefresh() {
  const b = $('#bendBody');
  const sc = b.scrollTop;
  bendRender();
  b.scrollTop = sc;
}

function bendRender() {
  if (!bendCtx) return;
  const { list, counts, errs } = bendCtx;
  $('#bendBody').innerHTML = list.map((v, i) => {
    const n = counts[i] || 0;
    if (n < 1) return '';
    const cands = S.bendCands[v.k] || [];
    const arr = batchEndGet(v.k, n);
    const cards = cands.map((c) => {
      const k = endKey(c);
      const used = arr.map((x, j) => (x === k ? j + 1 : 0)).filter(Boolean);
      const t = Math.max(0, c.g1 - epOffset(c.ep) - 1.0);
      const chips = Array.from({ length: n }, (_, j) =>
        '<i class="bnum' + (arr[j] === k ? ' on' : '') + '" data-gk="' + v.k + '" data-j="' + j +
        '" title="把第' + (j + 1) + '条的结尾指定到这个候选（再点一次取消）">' + (j + 1) + '</i>').join('');
      return '<div class="bendcd' + (used.length ? ' on' : '') + '" data-gk="' + v.k + '" data-ck="' + k + '"' +
        ' title="' + esc(c.reason || c.text || '') + '（点画面看视频，点数字指定）">' +
        '<img class="bthumb" loading="lazy" src="/api/frame?ep=' + c.ep + '&t=' + t.toFixed(2) + '" alt="">' +
        (c.at_ep_end ? '<b class="btag">集末</b>' : '') +
        (used.length ? '<b class="bused" title="已指定给第' + used.join('、') + '条">✓' + used.join('·') + '</b>' : '') +
        '<i class="bplayic" title="看视频：停在这里是什么效果">▶</i>' +
        '<div class="blab">第' + c.ep + '集 · 距开头 ' + fmt(c.elapsed || 0) + '</div>' +
        '<div class="bnums">' + chips + '</div>' +
        '</div>';
    }).join('');
    const opts = (sel) => '<option value="">自动（默认轮换）</option>' +
      cands.map((c) => '<option value="' + endKey(c) + '"' +
        (sel === endKey(c) ? ' selected' : '') + '>第' + c.ep + '集 ' + fmt1(c.g1) +
        (c.at_ep_end ? '（集末）' : '') +
        ' · ' + esc(String(c.text || '').replace(/\s+/g, ' ').slice(0, 14)) + '</option>').join('');
    const sel = arr.map((k, j) =>
      '<label class="bendc"><span>第' + (j + 1) + '条</span>' +
      '<select class="input" data-gk="' + v.k + '" data-i="' + j + '">' + opts(k) + '</select>' +
      '<i class="bplay" data-gk="' + v.k + '" data-i="' + j + '" title="预览这条当前选中的结尾">▶</i></label>'
    ).join('');
    return '<div class="bendrow"><div class="bendh">开头' + (i + 1) + ' · 第 ' +
      (v.ep || '?') + ' 集 ' + fmt1(v.g0) + ' → ' + fmt1(v.g1) + ' · <b>' + n + '</b> 条' +
      (cands.length ? '<span class="bendhact">' +
        '<a href="javascript:void(0)" class="bfill" data-gk="' + v.k + '" title="第1条用第1个候选、第2条用第2个…不够就回到自动">按候选顺序分配</a>' +
        '<a href="javascript:void(0)" class="bzero" data-gk="' + v.k + '">这一组全自动</a></span>' : '') +
      '</div>' +
      (cands.length
        ? '<div class="bendcands">' + cards + '</div>' +
          '<div class="bendgrid">' + sel + '</div>'
        : '<div class="empty">这个开头算不出结尾候选（' + esc(errs[i] || '后面剧情不够') + '）</div>') +
      '</div>';
  }).join('') || '<div class="empty">这一批没有可出片的条目</div>';
}

function bendCand(gk, key) {
  return (S.bendCands[gk] || []).find((c) => endKey(c) === key) || null;
}

/* 复用结尾候选预览播放器：往前 10 秒铺垫 + 停点后 1.5 秒，亲眼看卡在哪 */
function bendPreview(c) {
  pvOpen({ ep: c.ep, g0: c.g0, g1: c.g1 }, true);
}

function bendClose() { $('#bendModal').classList.remove('on'); }

/* 缩略图取开头后 0.8 秒：正好卡在动作发生处，也避开片头淡入的白帧 */
function thumbT(h) {
  const e = S.eps.find((x) => x.idx === h.ep);
  const inEp = Math.max(0, h.g0 - epOffset(h.ep));
  const t = inEp + Math.min(0.8, Math.max(0.2, (h.dur || 6) * 0.35));
  return Math.min(t, Math.max(0, (e ? e.dur : t) - 0.2));
}

function renderHooks() {
  try {
  renderHookFilter();
  const box = $('#hooks');
  const all = hookAll();
  const shown = hookShown();
  const fm = S.hookMeta || {};
  $('#hkCount').textContent = all.length
    ? ('共 ' + all.length + ' 个' + (fm.front_count
        ? ' · 前' + fm.front_eps + '集 ' + fm.front_count + ' 个' : ''))
    : '—';
  $('#btnResetHooks').hidden = !S.hkHide.size && !S.hkAdd.length && !Object.keys(S.hkShift).length;
  if (!all.length) {
    box.innerHTML = '<div class="empty">导入剧集后自动生成</div>';
    return;
  }
  if (!shown.length) {
    box.innerHTML = '<div class="empty">这个筛选下没有候选。切到「全部」，或在「类型设置」里重新启用被关掉的类型</div>';
    return;
  }
  box.innerHTML = shown.map((h, i) => {
    const gk = hkId(h);
    const n = S.batch[gk] ? S.batch[gk].count : 0;
    const on = hookSelected(h);
    const mv = Number(h.shift || 0);
    const cls = 'hook' + (on ? ' on' : '') + (n ? ' batched' : '') + (h.manual ? ' man' : '') +
      (Math.abs(mv) > 0.01 ? ' mvd' : '');
    const sub = { slap: ' · 巴掌', kiss: ' · 亲吻', intimate: ' · 亲密' }[h.subtype] || '';
    const kind = (h.primary_label || h.kind || '剧情节点') + sub;
    const score = h.manual ? '手动' : h.score.toFixed(0);
    const label = String(h.text || '').slice(0, 56) ||
      (h.visual_only ? '无台词 · 画面钩子' : '');
    return '<div class="' + cls + '" data-i="' + i + '">' +
      '<i class="hdel" title="从候选里删除">✕</i>' +
      '<i class="hi-ico" title="为什么算高光（证据与信号）">?</i>' +
      '<span class="hkind ' + hookKindClass(h) + '">' + esc(kind) + '</span>' +
      (on ? '<i class="hx" title="取消选择这个开头，回到默认开头">取消选择</i>' : '') +
      '<div class="hmv">' +
        '<i class="mvl" title="前移 0.5 秒（按住 Shift 挪 2 秒）">‹</i>' +
        '<i class="mvr" title="后移 0.5 秒（按住 Shift 挪 2 秒）">›</i></div>' +
      '<img class="hthumb" loading="lazy" src="/api/frame?ep=' + h.ep + '&t=' +
        thumbT(h).toFixed(2) + '" alt="">' +
      '<div class="htop"><span>第 ' + h.ep + ' 集</span><span>·</span><span>' + h.dur.toFixed(1) + 's</span>' +
        '<span class="hsc">' + score + '</span></div>' +
      '<div class="htxt">' + esc(label) + '</div>' +
      '<div class="hmeta"><span' + (Math.abs(mv) > 0.01 ? ' class="mvx"' : '') + '>' +
        fmt1(h.g0) + ' → ' + fmt1(h.g1) + '</span>' +
        (Math.abs(mv) > 0.01 ? '<em>' + (mv > 0 ? '+' : '−') + Math.abs(mv).toFixed(1) + 's</em>' : '') +
        '</div>' +
      '<i class="hplay" title="预览这一段（第一帧就是成片第一帧）">▶</i>' +
      '<div class="bstep" data-gk="' + gk + '" title="该开头批量出几条">' +
        '<i class="bm">−</i><b class="bn' + (n ? ' has' : '') + '">' + n + '</b><i class="bp">+</i></div>' +
    '</div>';
  }).join('');
  $$('#hooks .hook').forEach((el) => {
    el.onclick = () => selectHook(shown[Number(el.dataset.i)]);
  });
  $$('#hooks .hi-ico').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      openHookInfo(shown[Number(el.parentElement.dataset.i)]);
    };
  });
  $$('#hooks .hmv i').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      const h = shown[Number(el.parentElement.parentElement.dataset.i)];
      shiftHook(h, (el.classList.contains('mvl') ? -1 : 1) * (e.shiftKey ? MV_BIG : MV));
    };
  });
  $$('#hooks .bstep').forEach((st) => {
    st.onclick = (e) => {
      e.stopPropagation();
      const h = shown[Number(st.parentElement.dataset.i)];
      const cur = S.batch[st.dataset.gk] ? S.batch[st.dataset.gk].count : 0;
      if (e.target.classList.contains('bp')) setBatchCount(h, cur + 1);
      else if (e.target.classList.contains('bm')) setBatchCount(h, cur - 1);
    };
  });
  $$('#hooks .hplay').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      pvOpen(shown[Number(el.parentElement.dataset.i)], false);
    };
  });
  $$('#hooks .hx').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      clearHook();
      toast('已取消这条开头，回到默认开头');
    };
  });
  $$('#hooks .hdel').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      deleteHook(shown[Number(el.parentElement.dataset.i)]);
    };
  });
  bindThumbs('#hooks');
  } finally { syncClearPicks(); }   // 任何一条 return 分支都要刷新"清空选择"按钮
}

/* ── 缩略图抽不出来时：说清楚 + 点一下跑自检 ─────────
   换电脑最常见的就是 ffmpeg 被杀毒隔离、源视频路径变了，
   以前只会留一块无声的黑，用户不知道哪儿出错。 */
function bindThumbs(scope) {
  $$(scope + ' .hthumb').forEach((im) => {
    const fail = () => {
      im.classList.add('bad');
      im.alt = '取帧失败 · 点这里看原因';
      im.title = '缩略图没抽出来：点一下跑环境自检';
    };
    im.addEventListener('error', fail);
    if (im.complete && im.naturalWidth === 0) fail();
    im.addEventListener('click', (e) => {
      if (!im.classList.contains('bad')) return;
      e.stopPropagation();
      runDoctor();
    });
  });
}

/* ── 环境自检 ─────────────────────────────────────── */
async function runDoctor() {
  const body = $('#diagBody');
  $('#diagModal').classList.add('on');
  body.innerHTML = '<div class="dg"><div class="it ok"><span class="mk">…</span>' +
    '<span class="ms">正在检查 ffmpeg、目录权限和源视频……</span></div></div>';
  try {
    const d = await api('/api/doctor', undefined, 'GET');
    body.innerHTML = '<div class="dg">' + d.items.map((x) =>
      '<div class="it ' + (x.ok ? 'ok' : 'no') + '"><span class="mk">' + (x.ok ? '✓' : '✗') +
        '</span><span class="nm">' + esc(x.item) + '</span><span class="ms">' + esc(x.msg) +
        '</span></div>').join('') +
      '<div class="sum">' + (d.ok
        ? '环境正常。缩略图还黑的话，多半是这部片子的源视频解不开代码，换一部试试'
        : '带 <b>✗</b> 的那几项就是要修的地方') + '</div></div>';
  } catch (e) {
    body.innerHTML = '<div class="dg"><div class="it no"><span class="mk">✗</span>' +
      '<span class="ms">' + esc(e.message) + '</span></div></div>';
  }
}

$('#diagClose').onclick = () => $('#diagModal').classList.remove('on');
$('#diagModal').addEventListener('click', (e) => {
  if (e.target.id === 'diagModal') $('#diagModal').classList.remove('on');
});
$('#lnkDoc').onclick = () => runDoctor();
$('#fwDiag').onclick = () => runDoctor();
$('#fwNew').onclick = () => {
  $('#folder').value = '';
  $('#projName').value = '';
  $('#folder').focus();
  toast('把这部新剧的文件夹选进来（或直接粘贴路径），点「扫描并分析」');
};

/* ── 高光钩子 / 结尾候选 · 原片预览 ───────────────── */
/* PV.a 就是成片第一帧：钩子预览从「钩子起点 − 开头入戏」开始播，
   和出片切的第一刀完全对齐。以前这里固定往前多看 1.5 秒，
   成片却从钩子起点切，看起来就像"成片的钩子晚了 1.5 秒"。 */
const PV = { a: 0, b: 0, loop: false, l0: 0, cut: 0, ep: 0, isEnd: false };

function pvLabel() {
  const extra = Math.max(0, PV.cut - PV.a);
  const s = '第 ' + PV.ep + ' 集 · <b>' + fmt1(PV.a) + ' → ' + fmt1(PV.b) + '</b> · ' +
    (PV.b - PV.a).toFixed(1) + ' 秒';
  if (PV.isEnd) return s + '（含收尾前铺垫）';
  return s + (extra > 0.05 ? '（往前多看了 ' + extra.toFixed(1) + ' 秒）' : '（成片第一段）');
}

function pvOpen(h, isEnd) {
  const ep = S.eps.find((x) => x.idx === h.ep);
  if (!ep) return;
  const off = epOffset(h.ep);
  const l0 = Math.max(0, h.g0 - off), l1 = Math.min(ep.dur, Math.max(l0 + 1, h.g1 - off));
  PV.l0 = l0; PV.ep = h.ep; PV.isEnd = !!isEnd;
  // 结尾看的是「收在哪儿」：往前多给 10 秒铺垫；开头钩子 = 成片第一段，长度和出片一模一样
  PV.a = isEnd ? Math.max(0, l1 - 10) : Math.max(0, l0 - Math.max(0, Number(S.lead) || 0));
  PV.cut = PV.a;
  PV.done = false;
  PV.b = Math.min(ep.dur, l1 + (isEnd ? 1.5 : 0));
  const v = $('#pvVideo');
  $('#pvTitle').textContent = (isEnd ? '结尾候选预览' : '开头钩子预览') + ' · 第 ' + h.ep + ' 集';
  $('#pvInfo').innerHTML = pvLabel();
  $('#pvInfo').title = '钩子预览的第一帧就是成片第一帧。动作比台词早出现的话，' +
    '把「开头入戏」调大一点，或者点「多看前 1.5 秒」确认一下';
  $('#pvBack').hidden = !!isEnd;
  $('#pvModal').classList.add('on');
  try { vid.pause(); } catch (e) { /* 时间轴原片可能还没加载 */ }   // 别两边同时出声
  v.onloadedmetadata = () => {
    const portrait = v.videoHeight > v.videoWidth;
    v.classList.toggle('portrait', portrait);
    $('#pvModal .pvbox').classList.toggle('portrait', portrait);
    v.currentTime = PV.a;
    v.play().catch(() => { /* 自动播放被拦时让用户手动点 */ });
  };
  if (v.readyState >= 1) v.onloadedmetadata();
  v.src = '/api/src?ep=' + h.ep;
}

$('#pvBack').onclick = () => {
  const v = $('#pvVideo');
  if (!v.src) return;
  PV.a = Math.max(0, PV.a - 1.5);
  PV.done = false;
  try { v.currentTime = PV.a; } catch (e) { /* 元数据还没到 */ }
  if (v.paused) v.play().catch(() => {});
  $('#pvInfo').innerHTML = pvLabel();
  toast('往前多看 1.5 秒：' + fmt1(PV.a) + ' → ' + fmt1(PV.b));
};

/* 停在结尾后想重看：按钮/点画面/原生播放键都从这一段的开头重播
   （浏览器在播完后调 play() 会自动倒回 0 秒，这里把它拉回片段起点） */
function pvAgain() {
  const v = $('#pvVideo');
  if (!v.src) return;
  PV.done = false;
  v.currentTime = PV.a;
  $('#pvInfo').innerHTML = pvLabel();
  v.play().catch(() => { /* 需要用户手动点播放 */ });
}
$('#pvAgain').onclick = pvAgain;
$('#pvVideo').addEventListener('play', () => {
  const v = $('#pvVideo');
  if (!v.src) return;
  if (v.currentTime >= PV.b - 0.15 || v.currentTime < PV.a - 0.4) {
    PV.done = false;
    v.currentTime = PV.a;
    $('#pvInfo').innerHTML = pvLabel();
  }
});

function pvClose() {
  const v = $('#pvVideo');
  v.pause();
  v.removeAttribute('src'); v.load();
  $('#pvModal').classList.remove('on');
}

function pvTick() {
  const v = $('#pvVideo');
  if (!$('#pvModal').classList.contains('on') || !v.src) return;
  if (v.currentTime >= PV.b - 0.05) {
    if (PV.loop) { v.currentTime = PV.a; if (v.paused) v.play().catch(() => {}); }
    else {
      v.pause();
      // 结尾预览停在最后一帧：直接把「停在哪一集哪一秒」写出来，别让人猜
      if (PV.isEnd && !PV.done && v.currentTime > PV.a + 0.2) {
        PV.done = true;
        const ep = S.eps.find((x) => x.idx === PV.ep);
        $('#pvInfo').innerHTML = '已停在结尾 · 第 <b>' + PV.ep + '</b> 集 ' +
          fmt1(Math.min(PV.b, ep ? ep.dur : PV.b)) + '（成片就停在这一帧）';
      }
    }
  }
}

$('#pvClose').onclick = pvClose;
$('#pvVideo').addEventListener('timeupdate', pvTick);
$('#pvModal').addEventListener('click', (e) => { if (e.target.id === 'pvModal') pvClose(); });
$('#pvLoop').onclick = () => {
  PV.loop = !PV.loop;
  $('#pvLoop').classList.toggle('on', PV.loop);
  // 已经停在片段末尾时打开循环，立刻从片段开头重播
  const v = $('#pvVideo');
  if (PV.loop && v.src && v.currentTime >= PV.b - 0.1) {
    v.currentTime = PV.a;
    v.play().catch(() => { /* 需要用户手动点播放 */ });
  }
  toast(PV.loop ? '循环开：到点回到片段开头重播' : '循环关：到点停在片段末尾');
};
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && $('#pvModal').classList.contains('on')) {
    e.stopImmediatePropagation();   // 只关预览，别让同一下 Esc 把底下的面板也关了
    pvClose();
  }
});

/* ── 成品预览：按方案逐段播原片 ─────────────────────
   出片时是逐段 ffmpeg 裁切再拼接，这里拿同一个方案里的同一批
   {集, 起, 止} 在浏览器里按顺序播一遍：第一帧、每个切点、停在哪、
   尾帧停多久，和成片完全一致，但不用等转码（点开就播）。
   包装模板按全屏透明图叠在视频上，角标按出片的比例和位置摆。 */
const PP = { on: false, i: -1, steps: [], raf: 0, tailT0: 0 };

function ppTailDur() {
  if (!S.endframe) return 0;
  if (/\.(mp4|m4v|mov|mkv|webm|avi|flv|ts|wmv)$/i.test(S.endframe)) {
    const n = Number(S.efVideoDur) || 0;
    return n;
  }
  return Math.max(0.5, parseFloat($('#efDur').value) || 2.5);
}

function ppSteps() {
  const segs = (S.plan && S.plan.segments ? S.plan.segments : []).filter((s) => s.keep !== false);
  const out = segs.map((s) => ({
    kind: s.kind === 'hook' ? 'hook' : 'seg',
    ep: s.ep, a: Math.max(0, s.start), b: Math.max(s.start + 0.2, s.end),
  }));
  if (S.endframe) {
    const video = /\.(mp4|m4v|mov|mkv|webm|avi|flv|ts|wmv)$/i.test(S.endframe);
    out.push({ kind: 'tail', video, src: S.endframe, a: 0,
               b: video ? 0 : ppTailDur(), dur: ppTailDur() });
  }
  return out;
}

function ppPlaceLayers() {
  const ov = $('#ppOverlay'), lg = $('#ppLogo');
  if (S.overlay) {
    ov.src = '/api/file?p=' + encodeURIComponent(S.overlay);
    ov.style.opacity = String(parseFloat($('#ovOpacity').value) || 1);
  } else ov.hidden = true;
  if (S.logo) {
    lg.src = '/api/file?p=' + encodeURIComponent(S.logo);
    const pos = $('#logoPos').value, m = '2.2%', mb = '4.4%';
    lg.style.width = '14%';
    lg.style.left = (pos === 'tl' || pos === 'bl') ? m : '';
    lg.style.right = (pos === 'tr' || pos === 'br') ? m : '';
    lg.style.top = (pos === 'tl' || pos === 'tr') ? m : '';
    lg.style.bottom = (pos === 'bl' || pos === 'br') ? mb : '';
  } else lg.hidden = true;
}

function ppStrip() {
  const box = $('#ppStrip');
  box.innerHTML = PP.steps.map((s, i) => {
    const cls = 'ppstep' + (i === PP.i ? ' on' : '') +
      (s.kind === 'hook' ? ' hook' : '') + (s.kind === 'tail' ? ' tail' : '');
    const label = s.kind === 'tail'
      ? ('尾帧 ' + (s.video ? '视频' : s.dur.toFixed(1) + 's'))
      : ('第' + s.ep + '集 ' + fmt1(s.a) + ' · ' + (s.b - s.a).toFixed(1) + 's');
    return '<button class="' + cls + '" data-i="' + i + '">' + label + '</button>';
  }).join('');
  $$('#ppStrip .ppstep').forEach((b) => { b.onclick = () => ppGo(Number(b.dataset.i)); });
  const cur = box.querySelector('.on');
  if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

function ppInfo() {
  const st = PP.steps[PP.i];
  if (!st) return;
  const n = PP.i + 1, all = PP.steps.length;
  if (st.kind === 'tail') {
    $('#ppInfo').textContent = '第 ' + n + '/' + all + ' 段 · 引导尾帧' +
      (st.video ? '（按原视频时长播放）' : ' · 停 ' + st.dur.toFixed(1) + ' 秒');
    return;
  }
  $('#ppInfo').textContent = '第 ' + n + '/' + all + ' 段 · 第 ' + st.ep + ' 集 ' +
    fmt1(st.a) + ' → ' + fmt1(st.b) +
    (st.kind === 'hook' ? ' · 开头钩子' : '');
}

function ppLoad(show, txt) {
  const l = $('#ppLoad');
  if (!l) return;
  if (txt) $('#ppLoadTxt').textContent = txt;
  l.hidden = !show;
}

function ppGo(i) {
  if (!PP.on) return;
  const v = $('#ppVideo');
  if (i >= PP.steps.length) { ppDone(); return; }
  PP.i = i;
  const st = PP.steps[i];
  const tail = st.kind === 'tail';
  // 尾帧是整屏设计稿，默认不盖模板（和出片一致，勾了「尾帧也叠模板」才叠）
  const wrap = !tail || $('#efTemplate').checked;
  $('#ppOverlay').hidden = !(S.overlay && wrap);
  $('#ppLogo').hidden = !(S.logo && wrap);
  ppStrip();

  if (tail && !st.video) {          // 图片尾帧：静帧停留设定秒数
    v.pause();
    v.hidden = true;
    v.removeAttribute('src');
    v.dataset.src = '';
    try { v.load(); } catch (e) { /* 忽略 */ }
    const t = $('#ppTail');
    t.hidden = false;
    ppLoad(true, '正在载入尾帧…');
    t.onload = () => {
      ppLoad(false);
      $('#ppModal .ppbox').classList.toggle('portrait', t.naturalHeight > t.naturalWidth);
    };
    t.onerror = () => ppLoad(true, '尾帧读不出来：文件可能被移动或删除');
    t.src = '/api/file?p=' + encodeURIComponent(st.src);
    PP.tailT0 = performance.now();
    ppInfo();
    return;
  }
  $('#ppTail').hidden = true;
  v.hidden = false;
  const url = tail ? ('/api/file?p=' + encodeURIComponent(st.src))
                   : ('/api/src?ep=' + st.ep);
  const start = () => {
    ppLoad(false);
    if (v.videoWidth) {
      $('#ppModal .ppbox').classList.toggle('portrait', v.videoHeight > v.videoWidth);
    }
    if (st.a > 0.01) { try { v.currentTime = st.a; } catch (e) { /* 忽略 */ } }
    v.play().catch(() => { /* 自动播放被拦，用户自己点 */ });
  };
  if (v.dataset.src !== url) {
    v.dataset.src = url;
    v.onloadedmetadata = start;
    v.onerror = () => ppLoad(true, (tail ? '尾帧视频' : '第 ' + st.ep + ' 集') +
      '读不出来：源视频可能被移动或删除，点右边「说明」看环境自检');
    ppLoad(true, tail ? '正在载入尾帧…' : '正在载入第 ' + st.ep + ' 集…');
    v.src = url;
  } else if (v.readyState >= 1) {
    start();
  } else {
    ppLoad(true, tail ? '正在载入尾帧…' : '正在载入第 ' + st.ep + ' 集…');
  }
  ppInfo();
}

function ppDone() {
  PP.i = PP.steps.length;
  const v = $('#ppVideo');
  v.pause();
  $('#ppVideo').hidden = false;
  $('#ppTail').hidden = true;
  $('#ppInfo').textContent = '预览结束 · 共 ' + PP.steps.length + ' 段' +
    (S.plan ? ' · 正片 ' + fmt(S.plan.estimated) : '');
  ppStrip();
}

function ppLoopStart() {
  cancelAnimationFrame(PP.raf);
  const tick = () => {
    if (!PP.on) return;
    const st = PP.steps[PP.i], v = $('#ppVideo');
    if (st) {
      if (st.kind === 'tail' && !st.video) {
        if (performance.now() - PP.tailT0 >= st.dur * 1000) ppGo(PP.i + 1);
      } else if (v && !v.paused && st.b > 0 && v.currentTime >= st.b - 0.04) {
        ppGo(PP.i + 1);
      }
    }
    PP.raf = requestAnimationFrame(tick);
  };
  PP.raf = requestAnimationFrame(tick);
}

function ppOpen() {
  if (!S.ready) { toast('先导入一部剧', true); return; }
  if (!S.plan) { toast('先生成顺剪方案', true); return; }
  PP.steps = ppSteps();
  if (!PP.steps.length) { toast('方案里没有可播的片段', true); return; }
  if ($('#pvModal').classList.contains('on')) pvClose();
  try { vid.pause(); } catch (e) { /* 时间轴原片可能还没加载 */ }
  PP.on = true;
  $('#ppModal').classList.add('on');
  ppPlaceLayers();
  const total = (S.plan.estimated || 0) + ppTailDur();
  $('#ppNote').textContent = PP.steps.length + ' 段 · 总长约 ' + fmt(total) + ' · 与出片一致' +
    (Object.keys(S.batch).length ? ' · 批量只看当前这条' : '');
  $('#ppNote').title = '按当前方案逐段播原片：开头第一帧、每个切点、收尾停的位置、尾帧时长都和出片一致；' +
    '画质与音量归一以实际出片为准';
  ppGo(0);
  ppLoopStart();
}

function ppClose() {
  PP.on = false;
  ppLoad(false);
  cancelAnimationFrame(PP.raf);
  const v = $('#ppVideo');
  v.pause();
  v.removeAttribute('src');
  v.dataset.src = '';
  try { v.load(); } catch (e) { /* 忽略 */ }
  $('#ppTail').hidden = true;
  $('#ppModal').classList.remove('on');
}

const ppBtn = $('#btnPreviewOut');
if (ppBtn) ppBtn.onclick = async () => {
  if (PP.busy) return;                       // 连点只算一次
  if (!S.ready) { toast('先在左边导入一部剧，再来预览成品', true); return; }
  if (!S.plan) {
    // 还没生成方案（没点过钩子卡 / 刚换过剧）→ 直接算一条再预览，别让按钮看着像没反应
    if (!S.beats.length) {
      toast('这部剧没有读到台词，生成不了顺剪方案，也就没法预览成品', true);
      return;
    }
    PP.busy = true;
    const old = ppBtn.textContent;
    ppBtn.disabled = true;
    ppBtn.textContent = '正在算方案…';
    try { await makePlan(true); } finally {
      ppBtn.disabled = false;
      ppBtn.textContent = old;
      PP.busy = false;
    }
  }
  ppOpen();
};
$('#ppClose').onclick = ppClose;
$('#ppReplay').onclick = () => {
  if (!PP.on) { ppOpen(); return; }
  PP.ended = false;
  ppGo(0);
};
$('#ppModal').addEventListener('click', (e) => { if (e.target.id === 'ppModal') ppClose(); });

/* 批量结尾指定面板 */
document.addEventListener('click', (e) => {          // 兜底：onclick 丢失也能开
  if (e.target && e.target.id === 'bendLink') { e.preventDefault(); bendOpen(); }
});
$('#bendClose').onclick = bendClose;
$('#bendModal').addEventListener('click', (e) => { if (e.target.id === 'bendModal') bendClose(); });
$('#bendClear').onclick = () => {
  S.batchEnd = {};
  bendOpen();
  renderBatchBar();
  toast('已全部恢复自动轮换');
};
$('#bendBody').addEventListener('click', (e) => {
  const t = e.target;
  const card = t.closest ? t.closest('.bendcd') : null;
  if (t.classList.contains('bnum')) {                  // 点数字：把对应条指定到这个候选
    e.stopPropagation();
    const gk = t.dataset.gk, j = Number(t.dataset.j), k = card ? card.dataset.ck : null;
    if (!gk || !k) return;
    const idx = bendCtx.list.findIndex((v) => v.k === gk);
    const n = idx >= 0 ? (bendCtx.counts[idx] || 0) : 0;
    const arr = batchEndGet(gk, n);
    arr[j] = (arr[j] === k) ? null : k;
    S.batchEnd[gk] = arr;
    bendRefresh(); renderBatchBar();
    return;
  }
  if (card) {                                          // 点画面：视频预览这个结尾
    const c = bendCand(card.dataset.gk, card.dataset.ck);
    if (c) bendPreview(c);
    return;
  }
  const gk = t.dataset ? t.dataset.gk : null;
  if (!gk) return;
  const idx = bendCtx ? bendCtx.list.findIndex((v) => v.k === gk) : -1;
  const n = idx >= 0 ? (bendCtx.counts[idx] || 0) : 0;
  if (t.classList.contains('bfill')) {                 // 第1条→候选1、第2条→候选2…
    const cands = bendCandsFor(gk);
    const arr = Array.from({ length: n }, (_, j) => (cands[j] ? endKey(cands[j]) : null));
    S.batchEnd[gk] = arr;
    bendRefresh(); renderBatchBar();
  } else if (t.classList.contains('bzero')) {
    S.batchEnd[gk] = [];
    bendRefresh(); renderBatchBar();
  } else if (t.classList.contains('bplay')) {          // 预览 select 里当前选中的候选
    const selv = $('#bendBody select[data-gk="' + gk + '"][data-i="' + t.dataset.i + '"]');
    const c = selv && selv.value ? bendCand(gk, selv.value) : null;
    if (!c) { toast('这一条还是「自动」——自动轮换的结尾要出片时才定，先选个具体候选再预览', true); return; }
    bendPreview(c);
  }
});
$('#bendBody').addEventListener('change', (e) => {
  const sel = e.target;
  if (sel.tagName !== 'SELECT') return;
  const gk = sel.dataset.gk;
  const arr = S.batchEnd[gk] || [];
  arr[Number(sel.dataset.i)] = sel.value || null;
  S.batchEnd[gk] = arr;
  bendRefresh(); renderBatchBar();
});
$('#ppHint').onclick = () => toast('预览播的是原片本身：切点、顺序、尾帧和出片完全一致；' +
  '画质和音量归一（-16 LUFS）以实际出片为准');
$('#ppVideo').addEventListener('ended', () => { if (PP.on && PP.i < PP.steps.length) ppGo(PP.i + 1); });
document.addEventListener('keydown', (e) => {
  if (!$('#ppModal').classList.contains('on')) return;
  if (e.key === 'Escape') ppClose();
  else if (e.key === 'ArrowRight') ppGo(Math.min(PP.steps.length, PP.i + 1));
  else if (e.key === 'ArrowLeft') ppGo(Math.max(0, PP.i - 1));
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if ($('#pvModal').classList.contains('on')) return;   // 预览还开着：先关预览（它自己的监听负责）
  if ($('#bendModal').classList.contains('on')) bendClose();
});

function deleteHook(h) {
  const k = hkId(h);
  if (h.manual) S.hkAdd = S.hkAdd.filter((x) => hookKey(x) !== k);
  else S.hkHide.add(k);
  delete S.batch[k];
  saveHkEdits(); renderHooks(); renderBatchBar(); draw();
  toast('已从候选里删掉；误删点「恢复原样」能找回来');
}

function resetHooks() {
  S.hkHide = new Set();
  S.hkAdd = [];
  S.hkShift = {}; S.endShift = {};
  saveHkEdits();
  // 当前选中的开头 / 结尾也退回识别时的原始位置
  if (S.hook && S.hook._k) {
    const r = hookRaw().filter((x) => hookKey(x) === S.hook._k)[0];
    if (r) S.hook = { g0: r.g0, g1: r.g1, ep: r.ep, text: r.text, dur: r.dur, _k: S.hook._k };
  }
  if (S.endhook && S.endhook._k) {
    const r = endRaw().filter((x) => endKey(x) === S.endhook._k)[0];
    if (r) S.endhook = { g0: r.g0, g1: r.g1, ep: r.ep, text: r.text, dur: r.dur, _k: S.endhook._k };
  }
  renderHooks(); renderEnds(); renderHookBox(); draw();
  if (S.hook) makePlan();
  toast('候选已恢复成自动识别的结果（人工微调也一并清掉）');
}

/* ── 手动加一条 ── */
function fillAddEps() {
  $('#addEp').innerHTML = S.eps.map((e) =>
    '<option value="' + e.idx + '">第 ' + e.idx + ' 集 · ' + fmt(e.dur) + '</option>').join('');
}

function parseSec(v) {
  v = String(v || '').trim();
  if (!v) return NaN;
  if (v.indexOf(':') >= 0) {
    const p = v.split(':').map(Number);
    if (p.length !== 2 || p.some((x) => !isFinite(x))) return NaN;
    return p[0] * 60 + p[1];
  }
  const n = Number(v);
  return isFinite(n) ? n : NaN;
}

function addManualHook() {
  const ep = Number($('#addEp').value);
  const e = S.eps.find((x) => x.idx === ep);
  if (!e) { toast('先选一集', true); return; }
  let t0 = parseSec($('#addT0').value);
  let t1 = parseSec($('#addT1').value);
  if (!isFinite(t0)) { toast('填一下开始时间，比如 90 或 1:30', true); return; }
  if (!isFinite(t1)) t1 = t0 + 10;
  t0 = Math.max(0, Math.min(Math.max(0, e.dur - 0.6), t0));
  t1 = Math.max(t0 + 1, Math.min(e.dur, t1));
  if (t1 - t0 < 1) { toast('区间太短，至少 1 秒', true); return; }
  const h = {
    ep, g0: Number((e.offset + t0).toFixed(3)), g1: Number((e.offset + t1).toFixed(3)),
    dur: Number((t1 - t0).toFixed(2)), text: '手动添加的开头',
    kind: '手动', score: 0, manual: true, act: 0,
  };
  S.hkAdd = S.hkAdd.concat([h]);
  saveHkEdits();
  $('#addForm').hidden = true;
  $('#addT0').value = ''; $('#addT1').value = '';
  renderHooks();
  selectHook(h);
  toast('已加入候选并设为当前开头');
}

/* 当前选中的是不是这张候选：g0 相同还不够，集号也要一致，避免跨集同秒误判 */
function hookSelected(h) {
  if (!S.hook || !h) return false;
  if (S.hook._k && hkId(h) === S.hook._k) return true;
  return h.ep === S.hook.ep && Math.abs((h.g0 || 0) - (S.hook.g0 || 0)) < 0.15;
}

/* 取消选择：清空开头钩子，回到「第 1 集开头顺剪」 */
function clearHook(silent) {
  if (!S.hook) return;
  S.hook = null;
  renderHooks();
  renderHookBox();
  draw();
  if (!silent) makePlan();
}

/* ── 一键清空所有已选项 ─────────────────────────────────
   以前「取消选择」只清开头钩子，卡片上的 ×N 条数、指定结尾、
   结尾钩子都得一个个撤回。这里把四类选择一起收进来，点一次回到初始状态。 */
function pickStats() {
  const list = batchItems();
  const copies = list.length ? planCounts(list).reduce((a, b) => a + b, 0) : 0;
  const ends = Object.keys(S.batchEnd)
    .reduce((a, k) => a + (S.batchEnd[k] || []).filter(Boolean).length, 0);
  return {
    hook: S.hook ? 1 : 0,
    cards: list.length,
    copies,
    ends,
    endhook: S.endhook ? 1 : 0,
    total: (S.hook ? 1 : 0) + list.length + ends + (S.endhook ? 1 : 0),
  };
}

/* 按钮上直接写清将清掉多少项，空选时置灰，避免误以为"点了没反应" */
function syncClearPicks() {
  const s = pickStats();
  const label = s.total ? '清空全部选择（' + s.total + '）' : '清空全部选择';
  const tip = s.total
    ? '一次清空：开头钩子 ' + s.hook + ' · 卡片条数 ' + s.cards + ' 张（共 ' + s.copies +
      ' 条）· 指定结尾 ' + s.ends + ' 条 · 结尾钩子 ' + s.endhook
    : '当前没有已选内容';
  const btn = $('#btnClearHook');
  if (btn) {
    btn.textContent = label;
    btn.disabled = !s.total;
    btn.title = tip;
  }
  // 高光候选区里的同名按钮（面板内一键取消），文案跟批量条保持一致
  const b2 = $('#btnClearHooks');
  if (b2) {
    b2.textContent = s.total ? '一键取消选择（' + s.total + '）' : '一键取消选择';
    b2.disabled = !s.total;
    b2.title = tip;
  }
}

function clearAllPicks(silent) {
  const s = pickStats();
  if (!s.total) return null;
  S.hook = null;
  S.endhook = null;
  S.batch = {};
  S.batchEnd = {};
  S.bendCands = {};
  renderHooks();
  renderEnds();
  renderHookBox();
  renderBatchBar();
  draw();
  syncClearPicks();
  if (!silent) makePlan();
  return s;
}

function selectHook(h, silent) {
  // 再点一次已选中的候选 = 取消选择（卡片上的「取消选择」按钮同一套逻辑）
  if (hookSelected(h)) {
    clearHook(silent);
    if (!silent) toast('已取消选择，回到默认开头');
    return;
  }
  S.hook = { g0: h.g0, g1: h.g1, ep: h.ep, text: h.text, dur: h.dur, _k: hkId(h) };
  renderHooks();
  renderHookBox();
  draw();                       // 标记带上的选中态立即刷新
  if (!silent) makePlan();
}

/* ── 结尾钩子候选 ─────────────────────────────────── */
/* ── 结尾候选 ─────────────────────────────────────── */
// 优先展示"当前顺剪范围附近的候选"——用户要停的点就在这几集里，
// 全剧最后 10% 的悬念段落跟这段剧情没关系，点了也接不上。
function endKey(h) { return 'e' + h.ep + '@' + Number(h.g1).toFixed(2); }

function endRaw() {
  const c = (S.plan && S.plan.end_cands) || [];
  return c.length ? c.map((x) => ({
    g0: x.g0, g1: x.g1, ep: x.ep, score: x.score,
    text: x.text || '', hits: x.hits || [],
    dur: Math.max(1, x.g1 - x.g0), note: x.reason || '',
  })) : S.endings;
}

function endList() {
  return endRaw().map((h) => {
    const k = endKey(h);
    const d = S.endShift[k] || 0;
    const o = Object.assign({}, h, { _k: k, shift: d });
    if (d) {
      o.g0 = round3(h.g0 + d); o.g1 = round3(h.g1 + d);
      const t = textAt(h.ep, o.g0, o.g1, true);
      if (t) o.text = t;
    }
    return o;
  });
}

function renderEnds() {
  try {
  const box = $('#ends');
  const list = endList();
  if (!list.length) { box.innerHTML = '<div class="empty">导入剧集后自动生成</div>'; return; }
  const forced = S.endMode === 'point';
  box.innerHTML = list.map((h, i) => {
    const mv = Number(h.shift || 0);
    const on = forced && S.endhook && Math.abs(S.endhook.g1 - h.g1) < 0.25;
    return '<div class="hook end' + (on ? ' on' : '') + (Math.abs(mv) > 0.01 ? ' mvd' : '') +
      '" data-i="' + i + '">' +
      '<div class="hmv">' +
        '<i class="mvl" title="前移 0.5 秒（按住 Shift 挪 2 秒）">‹</i>' +
        '<i class="mvr" title="后移 0.5 秒（按住 Shift 挪 2 秒）">›</i></div>' +
      '<img class="hthumb" loading="lazy" src="/api/frame?ep=' + h.ep + '&t=' +
        (h.g1 - epOffset(h.ep) - 1.0).toFixed(2) + '" alt="">' +
      '<div class="htop"><span>第 ' + h.ep + ' 集</span><span>·</span><span>' +
        fmt1(h.g1 - epOffset(h.ep)) + '</span>' +
        '<span class="hsc">' + (h.score || 0).toFixed(0) + '</span>' +
        (h.note ? '<span class="hnote">' + esc(h.note) + '</span>' : '') + '</div>' +
      '<div class="htxt">' + esc((h.text || '').slice(-56)) + '</div>' +
      '<div class="hmeta"><span' + (Math.abs(mv) > 0.01 ? ' class="mvx"' : '') + '>' +
        (h.hits && h.hits.length ? esc(h.hits.slice(0, 3).join(' / '))
                                 : fmt1(h.g0) + ' → ' + fmt1(h.g1)) + '</span>' +
        (Math.abs(mv) > 0.01 ? '<em>' + (mv > 0 ? '+' : '−') + Math.abs(mv).toFixed(1) + 's</em>' : '') +
        '</div>' +
      '<i class="hplay" title="预览收尾（往前 10 秒铺垫 + 收尾点后 1.5 秒）">▶</i>' +
    '</div>';
  }).join('');
  $$('#ends .hook').forEach((el) => {
    el.onclick = () => selectEnd(list[Number(el.dataset.i)]);
  });
  $$('#ends .hmv i').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      const h = list[Number(el.parentElement.parentElement.dataset.i)];
      shiftEnd(h, (el.classList.contains('mvl') ? -1 : 1) * (e.shiftKey ? MV_BIG : MV));
    };
  });
  $$('#ends .hplay').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      pvOpen(list[Number(el.parentElement.dataset.i)], true);
    };
  });
  bindThumbs('#ends');
  } finally { syncClearPicks(); }
}

function selectEnd(e2, silent) {
  // 再点一次同一个 = 取消强制，回到自动择优
  const same = S.endhook && Math.abs(S.endhook.g1 - e2.g1) < 0.25 && S.endMode === 'point';
  if (same) {
    S.endhook = null; S.endMode = 'auto';
  } else {
    S.endhook = { g0: e2.g0, g1: e2.g1, ep: e2.ep, text: e2.text, dur: e2.dur, _k: e2._k };
    S.endMode = 'point';
  }
  $('#endMode').value = S.endMode;
  renderEnds();
  draw();
  if (!silent) makePlan();
}

function renderHookBox() {
  const box = $('#hookBox');
  if (!S.hook) {
    box.innerHTML = '<div class="empty sm">当前不使用开头钩子，直接从第 1 集开头顺剪。在下方候选卡片上点一下即可重新选择</div>';
    syncTabDots();
    return;
  }
  const sp = S.plan && S.plan.from;
  box.innerHTML =
    '<div class="hk"><i class="hki"></i><div>' +
      '<div class="hkt">' + esc(S.hook.text.slice(0, 120)) + '</div>' +
      '<div class="kv" style="margin-top:6px"><span>第 ' + S.hook.ep + ' 集 · ' +
        fmt1(S.hook.g0) + ' → ' + fmt1(S.hook.g1) + '</span><b>' + S.hook.dur.toFixed(1) + ' 秒</b></div>' +
      (sp ? '<div class="kv"><span>成片从这里开始（入戏 ' + (Number(S.lead) || 0).toFixed(1) + 's）</span><b>' +
        '第' + sp.ep + '集 ' + fmt1(sp.t) + '</b></div>' : '') +
      (sp ? '<div class="kv" style="margin-top:2px"><span>从这里顺接</span><b>' +
        (S.plan.ep_span ? '第 ' + S.plan.ep_span[0] + '–' + S.plan.ep_span[1] + ' 集' : '') +
        '</b></div>' : '') +
    '</div></div>';
  syncTabDots();
}

/* ── 方案 ─────────────────────────────────────────── */
function applyPlan(pl) {
  S.plan = pl;
  S.win = new Set(pl.beats_win || pl.beats_keep || []);
  S.drop = new Set(pl.beats_drop || []);
  renderStats();
  renderHookBox();   // 成片起点 / 顺接范围跟着方案走
  renderEnds();   // 结尾候选跟着顺剪范围走
  draw();
}

async function makePlan(silent) {
  if (!S.ready) return;
  if (!S.beats.length) return;  // 无字幕项目：不生成方案（界面已提示）
  try {
    const pl = await api('/api/plan', {
      target: S.target,
      hook: S.hook ? { g0: S.hook.g0, g1: S.hook.g1 } : null,
      end_hook: S.endhook ? { g0: S.endhook.g0, g1: S.endhook.g1 } : null,
      end_mode: S.endMode,
      tightness: S.tightness,
      hook_lead: S.lead,
      tail_mode: S.tailMode, tail_fixed: S.tailFixed,
    });
    applyPlan(pl);
    if (!silent) {
      let t = '顺剪：' + fmt(pl.estimated) + ' / 目标 ' + fmt(S.target) + ' · ' + pl.end_reason;
      if (pl.shortage) t = '这个开头后面的剧情不够 ' + fmt(S.target) + '，已顺到末尾';
      else if (pl.end_note) t = pl.end_note;
      toast(t, !!(pl.shortage || pl.end_note));
    }
  } catch (e) { toast(e.message, true); }
}

function scheduleSave() {
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(async () => {
    if (!S.plan || !S.ready) return;
    try {
      const pl = await api('/api/plan/beats', {
        beats_drop: Array.from(S.drop),
        hook: S.hook ? { g0: S.hook.g0, g1: S.hook.g1 } : null,
        target: S.target,
        hook_lead: S.lead,
        tail_mode: S.tailMode, tail_fixed: S.tailFixed,
      });
      applyPlan(pl);
    } catch (e) { toast(e.message, true); }
  }, 320);
}

/* ── 时间轴 ───────────────────────────────────────── */
const cv = $('#tl');
const ctx = cv.getContext('2d');
const mk = $('#marks');          // 全剧标记带：固定全剧比例，不随缩放变
const mkx = mk.getContext('2d');
let W = 800, H = 190, MW = 800, MH = 44;

function resize() {
  const dpr = window.devicePixelRatio || 1;
  const r = cv.getBoundingClientRect();
  W = Math.max(200, r.width); H = Math.max(120, r.height);
  // 显示尺寸交给 CSS 布局（flex），这里只设像素尺寸，避免内联高度反馈回布局
  cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  resizeMarks();
}

function resizeMarks() {
  const dpr = window.devicePixelRatio || 1;
  const r = mk.getBoundingClientRect();
  MW = Math.max(200, r.width); MH = 44;
  mk.width = Math.round(MW * dpr); mk.height = Math.round(MH * dpr);
  mkx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function xOf(t) { return (t - S.view.t0) / Math.max(0.001, S.view.t1 - S.view.t0) * W; }
function tOf(x) { return S.view.t0 + x / W * (S.view.t1 - S.view.t0); }

function draw() {
  ctx.clearRect(0, 0, W, H);
  const span = S.view.t1 - S.view.t0;
  if (!S.ready || span <= 0) return;

  const epTop = 0, epH = 20, bTop = 24, bH = Math.max(38, Math.min(62, H - 96)), rulerY = bTop + bH + 10;

  // 集色带
  S.eps.forEach((e, i) => {
    const x0 = xOf(e.offset), x1 = xOf(e.offset + e.dur);
    if (x1 < 0 || x0 > W) return;
    ctx.fillStyle = i % 2 ? '#12161f' : '#151a24';
    ctx.fillRect(x0, 0, Math.max(0.5, x1 - x0), H);
    ctx.strokeStyle = '#242c3b'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(Math.round(x0) + .5, 0); ctx.lineTo(Math.round(x0) + .5, H); ctx.stroke();
  });
  // 集号单独一遍：后一集的底色会把溢出的文字盖掉。
  // 步长按"当前窗口里一集占多少像素"自适应，全剧视图下就不会出现
  // 第1、第2、第4、第13 这种残缺编号
  const visN = Math.max(1, S.eps.filter((e) => e.offset + e.dur > S.view.t0 && e.offset < S.view.t1).length);
  const avgW = W / visN;
  const labelStep = avgW >= 34 ? 1 : (avgW >= 16 ? 5 : 10);
  ctx.font = '10px system-ui'; ctx.textAlign = 'left';
  S.eps.forEach((e, i) => {
    if (i % labelStep !== 0 && i !== S.eps.length - 1) return;
    const x0 = xOf(e.offset), x1 = xOf(e.offset + e.dur);
    if (x1 < 0 || x0 > W || x1 - x0 < 10) return;
    ctx.fillStyle = '#5b6579';
    ctx.fillText('第' + e.idx + '集', Math.max(2, Math.min(x0 + 4, W - 30)), 14);
  });

  // 钩子范围（以方案里的实际段落为准，避免比方块宽出一截）
  const hookR = (S.plan && S.plan.hook) || S.hook;
  const endR = (S.plan && S.plan.end) || S.endhook;
  if (hookR) {
    const x0 = Math.max(0, xOf(hookR.g0)), x1 = Math.min(W, xOf(hookR.g1));
    if (x1 > 0 && x0 < W) {
      ctx.fillStyle = 'rgba(255,138,61,.13)';
      ctx.fillRect(x0, 0, Math.max(2, x1 - x0), H);
      ctx.fillStyle = 'rgba(255,138,61,.9)';
      ctx.fillRect(x0, 0, Math.max(2, x1 - x0), 2);
      if (x1 - x0 > 40) {
        ctx.fillStyle = '#ffb27a'; ctx.font = '10px system-ui';
        ctx.fillText('钩子', x0 + 4, bTop + bH + 26);
      }
    }
  }

  // 结尾钩子范围
  if (endR) {
    const x0 = Math.max(0, xOf(endR.g0)), x1 = Math.min(W, xOf(endR.g1));
    if (x1 > 0 && x0 < W) {
      ctx.fillStyle = 'rgba(180,122,255,.14)';
      ctx.fillRect(x0, 0, Math.max(2, x1 - x0), H);
      ctx.fillStyle = 'rgba(180,122,255,.9)';
      ctx.fillRect(x0, H - 2, Math.max(2, x1 - x0), 2);
      if (x1 - x0 > 40) {
        ctx.fillStyle = '#c9a4ff'; ctx.font = '10px system-ui';
        ctx.fillText('结尾', x0 + 4, bTop + bH + 26);
      }
    }
  }

  // 节拍
  for (const b of S.beats) {
    const x0 = xOf(b.g0), x1 = xOf(b.g1);
    if (x1 < -2 || x0 > W + 2) continue;
    const w = Math.max(1.6, x1 - x0 - 0.8);
    const isHook = hookR && b.g0 >= hookR.g0 - 0.05 && b.g1 <= hookR.g1 + 0.05;
    const isEnd = endR && b.g0 >= endR.g0 - 0.05 && b.g1 <= endR.g1 + 0.05;
    const inWin = S.win.has(b.id);
    const dropped = inWin && S.drop.has(b.id);
    let col = '#1b2029';
    if (isHook) col = '#ff8a3d';
    else if (isEnd) col = '#b47aff';
    else if (inWin) col = dropped ? '#2b3240' : '#3f7df0';
    ctx.fillStyle = col;
    rrect(x0, bTop, w, bH, Math.min(4, w / 2));
    ctx.fill();
    if (dropped) {
      ctx.strokeStyle = '#4a5468'; ctx.lineWidth = 1;
      rrect(x0 + .5, bTop + .5, Math.max(1, w - 1), bH - 1, 3); ctx.stroke();
    }
    if (isHook) { ctx.fillStyle = 'rgba(255,255,255,.22)'; rrect(x0, bTop, w, 4, 2); ctx.fill(); }
    if (isEnd) { ctx.fillStyle = 'rgba(255,255,255,.22)'; rrect(x0, bTop + bH - 4, w, 4, 2); ctx.fill(); }
    if (b.id === S.hover) {
      ctx.strokeStyle = '#cfe0ff'; ctx.lineWidth = 1.5;
      rrect(x0 - .5, bTop - .5, w + 1, bH + 1, 5); ctx.stroke();
    }
  }

  // 刻度
  const step = niceStep(span, W);
  ctx.strokeStyle = '#2b3342'; ctx.fillStyle = '#5b6579'; ctx.font = '10px system-ui';
  ctx.textAlign = 'center';
  const t0 = Math.floor(S.view.t0 / step) * step;
  for (let t = t0; t <= S.view.t1 + step; t += step) {
    if (t < S.view.t0) continue;
    const x = xOf(t);
    if (x < -20 || x > W + 20) continue;
    ctx.beginPath(); ctx.moveTo(Math.round(x) + .5, rulerY - 5); ctx.lineTo(Math.round(x) + .5, rulerY); ctx.stroke();
    ctx.fillText(fmt(t), x, rulerY + 11);
  }
  // 保留段缩略条
  if (S.plan) {
    const y = H - 6;
    ctx.fillStyle = '#1d2431'; ctx.fillRect(0, y - 4, W, 3);
    for (const s of S.plan.segments) {
      if (!s.keep) continue;
      const x0 = xOf(s.g0), x1 = xOf(s.g1);
      ctx.fillStyle = s.kind === 'hook' ? '#ff8a3d' : (s.kind === 'end_hook' ? '#b47aff' : '#4c8dff');
      ctx.fillRect(x0, y - 4, Math.max(1.5, x1 - x0), 3);
    }
  }
  // 原片播放头
  if (S.playhead != null && S.playhead >= S.view.t0 && S.playhead <= S.view.t1) {
    const x = xOf(S.playhead);
    ctx.strokeStyle = '#ff5f4d'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
    ctx.fillStyle = '#ff5f4d';
    ctx.beginPath(); ctx.moveTo(x - 4, 0); ctx.lineTo(x + 4, 0); ctx.lineTo(x, 6); ctx.closePath(); ctx.fill();
  }
  drawMarks();
}

/* ── 全剧标记带：钩子 / 结尾候选直接标在完整时间轴上 ── */
/* 固定 0 → 全剧的比例，不随上面主时间轴缩放——缩小看细节时，
   也能一眼看到 18 个钩子和结尾候选在全剧的分布，点一下直接用。 */
function mxOf(t) { return t / Math.max(0.001, S.total) * MW; }

function drawMarks() {
  mkx.clearRect(0, 0, MW, MH);
  if (!S.ready || !S.total) return;

  // 集分格背景
  const epW = MW / Math.max(1, S.eps.length);
  const labelStep = epW >= 34 ? 1 : (epW >= 16 ? 5 : 10);
  S.eps.forEach((e, i) => {
    const x0 = mxOf(e.offset), x1 = mxOf(e.offset + e.dur);
    if (x1 < 0 || x0 > MW) return;
    mkx.fillStyle = i % 2 ? '#10141c' : '#131824';
    mkx.fillRect(x0, 0, Math.max(0.5, x1 - x0), MH);
    mkx.strokeStyle = '#1f2735'; mkx.lineWidth = 1;
    mkx.beginPath(); mkx.moveTo(Math.round(x0) + .5, 0); mkx.lineTo(Math.round(x0) + .5, MH); mkx.stroke();
  });
  // 集编号单独一遍画，否则后一集的底色会把溢出的文字盖掉
  mkx.font = '9px system-ui'; mkx.textAlign = 'left';
  S.eps.forEach((e, i) => {
    if (i % labelStep !== 0 && i !== S.eps.length - 1) return;
    const x0 = mxOf(e.offset), x1 = mxOf(e.offset + e.dur);
    if (x1 < 0 || x0 > MW || x1 - x0 < 8) return;
    const txt = '第' + e.idx + '集';
    const tw = mkx.measureText(txt).width;
    mkx.fillStyle = '#4a5468';
    mkx.fillText(txt, Math.max(2, Math.min(x0 + 3, MW - tw - 3)), MH - 3);
  });

  // 成片覆盖范围（淡蓝底）
  if (S.plan && S.plan.from && S.plan.to) {
    const a = mxOf(S.plan.from.g0), b = mxOf(S.plan.to.g1);
    mkx.fillStyle = 'rgba(76,141,255,.12)';
    mkx.fillRect(a, 0, Math.max(2, b - a), MH);
  }

  // 上排：结尾候选（紫）
  const ends = endList();
  ends.forEach((e2) => {
    const x = mxOf(e2.g1);
    const on = S.endhook && S.endMode === 'point' && Math.abs(S.endhook.g1 - e2.g1) < 0.25;
    mkx.fillStyle = on ? '#c9a4ff' : 'rgba(180,122,255,.72)';
    mkx.fillRect(x - 1.5, 5, 3, 8);
    if (on) { mkx.strokeStyle = '#e6d6ff'; mkx.strokeRect(x - 2.5, 4, 5, 10); }
  });

  // 下排：开头钩子候选（金）
  const hk = (S.plan && S.plan.hook) || S.hook;
  hookAll().forEach((h) => {
    const x0 = mxOf(h.g0), x1 = Math.max(x0 + 2.5, mxOf(h.g1));
    const w = Math.max(2.5, x1 - x0);
    const n = S.batch[hkId(h)];
    const on = hk && Math.abs(hk.g0 - h.g0) < 0.15;
    mkx.fillStyle = on ? '#ffa668' : 'rgba(255,138,61,.78)';
    mkx.fillRect(x0, 22, w, 9);
    if (on) { mkx.strokeStyle = '#ffe0c4'; mkx.strokeRect(x0 + .5, 22.5, w - 1, 8); }
    else if (n) { mkx.strokeStyle = '#e8b754'; mkx.strokeRect(x0 + .5, 22.5, w - 1, 8); }
  });

  // 主时间轴当前窗口（蓝框）
  const vx0 = mxOf(S.view.t0), vx1 = Math.max(vx0 + 3, mxOf(S.view.t1));
  mkx.fillStyle = 'rgba(122,168,255,.07)';
  mkx.fillRect(vx0, 0, vx1 - vx0, MH);
  mkx.strokeStyle = 'rgba(122,168,255,.85)'; mkx.lineWidth = 1;
  mkx.strokeRect(vx0 + .5, .5, vx1 - vx0 - 1, MH - 1);

  // 原片播放头（全剧位置）
  if (S.playhead != null) {
    const px = mxOf(S.playhead);
    if (px >= 0 && px <= MW) {
      mkx.strokeStyle = '#ff5f4d'; mkx.lineWidth = 1.5;
      mkx.beginPath(); mkx.moveTo(px, 0); mkx.lineTo(px, MH); mkx.stroke();
    }
  }
}

function hitMark(x, y) {
  const bdMax = 7;
  if (y <= 18) {                      // 上排：结尾候选
    let best = null, bd = bdMax;
    endList().forEach((e2) => {
      const d = Math.abs(mxOf(e2.g1) - x);
      if (d < bd) { bd = d; best = e2; }
    });
    return best ? { type: 'end', item: best } : null;
  }
  let best = null, bd = bdMax;        // 下排：开头钩子
  hookAll().forEach((h) => {
    const x0 = mxOf(h.g0), x1 = Math.max(x0 + 2.5, mxOf(h.g1));
    const d = x < x0 ? x0 - x : (x > x1 ? x - x1 : 0);
    if (d < bd) { bd = d; best = h; }
  });
  return best ? { type: 'hook', item: best } : null;
}

function showMarkTip(m, x, y) {
  const box = $('#tipbox');
  if (!m) { box.classList.remove('on'); return; }
  const it = m.item;
  if (m.type === 'hook') {
    const n = S.batch[hkId(it)];
    box.innerHTML =
      '<div class="tt">第 ' + it.ep + ' 集 · ' + fmt(it.g0) + '–' + fmt(it.g1) +
        ' · ' + (it.dur || 0).toFixed(1) + 's<span class="th">' + esc(it.kind || '剧情节点') + '</span></div>' +
      '<div>' + esc(String(it.text || '').slice(0, 80)) + '</div>' +
      '<div class="tt" style="margin-top:2px;color:#ffb27a">点击设为开头钩子' +
        (n ? ' · 批量 ' + n + ' 条' : '') + '</div>';
  } else {
    box.innerHTML =
      '<div class="tt">第 ' + it.ep + ' 集 · ' + fmt(it.g1) +
        '<span class="th" style="color:#c9a4ff">结尾候选</span></div>' +
      '<div>' + esc(String(it.text || '').slice(-70)) + '</div>' +
      '<div class="tt" style="margin-top:2px;color:#c9a4ff">' +
        esc(it.note || '点击设为结尾（再点一次取消）') + '</div>';
  }
  box.classList.add('on');
  const bw = box.offsetWidth, bh = box.offsetHeight;
  box.style.left = Math.max(4, Math.min(MW - bw - 4, x + 12)) + 'px';
  box.style.top = Math.max(4, mk.offsetTop + y - bh - 10) + 'px';
}

mk.addEventListener('mousemove', (e) => {
  const r = mk.getBoundingClientRect();
  const x = e.clientX - r.left;
  mk.style.cursor = (S.playhead != null && Math.abs(x - mxOf(S.playhead)) <= 5)
    ? 'ew-resize' : '';
  showMarkTip(hitMark(x, e.clientY - r.top), x, e.clientY - r.top);
});
mk.addEventListener('mouseleave', () => $('#tipbox').classList.remove('on'));
// 标记带上抓住播放头拖动 = 原片进度跟着走（跨集自动切源）
mk.addEventListener('mousedown', (e) => {
  const x = e.clientX - mk.getBoundingClientRect().left;
  if (S.playhead != null && Math.abs(x - mxOf(S.playhead)) <= 5) {
    e.preventDefault();
    S.mkSeek = true;
    vidSeek(x / MW * S.total, false);
  }
});
document.addEventListener('mousemove', (e) => {
  if (!S.mkSeek) return;
  vidSeek(Math.max(0, Math.min(MW, e.clientX - mk.getBoundingClientRect().left)) / MW * S.total, false);
});
document.addEventListener('mouseup', () => {
  if (S.mkSeek) { S.mkSeek = false; S.mkClickSkip = true; setTimeout(() => S.mkClickSkip = false, 0); }
});

/* ── 时间轴 · 原片视频（右侧栏 + 播放头同步） ──────── */
const vid = $('#vid');
const Vid = { ep: 0, pend: null, follow: true };

function vidLabel(txt) { $('#vidLbl').textContent = txt; }

function vidSeek(g, autoplay) {
  if (!S.ready || !S.eps.length) return;
  g = Math.max(0, Math.min(S.total - 0.05, g));
  const ep = S.eps.find((x) => g >= x.offset && g < x.offset + x.dur) || S.eps[S.eps.length - 1];
  const local = Math.max(0, Math.min(ep.dur - 0.05, g - ep.offset));
  if (ep.idx !== Vid.ep || !vid.src) {
    Vid.ep = ep.idx;
    Vid.pend = local;
    vid.src = '/api/src?ep=' + ep.idx;
    $('#vidPh').hidden = true;
    vidLabel('原片 · 第 ' + ep.idx + ' 集');
  } else if (vid.readyState >= 1) {
    vid.currentTime = local;
  } else {
    Vid.pend = local;
  }
  if (autoplay) vid.play().catch(() => { /* 浏览器拦自动播放时用户手动点 */ });
  vidHead(g, false);   // 播放头立刻到位，不等 timeupdate
}

function vidHead(g, playing) {
  S.playhead = g;
  if (playing && Vid.follow) {
    // 播放头快出窗口了就把视图平移过去，保持它在大约中间
    const span = S.view.t1 - S.view.t0;
    if (g < S.view.t0 + span * 0.08 || g > S.view.t1 - span * 0.08) {
      setView(g - span / 2, g + span / 2);
    }
  }
  draw();
}

vid.addEventListener('loadedmetadata', () => {
  if (Vid.pend != null) { vid.currentTime = Vid.pend; Vid.pend = null; }
});
vid.addEventListener('timeupdate', () => {
  const e = S.eps.find((x) => x.idx === Vid.ep);
  if (e) vidHead(e.offset + vid.currentTime, !vid.paused);
});
vid.addEventListener('play', () => {
  if ($('#pvModal').classList.contains('on')) pvClose();   // 别两边同时出声
});
vid.addEventListener('ended', () => {
  // 一集放完自动接下一集，一路顺下去
  const next = S.eps.find((x) => x.idx === Vid.ep + 1);
  if (next) vidSeek(next.offset, true);
});
// 直接点占位区也能播：从当前播放头继续（没动过就从当前窗口开头播）
$('#vidPh').addEventListener('click', () => {
  vidSeek(S.playhead > 0.5 ? S.playhead : S.view.t0, true);
});
// 播放头跟随开关
$('#vidFollow').onclick = () => {
  Vid.follow = !Vid.follow;
  $('#vidFollow').classList.toggle('on', Vid.follow);
  toast(Vid.follow ? '跟随开：播放头快出窗口时，视图自动平移跟上'
                   : '跟随关：视图不动，播放头自己走出屏幕');
};
// 右键 = 从这里播放（主时间轴和标记带都支持）
cv.addEventListener('contextmenu', (e) => {
  e.preventDefault();
  vidSeek(tOf(e.clientX - cv.getBoundingClientRect().left), true);
});
mk.addEventListener('contextmenu', (e) => {
  e.preventDefault();
  vidSeek((e.clientX - mk.getBoundingClientRect().left) / MW * S.total, true);
});

mk.addEventListener('click', (e) => {
  if (!S.ready || S.mkSeek || S.mkClickSkip) return;   // 刚拖完播放头的松手不算点击
  const r = mk.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  const m = hitMark(x, y);
  if (m && m.type === 'hook') {
    selectHook(m.item);
  } else if (m && m.type === 'end') {
    selectEnd(m.item);
  } else {
    // 点空白：主时间轴中心跳过去（保持当前缩放）
    const t = x / MW * S.total;
    const span = S.view.t1 - S.view.t0;
    setView(t - span / 2, t + span / 2);
    draw();
  }
});
mk.addEventListener('dblclick', (e) => {
  const ep = S.eps.find((x) => {
    const t = (e.clientX - mk.getBoundingClientRect().left) / MW * S.total;
    return t >= x.offset && t < x.offset + x.dur;
  });
  if (ep) zoomToEp(ep.idx);
});
mk.addEventListener('wheel', (e) => {
  e.preventDefault();
  const r = mk.getBoundingClientRect();
  const x = e.clientX - r.left;
  const t = x / MW * S.total;
  const span = S.view.t1 - S.view.t0;
  const k = e.deltaY > 0 ? 1.22 : 0.82;
  const ns = Math.min(Math.max(4, span * k), Math.max(10, S.total * 1.03));
  const ratio = Math.max(0, Math.min(1, (t - S.view.t0) / Math.max(0.001, span)));
  setView(t - ns * ratio, t + ns * (1 - ratio));
  draw();
}, { passive: false });

function rrect(x, y, w, h, r) {
  r = Math.min(r, h / 2, Math.max(0, w / 2));
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function niceStep(span, px) {
  const want = span / Math.max(3, px / 92);
  const cands = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800];
  for (const c of cands) if (c >= want) return c;
  return 3600;
}

function hitBeat(x) {
  const t = tOf(x);
  let best = -1, bestD = Infinity;
  for (const b of S.beats) {
    if (t >= b.g0 - 0.01 && t <= b.g1 + 0.01) {
      const d = Math.abs((b.g0 + b.g1) / 2 - t);
      if (d < bestD) { bestD = d; best = b.id; }
    }
  }
  return best;
}

/* 交互 */
// 光标是否压在播放头上（±7px 内），是的话按下=拖播放头而不是平移视图
function onHeadCv(x) {
  return S.playhead != null && S.playhead >= S.view.t0 && S.playhead <= S.view.t1
      && Math.abs(x - xOf(S.playhead)) <= 7;
}
cv.addEventListener('mousedown', (e) => {
  const r = cv.getBoundingClientRect();
  const x = e.clientX - r.left;
  if (onHeadCv(x)) {
    S.drag = { mode: 'seek' };
    vidSeek(tOf(x), false);           // 按下瞬间先吸附到位
    return;
  }
  S.drag = { mode: 'pan', x, t0: S.view.t0, t1: S.view.t1 };
  S.moved = 0;
});
cv.addEventListener('mousemove', (e) => {
  const r = cv.getBoundingClientRect();
  const x = e.clientX - r.left;
  if (S.drag && S.drag.mode === 'seek') {
    vidSeek(tOf(x), false);           // 播放中继续播、暂停中只走进度，视频实时跟手
    return;
  }
  if (S.drag) {
    const dx = x - S.drag.x;
    S.moved = Math.max(S.moved, Math.abs(dx));
    if (S.moved > 4) {
      const span = S.drag.t1 - S.drag.t0;
      const shift = -dx / W * span;
      setView(S.drag.t0 + shift, S.drag.t1 + shift);
      draw();
    }
    return;
  }
  cv.style.cursor = onHeadCv(x) ? 'ew-resize' : '';
  const id = hitBeat(x);
  if (id !== S.hover) { S.hover = id; draw(); }
  showTip(id, x, e.clientY - r.top);
});
cv.addEventListener('mouseup', (e) => {
  const r = cv.getBoundingClientRect();
  const x = e.clientX - r.left;
  const mode = S.drag && S.drag.mode;
  const wasDrag = S.moved > 4;
  S.drag = null;
  if (mode === 'seek' || wasDrag || !S.ready) return;
  const id = hitBeat(x);
  if (id >= 0) {
    if (!S.win.has(id)) { toast('这句不在顺剪范围内，拖动目标时长或换开头才会把它带进来', true); return; }
    if (S.drop.has(id)) S.drop.delete(id); else S.drop.add(id);
    draw(); scheduleSave();
  }
});
cv.addEventListener('mouseleave', () => {
  S.hover = -1; S.drag = null; draw();
  $('#tipbox').classList.remove('on');
});
cv.addEventListener('wheel', (e) => {
  e.preventDefault();
  const r = cv.getBoundingClientRect();
  const x = e.clientX - r.left;
  const t = tOf(x);
  const span = S.view.t1 - S.view.t0;
  const k = e.deltaY > 0 ? 1.22 : 0.82;
  const ns = Math.min(Math.max(4, span * k), Math.max(10, S.total * 1.03));
  const ratio = x / W;
  setView(t - ns * ratio, t + ns * (1 - ratio));
  draw();
}, { passive: false });
cv.addEventListener('dblclick', (e) => {
  const r = cv.getBoundingClientRect();
  const t = tOf(e.clientX - r.left);
  const ep = S.eps.find((x) => t >= x.offset && t < x.offset + x.dur);
  if (ep) zoomToEp(ep.idx);
});

function setView(t0, t1) {
  const span = Math.max(4, t1 - t0);
  const total = Math.max(span, S.total || span);
  let a = t0, b = t1;
  if (a < 0) { b -= a; a = 0; }
  if (b > total) { a -= (b - total); b = total; }
  S.view = { t0: Math.max(0, a), t1: Math.min(total, b) };
  $('#zoomLabel').textContent = S.view.t1 - S.view.t0 < S.total * 0.92
    ? '窗口 ' + fmt(S.view.t0) + '–' + fmt(S.view.t1) : '全剧';
}

function zoomToEp(idx) {
  const e = S.eps.find((x) => x.idx === idx);
  if (!e) return;
  setView(e.offset, e.offset + e.dur);
  draw();
}

function showTip(id, x, y) {
  const box = $('#tipbox');
  if (id < 0) { box.classList.remove('on'); return; }
  const b = S.beats.find((z) => z.id === id);
  if (!b) { box.classList.remove('on'); return; }
  const hr = (S.plan && S.plan.hook) || S.hook;
  const er = (S.plan && S.plan.end) || S.endhook;
  const stHook = hr && b.g0 >= hr.g0 - 0.05 && b.g1 <= hr.g1 + 0.05;
  const stEnd = er && b.g0 >= er.g0 - 0.05 && b.g1 <= er.g1 + 0.05;
  const inWin = S.win.has(b.id);
  const st = stHook ? '开头钩子'
    : stEnd ? '结尾'
    : !inWin ? '不在成片内'
    : (S.drop.has(id) ? '已挖掉 · 点击恢复' : '顺剪内 · 点击挖掉');
  box.innerHTML =
    '<div class="tt">第 ' + b.ep + ' 集 · ' + fmt(b.start) + ' · ' + b.dur.toFixed(1) + 's' +
      '<span class="th">' + st + '</span></div>' +
    '<div>' + esc(b.text.slice(0, 90)) + '</div>' +
    (b.hits && b.hits.length ? '<div class="tt" style="margin-top:3px;color:#ffb27a">' + b.hits.join(' / ') + '</div>' : '');
  box.classList.add('on');
  const bw = box.offsetWidth, bh = box.offsetHeight;
  box.style.left = Math.max(4, Math.min(W - bw - 4, x + 12)) + 'px';
  box.style.top = Math.max(4, Math.min(H - bh - 4, y + 14)) + 'px';
}

$('#zoomIn').onclick = () => {
  const c = (S.view.t0 + S.view.t1) / 2, s = (S.view.t1 - S.view.t0) * 0.6;
  setView(c - s / 2, c + s / 2); draw();
};
$('#zoomOut').onclick = () => {
  const c = (S.view.t0 + S.view.t1) / 2, s = (S.view.t1 - S.view.t0) * 1.7;
  setView(c - s / 2, c + s / 2); draw();
};
$('#zoomFit').onclick = () => { setView(0, S.total || 1); draw(); };
$('#zoomReset').onclick = () => { if (S.ready) makePlan(); };

/* ── 包装预览 ─────────────────────────────────────── */
function packOpts() {
  return {
    size: $('#size').value, fit: $('#fit').value,
    title: $('#title').value || S.name,
    end_frame: S.endframe, end_frame_dur: parseFloat($('#efDur').value) || 2.5,
    overlay: S.overlay, overlay_opacity: parseFloat($('#ovOpacity').value) || 1,
    template_on_endframe: $('#efTemplate').checked,
  };
}

async function previewCard(kind) {
  try {
    const r = await api('/api/card/preview', Object.assign({ kind }, packOpts()));
    $('#prev').innerHTML = '<img src="' + r.url + '&t=' + Date.now() + '" alt="预览">';
    $('#prev').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  } catch (e) { toast(e.message, true); }
}
$('#prevEf').onclick = () => previewCard('endframe');
$('#prevOv').onclick = () => previewCard('overlay');

/* 上传 */
function bindUpload(boxSel, inputSel, key) {
  const box = $(boxSel), input = $(inputSel);
  box.onclick = () => input.click();
  input.onchange = async () => {
    const f = input.files[0];
    if (!f) return;
    const fd = new FormData(); fd.append('file', f);
    try {
      const r = await fetch('/api/upload', { method: 'POST', body: fd }).then((x) => x.json());
      if (r.detail) throw new Error(r.detail);
      S[key] = r.path;
      box.classList.add('has');
      if (key === 'endframe') {
        await applyEndframe(r.path);
      } else {
        box.style.backgroundImage = 'url(/api/file?p=' + encodeURIComponent(r.path) + ')';
      }
      toast('已上传：' + r.name);
      if (key === 'endframe') renderStats();
      syncTabDots();
    } catch (e) { toast(e.message, true); }
  };
}

/* 引导尾帧：图片按「秒数」静帧；mp4 视频按原时长原样播放（含自带声音） */
async function applyEndframe(path) {
  const box = $('#efBox'), dur = $('#efDur');
  const url = '/api/file?p=' + encodeURIComponent(path);
  try {
    const info = await api('/api/media/info?p=' + encodeURIComponent(path), undefined, 'GET');
    if (info.is_video) {
      dur.value = info.duration.toFixed(1);
      dur.disabled = true;
      S.efVideoDur = info.duration;
      dur.title = '视频尾帧按原时长 ' + info.duration.toFixed(1) + 's 播放（' +
        (info.has_audio ? '含原声' : '无声') + '），秒数设置无效';
      if (info.thumb) box.style.backgroundImage = 'url(' + info.thumb + ')';
      toast('视频尾帧：' + info.duration.toFixed(1) + ' 秒' +
        (info.has_audio ? ' · 含声音' : ' · 无声'));
    } else {
      S.efVideoDur = 0;
      dur.disabled = false;
      dur.title = '引导尾帧停留时长（秒）';
      box.style.backgroundImage = 'url(' + url + ')';
    }
  } catch (e) {
    box.style.backgroundImage = 'url(' + url + ')';
    toast(e.message, true);
  }
}
bindUpload('#logoBox', '#logoFile', 'logo');
bindUpload('#efBox', '#efFile', 'endframe');
bindUpload('#ovBox', '#ovFile', 'overlay');
$('#efDur').oninput = () => { if (!$('#efDur').disabled) renderStats(); };

/* ── 出片：单条 / 批量，同一个按钮自动切换 ────────── */
/* 候选卡片右下设过 −N+ 条数 → 批量；一个都没设 → 单条顺剪 */
$('#btnRender').onclick = async () => {
  const list = batchItems();
  const batch = list.length > 0;
  if (!batch && !S.plan) { toast('先生成顺剪方案', true); return; }
  $('#out').innerHTML = '';
  $('#btnShowOut').hidden = true;
  $('#btnRender').disabled = true;
  const base = Object.assign(packOpts(), {
    logo: S.logo, logo_pos: $('#logoPos').value,
    outdir: S.outdir, outdir_sub: !$('#bFlat').checked,
    name_tpl: String(S.nameTpl || '').trim(),
  });
  try {
    if (batch) {
      const counts = planCounts(list);
      const items = list.map((v, i) => ({
        hook: { g0: v.g0, g1: v.g1 },
        count: v.count || 1,
        ends: endsForItem(v.k, counts[i] || 0),
      }));
      const total = S.batchAlloc === 'total' ? Math.max(1, Math.min(200, Math.round(S.batchTotal || 1))) : 0;
      const expect = planCounts(list).reduce((a, b) => a + b, 0);
      await apiStart('/api/batch', Object.assign(base, {
        target: S.target,
        end_mode: S.endMode, tightness: S.tightness,
        hook_lead: S.lead,
        tail_mode: S.tailMode, tail_fixed: S.tailFixed,
        end_hook: S.endhook ? { g0: S.endhook.g0, g1: S.endhook.g1 } : null,
        items, total,
      }), '批量出片');
      watchJob((res) => {
        $('#btnRender').disabled = false;
        if (!res) return;
        const errBlock = (res.errors && res.errors.length)
          ? '<div class="empty warn" style="margin-top:8px"><div>有 ' + res.errors.length +
            ' 条没出片：</div><div class="tipline">' +
            res.errors.map((e) => '开头' + e.hook + ' 第' + e.copy + '条：' + esc(e.msg)).join('<br>') +
            '</div></div>'
          : '';
        $('#out').innerHTML = res.results.map((r) =>
          '<video src="/api/video?path=' + encodeURIComponent(r.video) + '" controls preload="metadata"></video>' +
          '<div class="oi"><span>' + (r.name ? esc(r.name) + ' · ' : '') +
            '开头' + r.hook + ' · 第' + r.copy + '条 · ' + fmt(r.duration) +
            (r.from ? ' · 第' + r.from.ep + '集→第' + r.to.ep + '集' : '') +
            (r.end_spec ? ' · <b class="endspec">指定结尾</b>' : '') + ' · ' + esc(r.end_reason || '') +
          '</span><span>' + r.size_mb + ' MB</span></div>').join('') + errBlock;
        if (res.dir) showOutDir(res.dir);
        $('#btnShowOut').hidden = false;
        $('#btnShowOut').textContent = '查看本批成片（' + res.results.length + ' 条）';
        toast('批量完成：' + res.count + ' / ' + expect + ' 条成片，已存到本地文件夹');
      });
    } else {
      await apiStart('/api/render', base, '出片');
      watchJob((res) => {
        $('#btnRender').disabled = false;
        if (!res) return;
        $('#out').innerHTML =
          '<video src="/api/video?path=' + encodeURIComponent(res.video) + '" controls></video>' +
          '<div class="oi"><span>' + (res.name ? esc(res.name) + ' · ' : '') +
            fmt(res.duration) + ' · ' + res.width + '×' + res.height + '</span>' +
          '<span>' + res.size_mb + ' MB</span></div>';
        if (res.dir) showOutDir(res.dir);
        $('#btnShowOut').hidden = false;
        $('#btnShowOut').textContent = '查看本批成片';
        toast('成片完成：' + fmt(res.duration));
      });
    }
  } catch (e) {
    $('#btnRender').disabled = false;
    toast(e.message, true);
  }
};

/* 成片落地目录：显示出来 + 一键打开资源管理器 */
function showOutDir(dir) {
  S.lastDir = dir;
  S.outShown = dir;
  $('#btnOpenDir').hidden = false;
  const box = document.createElement('div');
  box.className = 'bout';
  box.innerHTML = '<span title="' + esc(dir) + '">成片已保存：' + esc(dir) + '</span>' +
    '<button class="btn xs ghost">打开文件夹</button>';
  box.querySelector('button').onclick = () =>
    api('/api/reveal', { path: dir }).catch((e) => toast(e.message, true));
  $('#out').appendChild(box);
}

/* ── 成片预览弹窗（不再挂在出片卡里，避免把出片数量/保存位置挤扁） ── */
function outModalOpen() {
  $('#outModal').classList.add('on');
}
function outModalClose() {
  $('#outModal').classList.remove('on');
  $$('#out video').forEach((v) => { try { v.pause(); } catch (e) { /* 忽略 */ } });
}
$('#btnShowOut').onclick = outModalOpen;
$('#outClose').onclick = outModalClose;
$('#outModal').onclick = (e) => { if (e.target === $('#outModal')) outModalClose(); };
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && $('#outModal').classList.contains('on')) outModalClose();
});
$('#outOpenDir').onclick = () => {
  const d = S.outShown || S.outdir || S.lastDir;
  if (!d) { toast('还没出过片，没有可打开的目录', true); return; }
  api('/api/reveal', { path: d }).catch((e) => toast(e.message, true));
};

/* ── 渲染（单条模式已并入上面的 #btnRender） ─────── */

/* ── 其它 ─────────────────────────────────────────── */
$('#btnScan').onclick = doScan;
$('#btnImportTop').onclick = () => openDirPicker();
$('#btnPick').onclick = () => openDirPicker();

/* ── 内置文件夹浏览器 ─────────────────────────────── */
const Dir = { cur: '', parent: null, mode: 'import' };

function dirOpen(mode) {
  Dir.mode = mode === 'out' ? 'out' : 'import';
  const out = Dir.mode === 'out';
  $('#dirTitle').textContent = out ? '选择成片保存到哪个文件夹' : '选择剧集文件夹';
  $('#dirOk').textContent = out ? '存到这里' : '选这个';
  $('#dirModal').classList.add('on');
  // 记住的路径只是"上次停留处"：读不到就退回盘符列表，别让弹窗一开就卡死
  let last = '';
  try {
    last = out ? (S.outdir || localStorage.getItem('dc.outBrowse') || '')
               : (Dir.cur || localStorage.getItem('dc.lastDir') || '');
  } catch (e) {
    last = out ? (S.outdir || '') : (Dir.cur || '');
  }
  dirGo(last || '', { fromLast: !!last });
}

/* 统一入口：任何一步抛错也要把弹窗打开并给出原因，不能"点了没反应" */
function openDirPicker(mode) {
  try {
    dirOpen(mode);
  } catch (e) {
    $('#dirModal').classList.add('on');
    try { dirGo('', {}); } catch (x) { /* 忽略 */ }
    toast('文件夹选择器打开失败：' + ((e && e.message) || e), true);
  }
}

function dirRenderError(msg, canRoot) {
  const list = $('#dirList');
  list.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'empty warn';
  box.textContent = msg;
  if (canRoot) {
    box.appendChild(document.createElement('br'));
    const btn = document.createElement('button');
    btn.className = 'btn sm';
    btn.textContent = '回到此电脑，重新选盘符';
    btn.onclick = () => dirGo('');
    box.appendChild(btn);
  }
  list.appendChild(box);
}

function dirGo(path, opts) {
  const list = $('#dirList');
  const fromLast = !!(opts && opts.fromLast) && !!path;
  list.innerHTML = '<div class="empty">加载中…</div>';
  api('/api/folder/list?path=' + encodeURIComponent(path || ''), undefined, 'GET').then((r) => {
    Dir.cur = r.path || '';
    Dir.parent = r.parent;
    $('#dirPath').value = r.path || '此电脑 · 请选择盘符';
    const dirs = r.dirs || [];
    const files = r.files || [];
    const nv = r.videos || 0;
    if (Dir.mode === 'out' && r.path) {
      try { localStorage.setItem('dc.outBrowse', r.path); } catch (e) { /* 忽略 */ }
    }
    let hint = Dir.mode === 'out'
      ? '点「存到这里」，成片会在里面建「剧名_时间」子文件夹'
      : (nv ? '这个文件夹里有 ' + nv + ' 个视频' + (r.subs ? '、' + r.subs + ' 个字幕文件' : '') + ' · 点「选这个」导入'
            : '进入文件夹后点「选这个」');
    if (r.notice) hint = r.notice + ' · ' + hint;
    $('#dirHint').textContent = hint;

    if (!dirs.length && !files.length) {
      list.innerHTML = '';
      const box = document.createElement('div');
      box.className = 'empty';
      box.textContent = r.empty
        ? '这个文件夹是空的：没有子文件夹，也没有视频 / 字幕文件。换一个文件夹，或把分集视频放进来。'
        : '这个文件夹里没有可导入的内容';
      list.appendChild(box);
      return;
    }
    list.innerHTML = '';
    dirs.forEach((d) => {
      const el = document.createElement('div');
      el.className = 'ditem';
      const ico = document.createElement('span'); ico.className = 'ico'; ico.textContent = '📁';
      const nm = document.createElement('span'); nm.className = 'nm'; nm.textContent = d.name; nm.title = d.name;
      el.append(ico, nm);
      el.onclick = () => dirGo(d.path);
      el.ondblclick = () => dirChoose(d.path);
      list.appendChild(el);
    });
    files.forEach((f) => {
      const el = document.createElement('div');
      el.className = 'ditem';
      el.style.opacity = '.8';
      const ico = document.createElement('span');
      ico.className = 'ico';
      ico.textContent = f.kind === 'video' ? '🎬' : '📄';
      const nm = document.createElement('span');
      nm.className = 'nm';
      const mb = f.size ? (f.size / 1024 / 1024).toFixed(1) + ' MB' : '';
      nm.textContent = f.name + (mb ? '  ·  ' + mb : '');
      nm.title = f.name;
      const tag = document.createElement('span');
      tag.className = 'ico';
      tag.style.cssText = 'width:auto;font-size:11px;opacity:.7';
      tag.textContent = f.kind === 'video' ? '视频' : '字幕';
      el.append(ico, nm, tag);
      el.onclick = () => toast('这是文件。点右下角「选这个」导入它所在的文件夹');
      list.appendChild(el);
    });
  }).catch((e) => {
    const msg = (e && e.message) ? e.message : '读取失败（原因未知）';
    Dir.cur = '';
    Dir.parent = null;
    if (fromLast) {
      // 上次记住的路径失效（换电脑 / 移动硬盘拔了）：清掉并自动回到盘符，弹窗不卡死
      try { localStorage.removeItem(Dir.mode === 'out' ? 'dc.outBrowse' : 'dc.lastDir'); } catch (x) { /* 忽略 */ }
      toast('上次记住的路径已失效：' + msg + '，已回到此电脑', true);
      dirGo('');
      return;
    }
    dirRenderError('无法读取这个文件夹：' + msg, !!path);
  });
}

function dirChoose(path) {
  path = (path || Dir.cur || '').trim();
  if (!path) { toast('请先进入一个文件夹', true); return; }
  if (Dir.mode === 'out') {
    S.lastDir = path;
    setOutDir(path);
    $('#dirModal').classList.remove('on');
    toast('成片会存到：' + path + '（自动建「剧名_时间」子文件夹）');
    return;
  }
  $('#folder').value = path;
  try { localStorage.setItem('dc.lastDir', path); } catch (e) { /* 隐私模式忽略 */ }
  const cur = $('#projName').value.trim();
  // 剧名还是上一部剧的名字（没手动改过）→ 换成这次选的文件夹名，避免导入新剧把旧项目覆盖
  if (!cur || cur === S.name) {
    const base = (path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '').trim();
    if (base) $('#projName').value = base.replace(/[（(].*$/, '').trim() || base;
  }
  $('#dirModal').classList.remove('on');
  toast('已选择：' + path);
}

function dirClose() { $('#dirModal').classList.remove('on'); }

$('#dirClose').onclick = dirClose;
$('#dirModal').onclick = (e) => { if (e.target === $('#dirModal')) dirClose(); };
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && $('#dirModal').classList.contains('on')) dirClose();
});
$('#dirUp').onclick = () => { if (Dir.parent !== null && Dir.parent !== undefined) dirGo(Dir.parent); };
$('#dirOk').onclick = () => dirChoose();
$('#dirPath').onkeydown = (e) => {
  if (e.key === 'Enter') { e.preventDefault(); dirGo($('#dirPath').value.trim()); }
};

/* ── 导出数量 / 保存位置 ─────────────────────────── */
$$('#bAlloc button').forEach((b) => {
  b.onclick = () => {
    S.batchAlloc = b.dataset.a;
    $$('#bAlloc button').forEach((x) => x.classList.toggle('on', x === b));
    renderBatchBar();
  };
});
$('#bTotal').oninput = (e) => {
  S.batchTotal = Math.max(1, Math.min(200, Number(e.target.value) || 0));
  renderBatchBar();
};
$('#bTotal').onchange = (e) => {
  // 填了 0、负数、一大串数字，回填成实际值，别让框里和计划对不上
  const t = Math.max(1, Math.min(200, Number(e.target.value) || 1));
  S.batchTotal = t;
  e.target.value = String(t);
  try { localStorage.setItem('dc.batchTotal', String(t)); } catch (x) { /* 忽略 */ }
  renderBatchBar();
};
$('#btnPickDir').onclick = () => openDirPicker('out');
/* 右栏矮的时候出片卡内部会滚动：展开「保存位置」就把它滚进视野，
   否则按钮可能停在卡片可视区下方，看起来像"点了没反应" */
(() => {
  const ex = document.querySelector('.expopts');
  if (!ex) return;
  ex.addEventListener('toggle', () => {
    if (!ex.open) return;
    setTimeout(() => {
      try { ex.scrollIntoView({ block: 'nearest' }); } catch (e) { /* 老浏览器忽略 */ }
    }, 30);
  });
})();
/* 保存目录既能在弹窗里选，也能直接粘贴；清空就回到项目 output */
function setOutDir(v) {
  S.outdir = String(v || '').trim().replace(/^"|"$/g, '');
  $('#bDir').value = S.outdir;
  try {
    if (S.outdir) localStorage.setItem('dc.outDir', S.outdir);
    else localStorage.removeItem('dc.outDir');
  } catch (e) { /* 隐私模式忽略 */ }
  $('#btnOpenDir').hidden = !(S.outdir || S.lastDir);
}
$('#bDir').onchange = (e) => setOutDir(e.target.value);
$('#bDir').onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); } };
$('#btnOpenDir').onclick = () => {
  const d = S.outdir || S.lastDir;
  if (!d) { toast('还没选保存文件夹', true); return; }
  api('/api/reveal', { path: d }).catch((e) => toast(e.message, true));
};

/* ── 文件名格式：末尾数字是起始序号，前面原样保留 ──────────
   预览走后端同一个解析函数，避免界面写一套、导出写另一套。 */
let nameTplTimer = null;
let nameTplSeq = 0;          // 预览请求序号：慢响应回来时丢掉，避免覆盖刚清空的提示
const NAME_TIP_EMPTY = '留空 = 按剧名命名。填了模板：末尾数字当起始序号，本批逐条 +1，遇到同名文件自动顺延。';

function nameTplTip(html, bad) {
  const el = $('#bNameTip');
  el.className = 'nametip' + (bad ? ' bad' : '');
  el.innerHTML = html;
}
async function nameTplPreview() {
  const seq = ++nameTplSeq;
  const tpl = String(S.nameTpl || '').trim();
  if (!tpl) {
    nameTplTip(NAME_TIP_EMPTY);
    return;
  }
  try {
    const r = await api('/api/name/preview', { name_tpl: tpl, count: 3 });
    if (seq !== nameTplSeq) return;          // 已有更新的输入/清空，丢弃这次结果
    if (!r.ok) { nameTplTip(esc(r.msg || '文件名格式不对'), true); return; }
    const first = (r.names || [])[0] || '';
    nameTplTip('本批首个文件：<span class="nt-name">' + esc(first) + '</span>' +
      '<br>序号起点 <b>' + r.start + '</b>，逐条 +1；同名自动顺延，不覆盖旧成片。' +
      ($('#bFlat').checked ? '' : '要跨批接着编号，勾选下方「直接放进所选文件夹」。'));
  } catch (e) {
    if (seq !== nameTplSeq) return;
    nameTplTip(esc(e.message), true);
  }
}
function setNameTpl(v, save) {
  S.nameTpl = String(v || '').trim();
  if ($('#bNameTpl').value !== S.nameTpl) $('#bNameTpl').value = S.nameTpl;
  $('#btnClearName').hidden = !S.nameTpl;
  if (save !== false) {
    try {
      if (S.nameTpl) localStorage.setItem('dc.nameTpl', S.nameTpl);
      else localStorage.removeItem('dc.nameTpl');
    } catch (x) { /* 隐私模式忽略 */ }
  }
  nameTplPreview();
}

/* 清空文件命名格式：不读输入框、不依赖 state 判断，点一次就一定清空。
   顺带取消未执行的防抖定时器、递增预览序号，避免旧的 blur/预览请求把内容或提示写回来。 */
function clearNameTpl() {
  clearTimeout(nameTplTimer);
  nameTplTimer = null;
  nameTplSeq++;
  S.nameTpl = '';
  const inp = $('#bNameTpl');
  if (inp) inp.value = '';
  const btn = $('#btnClearName');
  if (btn) btn.hidden = true;
  try { localStorage.removeItem('dc.nameTpl'); } catch (x) { /* 隐私模式忽略 */ }
  nameTplTip(NAME_TIP_EMPTY);
  try { if (inp) inp.focus(); } catch (x) { /* 老浏览器忽略 */ }
}
window.__nameTplBound = true;   // 给 index.html 的旧脚本兜底用：新 JS 已接管这个按钮

$('#bNameTpl').oninput = (e) => {
  S.nameTpl = e.target.value;
  $('#btnClearName').hidden = !S.nameTpl.trim();
  clearTimeout(nameTplTimer);
  nameTplTimer = setTimeout(() => setNameTpl(e.target.value), 350);
};
$('#bNameTpl').onblur = (e) => {
  // 值没变就不重复提交：清空后失焦不会再把空模板送回预览
  if (String(e.target.value || '').trim() === S.nameTpl) return;
  setNameTpl(e.target.value);
};
$('#bNameTpl').onkeydown = (e) => {
  if (e.key === 'Escape') { e.preventDefault(); clearNameTpl(); toast('已清空命名格式'); return; }
  if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); }
};
// 按下就阻止默认：按钮不抢输入框焦点，blur 不会先于 click 触发
$('#btnClearName').onmousedown = (e) => e.preventDefault();
$('#btnClearName').onclick = (e) => {
  e.preventDefault();
  e.stopPropagation();
  clearNameTpl();
  toast('已清空命名格式，回到按剧名命名');
};
try {
  S.outdir = localStorage.getItem('dc.outDir') || '';
  S.nameTpl = localStorage.getItem('dc.nameTpl') || '';
  const t = Number(localStorage.getItem('dc.batchTotal'));
  if (t >= 1 && t <= 200) S.batchTotal = t;
  $('#bFlat').checked = localStorage.getItem('dc.outFlat') === '1';
  const ldRaw = localStorage.getItem('dc.hookLead');
  const ld = ldRaw == null ? NaN : Number(ldRaw);
  if (isFinite(ld) && ld >= 0 && ld <= 5) S.lead = ld;
  const tm = localStorage.getItem('dc.tailMode');
  if (tm === 'auto' || tm === 'fixed' || tm === 'off') S.tailMode = tm;
  const tfRaw = localStorage.getItem('dc.tailFixed');
  const tf = tfRaw == null ? NaN : Number(tfRaw);
  if (isFinite(tf) && tf >= 0.3 && tf <= 6) S.tailFixed = tf;
} catch (e) { /* 隐私模式忽略 */ }
$('#hookLead').value = String(S.lead);
$('#tailMode').value = S.tailMode;
$('#tailFixed').value = String(S.tailFixed);
$('#tailFixRow').hidden = S.tailMode !== 'fixed';
function tailHint() {
  if (S.tailMode === 'off') return '去尾帧：不裁，集与集的接缝原样保留';
  if (S.tailMode === 'fixed') return '去尾帧：每个集末统一裁掉 ' + S.tailFixed.toFixed(1) + ' 秒';
  return '去尾帧：自动识别每集末尾的静止定帧，识别不到就不裁（不会硬切正片）';
}
$('#tailMode').onchange = (e) => {
  S.tailMode = e.target.value;
  $('#tailFixRow').hidden = S.tailMode !== 'fixed';
  try { localStorage.setItem('dc.tailMode', S.tailMode); } catch (x) { /* 忽略 */ }
  toast(tailHint());
  makePlan();
};
$('#tailFixed').onchange = (e) => {
  S.tailFixed = Math.max(0.3, Math.min(6, parseFloat(e.target.value) || 2));
  e.target.value = String(S.tailFixed);
  try { localStorage.setItem('dc.tailFixed', String(S.tailFixed)); } catch (x) { /* 忽略 */ }
  toast(tailHint());
  makePlan();
};
$('#bFlat').onchange = (e) => {
  try {
    if (e.target.checked) localStorage.setItem('dc.outFlat', '1');
    else localStorage.removeItem('dc.outFlat');
  } catch (x) { /* 忽略 */ }
  toast(e.target.checked ? '成片直接放进所选文件夹' : '成片会建「剧名_时间」子文件夹存放');
  nameTplPreview();
};
$('#bTotal').value = String(S.batchTotal);
$('#bDir').value = S.outdir;
$('#bNameTpl').value = S.nameTpl;
$('#btnClearName').hidden = !S.nameTpl;
nameTplPreview();
$('#btnOpenDir').hidden = !S.outdir;
$('#target').oninput = (e) => {
  S.target = Number(e.target.value);
  $('#tgVal').textContent = (S.target / 60).toFixed(1) + ' 分钟';
};
$('#target').onchange = () => makePlan();
$('#endMode').onchange = (e) => {
  S.endMode = e.target.value;
  if (S.endMode === 'point' && !S.endhook) {
    toast('先在下面「结尾钩子候选」点一个结尾，再切到这个模式', true);
  }
  renderEnds();
  makePlan();
};
$('#tightness').onchange = (e) => {
  S.tightness = parseFloat(e.target.value) || 0;
  makePlan();
};
$('#hookLead').onchange = (e) => {
  S.lead = Math.max(0, Math.min(5, parseFloat(e.target.value) || 0));
  try { localStorage.setItem('dc.hookLead', String(S.lead)); } catch (x) { /* 忽略 */ }
  toast(S.lead > 0
    ? '开头入戏：成片从钩子起点往前 ' + S.lead.toFixed(1) + ' 秒开始切（预览跟着变）'
    : '开头入戏：正好从钩子起点切');
  renderHookBox();
  makePlan();
};
$('#btnClearHook').onclick = () => {
  const s = clearAllPicks();
  if (!s) { toast('当前没有已选内容'); return; }
  const parts = [];
  if (s.hook) parts.push('开头钩子 1 个');
  if (s.cards) parts.push('卡片 ' + s.cards + ' 张（' + s.copies + ' 条）');
  if (s.ends) parts.push('指定结尾 ' + s.ends + ' 条');
  if (s.endhook) parts.push('结尾钩子 1 个');
  toast('已清空 ' + s.total + ' 项：' + parts.join(' · '));
};

/* 右栏参数区页签：顺剪设置 / 开头钩子 / 包装（同一张卡内切换，避免挤成一条缝） */
const RTAB_HINT = { plan: '', hook: '选一个当开头', pack: '角标 / 尾帧 / 模板',
  auto: '主题 / 时长 / 节奏', hl: '整剧高光 / 时长 / 结尾' };
function setRTab(t, save) {
  if (!Object.prototype.hasOwnProperty.call(RTAB_HINT, t)) t = 'plan';
  $$('#rTabs .rtab').forEach((b) => b.classList.toggle('on', b.dataset.t === t));
  $$('.tabpane').forEach((p) => {
    p.hidden = p.id !== 'pane' + t.charAt(0).toUpperCase() + t.slice(1);
  });
  const h = $('#rTabHint');
  if (h) h.textContent = RTAB_HINT[t] || '';
  if (save !== false) { try { localStorage.setItem('dc.rTab', t); } catch (e) { /* 隐私模式忽略 */ } }
  syncTabDots();
}
/* 页签圆点：该页里有已配置内容（钩子／包装素材）时亮起，避免藏起来被忘掉 */
function syncTabDots() {
  const hookTab = $('#rTabs .rtab[data-t="hook"]');
  const packTab = $('#rTabs .rtab[data-t="pack"]');
  if (hookTab) {
    hookTab.classList.toggle('dot', !!S.hook);
    hookTab.title = S.hook ? '已设开头钩子：第 ' + S.hook.ep + ' 集' : '从候选里挑一个当开头';
  }
  if (packTab) {
    const n = [S.logo, S.endframe, S.overlay].filter(Boolean).length;
    packTab.classList.toggle('dot', n > 0);
    packTab.title = n ? '已上传 ' + n + ' 个素材（logo / 尾帧 / 模板）' : '角标、引导尾帧、包装模板';
  }
  const autoTab = $('#rTabs .rtab[data-t="auto"]');
  if (autoTab) autoTab.classList.toggle('dot', !!S.auto.plan);
  const hlTab = $('#rTabs .rtab[data-t="hl"]');
  if (hlTab) hlTab.classList.toggle('dot', !!(S.hl && S.hl.plan));
}
$$('#rTabs .rtab').forEach((b) => { b.onclick = () => setRTab(b.dataset.t); });
syncTabDots();

/* ── 高光剪辑：整剧扫描 → 按目标时长选条数 → 每段去尾帧 ── */
function hlBody() {
  const body = {
    target: Number($('#hlTarget').value) || 600,
    end_mode: $('#hlEndMode').value,
    end_key: $('#hlEndKey').value || '',
    tail_mode: $('#hlTail').value,
    tail_fixed: 0.5,
    max_size_mb: 400,
    relax: $('#hlRelax').checked,
  };
  if (body.end_mode === 'custom') {
    body.end_ep = Number($('#hlEndEp').value) || 0;
    body.end_start = Number($('#hlEndStart').value) || 0;
    body.end_end = Number($('#hlEndEnd').value) || 0;
  }
  return body;
}

function renderHl(r) {
  const fb = $('#hlFb'), mt = $('#hlMetrics'), list = $('#hlList');
  const f = (r && r.feedback) || {};
  if (r && f.title) {
    fb.hidden = false;
    fb.className = 'acfb ' + (f.level || '');
    fb.innerHTML = '<b>' + esc(f.title) + '</b><div>' + esc(f.msg || '') + '</div>' +
      ((f.hints || []).length
        ? '<ul>' + f.hints.map((h) => '<li>' + esc(h) + '</li>').join('') + '</ul>' : '');
  } else {
    fb.hidden = true;
  }
  const p = (r && r.plan) || null;
  if (!p) {
    mt.hidden = true; list.innerHTML = '';
    $('#hlRender').hidden = true; $('#hlRelaxRow').hidden = true;
    return;
  }
  const bit = p.bitrate || {};
  mt.hidden = false;
  mt.innerHTML = [
    ['片段', p.clip_count + ' 段'],
    ['时长', fmt(p.estimated) + ' / ' + fmt(p.target)],
    ['填充', Math.round((p.fill || 0) * 100) + '%'],
    ['尾帧', Number(p.tail_total || 0).toFixed(1) + ' 秒'],
    ['码率', (bit.video_kbps || 0) + ' kbps'],
    ['预计', (bit.estimated_mb || 0) + ' MB'],
  ].map(([k, v]) => '<i>' + esc(k) + ' <b>' + esc(v) + '</b></i>').join('');
  list.innerHTML = (p.segments || []).map((s) => {
    const tail = s.tail_trim ? ' · 尾帧 -' + Number(s.tail_trim).toFixed(1) + 's' : '';
    return '<div class="hlitem"><b>第' + s.ep + '集</b><span>' +
      fmt(s.start) + '–' + fmt(s.end) + ' · ' + Number(s.dur).toFixed(1) + 's' + tail +
      (s.end_clip ? ' · 结尾' : '') + '</span>' +
      '<span class="sp">' + esc(String(s.text || '').slice(0, 34)) + '</span></div>';
  }).join('');
  const sel = $('#hlEndKey');
  const opts = r.end_options || p.end_options || [];
  sel.innerHTML = '<option value="">（自动选最后一段）</option>' + opts.map((o) =>
    '<option value="' + esc(o.key) + '">第' + o.ep + '集 ' + fmt(o.start) + ' · ' +
    Number(o.dur).toFixed(1) + 's</option>').join('');
  if (S.hl.endKey) sel.value = S.hl.endKey;
  $('#hlRender').hidden = !(p.segments && p.segments.length);
  $('#hlRelaxRow').hidden = !!p.complete;
}

$('#hlTarget').oninput = () => {
  const sec = Number($('#hlTarget').value) || 600;
  $('#hlTgVal').textContent = (sec / 60).toFixed(1) + ' 分钟';
  S.hl.target = sec;
};
$$('.hltargets [data-hlmin]').forEach((b) => {
  b.onclick = () => {
    $('#hlTarget').value = b.dataset.hlmin;
    $('#hlTarget').dispatchEvent(new Event('input'));
  };
});
$('#hlEndMode').onchange = () => {
  const m = $('#hlEndMode').value;
  $('#hlEndKey').hidden = m !== 'point';
  $('#hlCustom').hidden = m !== 'custom';
  S.hl.endMode = m;
};
$('#hlEndKey').onchange = () => { S.hl.endKey = $('#hlEndKey').value; };
$('#hlTail').onchange = () => { S.hl.tail = $('#hlTail').value; };

$('#hlPlan').onclick = async () => {
  if (!S.ready) { toast('先导入剧集', true); return; }
  const btn = $('#hlPlan');
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '扫描整剧高光…';
  try {
    const r = await api('/api/highlight/plan', hlBody());
    S.hl.plan = r.plan || null;
    renderHl(r);
    if (r.feedback) toast(r.feedback.title || '高光方案已生成', r.feedback.level === 'error');
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
};

$('#hlRender').onclick = async () => {
  if (!S.hl.plan) { toast('先生成高光方案', true); return; }
  const btn = $('#hlRender');
  btn.disabled = true;
  const base = Object.assign(packOpts(), {
    title: (($('#title').value || S.name || '高光') + '_高光').trim(),
    logo: S.logo, logo_pos: $('#logoPos').value,
    outdir: S.outdir, outdir_sub: !$('#bFlat').checked,
    name_tpl: String(S.nameTpl || '').trim(),
    relax: $('#hlRelax').checked,
  });
  try {
    await apiStart('/api/highlight/render', base, '高光出片');
    watchJob((res) => {
      btn.disabled = false;
      if (!res) return;
      $('#out').innerHTML =
        '<video src="/api/video?path=' + encodeURIComponent(res.video) + '" controls></video>' +
        '<div class="oi"><span>' + (res.name ? esc(res.name) + ' · ' : '') +
          fmt(res.duration) + ' · ' + res.width + '×' + res.height +
          ' · ' + (res.segments || 0) + ' 段</span><span>' + res.size_mb + ' MB' +
          (res.limit_mb ? ' / ' + res.limit_mb + ' MB' : '') + '</span></div>';
      if (res.dir) showOutDir(res.dir);
      $('#btnShowOut').hidden = false;
      $('#btnShowOut').textContent = '查看成片';
      toast('高光成片完成：' + fmt(res.duration) + ' · ' + res.size_mb + ' MB');
    });
  } catch (e) {
    btn.disabled = false;
    toast(e.message, true);
  }
};
try { setRTab(localStorage.getItem('dc.rTab') || 'plan', false); } catch (e) { setRTab('plan', false); }

$('#btnHookPrefs').onclick = openHookPrefs;
$('#hookPrefsClose').onclick = () => $('#hookPrefsModal').classList.remove('on');
$('#hookPrefsDone').onclick = () => $('#hookPrefsModal').classList.remove('on');
$('#hookPrefsReset').onclick = () => {
  S.hkPrefs = { enabled: {}, weight: {} };
  saveHookPrefs(); renderHookPrefs(); renderHooks();
  toast('已恢复默认类型设置');
};
$('#hookInfoClose').onclick = () => $('#hookInfoModal').classList.remove('on');
$('#hookPrefsModal').addEventListener('click', (e) => {
  if (e.target.id === 'hookPrefsModal') $('#hookPrefsModal').classList.remove('on');
});
$('#hookInfoModal').addEventListener('click', (e) => {
  if (e.target.id === 'hookInfoModal') $('#hookInfoModal').classList.remove('on');
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  $('#hookInfoModal').classList.remove('on');
  $('#hookPrefsModal').classList.remove('on');
});
$('#btnResetHooks').onclick = () => resetHooks();
$('#btnPickAllHooks').onclick = () => selectAllHooks();
$('#btnClearHooks').onclick = () => {
  const s = clearAllPicks();
  if (s) toast('已取消全部选择：开头 ' + s.hook + ' · 卡片 ' + s.cards +
               ' 张（' + s.copies + ' 条）· 指定结尾 ' + s.ends + ' 条');
};

/* 把一批候选应用到界面：换一批、以及"从头再看"都走这里 */
function applyHookBatch(hs) {
  const keep = new Set(hs.map(hookKey));
  S.highlights = hs;
  // 旧候选上的删除 / 微调 / 条数：只保留新池里还在的那几条，其余清掉
  S.hkHide = new Set(Array.from(S.hkHide).filter((k) => keep.has(k)));
  const sh = {};
  for (const k in S.hkShift) if (keep.has(k)) sh[k] = S.hkShift[k];
  S.hkShift = sh;
  const bt = {};
  for (const k in S.batch) if (keep.has(k)) bt[k] = S.batch[k];
  S.batch = bt;
  // 当前选中的开头如果新一批里还有，就继续选着；没有才放开
  if (S.hook && S.hook._k && !keep.has(S.hook._k)) S.hook = null;
  S.batchEnd = {}; S.bendCands = {}; S.plan = null;
  saveHkEdits(); renderHooks(); renderBatchBar(); renderStats(); draw();
  return hs.filter((h) => !S.hkHide.has(hookKey(h))).length;
}

$('#btnShuffleHooks').onclick = async () => {
  const btn = $('#btnShuffleHooks');
  if (btn.dataset.busy) return;
  btn.dataset.busy = '1';
  const label = btn.textContent;
  btn.textContent = '正在换…';
  try {
    let r = await api('/api/hooks/reseed', {});
    // 池子空了：明确告诉用户，并允许从头再看一遍（不改变当前这一批）
    if (r && r.exhausted) {
      const msg = r.msg || '已经把所有高光候选都看过了';
      if (!r.can_reset) { toast(msg); return; }
      if (!confirm(msg + '\n\n要从头再看一遍吗？点「确定」重新开始（当前这批的条数会清掉，开头能在新一批里对上就继续保持选中）。')) {
        toast(msg);
        return;
      }
      r = await api('/api/hooks/reseed', { reset: true });
    }
    const hs = (r && r.highlights) || [];
    if (!hs.length) throw new Error('没有返回候选');
    const left = applyHookBatch(hs);
    const more = Number(r.remaining);
    toast('换了一批：' + left + ' 个开头候选' +
          (isFinite(more) && more > 0 ? '（候选池还剩 ' + more + ' 条没看过）' : '') +
          '，不满意可以再点一次');
  } catch (e) {
    toast('换一批失败：' + ((e && e.message) || e), true);
  } finally {
    btn.textContent = label;
    delete btn.dataset.busy;
  }
};
$('#btnAddHook').onclick = () => {
  const box = $('#addForm');
  box.hidden = !box.hidden;
  if (box.hidden) return;
  // 有时间轴悬停的位置就直接填好，省得手输
  const b = S.beats.find((x) => x.id === S.hover);
  if (b) {
    $('#addEp').value = String(b.ep);
    $('#addT0').value = (b.g0 - epOffset(b.ep)).toFixed(1);
    $('#addT1').value = (b.g1 - epOffset(b.ep)).toFixed(1);
  }
  $('#addT0')['focus']();
};
$('#addOk').onclick = () => addManualHook();
['#addT0', '#addT1'].forEach((sel) => {
  $(sel).onkeydown = (e) => { if (e.key === 'Enter') addManualHook(); };
});
$('#btnClearEnd').onclick = () => {
  S.endhook = null; S.endMode = 'auto'; $('#endMode').value = 'auto';
  renderEnds(); makePlan();
};
$('#btnHelp').onclick = () => $('#helpModal').classList.add('on');
$('#helpClose').onclick = () => $('#helpModal').classList.remove('on');
$('#helpModal').onclick = (e) => { if (e.target.id === 'helpModal') $('#helpModal').classList.remove('on'); };

/* 页面自检：自己 script 标签上的 ?v= 和服务器当前版本不一致 → 提示刷新。
   防止「浏览器拿旧 JS，新按钮点了没反应」这类问题反复出现。 */
function verSelfCheck(serverVer) {
  const tag = document.querySelector('script[src*="app.js"]');
  const m = tag && tag.src ? tag.src.match(/[?&]v=(\d+)/) : null;
  const mine = m ? m[1] : '';
  if (!serverVer) return;
  // mine 为空说明脚本地址没带版本号（多半是老进程/老页面）——这种情况更容易
  // 命中浏览器缓存里的旧 JS，必须提示刷新，不能像以前那样直接跳过。
  if (!mine || serverVer !== mine) $('#verBanner').hidden = false;
}
/* 每 60 秒同步版本与解码状态：运行期连续失败会自动降级，只有轮询才能
   让徽标及时反映（以前 5 分钟且只查 asset_ver，降级后一直显示硬件）。 */
setInterval(async () => {
  try {
    const st = await api('/api/status', undefined, 'GET');
    verSelfCheck(st.asset_ver);
    renderHwChip(st.hwaccel);
  } catch (e) { /* 掉线时忽略 */ }
}, 60 * 1000);
$('#verReload').onclick = () => location.reload();

/* ── 硬件解码状态反馈 ───────────────────────────────
   顶部徽标只给结论；点开弹层给全部明细：设备能力、每个后端的自检结果、
   短样片收益基准、运行期硬件/软件次数与自动回退、最近错误，并可一键
   在 auto / smart / off / 指定后端之间切换。 */
/* 自检没结束时轮询到出结果：慢机器（驱动初始化 / 收益基准）可能要几十秒，
   不能只拉一次就永远停在"检测中…"。 */
async function pollHw(times = 40) {
  for (let i = 0; i < times; i++) {
    try {
      const d = await api('/api/hwaccel', undefined, 'GET');
      renderHwChip(d);
      if (!d.checking) return d;
    } catch (e) { /* 掉线时下一轮再试 */ }
    await new Promise((r) => setTimeout(r, 1500));
  }
  return null;
}

function renderHwChip(hw) {
  const el = $('#hwChip');
  if (!el || !hw) return;
  if (hw.checking) {
    el.className = 'chip ghost hwchip';
    el.textContent = '解码：检测中…';
    el.title = '正在检测硬件解码能力';
    return;
  }
  const tip = (hw.reason || '') + (hw.advisory ? '\n' + hw.advisory : '');
  if (hw.degraded) {
    el.className = 'chip warn hwchip';
    el.textContent = '解码：软件（自动降级）';
  } else if (hw.mode === 'hardware') {
    el.className = 'chip ok hwchip';
    el.textContent = '解码：硬件 · ' + String(hw.backend || '').toUpperCase();
  } else if (hw.capable) {
    el.className = 'chip warn hwchip';
    el.textContent = '解码：软件（设备可用·已回退）';
  } else {
    el.className = 'chip ghost hwchip';
    el.textContent = '解码：软件';
  }
  el.title = tip + '\n点击查看详情与切换模式';
}

function hwRow(ok, nm, ms) {
  return '<div class="it ' + (ok ? 'ok' : 'no') + '"><span class="mk">' +
    (ok ? '✓' : '✗') + '</span><span class="nm">' + esc(nm) +
    '</span><span class="ms">' + esc(ms) + '</span></div>';
}

function renderHwBody(d) {
  const body = $('#hwBody');
  const rows = [];
  rows.push(hwRow(d.mode === 'hardware', '当前模式',
    d.mode === 'hardware'
      ? ('硬件解码 · ' + (d.backend_label || d.backend || '') + '（' + (d.backend || '') + '）')
      : '软件解码' + (d.degraded ? '（连续失败后自动降级）' : '')));
  rows.push(hwRow(d.capable, '设备能力',
    d.capable ? (d.backend_label || '硬件设备可用') : (d.reason || '未检测到可用硬件解码设备')));
  rows.push(hwRow(d.compiled && d.compiled.length > 0, '编译支持',
    (d.compiled || []).join(' / ') || '当前 ffmpeg 未编译硬件加速后端'));
  (d.attempts || []).forEach((a) => rows.push(hwRow(a.ok, '自检 ' + a.name,
    (a.ok ? '通过（' + a.ms + 'ms）：' : '未通过：') + a.reason)));
  const bm = d.benchmark || {};
  if (bm.valid) {
    rows.push(hwRow(bm.faster, '短样片基准',
      '硬件 ' + bm.hw_ms + 'ms / 软件 ' + bm.sw_ms + 'ms（比值 ' + bm.ratio + '）'));
  }
  rows.push(hwRow(true, '运行统计',
    '硬件解码 ' + ((d.decodes || {}).hardware || 0) + ' 次 · 软件 ' +
    ((d.decodes || {}).software || 0) + ' 次 · 自动回退 ' + (d.fallbacks || 0) + ' 次'));
  if (d.fallbacks) rows.push(hwRow(false, '最近回退', d.last_error || '无错误信息'));

  const modes = [['auto', '自动启用'], ['smart', '智能（更快才用）'], ['off', '关闭']];
  (d.compiled || []).forEach((n) => modes.push([n, '强制 ' + n]));
  const cur = d.override || 'auto';
  const btns = modes.map(([m, label]) =>
    '<button class="btn sm ghost hwmode' + (m === cur ? ' cur' : '') +
    '" data-mode="' + esc(m) + '">' + esc(label) + '</button>').join('') +
    '<button class="btn sm hwmode" data-mode="__recheck">重新自检</button>';

  body.innerHTML = '<div class="dg">' + rows.join('') +
    '<div class="sum">' + esc(d.reason || '') + '</div>' +
    (d.advisory ? '<div class="sum">' + esc(d.advisory) + '</div>' : '') +
    '<div class="hwmodes"><span>模式：</span>' + btns + '</div></div>';

  $$('#hwBody .hwmode').forEach((b) => {
    b.onclick = async () => {
      const m = b.dataset.mode;
      const old = b.textContent;
      try {
        b.disabled = true;
        b.textContent = m === '__recheck' ? '自检中…' : '切换中…';
        const r = m === '__recheck'
          ? await api('/api/hwaccel/recheck', {})
          : await api('/api/hwaccel/mode', { mode: m });
        renderHwChip(r);
        renderHwBody(r);
        toast(m === '__recheck' ? '硬件解码自检完成' : ('解码模式已切换：' + m));
      } catch (e) {
        toast('操作失败：' + e.message, true);
        b.textContent = old;
        b.disabled = false;
      }
    };
  });
}

async function openHwModal() {
  $('#hwModal').classList.add('on');
  $('#hwBody').innerHTML = '<div class="dg"><div class="it ok"><span class="mk">…</span>' +
    '<span class="ms">正在读取硬件解码状态……</span></div></div>';
  try {
    const d = await api('/api/hwaccel', undefined, 'GET');
    renderHwChip(d);
    renderHwBody(d);
    if (d.checking) {
      const done = await pollHw();
      if (done) renderHwBody(done);
    }
  } catch (e) {
    $('#hwBody').innerHTML = '<div class="dg"><div class="it no"><span class="mk">✗</span>' +
      '<span class="ms">' + esc(e.message) + '</span></div></div>';
  }
}

$('#hwChip').onclick = () => openHwModal();
$('#hwClose').onclick = () => $('#hwModal').classList.remove('on');
$('#hwModal').addEventListener('click', (e) => {
  if (e.target.id === 'hwModal') $('#hwModal').classList.remove('on');
});

async function boot() {
  resize();
  try {
    const st = await api('/api/status', undefined, 'GET');
    verSelfCheck(st.asset_ver);
    if (!st.ffmpeg) toast('ffmpeg 未就绪：' + (st.error || ''), true);
    renderRecents(st.projects || []);
    renderHwChip(st.hwaccel);
    if (st.hwaccel && st.hwaccel.checking) pollHw();
    const s = await api('/api/state', undefined, 'GET');
    if (s && s.ready) applyProject(s);
  } catch (e) { /* 首屏无状态是正常的 */ }
}

/* ── 最近项目：列表 / 新建 / 删除 ─────────────────── */
/* 删掉一部剧后，把浏览器里跟着这部剧走的记录一并清掉：
   候选的人工增删 / 位移按剧名存，上次停留的目录也可能是这部剧的。
   不清的话重新导入同名剧会捡回旧选择和失效路径，扫描看起来就"不对劲"。 */
function forgetRecentLocal(name, folder) {
  try {
    localStorage.removeItem('dc_hooks_' + name);
    const norm = (p) => String(p || '').replace(/[\\/]+$/, '').toLowerCase();
    const f = norm(folder);
    ['dc.lastDir', 'dc.outBrowse', 'dc.outDir'].forEach((k) => {
      const v = localStorage.getItem(k);
      if (!v) return;
      if (!f || norm(v) === f) localStorage.removeItem(k);
    });
  } catch (err) { /* 隐私模式忽略 */ }
}
async function renderRecents(list) {
  const box = $('#recents');
  if (!list) {
    try {
      const st = await api('/api/status', undefined, 'GET');
      list = st.projects || [];
    } catch (e) { return; }
  }
  const purgeBtn = $('#btnPurgeMiss');
  if (!list.length) {
    box.innerHTML = '<div class="empty">暂无项目，点右上「＋ 新建」导入一部剧</div>';
    if (purgeBtn) purgeBtn.hidden = true;
    return;
  }
  if (purgeBtn) purgeBtn.hidden = !list.some((p) => p.missing);
  box.innerHTML = list.map((p) => {
    const cur = !!S.name && p.name === S.name;
    const info = p.eps + ' 集 · ' + fmt(p.total);
    return '<div class="rec' + (cur ? ' cur' : '') + (p.missing ? ' bad' : '') +
      '" data-p="' + esc(p.path) + '" data-miss="' + (p.missing ? '1' : '') +
      '" title="' + esc(p.missing ? '源视频不在这台电脑上：' + (p.folder || '未知位置') : info) + '">' +
      '<b>' + esc(p.name) + '</b>' +
      '<span>' + (p.missing ? '路径失效' : info) + '</span>' +
      '<button class="recdel" title="删除这个项目（保留已识别的字幕缓存）">✕</button>' +
    '</div>';
  }).join('');
  $$('#recents .rec').forEach((el) => {
    el.onclick = async () => {
      // 源视频不在本机：先问一句，别让人以为打开就是黑屏坏掉了
      if (el.dataset.miss === '1') {
        const nm = el.querySelector('b').textContent;
        if (!confirm('「' + nm + '」的原片不在这台电脑上：\n' + (el.getAttribute('title') || '') + '\n\n' +
                     '打开后只能看到上次存下的时间轴和台词，缩略图、原片播放和出片都用不了。\n' +
                     '要从这台电脑上的素材重新导入，请点右上「＋ 新建」。\n\n仍然打开吗？')) return;
      }
      try {
        const d = await api('/api/open', { path: el.dataset.p });
        applyProject(d);
        renderRecents();
      } catch (e) { toast(e.message, true); }
    };
  });
  $$('#recents .recdel').forEach((btn) => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const row = btn.closest('.rec');
      const name = row.querySelector('b').textContent;
      const info = row.querySelector('span').textContent;
      if (!confirm('删除项目「' + name + '」（' + info + '）？\n\n只删项目存档，原片和已识别的字幕缓存都保留，' +
                   '以后重新导入还是秒级。')) return;
      try {
        const r = await api('/api/project/delete', { path: row.dataset.p });
        forgetRecentLocal(r.name, r.folder);
        // 删除时若还有扫描在跑，别把按钮 / 遮罩留在"导入中"的状态
        clearInterval(S.jobTimer);
        S.scanPending = false;
        $('#btnScan').disabled = false;
        impHide();
        toast('已删除项目：' + r.name);
        if (r.current) { location.reload(); return; }
        renderRecents();
      } catch (err) { toast(err.message, true); }
    };
  });
}

$('#btnPurgeMiss').onclick = async () => {
  const n = $$('#recents .rec.bad').length;
  if (!n) return;
  if (!confirm('清掉 ' + n + ' 个「源视频已不在这台电脑上」的项目？\n\n' +
               '只删项目存档，原片和已识别的字幕缓存都保留。')) return;
  try {
    const r = await api('/api/projects/purge_missing', {});
    (r.removed || []).forEach((nm) => forgetRecentLocal(nm, ''));
    clearInterval(S.jobTimer);
    S.scanPending = false;
    $('#btnScan').disabled = false;
    impHide();
    toast('已清理 ' + r.removed.length + ' 个失效项目');
    renderRecents();
    const st = await api('/api/state', undefined, 'GET');
    // 以前这里无条件 reload；后端已经没有可接回的项目时会一直刷，
    // 页面反复重载，看着就像「扫描并分析」按钮点不动。
    if (st && st.ready) applyProject(st);
    else if (S.ready) location.reload();
  } catch (e) { toast(e.message, true); }
};

$('#btnNewProj').onclick = () => {
  $('#folder').value = '';
  $('#projName').value = S.ready ? '' : $('#projName').value;
  endScan();            // 重新导入前先把扫描按钮从任何异常状态里放出来
  impHide();
  openDirPicker();
};

/* ── 自动混剪：多模态识别概况 + 主题 / 时长 / 节奏约束 ───────────── */
async function loadAutoState() {
  if (!S.ready) return;
  try {
    const r = await api('/api/multimodal', undefined, 'GET');
    if (!r.ready) return;
    S.auto.deep = !!r.deep;
    S.auto.topics = r.topics || [];
    S.auto.stats = r.stats || {};
    acShowStat(r);
  } catch (e) { /* 首屏无状态是正常的 */ }
}
function acShowStat(r) {
  const st = r.stats || {};
  const top = (r.topics || []).slice(0, 3)
    .map((t) => t.name + ' ' + (t.share * 100).toFixed(0) + '%').join(' · ') || '未识别到明确主题';
  const sc = Object.keys(st.scenes || {}).slice(0, 3).join(' / ') || '—';
  const em = Object.keys(st.emotions || {}).slice(0, 3).join(' / ') || '—';
  $('#acStat').textContent =
    (r.deep ? '深度识别（台词 + 画面 + 声音 + 动作）' : '仅台词识别（点「识别素材」可提升准确度）') +
    ' · ' + (st.units || 0) + ' 个融合片段 / ' + fmt(st.seconds || 0) +
    '\n主题：' + top + '　场景：' + sc + '\n情绪：' + em +
    '　平均节奏 ' + (st.pace || 0).toFixed(2) + '　跨模态置信度 ' + (st.confidence || 0).toFixed(2);
  $('#acStat').style.whiteSpace = 'pre-line';
  $('#acTopics').innerHTML = (r.topics || []).slice(0, 6).map((t) =>
    '<button type="button" data-t="' + esc(t.name) + '">' + esc(t.name) +
    '<b>' + (t.share * 100).toFixed(0) + '%</b></button>').join('');
  $$('#acTopics button').forEach((b) => {
    b.onclick = () => { $('#acTopic').value = b.dataset.t; $('#acTopic').focus(); };
  });
  $('#acTopicList').innerHTML = (r.topics || []).map((t) =>
    '<option value="' + esc(t.name) + '"></option>').join('');
}
function acFeedback(fb) {
  const el = $('#acFb');
  if (!fb) { el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  el.className = 'acfb ' + (fb.level === 'success' ? 'ok' : fb.level === 'warning' ? 'warn' : 'err');
  const hints = (fb.hints || []).filter(Boolean);
  el.innerHTML = '<b>' + esc(fb.title) + '</b>' + esc(fb.msg) +
    (hints.length ? '<ul>' + hints.map((h) => '<li>' + esc(h) + '</li>').join('') + '</ul>' : '');
}
function acMetrics(m) {
  if (!m) { $('#acMetrics').hidden = true; return; }
  const chip = (k, v) => '<i>' + k + ' <b>' + esc(v) + '</b></i>';
  $('#acMetrics').innerHTML = [
    chip('成片', m.estimated + 's / ' + m.target + 's'),
    chip('片段', m.segments),
    chip('主题一致', (m.topic_purity * 100).toFixed(0) + '%'),
    chip('关键信息', (m.key_coverage * 100).toFixed(0) + '%'),
    chip('节奏', m.pace_avg.toFixed(2)),
    chip('转场代价', m.transition_avg.toFixed(2)),
    chip('置信度', m.confidence.toFixed(2)),
  ].join('');
  $('#acMetrics').hidden = false;
}
function acPlanView(plan) {
  $('#acList').innerHTML = plan.segments.map((s, i) => {
    const fd = (s.fade_in || s.fade_out)
      ? '<span class="fade">软转场 ' + (Math.max(s.fade_in || 0, s.fade_out || 0)).toFixed(2) + 's</span>' : '';
    return '<div class="acseg" title="' + esc((s.evidence || []).join(' · ')) + '">' +
      '<div class="acl"><span class="n">' + (i + 1) + '</span>' +
      '<span>第 ' + s.ep + ' 集 · ' + s.start.toFixed(1) + '–' + s.end.toFixed(1) + 's</span>' +
      '<span class="tag">' + esc(s.topic) + '</span>' +
      '<span class="tag">' + esc(s.emotion) + '</span>' +
      '<span class="tag">' + esc(s.scene) + '</span>' +
      '<span>节奏 ' + s.pace.toFixed(2) + '</span>' + fd + '</div>' +
      '<div class="act">' + esc((s.text || '（无台词画面段）').slice(0, 90)) + '</div></div>';
  }).join('');
}
$('#acScan').onclick = async () => {
  if (!S.ready) { toast('先导入剧集', true); return; }
  const b = $('#acScan');
  b.disabled = true;
  try {
    await apiStart('/api/multimodal/scan', { force: true }, '识别素材');
    watchJob((res) => {
      b.disabled = false;
      loadAutoState();
      S.auto.plan = null; $('#acRender').hidden = true;
      acFeedback(null); $('#acMetrics').hidden = true; $('#acList').innerHTML = '';
      if (res) toast('识别完成：' + res.units + ' 个融合片段');
    }, () => { b.disabled = false; });
  } catch (e) {
    b.disabled = false;
    toast(e.message, true);
  }
};
$('#acPlan').onclick = async () => {
  if (!S.ready) { toast('先导入剧集', true); return; }
  const b = $('#acPlan');
  b.disabled = true;
  try {
    const r = await api('/api/autocut/plan', {
      topic: $('#acTopic').value.trim(),
      target: Number($('#acTarget').value) || 180,
      style: $('#acStyle').value,
      transition: $('#acTrans').value,
      relax: $('#acRelax').checked,
    });
    acFeedback(r.feedback);
    S.auto.plan = r.plan || null;
    $('#acRender').hidden = !r.plan;
    if (r.plan) { acMetrics(r.metrics); acPlanView(r.plan); syncTabDots(); }
    else { $('#acMetrics').hidden = true; $('#acList').innerHTML = ''; }
    toast(r.feedback.title, !r.feedback.ok);
  } catch (e) {
    toast(e.message, true);
  } finally {
    b.disabled = false;
  }
};
$('#acRender').onclick = async () => {
  if (!S.auto.plan) { toast('先生成混剪方案', true); return; }
  const b = $('#acRender');
  b.disabled = true;
  $('#out').innerHTML = '';
  $('#btnShowOut').hidden = true;
  const base = Object.assign(packOpts(), {
    logo: S.logo, logo_pos: $('#logoPos').value,
    outdir: S.outdir, outdir_sub: !$('#bFlat').checked,
    name_tpl: String(S.nameTpl || '').trim(),
    title: ($('#title').value || S.name) + '_自动混剪',
  });
  try {
    await apiStart('/api/autocut/render', base, '自动混剪出片');
    watchJob((res) => {
      b.disabled = false;
      if (!res) return;
      $('#out').innerHTML =
        '<video src="/api/video?path=' + encodeURIComponent(res.video) + '" controls></video>' +
        '<div class="oi"><span>' + (res.name ? esc(res.name) + ' · ' : '') +
        fmt(res.duration) + ' · ' + res.width + '×' + res.height + ' · ' +
        res.segments + ' 段</span><span>' + res.size_mb + ' MB</span></div>';
      if (res.dir) showOutDir(res.dir);
      $('#btnShowOut').hidden = false;
      $('#btnShowOut').textContent = '查看自动混剪成片';
      toast('自动混剪完成：' + fmt(res.duration));
    }, () => { b.disabled = false; });
  } catch (e) {
    b.disabled = false;
    toast(e.message, true);
  }
};

window.addEventListener('resize', () => { resize(); draw(); });
/* 只有整段脚本跑完、所有按钮都绑上，才标记为就绪；
   index.html 的兜底脚本靠这个标记判断要不要接管关键按钮。 */
window.__APP_READY__ = true;
boot();
