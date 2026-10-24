'use strict';
const $ = (id) => document.getElementById(id);
let exiting = false;
let state = {contacts: [], revision: ''}, picked = new Set(), token = '', filter = 'all', current = '', view = 'contacts', busy = false, detailEpoch = 0;
const titles = {contacts: ['联系人', '联系人', '勾选允许回复的联系人。'], memory: ['记忆', '会话记忆', '查看和管理联系人记忆。'], attachments: ['附件', '附件资料', '查看和管理已解析的资料。'], settings: ['说明', '使用说明', '本地配置与使用范围。']};
function node(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function notify(text, error = false) { $('notice').textContent = text; $('notice').className = `notice${error ? ' error' : ''}`; $('notice').hidden = false; }
function dirty() { return state.contacts.some(c => picked.has(c.id) !== c.selected); }
async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token, ...options.headers}, signal: AbortSignal.timeout(10000)});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '输入格式不正确，请检查后重试');
  return data;
}
function accept(data, preserve = false) { const old = picked; state = data; picked = preserve ? new Set([...old].filter(id => data.contacts.some(c => c.id === id))) : new Set(data.contacts.filter(c => c.selected).map(c => c.id)); if (data.csrf_token) token = data.csrf_token; render(); }
function visible() { const q = $('search').value.toLocaleLowerCase().trim(); return state.contacts.filter(c => [c.name, c.conversation, c.sender].some(s => s.toLocaleLowerCase().includes(q)) && (filter === 'all' || picked.has(c.id) === (filter === 'selected'))); }
function render() {
  $('selected-count').textContent = picked.size; $('total-count').textContent = state.contacts.length;
  $('dirty-status').textContent = dirty() ? '未保存' : '已同步';
  $('save-status').textContent = dirty() ? `已选 ${picked.size} 位，待保存` : '选择尚未修改';
  $('save').disabled = !dirty() || busy;
  $('quit-open').disabled = !state.shutdown_available || busy || exiting;
  $('quit-open').title = state.shutdown_available ? '' : '当前启动方式请在运行终端按 Control+C 退出';
  const rows = visible(); $('visible-count').textContent = `显示 ${rows.length} 位联系人`;
  $('select-visible').checked = rows.length > 0 && rows.every(c => picked.has(c.id));
  $('select-visible').indeterminate = rows.some(c => picked.has(c.id)) && !rows.every(c => picked.has(c.id));
  $('select-visible').disabled = !rows.length || busy;
  $('contacts').replaceChildren();
  if (!rows.length) { const empty = node('div', 'empty'); empty.append(node('strong', '', state.contacts.length ? '没有匹配的联系人' : '先添加你的第一位联系人'), node('span', '', state.contacts.length ? '试试其他关键词，或切换筛选。' : '手动录入会话身份后，在这里勾选并保存。')); $('contacts').append(empty); }
  rows.forEach(c => {
    const row = node('div', `contact-row${picked.has(c.id) ? ' selected' : ''}`);
    const person = node('label', 'person'), cb = node('input'); cb.type = 'checkbox'; cb.checked = picked.has(c.id); cb.disabled = busy; cb.setAttribute('aria-label', `允许回复 ${c.name}`);
    cb.addEventListener('change', () => { if (cb.checked) picked.add(c.id); else picked.delete(c.id); render(); });
    const info = node('div'); info.append(node('div', 'person-name', c.name), node('div', 'person-source', c.source));
    person.append(cb, node('span', 'avatar', Array.from(c.name)[0]), info);
    const ids = node('div', 'identities'); ids.append(node('div', '', c.conversation), node('div', '', c.sender));
    const manage = node('button', 'manage', '查看'); manage.setAttribute('aria-label', `管理 ${c.name}`); manage.addEventListener('click', () => { current = c.id; setView('memory'); });
    row.append(person, ids, manage); $('contacts').append(row);
  });
  for (const selector of [$('memory-contact'), $('attachment-contact')]) {
    selector.replaceChildren(new Option('请选择已保存的联系人', ''));
    state.contacts.filter(c => c.selected).forEach(c => selector.add(new Option(`${c.name} · ${c.sender}`, c.id)));
    selector.value = state.contacts.some(c => c.id === current && c.selected) ? current : '';
  }
}
function setView(next) { view = next; const t = titles[next]; $('breadcrumb').textContent = t[0]; $('page-title').textContent = t[1]; $('page-subtitle').textContent = t[2]; document.querySelectorAll('.view').forEach(n => n.hidden = n.id !== `view-${next}`); document.querySelectorAll('.nav').forEach(n => n.classList.toggle('active', n.dataset.view === next)); $('add-open').hidden = next !== 'contacts'; if (next === 'memory' || next === 'attachments') loadDetails(); }
async function loadDetails() {
  const epoch = ++detailEpoch; const contact = state.contacts.find(c => c.id === current && c.selected);
  $('memory-contact').value = contact ? current : ''; $('attachment-contact').value = contact ? current : '';
  $('memory-form').querySelectorAll('input,textarea,select,button').forEach(n => n.disabled = !contact);
  $('memory-list').textContent = $('document-list').textContent = contact ? '正在读取本地资料…' : '请先在联系人页勾选、保存，再选择联系人。';
  $('session-list').textContent = contact ? '正在读取…' : '选择联系人后查看。';
  $('session-reset').disabled = !contact;
  if (!contact) return;
  try {
    const data = await api(`/api/contacts/${current}/details`); if (epoch !== detailEpoch) return;
    const cid = current;
    $('session-list').replaceChildren();
    const recent = data.short_term?.recent || [];
    if (!recent.length) $('session-list').textContent = '当前没有有效短期记忆。';
    recent.forEach(m => { const row = node('div', 'data-item'); row.append(node('strong', '', m.role === 'assistant' ? '助手' : '联系人'), node('p', '', m.text)); $('session-list').append(row); });
    $('memory-list').replaceChildren(); $('document-list').replaceChildren();
    if (!data.memory.length) $('memory-list').textContent = '还没有有效记忆。可以在左侧添加，或通过入站消息明确记住。';
    data.memory.forEach(m => {
      const item = node('div', 'data-item'), text = node('div'); text.append(node('strong', '', m.key), node('p', '', m.value), node('small', '', m.expires ? `到期：${new Date(m.expires * 1000).toLocaleString('zh-CN')}` : '长期保留'));
      const del = node('button', '', '删除'); del.addEventListener('click', () => run(async () => { await api(`/api/contacts/${cid}/memory/${encodeURIComponent(m.key)}`, {method: 'DELETE'}); await loadDetails(); notify('已删除显式记忆，原历史记录仍保留。'); })); item.append(text, del); $('memory-list').append(item);
    });
    if (!data.documents.length) $('document-list').textContent = '还没有已解析的资料。通过 attachment-add 导入文件后，在这里查看。';
    data.documents.forEach(d => {
      const item = node('div', 'data-item'), text = node('div'); text.append(node('strong', '', d.name), node('p', '', d.evidence), node('small', '', `资料 ID：${d.id}`));
      const del = node('button', '', '移除'); del.addEventListener('click', () => run(async () => { await api(`/api/contacts/${cid}/documents/${d.id}`, {method: 'DELETE'}); await loadDetails(); notify('已移除检索资料，原文件与历史记录仍保留。'); })); item.append(text, del); $('document-list').append(item);
    });
  } catch (e) { if (epoch !== detailEpoch) return; $('memory-list').textContent = $('document-list').textContent = e.message; notify(e.message, true); }
}
async function run(action) { if (busy) return; busy = true; document.querySelector('main').classList.add('busy'); try { await action(); } catch (e) { notify(e.message || '本地服务暂时不可用，请重试', true); } finally { busy = false; document.querySelector('main').classList.remove('busy'); render(); } }
document.querySelectorAll('.nav').forEach(n => n.addEventListener('click', () => setView(n.dataset.view)));
document.querySelectorAll('.filter').forEach(n => n.addEventListener('click', () => { filter = n.dataset.filter; document.querySelectorAll('.filter').forEach(b => b.classList.toggle('active', b === n)); render(); }));
$('search').addEventListener('input', render);
$('select-visible').addEventListener('change', e => { const all = e.target.checked; visible().forEach(c => all ? picked.add(c.id) : picked.delete(c.id)); render(); });
$('clear-selection').addEventListener('click', () => { picked.clear(); render(); });
$('reload').addEventListener('click', () => run(async () => { accept(await api('/api/state')); notify('已恢复保存的选择。'); }));
$('save').addEventListener('click', () => run(async () => { accept(await api('/api/selection', {method: 'POST', body: JSON.stringify({revision: state.revision, ids: [...picked]})})); notify('已保存选择。'); }));
$('add-open').addEventListener('click', () => { $('add-error').textContent = ''; $('add-dialog').showModal(); });
$('add-close').addEventListener('click', () => $('add-dialog').close()); $('add-cancel').addEventListener('click', () => $('add-dialog').close());
$('contact-form').addEventListener('submit', async e => {
  e.preventDefault(); const submit = e.target.querySelector('[type=submit]'); submit.disabled = true;
  try { const data = Object.fromEntries(new FormData(e.target)); const hadDraft = dirty(); accept(await api('/api/contacts', {method: 'POST', body: JSON.stringify({...data, revision: state.revision})}), hadDraft); $('add-dialog').close(); e.target.reset(); notify('联系人已添加，请勾选后保存。'); } catch (error) { $('add-error').textContent = error.message; } finally { submit.disabled = false; }
});
for (const id of ['memory-contact', 'attachment-contact']) $(id).addEventListener('change', e => { current = e.target.value; loadDetails(); });
$('memory-form').addEventListener('submit', e => { e.preventDefault(); const data = Object.fromEntries(new FormData(e.target)); run(async () => { await api(`/api/contacts/${current}/memory`, {method: 'POST', body: JSON.stringify({key: data.key, value: data.value, ttl: data.ttl ? Number(data.ttl) : null})}); e.target.reset(); await loadDetails(); notify('记忆已保存，仅用于该会话。'); }); });
window.addEventListener('beforeunload', e => { if (!exiting && dirty()) { e.preventDefault(); e.returnValue = ''; } });
api('/api/state').then(data => accept(data)).catch(e => { $('visible-count').textContent = '读取失败'; $('dirty-status').textContent = '配置不可用'; notify(e.message, true); });

