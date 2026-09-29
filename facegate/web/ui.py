# -*- coding: utf-8 -*-
"""HTML/CSS/JS админ-панели (строки-шаблоны для facegate.web.app).

Шаблоны намеренно НЕ используют Jinja: подстановка идёт через ``%%KEY%%``,
поэтому JS/CSS не конфликтует с шаблонизатором и страница отдаётся как есть.

Панель заменяет окно cv2.imshow на сервере: всё, что видит камера, показывается
здесь как MJPEG-поток (``/stream/<client_id>``) с рамками распознанных лиц
поверх. Вкладки: «Камеры», «Люди», «Саундборд», «Настройки», «Журнал».

Новое в этой версии:
  * **добавление лица из текущего кадра** — кнопка «+» на чипе лица в карточке
    камеры или «добавить лицо» в её действиях: сервер находит лица в последнем
    кадре, показывает превью, и после ввода имени человек сразу попадает в базу;
  * **саундборд** — библиотека записанных/синтезированных звуков: проигрывание
    в браузере, трансляция клиентам, скрытие и удаление элементов, загрузка файлов.
"""
import json

FAVICON = ("data:image/svg+xml,"
           "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
           "%3Crect width='32' height='32' rx='6' fill='%2312141a'/%3E"
           "%3Ccircle cx='16' cy='13' r='6' fill='none' stroke='%234f8cff' stroke-width='2'/%3E"
           "%3Cpath d='M5 27c3-6 19-6 22 0' fill='none' stroke='%234f8cff' stroke-width='2'/%3E"
           "%3C/svg%3E")

CSS = """
  :root { --bg:#12141a; --card:#1b1f2a; --card2:#141824; --line:#2a3040; --text:#e8eaf0;
          --muted:#8a93a6; --accent:#4f8cff; --ok:#37c26e; --warn:#e6b23c; --err:#e05252; }
  * { box-sizing:border-box; }
  html,body { margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:'Segoe UI',system-ui,Arial,sans-serif;
         font-size:14px; }
  a { color:var(--accent); }
  header { position:sticky; top:0; z-index:20; background:rgba(18,20,26,.96);
           border-bottom:1px solid var(--line); backdrop-filter:blur(6px); }
  .hwrap { max-width:1400px; margin:0 auto; padding:10px 16px; display:flex; gap:16px;
           align-items:center; flex-wrap:wrap; }
  .brand { font-weight:700; font-size:16px; letter-spacing:.2px; display:flex; gap:8px;
           align-items:center; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--ok);
                box-shadow:0 0 8px var(--ok); }
  .brand .dot.off { background:var(--err); box-shadow:0 0 8px var(--err); }
  nav { display:flex; gap:4px; flex-wrap:wrap; }
  nav button { background:transparent; border:1px solid transparent; color:var(--muted);
               border-radius:8px; padding:7px 13px; font-size:13px; cursor:pointer; margin:0; }
  nav button:hover { color:var(--text); background:#20263404; }
  nav button.active { color:var(--text); background:var(--card); border-color:var(--line); }
  .hstats { margin-left:auto; display:flex; gap:14px; align-items:center; flex-wrap:wrap;
            color:var(--muted); font-size:12px; }
  .hstats b { color:var(--text); font-weight:600; }
  .wrap { max-width:1400px; margin:0 auto; padding:18px 16px 60px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:16px; margin-bottom:16px; }
  .card > h2 { font-size:15px; margin:0 0 12px; font-weight:600; }
  .row { display:flex; gap:12px; flex-wrap:wrap; }
  .col { flex:1 1 260px; min-width:220px; }
  label { display:block; font-size:12px; color:var(--muted); margin:10px 0 4px; }
  input[type=text],input[type=password],input[type=number],select,textarea {
      width:100%; padding:9px 11px; border-radius:8px; border:1px solid var(--line);
      background:#10131b; color:var(--text); font-size:14px; font-family:inherit; }
  input[type=file] { color:var(--muted); font-size:13px; }
  textarea { min-height:64px; resize:vertical; }
  input:focus,select:focus,textarea:focus { outline:none; border-color:var(--accent); }
  .hint { font-size:11px; color:var(--muted); margin-top:4px; line-height:1.45; }
  button { background:var(--accent); color:#fff; border:none; border-radius:8px;
           padding:9px 16px; font-size:13px; cursor:pointer; }
  button:hover { filter:brightness(1.12); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  button.ghost { background:transparent; border:1px solid var(--line); color:var(--muted);
                 padding:6px 11px; font-size:12px; }
  button.ghost:hover { color:var(--text); border-color:var(--accent); }
  button.danger { background:transparent; border:1px solid var(--err); color:var(--err);
                  padding:6px 11px; font-size:12px; }
  button.on { background:var(--ok); }
  button.off { background:#3a4152; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); gap:14px; }
  .camgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:16px; }
  .person { background:var(--card2); border:1px solid var(--line); border-radius:10px;
            overflow:hidden; display:flex; flex-direction:column; }
  .person img.thumb { width:100%; height:150px; object-fit:cover; display:block;
                      background:#0d1017; }
  .pbody { padding:10px 12px; flex:1; }
  .pname { font-weight:600; font-size:14px; word-break:break-word; }
  .pmeta { font-size:12px; color:var(--muted); margin-top:3px; }
  .badge { display:inline-block; font-size:11px; padding:2px 8px; border-radius:20px;
           margin-top:8px; }
  .b-ready { background:rgba(55,194,110,.15); color:var(--ok); }
  .b-pending { background:rgba(230,178,60,.15); color:var(--warn); }
  .b-missing { background:rgba(138,147,166,.15); color:var(--muted); }
  .b-error { background:rgba(224,82,82,.15); color:var(--err); }
  .pactions { display:flex; gap:6px; padding:0 12px 12px; flex-wrap:wrap; }
  .photos { display:flex; gap:4px; flex-wrap:wrap; padding:0 12px 10px; }
  .photos .ph { position:relative; }
  .photos img { width:44px; height:44px; object-fit:cover; border-radius:6px;
                border:1px solid var(--line); display:block; background:#0d1017; }
  .photos .x { position:absolute; top:-6px; right:-6px; width:17px; height:17px;
               border-radius:50%; background:var(--err); color:#fff; font-size:11px;
               line-height:17px; text-align:center; cursor:pointer; border:none; padding:0; }
  /* --- камеры --- */
  .cam { background:var(--card2); border:1px solid var(--line); border-radius:12px;
         overflow:hidden; display:flex; flex-direction:column; }
  .camhead { display:flex; align-items:center; gap:8px; padding:9px 12px;
             border-bottom:1px solid var(--line); background:#171b26; }
  .camhead .cid { font-weight:600; font-size:13px; word-break:break-all; }
  .pill { font-size:10px; padding:2px 7px; border-radius:20px; background:rgba(138,147,166,.16);
          color:var(--muted); white-space:nowrap; }
  .pill.live { background:rgba(55,194,110,.18); color:var(--ok); }
  .pill.dead { background:rgba(224,82,82,.18); color:var(--err); }
  .stage { position:relative; width:100%; background:#080a0f; overflow:hidden;
           aspect-ratio:16/9; }
  .stage img.feed { position:absolute; inset:0; width:100%; height:100%; object-fit:fill;
                    display:block; }
  .boxes { position:absolute; inset:0; pointer-events:none; }
  .box { position:absolute; border:2px solid var(--err); border-radius:3px; }
  .box.known { border-color:var(--ok); }
  .box span { position:absolute; top:-19px; left:-2px; font-size:11px; padding:1px 6px;
              border-radius:4px 4px 4px 0; background:var(--err); color:#fff; white-space:nowrap; }
  .box.known span { background:var(--ok); color:#08130c; }
  .nosignal { position:absolute; inset:0; display:flex; flex-direction:column; gap:6px;
              align-items:center; justify-content:center; color:var(--muted); font-size:13px;
              background:rgba(8,10,15,.55); }
  .camstats { display:flex; gap:12px; flex-wrap:wrap; padding:9px 12px; font-size:11px;
              color:var(--muted); border-top:1px solid var(--line); }
  .camstats b { color:var(--text); font-weight:600; }
  .camactions { display:flex; gap:6px; padding:0 12px 12px; flex-wrap:wrap; }
  .faces { padding:0 12px 10px; display:flex; gap:6px; flex-wrap:wrap; }
  .facechip { font-size:11px; padding:3px 8px; border-radius:20px;
              background:rgba(55,194,110,.14); color:var(--ok); }
  .facechip.unk { background:rgba(224,82,82,.14); color:var(--err); }
  /* --- журнал --- */
  .logbox { font-family:Consolas,'Courier New',monospace; font-size:12px; line-height:1.5;
            max-height:46vh; overflow-y:auto; background:#0e1118; border:1px solid var(--line);
            border-radius:8px; padding:8px 10px; }
  .logbox div { padding:1px 0; border-bottom:1px dashed #1e2431; word-break:break-word; }
  .lv-DEBUG { color:#6f7a90; } .lv-INFO { color:#c3cbdb; }
  .lv-WARNING { color:var(--warn); } .lv-ERROR { color:var(--err); }
  .lv-CRITICAL { color:#fff; background:var(--err); }
  .ev { display:flex; gap:8px; align-items:baseline; }
  .ev .t { color:var(--muted); font-size:11px; white-space:nowrap; }
  .empty { color:var(--muted); text-align:center; padding:34px 0; font-size:13px;
           line-height:1.7; }
  .toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }
  .toolbar .spacer { margin-left:auto; }
  .toast { position:fixed; right:16px; bottom:16px; z-index:60; display:flex;
           flex-direction:column; gap:8px; max-width:380px; }
  .toast div { background:var(--card); border:1px solid var(--line); border-left:3px solid var(--accent);
               border-radius:8px; padding:10px 13px; font-size:13px; box-shadow:0 8px 24px rgba(0,0,0,.4);
               animation:slide .18s ease-out; }
  .toast div.ok { border-left-color:var(--ok); }
  .toast div.err { border-left-color:var(--err); }
  @keyframes slide { from { transform:translateX(20px); opacity:0; } to { transform:none; opacity:1; } }
  dialog { background:var(--card); color:var(--text); border:1px solid var(--line);
           border-radius:12px; padding:20px; max-width:420px; width:92%; }
  dialog::backdrop { background:rgba(0,0,0,.55); }
  .warnbar { background:rgba(230,178,60,.13); border:1px solid rgba(230,178,60,.4);
             color:var(--warn); padding:10px 13px; border-radius:10px; margin-bottom:14px;
             font-size:13px; }
  .kv { display:flex; gap:8px; font-size:12px; color:var(--muted); }
  .kv b { color:var(--text); font-weight:600; }
  .login { min-height:100vh; display:flex; align-items:center; justify-content:center;
           padding:20px; }
  .login .box { background:var(--card); border:1px solid var(--line); border-radius:14px;
                padding:26px; width:100%; max-width:380px; }
  .login h1 { font-size:19px; margin:0 0 4px; }
  .login .sub { color:var(--muted); font-size:13px; margin-bottom:16px; }
  .err-text { color:var(--err); font-size:13px; margin:10px 0 0; min-height:18px; }
  .settings-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(310px,1fr)); gap:14px; }
  .sgroup { background:var(--card2); border:1px solid var(--line); border-radius:10px; padding:14px; }
  .sgroup h3 { margin:0 0 6px; font-size:13px; color:var(--accent); text-transform:uppercase;
               letter-spacing:.6px; }
  .switch { display:flex; align-items:center; gap:10px; margin:12px 0; }
  .switch input { width:18px; height:18px; accent-color:var(--accent); }
  .switch span { font-size:13px; }
  .field-err { color:var(--err); font-size:11px; margin-top:3px; }
  table.mini { width:100%; border-collapse:collapse; font-size:12px; }
  table.mini td { padding:4px 6px; border-bottom:1px solid var(--line); color:var(--muted); }
  table.mini td:last-child { text-align:right; color:var(--text); }
  code { background:#10131b; padding:1px 5px; border-radius:4px; font-size:12px; }
  /* --- чипы лиц с кнопкой добавления --- */
  .facechip { position:relative; display:inline-flex; align-items:center; gap:5px; }
  .facechip button.addface { background:rgba(255,255,255,.14); border:none; color:inherit;
      border-radius:50%; width:17px; height:17px; line-height:15px; font-size:12px;
      padding:0; cursor:pointer; margin:0; }
  .facechip button.addface:hover { background:var(--accent); color:#fff; }
  /* --- саундборд --- */
  .sbgrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:14px; }
  .snd { background:var(--card2); border:1px solid var(--line); border-radius:10px;
         padding:12px; display:flex; flex-direction:column; gap:8px; }
  .snd .stitle { font-weight:600; font-size:13px; word-break:break-word; }
  .snd .smeta { font-size:11px; color:var(--muted); display:flex; gap:8px; flex-wrap:wrap; }
  .snd .sactions { display:flex; gap:6px; flex-wrap:wrap; }
  .snd audio { width:100%; height:32px; }
  .kind-badge { font-size:10px; padding:2px 7px; border-radius:20px; white-space:nowrap; }
  .kb-upload { background:rgba(79,140,255,.16); color:var(--accent); }
  .kb-greeting { background:rgba(55,194,110,.15); color:var(--ok); }
  .kb-announce { background:rgba(230,178,60,.15); color:var(--warn); }
  /* --- диалог добавления лица из кадра --- */
  dialog.wide { max-width:520px; }
  #face-preview { width:100%; max-height:280px; object-fit:contain; background:#0d1017;
                  border:1px solid var(--line); border-radius:8px; display:block; }
  #face-list { display:flex; gap:8px; flex-wrap:wrap; margin:8px 0; }
  #face-list .fcand { border:2px solid var(--line); border-radius:8px; cursor:pointer;
                      padding:2px; background:#0d1017; }
  #face-list .fcand.sel { border-color:var(--accent); }
  #face-list img { width:72px; height:72px; object-fit:cover; display:block; border-radius:5px; }
  #face-list .fcand div { font-size:10px; color:var(--muted); text-align:center; padding:2px 0; }
"""

