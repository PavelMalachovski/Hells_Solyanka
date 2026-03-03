// Telegram Web App initialization
const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor('#0d0806');
  tg.setBackgroundColor('#0d0806');
}

// API base — served from the same origin via aiohttp
const API = '/api';

// ─── Tab switching ───
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.querySelector(`.tab[data-tab="${name}"]`).classList.add('active');
  document.getElementById(`tab-${name}`).classList.add('active');

  if (name === 'stats') loadStats();
  if (name === 'packs') loadPacks();
}

// ─── Toast notifications ───
function showToast(msg, isError = false) {
  let toast = document.querySelector('.toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.classList.toggle('error', isError);
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 2500);
}

// ─── API helpers ───
async function apiFetch(path) {
  try {
    const headers = {};
    if (tg?.initData) headers['X-Telegram-Init-Data'] = tg.initData;
    const res = await fetch(API + path, { headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (e) {
    console.error('API error:', e);
    showToast('Ошибка загрузки', true);
    return null;
  }
}

async function apiPost(path, body = {}) {
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (tg?.initData) headers['X-Telegram-Init-Data'] = tg.initData;
    const res = await fetch(API + path, { method: 'POST', headers, body: JSON.stringify(body) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (e) {
    console.error('API error:', e);
    showToast('Ошибка', true);
    return null;
  }
}

// ─── Load stats ───
async function loadStats() {
  const data = await apiFetch('/stats');
  if (!data) return;
  document.getElementById('stat-total').textContent = data.total ?? '—';
  document.getElementById('stat-sent').textContent = data.sent ?? '—';
  document.getElementById('stat-pending').textContent = data.pending ?? '—';
  document.getElementById('stat-packs').textContent = data.packs ?? '—';
}

// ─── Load packs ───
async function loadPacks() {
  const container = document.getElementById('packs-list');
  container.innerHTML = '<div class="loading">Загрузка...</div>';
  const data = await apiFetch('/packs');
  if (!data || !data.packs) {
    container.innerHTML = '<div class="loading">Нет данных</div>';
    return;
  }
  container.innerHTML = '';
  data.packs.forEach(pack => {
    const card = document.createElement('div');
    card.className = 'pack-card';
    card.innerHTML = `
      <div>
        <div class="pack-name">${escapeHtml(pack.name)}</div>
        <div class="pack-info">${pack.sent}/${pack.total} отправлено</div>
      </div>
      <div class="pack-count">${pack.total}</div>
    `;
    card.onclick = () => toggleQuestions(card, pack.name);
    container.appendChild(card);
  });
}

// ─── Toggle questions in pack ───
async function toggleQuestions(card, packName) {
  let qs = card.querySelector('.pack-questions');
  if (qs) {
    qs.classList.toggle('open');
    return;
  }
  const data = await apiFetch('/pack/' + encodeURIComponent(packName));
  if (!data || !data.questions) return;

  qs = document.createElement('div');
  qs.className = 'pack-questions open';
  data.questions.forEach(q => {
    const qCard = document.createElement('div');
    qCard.className = 'q-card';
    qCard.innerHTML = `
      <div class="q-number">Вопрос ${q.number}</div>
      <div class="q-text">${escapeHtml(q.text || '—')}</div>
      ${q.answer ? `<div class="q-answer">Ответ: ${escapeHtml(q.answer)}</div>` : ''}
      <span class="q-sent ${q.is_sent ? 'yes' : 'no'}">${q.is_sent ? '✓ Отправлен' : '○ Ожидает'}</span>
    `;
    qs.appendChild(qCard);
  });
  card.appendChild(qs);
}

// ─── Send admin command ───
async function sendCommand(command) {
  const btn = event.currentTarget;
  btn.classList.add('sending');
  const data = await apiPost('/command', { command });
  btn.classList.remove('sending');
  if (data?.ok) {
    showToast(data.message || 'Выполнено ✓');
    if (command === '/send_now') loadStats();
  } else {
    showToast(data?.message || 'Ошибка выполнения', true);
  }
}

// ─── Confirm clear ───
function confirmClear() {
  if (tg?.showConfirm) {
    tg.showConfirm('Очистить всю базу вопросов?', (ok) => {
      if (ok) sendCommand('/clear');
    });
  } else if (confirm('Очистить всю базу вопросов?')) {
    sendCommand('/clear');
  }
}

// ─── Utility ───
function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

// ─── Init ───
document.addEventListener('DOMContentLoaded', () => {
  loadStats();
});