$('quit-open').addEventListener('click', () => {
  if (busy || exiting) return;
  $('quit-unsaved').hidden = !dirty();
  $('quit-error').textContent = '';
  $('quit-dialog').showModal();
});
$('quit-cancel').addEventListener('click', () => $('quit-dialog').close());
$('quit-confirm').addEventListener('click', async () => {
  if (busy || exiting) return;
  $('quit-confirm').disabled = true;
  $('quit-cancel').disabled = true;
  try {
    await api('/api/shutdown', {method: 'POST'});
    exiting = true;
    detailEpoch++;
    $('quit-dialog').close();
    document.querySelector('.shell').hidden = true;
    document.querySelector('.sidebar').hidden = true;
    $('exit-screen').hidden = false;
  } catch (e) {
    $('quit-error').textContent = e.message || '退出未确认。请在运行终端按 Control+C。';
    $('quit-confirm').disabled = false;
    $('quit-cancel').disabled = false;
  }
});

$('session-reset').addEventListener('click', () => {
  if (!current || !confirm('重置此联系人的短期记忆？长期记忆和审计记录会保留。')) return;
  const cid = current;
  run(async () => { await api(`/api/contacts/${cid}/session/reset`, {method: 'POST'}); await loadDetails(); notify('已重置短期记忆。'); });
});