JS = r"""
'use strict';
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const CSRF = document.getElementById('csrf').content;

/* ---------- утилиты ---------- */
function esc(s){ const d=document.createElement('div'); d.textContent = (s===null||s===undefined)?'':String(s); return d.innerHTML; }
function escAttr(s){ return esc(s).replace(/'/g,'&#39;').replace(/"/g,'&quot;'); }
function toast(msg, kind){
  const box = $('#toasts'); const d = document.createElement('div');
  d.className = kind || ''; d.textContent = msg; box.appendChild(d);
  setTimeout(()=>{ d.style.opacity='0'; d.style.transition='opacity .3s';
                   setTimeout(()=>d.remove(), 320); }, 3600);
}
function fmtTime(ts){ const d=new Date(ts*1000); return d.toLocaleTimeString(); }
function fmtAge(sec){
  if (sec===null||sec===undefined) return '—';
  if (sec < 1) return 'только что';
  if (sec < 60) return Math.round(sec)+' с назад';
  if (sec < 3600) return Math.round(sec/60)+' мин назад';
  return Math.round(sec/3600)+' ч назад';
}
async function api(path, opts){
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers, { 'X-CSRF-Token': CSRF });
  let res;
  try { res = await fetch(path, opts); }
  catch(e){ toast('Сервер недоступен: '+e, 'err'); throw e; }
  if (res.status === 401){ location.href = '/login?next='+encodeURIComponent(location.pathname); return null; }
  let body = null;
  try { body = await res.json(); } catch(e) {}
  return { ok: res.ok, status: res.status, body };
}
const getJSON = p => api(p, { method:'GET' });
const postJSON = (p, data) => api(p, { method:'POST', headers:{'Content-Type':'application/json'},
                                       body: data===undefined ? '{}' : JSON.stringify(data) });

/* ---------- состояние ---------- */
const S = {
  tab: location.hash.replace('#','') || 'cameras',
  cams: {},              // client_id -> DOM elements
  camOrder: [],
  logSeq: 0, evSeq: 0,
  logPaused: false, logLevel: 'INFO',
  spec: null, dirty: false,
  boot: JSON.parse(document.getElementById('boot').textContent),
  timers: []
};

/* ---------- вкладки ---------- */
function switchTab(name){
  S.tab = name; location.hash = name;
  $$('nav button').forEach(b => b.classList.toggle('active', b.dataset.tab===name));
  $$('.tabpane').forEach(p => p.style.display = (p.id==='tab-'+name) ? '' : 'none');
  if (name==='people') refreshPersons();
  if (name==='soundboard') refreshSoundboard();
  if (name==='settings') refreshSettings();
  if (name==='logs'){ refreshLogs(true); refreshEvents(true); }
  if (name==='cameras') refreshCameras();
}

/* ---------- шапка / статус ---------- */
async function refreshStatus(){
  const r = await getJSON('/api/status');
  if (!r || !r.ok) return;
  const s = r.body;
  const t = s.totals || {};
  $('#st-uptime').textContent = s.uptime_human;
  // только ТЕКУЩЕЕ состояние: клиенты онлайн и лица в кадре прямо сейчас
  $('#st-clients').textContent = s.clients_online;
  const cw = $('#st-clients-wrap');
  if (cw) cw.title = 'клиентов онлайн сейчас (всего зарегистрировано: ' + s.clients_total + ')';
  const curFaces = (t.current_faces != null) ? t.current_faces : (s.current_faces || 0);
  $('#st-faces').textContent = curFaces;
  $('#st-greets').textContent = s.counters.greets_sent;
  $('#dot').classList.toggle('off', s.clients_online === 0);
  const btnRec = $('#btn-recog'), btnGreet = $('#btn-greeting');
  if (btnRec){ const on = s.settings.recognition_enabled;
    btnRec.textContent = on ? 'Распознавание: вкл' : 'Распознавание: выкл';
    btnRec.className = 'ghost ' + (on ? 'on' : 'off'); }
  if (btnGreet){ const on = s.settings.greeting_enabled;
    btnGreet.textContent = on ? 'Приветствия: вкл' : 'Приветствия: выкл';
    btnGreet.className = 'ghost ' + (on ? 'on' : 'off'); }
}

/* ---------- КАМЕРЫ ---------- */
function camCard(c){
  const el = document.createElement('div');
  el.className = 'cam'; el.dataset.id = c.client_id;
  el.innerHTML =
    '<div class="camhead"><span class="pill">—</span><span class="cid"></span>' +
    '<span class="spacer" style="margin-left:auto"></span>' +
    '<button class="ghost" data-act="full">на весь экран</button></div>' +
    '<div class="stage"><img class="feed" alt=""><div class="boxes"></div>' +
    '<div class="nosignal" style="display:none">НЕТ СИГНАЛА<div style="font-size:11px"></div></div></div>' +
    '<div class="faces"></div>' +
    '<div class="camstats"></div>' +
    '<div class="camactions">' +
      '<button class="ghost" data-act="snap">снимок на сервер</button>' +
      '<a class="ghost" data-act="dl" download style="text-decoration:none;padding:6px 11px;border:1px solid var(--line);border-radius:8px;color:var(--muted);font-size:12px" href="#">скачать кадр</a>' +
      '<button class="ghost" data-act="addface" title="Добавить новое лицо из текущего кадра">＋ лицо из кадра</button>' +
      '<button class="ghost" data-act="announce">объявить</button>' +
      '<button class="danger" data-act="forget">убрать</button>' +
    '</div>';
  el.querySelector('[data-act=snap]').onclick = () => snapshot(c.client_id);
  el.querySelector('[data-act=addface]').onclick = () => addFaceFromCamera(c.client_id);
  el.querySelector('[data-act=announce]').onclick = () => announce(c.client_id);
  el.querySelector('[data-act=forget]').onclick = () => forgetCam(c.client_id);
  el.querySelector('[data-act=full]').onclick = () => { location.href = '/camera/'+encodeURIComponent(c.client_id); };
  const img = el.querySelector('img.feed');
  img.src = '/stream/' + encodeURIComponent(c.client_id);
  img.onerror = () => { setTimeout(()=>{ img.src = '/stream/'+encodeURIComponent(c.client_id)+'?r='+Date.now(); }, 3000); };
  // download + ?download=1: браузер сохраняет файл, а не открывает JPEG
  // в текущей вкладке (раньше клик уводил из панели и рвал сессию просмотра)
  const dl = el.querySelector('[data-act=dl]');
  const fname = (c.client_id||'frame').replace(/[^a-zA-Z0-9_.-]/g,'_')+'.jpg';
  dl.href = '/api/cameras/'+encodeURIComponent(c.client_id)+'/frame.jpg?download=1';
  dl.setAttribute('download', fname);
  return el;
}

function updateCam(el, c){
  const pill = el.querySelector('.pill');
  pill.textContent = c.online ? 'в эфире' : 'нет сигнала';
  pill.className = 'pill ' + (c.online ? 'live' : 'dead');
  el.querySelector('.cid').textContent = c.client_id;
  el.querySelector('.nosignal').style.display = c.online ? 'none' : '';
  const ns = el.querySelector('.nosignal div');
  if (ns) ns.textContent = c.last_frame_age===null ? 'ожидание первого кадра'
                                 : 'последний кадр ' + fmtAge(c.last_frame_age);
  const stage = el.querySelector('.stage');
  if (c.width && c.height) stage.style.aspectRatio = c.width + '/' + c.height;
  const st = el.querySelector('.camstats');
  st.innerHTML =
    '<span>FPS <b>'+(c.fps||0)+'</b></span>' +
    '<span>Кадр <b>'+(c.width&&c.height ? c.width+'×'+c.height : '—')+'</b></span>' +
    '<span>Кадров <b>'+c.frames+'</b></span>' +
    '<span>Лиц <b>'+c.faces+'</b></span>' +
    '<span>Узнано <b>'+c.recognized+'</b></span>';
  // рамки поверх потока
  const boxes = el.querySelector('.boxes');
  if (c.width && c.height && c.annotations && c.annotations.length){
    boxes.innerHTML = c.annotations.map(a => {
      const l = a.left/c.width*100, t = a.top/c.height*100;
      const w = Math.max(0,(a.right-a.left))/c.width*100, h = Math.max(0,(a.bottom-a.top))/c.height*100;
      return '<div class="box '+(a.known?'known':'')+'" style="left:'+l+'%;top:'+t+'%;width:'+w+'%;height:'+h+'%">' +
             (a.label ? '<span>'+esc(a.label)+'</span>' : '') + '</div>';
    }).join('');
  } else { boxes.innerHTML = ''; }
  const chips = el.querySelector('.faces');
  chips.innerHTML = (c.annotations||[]).map(a =>
    '<span class="facechip'+(a.known?'':' unk')+'">'+esc(a.name||'неизвестный')+' · '+
    (a.distance!==null&&a.distance!==undefined?a.distance.toFixed(3):'—')+
    '<button class="addface" title="Добавить это лицо в базу" data-box="'+
    [a.left,a.top,a.right,a.bottom].join(',')+'">+</button></span>'
  ).join('');
  chips.querySelectorAll('button.addface').forEach(b => {
    b.onclick = () => openFaceDialog(c.client_id, b.dataset.box.split(',').map(Number), null);
  });
  const meta = c.meta || {};
  if (meta.hostname || meta.camera_id!==undefined){
    const host = meta.hostname ? ' · хост '+esc(meta.hostname) : '';
    const cam = (meta.camera_id!==undefined&&meta.camera_id!==null) ? ' · камера '+esc(meta.camera_id) : '';
    el.querySelector('.camhead .spacer').innerHTML = '<span class="pill">'+host.replace(' · ','')+cam.replace(' · ',' / ')+'</span>';
  }
}

async function refreshCameras(){
  const r = await getJSON('/api/cameras');
  if (!r || !r.ok) return;
  const list = r.body.cameras || [];
  const grid = $('#camgrid');
  const ids = list.map(c => c.client_id);
  // удалить пропавшие
  Object.keys(S.cams).forEach(id => { if (!ids.includes(id)){ S.cams[id].remove(); delete S.cams[id]; } });
  // порядок + создание
  ids.forEach((id, i) => {
    let el = S.cams[id];
    if (!el){ el = camCard({ client_id:id }); S.cams[id] = el; }
    if (grid.children[i] !== el) grid.insertBefore(el, grid.children[i] || null);
  });
  list.forEach(c => { const el = S.cams[c.client_id]; if (el) updateCam(el, c); });
  $('#cam-empty').style.display = list.length ? 'none' : '';
}

async function snapshot(id){
  const r = await postJSON('/api/cameras/'+encodeURIComponent(id)+'/snapshot');
  if (r && r.ok) toast('Снимок сохранён: '+(r.body.name||r.body.path||'ok'), 'ok');
  else toast('Не удалось сохранить снимок: '+errorMessage(r), 'err');
}
async function forgetCam(id){
  if (!confirm('Убрать клиента «'+id+'» из панели? (если он ещё шлёт видео — появится снова)')) return;
  const r = await api('/api/cameras/'+encodeURIComponent(id), { method:'DELETE' });
  if (r && r.ok){ delete S.cams[id]; refreshCameras(); toast('Клиент убран', 'ok'); }
}
/* Объявление: модальное окно вместо prompt() — текст можно править, виден
   адресат и статус озвучки. Команда уходит ОДНА и сразу со звуком. */
const ANN = { target:null };
function announce(id){
  ANN.target = id || null;
  $('#announce-target').textContent = id ? id : 'всем клиентам в эфире';
  const ta = $('#announce-text');
  ta.value = ta.value || 'Внимание, тестовое сообщение';
  $('#announce-err').textContent = '';
  $('#announce-dialog').showModal();
  setTimeout(()=>{ ta.focus(); ta.select(); }, 50);
}
async function submitAnnounce(){
  const text = $('#announce-text').value.trim();
  const err = $('#announce-err'); err.textContent = '';
  if (!text){ err.textContent = 'Введите текст объявления'; return; }
  const btn = $('#announce-ok'); btn.disabled = true; btn.textContent = 'Отправка…';
  try {
    const r = await postJSON('/api/announce', { text: text, client_id: ANN.target });
    if (r && r.ok){
      const b = r.body || {};
      if (b.voice === 'ready')
        toast('Объявление отправлено '+(b.sent||0)+' адресатам со звуком'+
              (b.cached ? ' (готовая озвучка из кэша)' : ''), 'ok');
      else if (b.voice === 'pending')
        toast('Объявление принято: озвучка синтезируется и уйдёт клиентам, ' +
              'как только будет готова', 'ok');
      else
        toast('Объявление отправлено без звука: '+(b.warning||'TTS недоступен'), 'err');
      $('#announce-dialog').close();
    } else {
      err.textContent = errorMessage(r) || 'не отправлено';
    }
  } finally { btn.disabled = false; btn.textContent = 'Объявить'; }
}
/* Единая расшифровка ошибок API: 503 «движок занят» больше не выглядит как сбой */
function errorMessage(r){
  const body = (r && r.body) || {};
  if (body.error) return body.error;
  if (r && r.status === 503) return 'Сервер занят обработкой видео — повторите через пару секунд';
  if (r && r.status === 409) return 'Конфликт: '+(body.error||'проверьте данные');
  return r ? ('Ошибка '+r.status) : 'Сервер не ответил';
}
async function toggleSetting(key){
  const cur = S.lastSettings[key];
  const r = await postJSON('/api/settings', { settings: { [key]: !cur } });
  if (r && r.ok){ S.lastSettings[key] = !cur; refreshStatus();
    toast(key+' = '+(!cur ? 'вкл' : 'выкл'), 'ok'); }
}

/* ---------- ЛЮДИ ---------- */
const badge = st => ({
  ready:  ['b-ready','озвучка готова'],
  pending:['b-pending','синтезируется…'],
  missing:['b-missing','нет озвучки'],
  error:  ['b-error','ошибка озвучки']
}[st] || ['b-missing', st||'?']);

async function refreshPersons(){
  const r = await getJSON('/api/persons');
  if (!r || !r.ok) return;
  const list = r.body.persons || [];
  $('#persons').innerHTML = list.map(p => {
    const [bc,bt] = badge(p.voice_status);
    const photos = (p.photos||[]).map(f =>
      '<span class="ph"><img src="/photo/'+encodeURIComponent(f)+'" alt="" loading="lazy">' +
      '<button class="x" title="Удалить фото" data-photo="'+escAttr(f)+'" data-name="'+escAttr(p.name)+'">×</button></span>').join('');
    return '<div class="person" data-name="'+escAttr(p.name)+'">' +
      '<img class="thumb" src="'+(p.photos&&p.photos.length ? '/photo/'+encodeURIComponent(p.photos[0]) : '')+'" alt="" onerror="this.style.visibility=\'hidden\'">' +
      '<div class="pbody"><div class="pname">'+esc(p.name)+'</div>' +
      '<div class="pmeta">фото: '+(p.photos||[]).length+
        (p.voice_variants ? ' · фраз: '+(p.voice_ready_variants||0)+'/'+p.voice_variants : '')+
        ' · <span title="сколько раз сервер поздоровался с этим человеком с момента запуска"'+
        ' style="color:var(--accent)">приветствий: <b>'+(p.greet_count||0)+'</b></span></div>' +
      '<div class="pmeta" title="'+escAttr((p.phrases||[p.phrase]).join(' | '))+'">'+
        esc(p.phrase||'')+((p.phrases||[]).length>1 ? ' <span style="color:var(--muted)">и ещё '+
        ((p.phrases||[]).length-1)+'</span>' : '')+'</div>' +
      '<span class="badge '+bc+'">'+bt+'</span></div>' +
      '<div class="photos">'+photos+'</div>' +
      '<div class="pactions">' +
        '<button class="ghost" data-act="resynth">переозвучить</button>' +
        '<button class="ghost" data-act="greet">проверить привет</button>' +
        '<button class="ghost" data-act="rename">переименовать</button>' +
        '<button class="danger" data-act="del">удалить</button>' +
      '</div></div>';
  }).join('');
  $('#persons-empty').style.display = list.length ? 'none' : '';
  $$('#persons .person').forEach(el => {
    const name = el.dataset.name;
    el.querySelector('[data-act=resynth]').onclick = () => resynth(name);
    el.querySelector('[data-act=greet]').onclick = () => greetPerson(name);
    el.querySelector('[data-act=rename]').onclick = () => renamePerson(name);
    el.querySelector('[data-act=del]').onclick = () => delPerson(name);
    el.querySelectorAll('[data-photo]').forEach(b => b.onclick = () => delPhoto(name, b.dataset.photo));
  });
}
async function uploadPerson(){
  const name = $('#pname').value.trim();
  const files = $('#pfiles').files;
  if (!name) return toast('Укажите имя', 'err');
  if (!files.length) return toast('Выберите хотя бы одно фото', 'err');
  const fd = new FormData();
  fd.append('name', name);
  for (const f of files) fd.append('photos', f, f.name);
  const btn = $('#btn-add'); btn.disabled = true; btn.textContent = 'Загрузка…';
  try {
    const r = await api('/api/persons', { method:'POST', body: fd, headers:{ 'X-CSRF-Token': CSRF } });
    ((r&&r.body&&r.body.results)||[]).forEach(x => toast((x.ok?'✓ ':'✗ ')+x.message, x.ok?'ok':'err'));
    if (r && r.ok){ $('#pname').value=''; $('#pfiles').value=''; refreshPersons(); }
  } finally { btn.disabled = false; btn.textContent = 'Добавить'; }
}
async function delPerson(name){
  if (!confirm('Удалить «'+name+'» вместе со всеми фото?')) return;
  const r = await api('/api/persons/'+encodeURIComponent(name), { method:'DELETE' });
  toast(r&&r.ok ? 'Удалён: '+name : 'Не удалось удалить: '+errorMessage(r), r&&r.ok?'ok':'err');
  refreshPersons();
}
async function delPhoto(name, fname){
  if (!confirm('Удалить фото '+fname+' у «'+name+'»?')) return;
  const r = await api('/api/persons/'+encodeURIComponent(name)+'/photos/'+encodeURIComponent(fname), { method:'DELETE' });
  toast(r&&r.ok ? 'Фото удалено' : ((r&&r.body&&r.body.error)||'Ошибка'), r&&r.ok?'ok':'err');
  refreshPersons();
}
async function resynth(name){
  const r = await postJSON('/api/persons/'+encodeURIComponent(name)+'/resynthesize');
  const n = (r&&r.body&&r.body.variants) || 1;
  toast('Переозвучка «'+name+'»: '+((r&&r.body&&r.body.voice)||'?')+
        (n>1 ? ' ('+n+' варианта фраз в очереди)' : ''), r&&r.ok?'ok':'err');
  refreshPersons();
}
async function renamePerson(name){
  const nn = prompt('Новое имя для «'+name+'»:', name);
  if (!nn || nn.trim()===name) return;
  const r = await postJSON('/api/persons/'+encodeURIComponent(name)+'/rename', { name: nn.trim() });
  toast(r&&r.ok ? 'Переименовано в «'+nn.trim()+'»' : errorMessage(r), r&&r.ok?'ok':'err');
  refreshPersons();
}
async function greetPerson(name){
  const cams = await getJSON('/api/cameras');
  const list = ((cams&&cams.body&&cams.body.cameras)||[]);
  const online = list.filter(c=>c.online);
  const pool = online.length ? online : list;      // мягкий порог «в эфире»
  if (!pool.length) return toast('Нет клиентов — некому отправить приветствие', 'err');
  const id = pool.length===1 ? pool[0].client_id
           : prompt('Отправить клиенту:', pool.map(c=>c.client_id).join(', ')) ;
  if (!id) return;
  const r = await postJSON('/api/persons/'+encodeURIComponent(name)+'/greet', { client_id: id });
  if (r && r.ok){
    const b = r.body||{};
    toast('Приветствие отправлено '+id+(b.voice==='ready' ? ' со звуком: «'+(b.text||'')+'»'
        : ' — озвучка синтезируется и придёт следом'), 'ok');
  } else toast(errorMessage(r), 'err');
}

/* ---------- ДОБАВЛЕНИЕ ЛИЦА ИЗ ТЕКУЩЕГО КАДРА ---------- */
const FD = { cam:null, box:null, faces:[] };
function facePreviewUrl(cam, box){
  return '/api/cameras/'+encodeURIComponent(cam)+'/face.jpg?left='+box[0]+'&top='+box[1]+
         '&right='+box[2]+'&bottom='+box[3]+'&margin=0.35&r='+Date.now();
}
function openFaceDialog(cam, box, faces){
  FD.cam = cam; FD.box = box ? box.slice() : null; FD.faces = faces || [];
  $('#face-cam').textContent = cam;
  $('#face-preview').src = facePreviewUrl(cam, FD.box || [0,0,0,0]);
  const list = $('#face-list');
  if (FD.faces.length > 1){
    list.style.display = 'flex';
    list.innerHTML = FD.faces.map((f,i) =>
      '<div class="fcand" data-i="'+i+'"><img src="'+facePreviewUrl(cam,[f.left,f.top,f.right,f.bottom])+'" alt="">'+
      '<div>'+(f.known ? esc(f.name) : 'неизвестный')+'</div></div>').join('');
    $$('#face-list .fcand').forEach(el => el.onclick = () => {
      const f = FD.faces[Number(el.dataset.i)];
      FD.box = [f.left, f.top, f.right, f.bottom];
      $('#face-preview').src = facePreviewUrl(FD.cam, FD.box);
      $$('#face-list .fcand').forEach(x => x.classList.remove('sel'));
      el.classList.add('sel');
    });
  } else { list.style.display = 'none'; list.innerHTML = ''; }
  $('#face-name').value = ''; $('#face-err').textContent = '';
  $('#face-dialog').showModal();
  setTimeout(()=>$('#face-name').focus(), 50);
}
async function addFaceFromCamera(cam){
  toast('Ищем лица в кадре…', '');
  const r = await getJSON('/api/cameras/'+encodeURIComponent(cam)+'/faces');
  if (!r || !r.ok){
    toast('Не удалось найти лица: '+errorMessage(r), 'err');
    // движок распознавания был занят потоком видео — повторяем автоматически
    if (r && r.status === 503) setTimeout(()=>addFaceFromCamera(cam), 2500);
    return;
  }
  const faces = r.body.faces || [];
  if (!faces.length){ toast('В текущем кадре лиц не найдено', 'err'); return; }
  const f = faces.find(x => !x.known) || faces[0];
  openFaceDialog(cam, [f.left, f.top, f.right, f.bottom], faces);
}
async function submitFaceFromFrame(){
  const name = $('#face-name').value.trim();
  const err = $('#face-err'); err.textContent = '';
  if (!name){ err.textContent = 'Введите имя'; return; }
  if (!FD.box){ err.textContent = 'Лицо не выбрано'; return; }
  const btn = $('#face-ok'); btn.disabled = true; btn.textContent = 'Добавление…';
  try{
    const r = await postJSON('/api/persons/from-frame', {
      client_id: FD.cam, name: name,
      left: FD.box[0], top: FD.box[1], right: FD.box[2], bottom: FD.box[3] });
    if (r && r.ok){
      toast('Добавлен: '+r.body.name+(r.body.voice==='ready' ? ' (озвучка готова)' : ' (озвучка синтезируется)'), 'ok');
      $('#face-dialog').close();
      refreshPersons();
    } else {
      err.textContent = errorMessage(r) || 'Не удалось добавить';
      if (r && r.status === 503) setTimeout(submitFaceFromFrame, 2500);
    }
  } finally { btn.disabled = false; btn.textContent = 'Добавить'; }
}

/* ---------- САУНДБОРД ---------- */
const SB = { showHidden:false, items:[] };
function fmtSize(b){
  if (!b) return '0 Б';
  if (b < 1024) return b+' Б';
  if (b < 1048576) return (b/1024).toFixed(0)+' КБ';
  return (b/1048576).toFixed(1)+' МБ';
}
function fmtDate(ts){ return ts ? new Date(ts*1000).toLocaleString() : '—'; }
function sndCard(s){
  const isUpload = s.kind === 'upload';
  const kindLabel = isUpload ? 'загружен' : (s.voice_kind === 'announce' ? 'объявление' : 'приветствие');
  const kindCls = isUpload ? 'kb-upload' : (s.voice_kind === 'announce' ? 'kb-announce' : 'kb-greeting');
  return '<div class="snd" data-id="'+escAttr(s.id)+'" data-title="'+escAttr(s.title)+'"'+
    ' style="'+(s.hidden ? 'opacity:.45' : '')+'">' +
    '<div class="stitle">'+esc(s.title)+(s.hidden ? ' <span class="pill">скрыт</span>' : '')+'</div>' +
    '<div class="smeta"><span class="kind-badge '+kindCls+'">'+kindLabel+
      (s.name ? ' · '+esc(s.name) : '')+'</span>' +
      '<span>'+esc((s.filename||'').split('.').pop().toUpperCase())+'</span>' +
      '<span>'+fmtSize(s.size)+'</span><span>'+fmtDate(s.created)+'</span></div>' +
    '<audio controls preload="none" src="/api/soundboard/'+encodeURIComponent(s.id)+'/file"></audio>' +
    '<div class="sactions">' +
      '<button class="ghost" data-act="bcast" title="Проиграть через колонки клиентов">📢 клиентам</button>' +
      '<button class="ghost" data-act="hide">'+(s.hidden ? 'показать' : 'скрыть')+'</button>' +
      '<button class="danger" data-act="del">удалить</button>' +
    '</div></div>';
}
async function refreshSoundboard(){
  const r = await getJSON('/api/soundboard' + (SB.showHidden ? '?include_hidden=1' : ''));
  if (!r || !r.ok) return;
  SB.items = r.body.items || [];
  $('#sbgrid').innerHTML = SB.items.map(sndCard).join('');
  $('#sb-empty').style.display = SB.items.length ? 'none' : '';
  $$('#sbgrid .snd').forEach(el => {
    const id = el.dataset.id, title = el.dataset.title;
    const hidden = SB.items.find(x => x.id === id) ? !!SB.items.find(x => x.id === id).hidden : false;
    el.querySelector('[data-act=bcast]').onclick = () => broadcastSound(id, title);
    el.querySelector('[data-act=hide]').onclick = () => toggleSoundHidden(id, !hidden);
    el.querySelector('[data-act=del]').onclick = () => deleteSound(id, title);
  });
}
async function uploadSound(){
  const inp = $('#sbfile');
  const f = inp.files && inp.files[0];
  if (!f) return toast('Выберите аудиофайл', 'err');
  const fd = new FormData();
  fd.append('file', f, f.name);
  const btn = $('#btn-sb-upload'); btn.disabled = true; btn.textContent = 'Загрузка…';
  try{
    const r = await api('/api/soundboard/upload', { method:'POST', body: fd,
                                                    headers:{ 'X-CSRF-Token': CSRF } });
    if (r && r.ok){
      toast('Звук добавлен: '+((r.body.item && r.body.item.title) || f.name), 'ok');
      inp.value = ''; refreshSoundboard();
    } else toast('Ошибка загрузки: '+errorMessage(r), 'err');
  } finally { btn.disabled = false; btn.textContent = 'Загрузить'; }
}
async function broadcastSound(id, title){
  const cams = await getJSON('/api/cameras');
  const all = ((cams&&cams.body&&cams.body.cameras)||[]);
  const online = all.filter(c=>c.online);
  // сервер шлёт команды по мягкому порогу (command_stale_after), поэтому
  // короткая потеря кадров не должна блокировать трансляцию звука
  const list = online.length ? online : all;
  if (!list.length) return toast('Нет клиентов — некому транслировать', 'err');
  let clientId = null;
  if (list.length > 1){
    const v = prompt('Транслировать «'+title+'» клиенту (пусто = всем в эфире):\n'+
                     list.map(c=>c.client_id).join(', '), '');
    if (v === null) return;
    clientId = v.trim() || null;
  }
  const r = await postJSON('/api/soundboard/'+encodeURIComponent(id)+'/broadcast',
                           { client_id: clientId });
  if (r && r.ok) toast('📢 Отправлено клиентам: '+(r.body.sent||0), 'ok');
  else toast(errorMessage(r), 'err');
}
async function toggleSoundHidden(id, hidden){
  const r = await postJSON('/api/soundboard/'+encodeURIComponent(id)+'/hide', { hidden: hidden });
  if (r && r.ok) refreshSoundboard();
  else toast(errorMessage(r), 'err');
}
async function deleteSound(id, title){
  if (!confirm('Удалить звук «'+title+'»?')) return;
  const r = await api('/api/soundboard/'+encodeURIComponent(id), { method:'DELETE' });
  if (r && r.ok) toast('Удалено', 'ok');
  else toast(errorMessage(r), 'err');
  refreshSoundboard();
}

/* ---------- НАСТРОЙКИ ---------- */
function renderSettings(desc){
  S.spec = desc.spec; S.lastSettings = Object.assign({}, desc.settings);
  const groups = {};
  Object.keys(desc.spec).forEach(k => { const g = desc.spec[k].group||'other';
    (groups[g] = groups[g]||[]).push(k); });
  S.hiddenSpec = Object.keys(desc.spec).filter(k => desc.spec[k].hidden);
  const order = Object.keys(desc.groups||{});
  Object.keys(groups).forEach(g => { if (!order.includes(g)) order.push(g); });
  $('#settings-form').innerHTML = order.map(g => {
    const keys = (groups[g]||[]).filter(k => !(desc.spec[k]||{}).hidden);
    if (!keys.length) return '';
    return '<div class="sgroup"><h3>'+esc((desc.groups&&desc.groups[g])||g)+'</h3>' +
      keys.map(k => fieldHTML(k, desc.spec[k], desc.settings[k])).join('') + '</div>';
  }).join('');
  $$('#settings-form input,#settings-form select,#settings-form textarea').forEach(i =>
    i.addEventListener('input', ()=>{ S.dirty = true; }));
}
function fieldHTML(key, spec, value){
  const id = 'set-'+key;
  let input = '';
  if (spec.type === 'bool'){
    return '<div class="switch"><input type="checkbox" id="'+id+'" data-key="'+key+'"'+(value?' checked':'')+'>' +
           '<span>'+esc(spec.label)+'</span></div><div class="hint">'+esc(spec.help||'')+'</div>';
  } else if (spec.type === 'choice'){
    input = '<select id="'+id+'" data-key="'+key+'">' +
      (spec.choices||[]).map(c => '<option value="'+esc(c)+'"'+(c===value?' selected':'')+'>'+esc(c)+'</option>').join('') + '</select>';
  } else if (spec.type === 'text'){
    // многострочный пул шаблонов (фразы приветствия): строка = вариант фразы
    input = '<textarea id="'+id+'" data-key="'+key+'" rows="'+(spec.rows||5)+
            '" spellcheck="false" placeholder="Одна фраза на строку, {name} — имя">'+
            esc(value===null?'':value)+'</textarea>';
  } else if (spec.type === 'str' || spec.type === 'nullstr'){
    input = '<input type="text" id="'+id+'" data-key="'+key+'" value="'+escAttr(value===null?'':value)+'" maxlength="'+(spec.maxlen||200)+'">';
  } else {
    const v = (value===null||value===undefined) ? '' : value;
    input = '<input type="number" id="'+id+'" data-key="'+key+'" value="'+escAttr(v)+'"' +
      (spec.min!==undefined?' min="'+spec.min+'"':'') + (spec.max!==undefined?' max="'+spec.max+'"':'') +
      (spec.step!==undefined?' step="'+spec.step+'"':'') + ' placeholder="'+(spec.type==='nullint'?'авто':'')+'">';
  }
  return '<label for="'+id+'">'+esc(spec.label)+'</label>' + input +
         '<div class="hint">'+esc(spec.help||'')+'</div><div class="field-err" data-err="'+key+'"></div>';
}
function collectSettings(){
  const out = {};
  $$('#settings-form [data-key]').forEach(el => {
    const k = el.dataset.key, spec = S.spec[k];
    if (spec.type === 'bool'){ out[k] = el.checked; return; }
    let v = el.value;
    if (spec.type === 'text'){
      // строки → пул шаблонов; пустые строки и дубликаты отбрасывает сервер
      v = String(v).split('\n').map(x => x.trim()).filter(x => x).join('\n');
      out[k] = v; return;
    }
    if (spec.type === 'int' || spec.type === 'float'){ v = v === '' ? null : Number(v); }
    if ((spec.type === 'nullint' || spec.type === 'nullstr') && v === '') v = null;
    out[k] = v;
  });
  return out;
}
async function saveSettings(){
  const patch = collectSettings();
  const btn = $('#btn-save-settings'); btn.disabled = true;
  const r = await postJSON('/api/settings', { settings: patch });
  btn.disabled = false;
  if (!r) return;
  $$('#settings-form .field-err').forEach(e => e.textContent = '');
  if (r.body && r.body.errors) Object.keys(r.body.errors).forEach(k => {
    const e = document.querySelector('#settings-form [data-err="'+k+'"]'); if (e) e.textContent = r.body.errors[k];
  });
  if (r.ok){
    S.dirty = false;
    const applied = Object.keys(r.body.applied||{});
    toast(applied.length ? 'Сохранено: '+applied.join(', ') : 'Изменений нет', 'ok');
    if (r.body.voice_rebuild) toast('Фразы/голос изменены — озвучка пересинтезируется для всех ('+r.body.voice_rebuild+' чел.)', 'ok');
    refreshSettings(); refreshStatus();
  } else toast('Не сохранено: '+errorMessage(r), 'err');
}
async function refreshSettings(){
  const r = await getJSON('/api/settings');
  if (r && r.ok) renderSettings(r.body);
  const info = await getJSON('/api/voice-info');
  if (info && info.ok){
    const v = info.body;
    $('#voice-info').innerHTML = '<table class="mini">' +
      '<tr><td>Готовых фраз</td><td>'+v.ready+' из '+v.total+' чел.</td></tr>' +
      '<tr><td>Фраз в пуле приветствия</td><td>'+(v.variants||1)+
        (v.templates && v.templates.length>1 ? ' <span style="color:var(--muted)">(при распознавании чередуются)</span>' : '')+'</td></tr>' +
      '<tr><td>В очереди синтеза</td><td>'+v.pending+'</td></tr>' +
      '<tr><td>С ошибками</td><td>'+v.errors+'</td></tr>' +
      '<tr><td>Файлов в кэше</td><td>'+v.files+' ('+(v.bytes/1024).toFixed(0)+' КБ)</td></tr>' +
      '<tr><td>Движок</td><td>'+esc(v.engine||'?')+(v.engine_pref&&v.engine_pref!=='auto'?' <span class="pill">выбран: '+esc(v.engine_pref)+'</span>':'')+'</td></tr>' +
      (v.available_engines ? '<tr><td>Доступны в системе</td><td>'+(Object.keys(v.available_engines).filter(k=>v.available_engines[k]).join(', ')||'—')+'</td></tr>' : '') +
      '<tr><td>Каталог</td><td>'+esc(v.dir||'')+'</td></tr></table>' +
      (v.error_list && v.error_list.length ? '<div class="hint" style="color:var(--err)">Ошибки: '+esc(v.error_list.join('; '))+'</div>' : '');
  }
}
async function rebuildVoices(){
  if (!confirm('Пересинтезировать озвучку для всех людей?')) return;
  const r = await postJSON('/api/voice-info/rebuild');
  toast(r&&r.ok ? 'Пересинтез запущен ('+r.body.queued+' чел.)' : 'Ошибка', r&&r.ok?'ok':'err');
  setTimeout(refreshSettings, 1500);
}

/* ---------- ЖУРНАЛ ---------- */
function appendLog(el, items){
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
  items.forEach(i => {
    const d = document.createElement('div');
    d.className = 'lv-'+i.level;
    d.textContent = new Date(i.ts*1000).toLocaleTimeString()+' ['+i.level+'] '+i.name+': '+i.message;
    el.appendChild(d);
  });
  while (el.children.length > 800) el.removeChild(el.firstChild);
  if (atBottom || !S.logPaused) el.scrollTop = el.scrollHeight;
}
async function refreshLogs(reset){
  if (S.logPaused && !reset) return;
  const el = $('#loglines');
  if (reset){ el.innerHTML=''; S.logSeq = 0; }
  const r = await getJSON('/api/logs?since='+S.logSeq+'&limit=300');
  if (!r || !r.ok) return;
  const lvl = S.logLevel;
  const order = { DEBUG:10, INFO:20, WARNING:30, ERROR:40, CRITICAL:50 };
  const items = (r.body.lines||[]).filter(i => (order[i.level]||0) >= (order[lvl]||20));
  S.logSeq = r.body.next || S.logSeq;
  appendLog(el, items);
}
async function refreshEvents(reset){
  const el = $('#evlines');
  if (reset){ el.innerHTML=''; S.evSeq = 0; }
  const r = await getJSON('/api/events?since='+S.evSeq+'&limit=200');
  if (!r || !r.ok) return;
  S.evSeq = r.body.next || S.evSeq;
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
  (r.body.events||[]).forEach(e => {
    const d = document.createElement('div'); d.className = 'ev';
    let text = e.type;
    if (e.type === 'greet') text = '✅ '+e.name+' — «'+e.text+'»'+(e.audio?' 🔊':' (без аудио)')+' → '+e.client_id;
    else if (e.type === 'unknown') text = '❌ неизвестное лицо (dist '+e.distance.toFixed(3)+') — '+e.client_id;
    else if (e.type === 'throttled') text = '⏳ '+e.name+' — кулдаун, приветствие пропущено';
    else if (e.type === 'client_gone') text = '🔌 клиент отключился: '+e.client_id;
    else if (e.type === 'announce') text = '📢 объявление → '+(e.client_id||'всем')+': «'+e.text+'»';
    else if (e.type === 'sound_broadcast') text = '🔊 саундборд → '+(e.client_id||'всем')+': «'+e.text+'»';
    else if (e.type === 'person_added') text = '➕ добавлен человек: '+e.text+(e.source==='frame'?' (из кадра)':'');
    else if (e.text) text = e.type+': '+e.text;
    d.innerHTML = '<span class="t">'+fmtTime(e.ts)+'</span><span>'+esc(text)+'</span>';
    el.appendChild(d);
  });
  while (el.children.length > 500) el.removeChild(el.firstChild);
  if (atBottom || !S.logPaused) el.scrollTop = el.scrollHeight;
  $('#ev-empty').style.display = el.children.length ? 'none' : '';
}

/* ---------- пароль ---------- */
async function changePassword(){
  const oldp = $('#old-pass').value, newp = $('#new-pass').value, new2 = $('#new-pass2').value;
  const err = $('#pass-err'); err.textContent = '';
  if (newp !== new2){ err.textContent = 'Пароли не совпадают'; return; }
  const r = await postJSON('/api/auth/password', { old_password: oldp, new_password: newp });
  if (r && r.ok){ $('#pass-dialog').close(); toast('Пароль изменён', 'ok');
    ['#old-pass','#new-pass','#new-pass2'].forEach(s=>$(s).value=''); $('#warnbar') && ($('#warnbar').style.display='none'); }
  else err.textContent = (r&&r.body&&r.body.error) || 'Ошибка';
}

/* ---------- старт ---------- */
function boot(){
  $$('nav button').forEach(b => b.onclick = () => switchTab(b.dataset.tab));
  $('#btn-add').onclick = uploadPerson;
  $('#btn-save-settings').onclick = saveSettings;
  $('#btn-rebuild-voices').onclick = rebuildVoices;
  $('#btn-recog').onclick = () => toggleSetting('recognition_enabled');
  $('#btn-greeting').onclick = () => toggleSetting('greeting_enabled');
  $('#btn-reset-throttle').onclick = async () => { const r = await postJSON('/api/throttle/reset');
      toast(r&&r.ok ? 'Кулдауны сброшены' : 'Ошибка', 'ok'); };
  $('#btn-announce-all').onclick = () => announce(null);
  $('#announce-ok').onclick = submitAnnounce;
  $('#announce-cancel').onclick = () => $('#announce-dialog').close();
  $('#announce-text').addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter'){ e.preventDefault(); submitAnnounce(); } });
  $('#btn-refresh-cams').onclick = refreshCameras;
  $('#log-level').onchange = e => { S.logLevel = e.target.value; refreshLogs(true); };
  $('#btn-log-pause').onclick = e => { S.logPaused = !S.logPaused;
      e.target.textContent = S.logPaused ? '▶ продолжить' : '⏸ пауза';
      e.target.classList.toggle('on', S.logPaused); };
  $('#btn-clear-log').onclick = () => { $('#loglines').innerHTML=''; };
  $('#btn-pass').onclick = () => $('#pass-dialog').showModal();
  $('#btn-pass-cancel').onclick = () => $('#pass-dialog').close();
  $('#btn-pass-ok').onclick = changePassword;
  $('#pfiles').onchange = e => { $('#pcount').textContent = e.target.files.length ? ' выбрано: '+e.target.files.length : ''; };
  $('#btn-sb-upload').onclick = uploadSound;
  $('#btn-sb-refresh').onclick = refreshSoundboard;
  $('#sb-show-hidden').onchange = e => { SB.showHidden = e.target.checked; refreshSoundboard(); };
  $('#face-ok').onclick = submitFaceFromFrame;
  $('#face-cancel').onclick = () => $('#face-dialog').close();
  $('#face-name').addEventListener('keydown', e => { if (e.key === 'Enter'){ e.preventDefault(); submitFaceFromFrame(); } });
  S.timers.push(setInterval(refreshStatus, 3000));
  S.timers.push(setInterval(()=>{ if (S.tab==='cameras') refreshCameras(); }, 1200));
  S.timers.push(setInterval(()=>{ if (S.tab==='logs' && !S.logPaused){ refreshLogs(); refreshEvents(); } }, 1500));
  S.timers.push(setInterval(()=>{ if (S.tab==='people') refreshPersons(); }, 8000));
  window.addEventListener('beforeunload', e => { if (S.dirty){ e.preventDefault(); e.returnValue=''; } });
  S.lastSettings = Object.assign({}, S.boot.settings || {});
  switchTab(['cameras','people','soundboard','settings','logs'].includes(S.tab) ? S.tab : 'cameras');
  refreshStatus();
  if (S.boot.must_change_password) toast('Смените пароль по умолчанию (кнопка «пароль» справа вверху)', 'err');
}
document.addEventListener('DOMContentLoaded', boot);
"""

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — админ-панель распознавания лиц</title>
<link rel="icon" href="%%FAVICON%%">
<style>%%CSS%%</style>
</head>
<body>
<div class="login">
  <form class="box" method="post" action="/login">
    <h1>Админ-панель</h1>
    <div class="sub">Система распознавания лиц · v%%VERSION%%</div>
    <input type="hidden" name="csrf_token" value="%%CSRF%%">
    <input type="hidden" name="next" value="%%NEXT%%">
    <label for="username">Пользователь</label>
    <input type="text" id="username" name="username" autocomplete="username" autofocus required>
    <label for="password">Пароль</label>
    <input type="password" id="password" name="password" autocomplete="current-password" required>
    <p class="err-text">%%ERROR%%</p>
    <button type="submit" style="width:100%">Войти</button>
    <div class="hint" style="margin-top:14px">
      По умолчанию <code>admin</code> / <code>admin</code> — смените пароль после первого входа.
      Задать свои учётные данные можно переменными <code>ADMIN_USER</code> / <code>ADMIN_PASSWORD</code>
      или аргументами <code>--admin-user</code> / <code>--admin-password</code>.
    </div>
  </form>
