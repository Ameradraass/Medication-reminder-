import { firebaseConfig } from './firebase-config.js';

const $ = s => document.querySelector(s);
const load = (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d } catch { return d } };
const save = (k, v) => localStorage.setItem(k, JSON.stringify(v));
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const MONTHS = ['كانون الثاني','شباط','آذار','نيسان','أيار','حزيران','تموز','آب','أيلول','تشرين الأول','تشرين الثاني','كانون الأول'];
const MISSED_AFTER = 30;   // دقائق: بعدها تُعتبر الجرعة فائتة ويُبلَّغ المرافق
const SNOOZE_MIN = 10;
const LOW_DAYS = 2;        // التنبيه قبل يومين من نفاد العلبة

let meds = load('meds', []), log = load('log', {}), link = load('link', null);
const snooze = {};
let ringing = null, ringTimer = null, db = null, fs = null, unsub = null;

/* ---------- الأصوات (مولّدة بالكود، لا حاجة لملفات) ---------- */
const SOUNDS = {
  bell:   { name: 'جرس',        type: 'sine',     seq: [[880,.15],[1175,.15],[880,.15],[1175,.45]] },
  beep:   { name: 'صفير متقطع', type: 'square',   seq: [[1000,.12],[0,.08],[1000,.12],[0,.08],[1000,.12]] },
  melody: { name: 'نغمة هادئة', type: 'triangle', seq: [[523,.2],[659,.2],[784,.2],[1047,.4]] },
  siren:  { name: 'سارينة',     type: 'sawtooth', seq: [[600,.3],[900,.3],[600,.3],[900,.3]] },
  chime:  { name: 'رنين عالٍ',  type: 'sine',     seq: [[1568,.1],[1318,.1],[1568,.1],[1318,.1],[1568,.3]] }
};
let ctx;
function unlockAudio() { ctx = ctx || new (window.AudioContext || window.webkitAudioContext)(); if (ctx.state === 'suspended') ctx.resume(); }
function playOnce(id) {
  if (!ctx) return;
  const s = SOUNDS[id] || SOUNDS.bell; let t = ctx.currentTime;
  for (const [f, d] of s.seq) {
    if (f) {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = s.type; o.frequency.value = f;
      g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(.4, t + .02); g.gain.exponentialRampToValueAtTime(.0001, t + d);
      o.connect(g).connect(ctx.destination); o.start(t); o.stop(t + d + .02);
    }
    t += d + .03;
  }
}

/* ---------- الإشعارات ---------- */
async function notify(title, body) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  const reg = await navigator.serviceWorker?.getRegistration();
  if (reg) reg.showNotification(title, { body, icon: 'icon.svg', vibrate: [300, 100, 300, 100, 300], tag: title + body });
  else new Notification(title, { body });
}

/* ---------- Firebase (للربط مع هاتف آخر) ---------- */
async function initFirebase() {
  if (!firebaseConfig.apiKey || firebaseConfig.apiKey.startsWith('YOUR')) return;
  try {
    const a = await import('https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js');
    fs = await import('https://www.gstatic.com/firebasejs/10.12.2/firebase-firestore.js');
    db = fs.getFirestore(a.initializeApp(firebaseConfig));
  } catch (e) { console.warn('Firebase غير متاح', e); }
}
async function pushEvent(e) {
  if (!db || !link || link.role !== 'patient') return;
  try { await fs.addDoc(fs.collection(db, 'links', link.code, 'events'), { ...e, ts: Date.now() }); } catch (err) { console.warn(err); }
}
function listenCaregiver() {
  if (unsub) { unsub(); unsub = null; }
  if (!db || !link || link.role !== 'caregiver') return;
  const q = fs.query(fs.collection(db, 'links', link.code, 'events'), fs.orderBy('ts', 'desc'), fs.limit(50));
  let first = true;
  unsub = fs.onSnapshot(q, snap => {
    renderRemote(snap.docs.map(d => d.data()));
    if (!first) snap.docChanges().forEach(c => { if (c.type === 'added') alertCaregiver(c.doc.data()); });
    first = false;
  });
}
function eventText(e) {
  if (e.type === 'taken') return `✅ أخذ ${e.med} (موعد ${e.time})`;
  if (e.type === 'missed') return `⚠️ لم يأخذ ${e.med} (موعد ${e.time})`;
  if (e.type === 'skipped') return `⏭ تخطّى ${e.med} (موعد ${e.time})`;
  if (e.type === 'lowstock') return `🛒 ${e.med} يوشك على النفاد (يكفي ${e.daysLeft} يوم)`;
  return e.type;
}
function alertCaregiver(e) { unlockAudio(); playOnce(e.type === 'taken' ? 'melody' : 'siren'); notify('مُذكّر الدواء', eventText(e)); }