</div>
</body>
</html>"""

ADMIN_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Админ-панель — распознавание лиц</title>
<link rel="icon" href="%%FAVICON%%">
<meta id="csrf" name="csrf-token" content="%%CSRF%%">
<style>%%CSS%%</style>
</head>
<body>
<script id="boot" type="application/json">%%BOOT%%</script>

<header>
  <div class="hwrap">
    <div class="brand"><span class="dot" id="dot"></span> Распознавание лиц</div>
    <nav>
      <button data-tab="cameras">Камеры</button>
      <button data-tab="people">Люди</button>
      <button data-tab="soundboard">Саундборд</button>
      <button data-tab="settings">Настройки</button>
      <button data-tab="logs">Журнал</button>
    </nav>
    <div class="hstats">
      <span>аптайм <b id="st-uptime">—</b></span>
      <span title="клиентов онлайн сейчас (всего зарегистрировано: —)" id="st-clients-wrap">клиентов <b id="st-clients">—</b></span>
      <span title="сколько лиц видно в кадре прямо сейчас">лиц в кадре <b id="st-faces">0</b></span>
      <span title="сколько раз сервер поздоровался с момента запуска">приветствий <b id="st-greets">0</b></span>
      <button class="ghost" id="btn-pass">%%USERNAME%% · пароль</button>
      <form method="post" action="/logout" style="display:inline;margin:0">
        <input type="hidden" name="csrf_token" value="%%CSRF%%">
        <button class="ghost" type="submit">выйти</button>
      </form>
    </div>
  </div>
</header>

<div class="wrap">
  %%WARNBAR%%

  <!-- ===== КАМЕРЫ ===== -->
  <section class="tabpane" id="tab-cameras">
    <div class="card">
      <div class="toolbar">
        <b style="font-size:15px">Видео с камер в реальном времени</b>
        <span class="spacer" style="margin-left:auto"></span>
        <button class="ghost" id="btn-recog">Распознавание</button>
        <button class="ghost" id="btn-greeting">Приветствия</button>
        <button class="ghost" id="btn-reset-throttle">сбросить кулдауны</button>
        <button class="ghost" id="btn-announce-all">объявить всем</button>
        <button class="ghost" id="btn-refresh-cams">обновить</button>
      </div>
      <div class="hint" style="margin:-4px 0 12px">
        Окно OpenCV на сервере больше не открывается — весь видеопоток с камер виден здесь.
        Рамки и имена рисуются поверх потока браузером, снимок «на сервер» сохраняет кадр
        с разметкой в <code>debug_frames/</code>.
      </div>
      <div id="camgrid" class="camgrid"></div>
      <div id="cam-empty" class="empty" style="display:none">
        Пока ни одной камеры.<br>
        Запустите клиент на машине с камерой:<br>
        <code>python client.py --server-ip &lt;адрес этого сервера&gt;</code><br>
        Или проверьте сервер без камеры: <code>python tools/e2e_check.py --image known_faces/фото.jpg</code>
      </div>
    </div>
  </section>

  <!-- ===== ЛЮДИ ===== -->
  <section class="tabpane" id="tab-people" style="display:none">
    <div class="card">
      <h2>Добавить человека</h2>
      <div class="row">
        <div class="col">
          <label for="pname">Имя</label>
          <input type="text" id="pname" placeholder="Например: Иван Петров" maxlength="64">
        </div>
        <div class="col">
          <label for="pfiles">Фотографии (лицо крупно, можно несколько ракурсов)</label>
          <input type="file" id="pfiles" accept="image/jpeg,image/png" multiple>
          <div class="hint" id="pcount"></div>
        </div>
      </div>
      <button id="btn-add" style="margin-top:14px">Добавить</button>
      <div class="hint">Эмбеддинг попадает в распознавание сразу, без перезапуска сервера.
        Озвучка синтезируется в фоне для <b>всех</b> фраз из пула (вкладка «Настройки» →
        «Фразы приветствия») — статус и число готовых вариантов видны на карточке.</div>
    </div>
    <div class="card">
      <h2>Известные лица</h2>
      <div class="hint" style="margin:-6px 0 10px">
        Новое лицо можно добавить не только файлом, но и <b>прямо из текущего кадра камеры</b>:
        на вкладке «Камеры» нажмите «＋ лицо из кадра» или «+» на чипе лица.
      </div>
      <div id="persons" class="grid"></div>
      <div id="persons-empty" class="empty" style="display:none">Пока никого нет</div>
    </div>
  </section>

  <!-- ===== САУНДБОРД ===== -->
  <section class="tabpane" id="tab-soundboard" style="display:none">
    <div class="card">
      <div class="toolbar">
        <b style="font-size:15px">Саундборд — записанные звуки</b>
        <span class="spacer" style="margin-left:auto"></span>
        <label style="display:flex;align-items:center;gap:6px;margin:0;color:var(--muted);font-size:12px;cursor:pointer">
          <input type="checkbox" id="sb-show-hidden" style="width:auto"> показывать скрытые
        </label>
        <button class="ghost" id="btn-sb-refresh">обновить</button>
      </div>
      <div class="row" style="align-items:flex-end">
        <div class="col">
          <label for="sbfile">Загрузить звук (WAV, MP3, OGG, FLAC, M4A — до 25 МБ)</label>
          <input type="file" id="sbfile" accept=".wav,.mp3,.ogg,.flac,.m4a,audio/*">
        </div>
        <div class="col" style="flex:0 0 auto">
          <button id="btn-sb-upload">Загрузить</button>
        </div>
      </div>
      <div class="hint" style="margin-top:8px">
        ▶ — воспроизведение в браузере, «📢 клиентам» — трансляция звука на колонки клиентов
        (не-WAV форматы сервер конвертирует через ffmpeg). Синтезированные приветствия и
        объявления попадают сюда автоматически. Любой звук можно <b>скрыть</b> — он пропадёт
        из списка, но останется на диске (вернуть — галочка «показывать скрытые»).
      </div>
      <div id="sbgrid" class="sbgrid" style="margin-top:12px"></div>
      <div id="sb-empty" class="empty" style="display:none">
        Звуков пока нет.<br>Загрузите файл или добавьте человека — его приветствие появится здесь.
      </div>
    </div>
  </section>

  <!-- ===== НАСТРОЙКИ ===== -->
  <section class="tabpane" id="tab-settings" style="display:none">
    <div class="card">
      <div class="toolbar">
        <b style="font-size:15px">Параметры сервера</b>
        <span class="spacer" style="margin-left:auto"></span>
        <button class="ghost" id="btn-rebuild-voices">пересинтезировать всю озвучку</button>
        <button id="btn-save-settings">Сохранить</button>
      </div>
      <div class="hint" style="margin:-4px 0 14px">
        Применяется мгновенно, без перезапуска сервера, и сохраняется в
        <code>admin_data/settings.json</code>.
      </div>
      <div id="settings-form" class="settings-grid"></div>
    </div>
    <div class="card">
      <h2>Озвучка</h2>
      <div id="voice-info" class="hint">загрузка…</div>
    </div>
  </section>

  <!-- ===== ЖУРНАЛ ===== -->
  <section class="tabpane" id="tab-logs" style="display:none">
    <div class="card">
      <div class="toolbar">
        <b style="font-size:15px">События распознавания</b>
        <span class="spacer" style="margin-left:auto"></span>
        <button class="ghost" id="btn-refresh-events" onclick="refreshEvents(true)">обновить</button>
      </div>
      <div id="evlines" class="logbox" style="max-height:32vh"></div>
      <div id="ev-empty" class="empty">Событий пока нет</div>
    </div>
    <div class="card">
      <div class="toolbar">
        <b style="font-size:15px">Лог сервера</b>
        <select id="log-level" style="width:auto;padding:6px 10px">
          <option value="DEBUG">DEBUG</option>
          <option value="INFO" selected>INFO</option>
          <option value="WARNING">WARNING</option>
          <option value="ERROR">ERROR</option>
        </select>
        <button class="ghost" id="btn-log-pause">⏸ пауза</button>
        <button class="ghost" id="btn-clear-log">очистить</button>
      </div>
      <div id="loglines" class="logbox"></div>
    </div>
  </section>
</div>

<div class="toast" id="toasts"></div>

<dialog id="pass-dialog">
  <h2 style="margin:0 0 12px;font-size:16px">Смена пароля</h2>
  <label for="old-pass">Текущий пароль</label>
  <input type="password" id="old-pass" autocomplete="current-password">
  <label for="new-pass">Новый пароль</label>
  <input type="password" id="new-pass" autocomplete="new-password">
  <label for="new-pass2">Повторите новый пароль</label>
  <input type="password" id="new-pass2" autocomplete="new-password">
  <p class="err-text" id="pass-err"></p>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
    <button class="ghost" id="btn-pass-cancel">Отмена</button>
    <button id="btn-pass-ok">Сохранить</button>
  </div>
</dialog>

<dialog id="announce-dialog">
  <h2 style="margin:0 0 4px;font-size:16px">Объявление клиентам</h2>
  <div class="hint" style="margin-bottom:10px">
    Адресат: <b id="announce-target">всем клиентам в эфире</b>.
    Фраза будет синтезирована один раз и переиспользована из кэша при повторе.
  </div>
  <label for="announce-text">Текст объявления</label>
  <textarea id="announce-text" rows="4" maxlength="400"
            placeholder="Внимание, через 5 минут обед"></textarea>
  <div class="hint">Ctrl+Enter — отправить. Если озвучка ещё не готова, команда
    уйдёт клиентам сразу после синтеза (без «пустого» сообщения).</div>
  <p class="err-text" id="announce-err"></p>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px">
    <button class="ghost" id="announce-cancel">Отмена</button>
    <button id="announce-ok">Объявить</button>
  </div>
</dialog>

<dialog id="face-dialog" class="wide">
  <h2 style="margin:0 0 4px;font-size:16px">Новое лицо из кадра камеры</h2>
  <div class="hint" style="margin-bottom:10px">
    Камера: <b id="face-cam"></b> — лицо будет вырезано из текущего кадра и станет эталоном.
    Если лиц несколько — выберите нужное миниатюрой.
  </div>
  <div id="face-list"></div>
  <img id="face-preview" alt="Превью лица">
  <label for="face-name">Имя человека</label>
  <input type="text" id="face-name" maxlength="64" placeholder="Например: Иван Петров" autocomplete="off">
  <div class="hint">Если имя уже существует — добавится фото нового ракурса (это повышает
    точность распознавания). Озвучка приветствия синтезируется автоматически.</div>
  <p class="err-text" id="face-err"></p>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px">
    <button class="ghost" id="face-cancel">Отмена</button>
    <button id="face-ok">Добавить</button>
  </div>
</dialog>

<script>%%JS%%</script>
</body>
</html>"""

CAMERA_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%CLIENT%% — камера</title>
<link rel="icon" href="%%FAVICON%%">
<style>%%CSS%%
  .full { max-width:1600px; }
  .stage { aspect-ratio:auto; height:calc(100vh - 170px); }
</style>
</head>
<body>
<header><div class="hwrap">
  <div class="brand"><span class="dot"></span> %%CLIENT%%</div>
  <div class="hstats">
    <span id="info" style="color:var(--muted);font-size:12px"></span>
    <a class="ghost" href="/" style="text-decoration:none;padding:6px 11px;border:1px solid var(--line);border-radius:8px;color:var(--muted);font-size:12px">← к панели</a>
  </div>
</div></header>
<div class="wrap full">
  <div class="cam">
    <div class="stage"><img class="feed" src="/stream/%%CLIENT_ENC%%" alt="">
      <div class="boxes" id="boxes"></div>
      <div class="nosignal" id="nosignal" style="display:none">НЕТ СИГНАЛА</div></div>
  </div>
</div>
<script>
async function tick(){
  try {
    const r = await fetch('/api/cameras/%%CLIENT_ENC%%');
    const c = await r.json();
    document.getElementById('nosignal').style.display = c.online ? 'none' : '';
    document.getElementById('info').textContent =
      (c.width||'?')+'×'+(c.height||'?')+' · '+(c.fps||0)+' fps · лиц: '+(c.faces||0)+
      ' · кадров: '+(c.frames||0);
    const b = document.getElementById('boxes');
    b.innerHTML = (c.annotations||[]).map(a => {
      const l=a.left/c.width*100, t=a.top/c.height*100;
      const w=(a.right-a.left)/c.width*100, h=(a.bottom-a.top)/c.height*100;
      return '<div class="box '+(a.known?'known':'')+'" style="left:'+l+'%;top:'+t+'%;width:'+w+'%;height:'+h+'%">'+
             (a.label?'<span>'+a.label.replace(/[<>&]/g,'')+'</span>':'')+'</div>';
    }).join('');
  } catch(e){}
}
setInterval(tick, 1000); tick();
</script>
</body>
</html>"""


def render(template, **values):
    out = template
    for key, value in values.items():
        out = out.replace("%%" + key + "%%", "" if value is None else str(value))
    return out


def boot_json(**data):
    """JSON для тега <script type="application/json"> — экранируем '</' от закрытия тега."""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