/* ---------- المنطق ---------- */
const ymd = d => `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
const daysLeft = m => m.stock / (m.perDose * m.times.length);

function record(key, m, t, status) {
  if (log[key]) return;
  log[key] = { status, med: m.name, time: t, ts: Date.now() };
  if (status === 'taken') m.stock = Math.max(0, m.stock - m.perDose);
  save('log', log); save('meds', meds);
  if (ringing && ringing.key === key) stopRing();
  pushEvent({ type: status, med: m.name, time: t });
  if (status === 'missed') notify('جرعة فائتة', `لم تؤكّد تناول ${m.name} (موعد ${t})`);
  renderAll();
}
function startRing(key, m, t) {
  ringing = { key, m, t };
  $('#ringTitle').textContent = `حان موعد ${m.name}`;
  $('#ringSub').textContent = `الساعة ${t} — الجرعة: ${m.perDose} حبة`;
  $('#ringModal').hidden = false;
  notify(`حان موعد ${m.name}`, `الساعة ${t}`);
  const go = () => { playOnce(m.sound); navigator.vibrate?.([400, 150, 400]); };
  go(); ringTimer = setInterval(go, 3000);
}
function stopRing() { clearInterval(ringTimer); ringing = null; $('#ringModal').hidden = true; }

function tick() {
  const now = new Date(), today = ymd(now), mon = now.getMonth() + 1;
  for (const m of meds) {
    if (!m.months.includes(mon)) continue;
    for (const t of m.times) {
      const [h, mi] = t.split(':').map(Number);
      const due = new Date(now.getFullYear(), now.getMonth(), now.getDate(), h, mi);
      if (due < new Date(m.createdAt)) continue;
      const key = `${today}|${m.id}|${t}`;
      if (log[key]) continue;
      const diff = (now - due) / 60000;
      if (diff < 0) continue;
      if (diff > MISSED_AFTER) { record(key, m, t, 'missed'); continue; }
      if (snooze[key] && now < snooze[key]) continue;
      if (!ringing) startRing(key, m, t);
    }
    if (daysLeft(m) <= LOW_DAYS && m.lastLow !== today) {
      m.lastLow = today; save('meds', meds);
      const d = Math.floor(daysLeft(m));
      notify('اشترِ علبة جديدة 🛒', `${m.name} يكفي ${d} يوم فقط`);
      pushEvent({ type: 'lowstock', med: m.name, time: '', daysLeft: d });
      renderAll();
    }
  }
}

/* ---------- الواجهة ---------- */
function renderMeds() {
  $('#medList').innerHTML = meds.map(m => {
    const d = Math.floor(daysLeft(m)), low = daysLeft(m) <= LOW_DAYS;
    return `<div class="card med"><div>
      <b>${esc(m.name)}</b>
      <small>🕒 ${m.times.join(' ، ')}</small>
      <small>📅 ${m.months.length === 12 ? 'كل الأشهر' : m.months.map(x => MONTHS[x - 1]).join('، ')}</small>
      <small>🔊 ${SOUNDS[m.sound].name}</small>
      <small class="${low ? 'low' : ''}">📦 المتبقي ${m.stock} حبة (≈ ${d} يوم)${low ? ' — اشترِ علبة جديدة!' : ''}</small>
    </div><div>
      <button class="ghost" data-restock="${m.id}">تحديث المخزون</button>
      <button class="ghost" data-del="${m.id}">🗑</button></div></div>`;
  }).join('') || '<p style="color:var(--mut)">لا توجد أدوية بعد.</p>';
}
function renderLog() {
  const rows = Object.values(log).sort((a, b) => b.ts - a.ts).slice(0, 100);
  $('#logList').innerHTML = rows.map(r => `<div class="card"><b>${esc(r.med)}</b> — ${r.time}
    <span class="tag ${r.status}">${{ taken: 'أُخذ', missed: 'فائت', skipped: 'تخطّي' }[r.status]}</span>
    <small style="display:block;color:var(--mut)">${new Date(r.ts).toLocaleString('ar')}</small></div>`).join('') || '<p style="color:var(--mut)">السجل فارغ.</p>';
}
function renderLink() {
  const box = $('#linkBox');
  if (!db) { box.innerHTML = '<h2>الربط غير مفعّل</h2><p>أضف إعدادات Firebase في الملف <code>firebase-config.js</code> (راجع README).</p>'; return; }
  if (!link) {
    box.innerHTML = `<h2>ربط مع هاتف آخر</h2>
      <button class="primary" id="mkPatient">أنا المريض — أنشئ رمز ربط</button>
      <hr style="border-color:var(--bd);margin:14px 0">
      <label>أنا المرافق — أدخل رمز المريض<input id="codeIn" maxlength="8" placeholder="XXXXXXXX" style="text-transform:uppercase"></label>
      <button class="primary" id="joinCare">متابعة المريض</button>`;
    $('#mkPatient').onclick = () => setLink({ role: 'patient', code: genCode() });
    $('#joinCare').onclick = () => { const c = $('#codeIn').value.trim().toUpperCase(); if (c.length >= 6) { unlockAudio(); setLink({ role: 'caregiver', code: c }); } };
  } else {
    box.innerHTML = `<h2>${link.role === 'patient' ? 'وضع المريض' : 'وضع المرافق'}</h2>
      <p>رمز الربط: <b style="font-size:22px;letter-spacing:3px">${link.code}</b></p>
      <p style="color:var(--mut)">${link.role === 'patient' ? 'أدخل هذا الرمز في هاتف المرافق. سيصله إشعار عند أخذ الدواء أو نسيانه أو قرب نفاد العلبة.' : 'ستظهر هنا أحداث المريض وتصلك إشعارات فورية (يجب إبقاء التطبيق مفتوحًا).'}</p>
      <button class="danger" id="unlink">إلغاء الربط</button>`;
    $('#unlink').onclick = () => setLink(null);
  }
}
function renderRemote(events) {
  $('#remote').innerHTML = events.map(e => `<div class="card"><span class="${e.type}">${esc(eventText(e))}</span>
    <small style="display:block;color:var(--mut)">${new Date(e.ts).toLocaleString('ar')}</small></div>`).join('');
}
function renderAll() { renderMeds(); renderLog(); renderLink(); }
const genCode = () => Array.from(crypto.getRandomValues(new Uint8Array(8)), b => 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'[b % 32]).join('');
function setLink(l) { link = l; save('link', l); if (!l && unsub) { unsub(); unsub = null; $('#remote').innerHTML = ''; } renderLink(); listenCaregiver(); }

/* ---------- ربط الأحداث ---------- */
$('#months').innerHTML = MONTHS.map((n, i) => `<label><input type="checkbox" value="${i + 1}" checked>${n}</label>`).join('');
$('#sound').innerHTML = Object.entries(SOUNDS).map(([k, v]) => `<option value="${k}">${v.name}</option>`).join('');
$('#preview').onclick = () => { unlockAudio(); playOnce($('#sound').value); };
$('#addTime').onclick = () => { const i = document.createElement('input'); i.type = 'time'; i.value = '20:00'; $('#times').append(i); };
$('#enable').onclick = async () => { unlockAudio(); playOnce('chime'); if ('Notification' in window) await Notification.requestPermission(); $('#enable').textContent = '✅ مفعّل'; };
document.addEventListener('click', unlockAudio, { once: true });

$('#medForm').onsubmit = e => {
  e.preventDefault();
  const times = [...document.querySelectorAll('#times input')].map(i => i.value).filter(Boolean);
  const months = [...document.querySelectorAll('#months input:checked')].map(i => +i.value);
  if (!times.length || !months.length) return alert('اختر وقتًا وشهرًا واحدًا على الأقل');
  meds.push({ id: Date.now().toString(36), name: $('#name').value.trim(), times: [...new Set(times)].sort(), months,
    sound: $('#sound').value, stock: +$('#stock').value || 0, perDose: +$('#perDose').value || 1, createdAt: Date.now() });
  save('meds', meds); e.target.reset();
  $('#times').innerHTML = '<input type="time" value="08:00">';
  document.querySelectorAll('#months input').forEach(i => i.checked = true);
  renderAll(); tick();
};
$('#medList').onclick = e => {
  const d = e.target.dataset.del, r = e.target.dataset.restock;
  if (d && confirm('حذف هذا الدواء؟')) { meds = meds.filter(m => m.id !== d); save('meds', meds); renderAll(); }
  if (r) { const m = meds.find(x => x.id === r), v = prompt('عدد الحبوب المتوفرة الآن:', m.stock); if (v !== null && !isNaN(+v)) { m.stock = +v; m.lastLow = null; save('meds', meds); renderAll(); } }
};
$('#takenBtn').onclick = () => record(ringing.key, ringing.m, ringing.t, 'taken');
$('#skipBtn').onclick = () => record(ringing.key, ringing.m, ringing.t, 'skipped');
$('#snoozeBtn').onclick = () => { snooze[ringing.key] = new Date(Date.now() + SNOOZE_MIN * 60000); stopRing(); };
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.toggle('active', x === b));
  ['meds', 'history', 'link'].forEach(t => $('#tab-' + t).hidden = t !== b.dataset.tab);
});

if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js');
await initFirebase();
renderAll(); listenCaregiver(); tick(); setInterval(tick, 15000);
