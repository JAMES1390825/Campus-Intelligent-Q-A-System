let chatSessionId = null;
let isSending = false;
let currentAbortController = null;
let currentAssistantBubble = null;
let requestTimeoutHandle = null;
let manualCancelReason = null;

const REQUEST_TIMEOUT_MS = 45_000;
const DEFAULT_TOP_K = 4;

const SESSION_STORAGE_KEY = 'chat_session_id';

let chatLogEl;
let chatStatusEl;
let chatInputEl;
let sendBtnEl;
let sessionListEl;
let sessionTitleEl;
let sessionsCache = [];
let currentSessionSummary = null;

function setupUserMenu(role) {
  const trigger = document.getElementById('userMenuButton');
  const dropdown = document.getElementById('userMenuDropdown');
  if (!trigger || !dropdown) return;
  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    dropdown.classList.toggle('open');
  });
  document.addEventListener('click', (e) => {
    if (!dropdown.contains(e.target) && e.target !== trigger && !trigger.contains(e.target)) {
      dropdown.classList.remove('open');
    }
  });
  const changeBtn = document.getElementById('menuChangePwd');
  const logoutBtn = document.getElementById('menuLogout');
  if (changeBtn) {
    changeBtn.onclick = () => {
      dropdown.classList.remove('open');
      goToChangePassword(role);
    };
  }
  if (logoutBtn) {
    logoutBtn.onclick = () => {
      dropdown.classList.remove('open');
      logout(role);
    };
  }
}

function logout(role) {
  if (role === 'admin') {
    localStorage.removeItem('admin_token');
    window.location.href = '/admin';
    return;
  }
  localStorage.removeItem('user_token');
  localStorage.removeItem('user_must_change');
  window.location.href = '/';
}

function goToChangePassword(role) {
  window.location.href = `/change-password?role=${role}`;
}

function ensureAuth() {
  const token = localStorage.getItem('user_token');
  if (!token) {
    window.location.href = '/';
  }
}

function escapeHtml(str = '') {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function escapeAttr(str = '') {
  return escapeHtml(str).replace(/"/g, '&quot;');
}

function formatRichText(text = '') {
  return escapeHtml(text).replace(/\n/g, '<br/>');
}

function sanitizeAnswerText(text = '') {
  if (!text) return '';
  let result = String(text);
  result = result.replace(/[（(]?来源[:：][^\n）)]+[）)]?/g, '');
  result = result.replace(/^\s*-?\s*来源[:：].*$/gm, '');
  result = result.replace(/[\[{【](?:[^\]}】]*?\.)?(pdf|docx?|doc|pptx?|ppt|txt)[^\]}】]*?[\]}】]/gi, '');
  result = result.replace(/【\s*】/g, '');
  result = result.replace(/\n{3,}/g, '\n\n');
  return result.trim();
}

function formatTimeAgo(tsSeconds) {
  if (!tsSeconds) return '刚刚';
  const diff = Date.now() - tsSeconds * 1000;
  if (diff < 60_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.max(1, Math.floor(diff / 60_000))} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return `${Math.floor(diff / 86_400_000)} 天前`;
}

function setSessionTitle(title) {
  if (!sessionTitleEl) return;
  sessionTitleEl.textContent = title || '对话';
}

function scrollChatToBottom() {
  if (chatLogEl) {
    chatLogEl.scrollTop = chatLogEl.scrollHeight;
  }
}

function forceCancelInFlight(reason = '请求已取消') {
  if (!isSending) return;
  cancelCurrentRequest(reason);
  clearInFlightRequestState();
}

function clearHistory(message = '开始对话吧～') {
  if (!chatLogEl) return;
  chatLogEl.innerHTML = `<div class="chat-placeholder">${message}</div>`;
}

function appendMessage(role, htmlContent) {
  if (!chatLogEl) return null;
  const placeholder = chatLogEl.querySelector('.chat-placeholder');
  if (placeholder) placeholder.remove();
  const bubble = document.createElement('div');
  bubble.className = `chat-bubble ${role}`;
  bubble.innerHTML = `
    <div class="avatar">${role === 'user' ? '我' : 'AI'}</div>
    <div class="bubble-body">
      <div class="message-text">${htmlContent || ''}</div>
    </div>
  `;
  chatLogEl.appendChild(bubble);
  scrollChatToBottom();
  return bubble;
}

function setBubbleContent(bubble, html) {
  if (!bubble) return;
  const body = bubble.querySelector('.bubble-body');
  if (body) {
    body.innerHTML = html;
  }
  scrollChatToBottom();
}

function setBubbleError(bubble, text) {
  setBubbleContent(bubble, `<div class="message-text error">${escapeHtml(text)}</div>`);
}

function renderAnswerContent(answer, metaInfo) {
  const meta = metaInfo || {};
  const cleanAnswer = sanitizeAnswerText(answer);
  const latencyLine = typeof meta.latency === 'number'
    ? `<div class="meta-row">响应 ${Math.round(meta.latency)} ms</div>`
    : '';
  return `
    <div class="message-text">${formatRichText(cleanAnswer)}</div>
    ${latencyLine}
  `;
}

function setStatus(msg = '') {
  if (chatStatusEl) {
    chatStatusEl.textContent = msg;
  }
}


async function ensureSession(forceNew = false) {
  const token = localStorage.getItem('user_token');
  if (!token) return null;
  if (!forceNew) {
    chatSessionId = chatSessionId || localStorage.getItem(SESSION_STORAGE_KEY);
    if (chatSessionId) {
      updateSessionBadge();
      return chatSessionId;
    }
  }
  const summary = await createFreshSession(null, true);
  return summary ? summary.session_id : null;
}

function updateSessionBadge(summary) {
  if (summary) {
    currentSessionSummary = {
      ...(currentSessionSummary || {}),
      ...summary,
    };
    if (!currentSessionSummary.session_id) {
      currentSessionSummary.session_id = chatSessionId;
    }
    setSessionTitle(currentSessionSummary.title || '对话');
  } else if (currentSessionSummary) {
    setSessionTitle(currentSessionSummary.title || '对话');
  }
  const badge = document.getElementById('sessionBadge');
  if (badge) {
    badge.textContent = chatSessionId ? `会话 ID: ${chatSessionId.slice(0, 8)}...` : '会话未建立';
  }
}

function getSessionName(summary) {
  if (!summary) return '未命名对话';
  return (summary.title || '').trim() || '未命名对话';
}

function renderSessionList(activeId = chatSessionId) {
  if (!sessionListEl) return;
  if (!sessionsCache.length) {
    sessionListEl.innerHTML = '<div class="session-placeholder">暂无会话</div>';
    return;
  }
  sessionListEl.innerHTML = sessionsCache.map((sess) => {
    const isActive = sess.session_id === activeId;
    const meta = `${formatTimeAgo(sess.updated_at)} · ${sess.message_count || 0} 条`;
    return `
      <div class="session-item ${isActive ? 'active' : ''}">
        <button class="session-main" data-id="${escapeAttr(sess.session_id)}">
          <div class="session-item-title">${escapeHtml(getSessionName(sess))}</div>
          <div class="session-item-meta">${escapeHtml(meta)}</div>
        </button>
        <div class="session-item-actions">
          <button class="ghost" data-action="rename" data-id="${escapeAttr(sess.session_id)}">改</button>
          <button class="ghost danger" data-action="delete" data-id="${escapeAttr(sess.session_id)}">删</button>
        </div>
      </div>
    `;
  }).join('');

  sessionListEl.querySelectorAll('.session-main').forEach((btn) => {
    btn.onclick = () => switchSession(btn.getAttribute('data-id'));
  });
  sessionListEl.querySelectorAll('button[data-action="rename"]').forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      promptRenameSession(btn.getAttribute('data-id'));
    };
  });
  sessionListEl.querySelectorAll('button[data-action="delete"]').forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      deleteSessionById(btn.getAttribute('data-id'));
    };
  });
}

async function refreshSessions(activeId = chatSessionId) {
  const token = localStorage.getItem('user_token');
  if (!token) return;
  try {
    const resp = await fetch('/api/session', {
      headers: { 'Authorization': 'Bearer ' + token },
    });
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    sessionsCache = data.sessions || [];
    const active = sessionsCache.find((s) => s.session_id === chatSessionId);
    if (active) {
      updateSessionBadge(active);
    }
    renderSessionList(activeId);
  } catch (e) {
    console.warn('加载会话失败', e);
  }
}

async function switchSession(sessionId) {
  if (!sessionId || sessionId === chatSessionId) {
    await loadHistory();
    return;
  }
  forceCancelInFlight('已切换会话，终止上一请求');
  chatSessionId = sessionId;
  localStorage.setItem(SESSION_STORAGE_KEY, chatSessionId);
  updateSessionBadge();
  await loadHistory();
  renderSessionList();
}

async function createFreshSession(title = null, keepHistory = false) {
  const token = localStorage.getItem('user_token');
  if (!token) {
    window.location.href = '/';
    return null;
  }
  try {
    const resp = await fetch('/api/session/new', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + token,
      },
      body: JSON.stringify({ title }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    chatSessionId = data.session_id;
    localStorage.setItem(SESSION_STORAGE_KEY, chatSessionId);
    updateSessionBadge({ session_id: data.session_id, title: data.title, updated_at: data.created_at });
    if (!keepHistory) {
      clearHistory('开始新的对话吧～');
    }
    await refreshSessions(chatSessionId);
    return data;
  } catch (e) {
    setStatus('创建会话失败: ' + e.message);
    return null;
  }
}

async function startNewSession() {
  await createFreshSession(null, false);
  await loadHistory();
}

async function promptRenameSession(sessionId) {
  const target = sessionsCache.find((s) => s.session_id === sessionId) || currentSessionSummary;
  const initial = target ? getSessionName(target) : '';
  const next = window.prompt('输入新的会话名称', initial);
  if (next === null) return;
  const title = next.trim();
  if (!title) {
    alert('标题不能为空');
    return;
  }
  const token = localStorage.getItem('user_token');
  if (!token) return;
  try {
    const resp = await fetch(`/api/session/${encodeURIComponent(sessionId)}`, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + token,
      },
      body: JSON.stringify({ title }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const summary = await resp.json();
    if (sessionId === chatSessionId) {
      updateSessionBadge(summary);
    }
    await refreshSessions(sessionId);
  } catch (e) {
    alert('重命名失败: ' + e.message);
  }
}

async function deleteSessionById(sessionId) {
  if (!window.confirm('确定删除该会话吗？操作不可恢复。')) return;
  const token = localStorage.getItem('user_token');
  if (!token) return;
  try {
    const resp = await fetch(`/api/session/${encodeURIComponent(sessionId)}`, {
      method: 'DELETE',
      headers: { 'Authorization': 'Bearer ' + token },
    });
    if (!resp.ok) throw new Error(await resp.text());
    sessionsCache = sessionsCache.filter((s) => s.session_id !== sessionId);
    if (chatSessionId === sessionId) {
      await createFreshSession(null, false);
      await loadHistory();
    } else {
      renderSessionList();
    }
    setStatus('会话已删除');
  } catch (e) {
    alert('删除失败: ' + e.message);
  }
}

async function resetConversation() {
  if (isSending) {
    cancelCurrentRequest('已清空上下文，终止当前请求');
  }
  localStorage.removeItem(SESSION_STORAGE_KEY);
  chatSessionId = null;
  clearHistory('正在创建新会话...');
  await createFreshSession(null, false);
  setStatus('已开始新的会话');
  await loadHistory();
}

async function loadHistory() {
  const token = localStorage.getItem('user_token');
  if (!token) return;
  await ensureSession();
  if (!chatSessionId) return;
  clearHistory('正在加载会话...');
  try {
    const resp = await fetch(`/api/session/${chatSessionId}/history`, {
      headers: { 'Authorization': 'Bearer ' + token }
    });
    if (!resp.ok) {
      throw new Error(await resp.text());
    }
    const data = await resp.json();
    const history = data.history || [];
    if (data.title) {
      updateSessionBadge({ session_id: chatSessionId, title: data.title });
    } else {
      updateSessionBadge();
    }
    if (!history.length) {
      clearHistory();
      setStatus('暂无历史');
      renderSessionList(chatSessionId);
      return;
    }
    chatLogEl.innerHTML = '';
    history.forEach((msg) => {
      const role = msg.role === 'assistant' ? 'assistant' : 'user';
      const content = role === 'assistant'
        ? formatRichText(sanitizeAnswerText(msg.content || ''))
        : formatRichText(msg.content || '');
      appendMessage(role, content);
    });
    setStatus(`已加载 ${history.length} 条历史`);
    renderSessionList(chatSessionId);
  } catch (e) {
    setStatus('加载历史失败: ' + e);
  }
}

async function ask() {
  ensureAuth();
  if (isSending) return;
  const token = localStorage.getItem('user_token');
  if (!token) { alert('请先登录'); return; }
  const query = chatInputEl.value.trim();
  if (!query) { setStatus('请输入问题'); return; }
  const top_k = DEFAULT_TOP_K;
  const streaming = document.getElementById('streaming').checked;
  await ensureSession();
  if (!chatSessionId) { setStatus('无法建立会话'); return; }

  chatInputEl.value = '';
  const userBubble = appendMessage('user', formatRichText(query));
  const assistantBubble = appendMessage('assistant', escapeHtml('思考中...'));
  setStatus('处理中...');
  setSending(true);
  currentAssistantBubble = assistantBubble;
  currentAbortController = new AbortController();
  startRequestTimeout();

  const payload = { query, top_k, session_id: chatSessionId };

  try {
    if (streaming) {
      await handleStreamingRequest(payload, assistantBubble, token, currentAbortController.signal);
    } else {
      await handleStandardRequest(payload, assistantBubble, token, currentAbortController.signal);
    }
    setStatus('完成');
    refreshSessions(chatSessionId);
  } catch (e) {
    if (e.name === 'AbortError') {
      const reason = manualCancelReason || '请求已取消';
      if (assistantBubble) {
        setBubbleContent(assistantBubble, `<div class="message-text muted">${escapeHtml(reason)}</div>`);
      }
      setStatus(reason);
    } else {
      console.error(e);
      setBubbleError(assistantBubble, e.message || '请求失败');
      setStatus('错误: ' + (e.message || e));
    }
  } finally {
    manualCancelReason = null;
    clearInFlightRequestState();
  }
}

async function handleStreamingRequest(payload, assistantBubble, token, signal) {
  payload.streaming = true;
  const resp = await fetch('/api/query/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
    body: JSON.stringify(payload),
    signal,
  });
  if (!resp.ok) {
    await handleRequestError(resp, assistantBubble);
    return;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let answer = '';
  let meta = null;
  const textEl = assistantBubble.querySelector('.message-text');
  if (textEl) textEl.innerHTML = '流式生成中...';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    const chunk = decoder.decode(value, { stream: true });
    const pieces = chunk.split('\n');
    for (const piece of pieces) {
      if (!piece) continue;
      if (piece.startsWith('__META__')) {
        meta = JSON.parse(piece.substring(8));
        continue;
      }
      answer += piece;
      if (textEl) textEl.innerHTML = formatRichText(sanitizeAnswerText(answer));
    }
  }
  if (meta) {
    finalizeAssistantBubble(assistantBubble, answer, {});
  } else if (textEl) {
    textEl.innerHTML = formatRichText(sanitizeAnswerText(answer || '生成完成'));
  }
}

async function handleStandardRequest(payload, assistantBubble, token, signal) {
  const resp = await fetch('/api/query', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
    body: JSON.stringify(payload),
    signal,
  });
  if (!resp.ok) {
    await handleRequestError(resp, assistantBubble);
    return;
  }
  const data = await resp.json();
  finalizeAssistantBubble(assistantBubble, data.answer, {
    latency: data.latency_ms,
  });
}

function finalizeAssistantBubble(bubble, answer, meta) {
  setBubbleContent(bubble, renderAnswerContent(answer, meta));
}

async function handleRequestError(resp, assistantBubble) {
  if (resp.status === 401) {
    localStorage.removeItem('user_token');
    setBubbleError(assistantBubble, '登录已失效，请重新登录');
    window.location.href = '/';
    throw new Error('登录已过期');
  }
  if (resp.status === 403) {
    localStorage.setItem('user_must_change', '1');
    setBubbleError(assistantBubble, '需先修改密码后再继续');
    openPwdModal();
    throw new Error('请先修改密码');
  }
  let errText = '请求失败';
  try {
    const data = await resp.json();
    errText = data.detail || JSON.stringify(data);
  } catch (e) {
    errText = await resp.text();
  }
  throw new Error(errText);
}

function setSending(flag) {
  isSending = flag;
  if (chatInputEl) {
    chatInputEl.disabled = flag;
  }
  if (sendBtnEl) {
    sendBtnEl.disabled = false;
    sendBtnEl.textContent = flag ? '停止' : '发送';
    if (flag) {
      sendBtnEl.classList.add('danger');
    } else {
      sendBtnEl.classList.remove('danger');
    }
  }
}

function startRequestTimeout() {
  clearRequestTimeout();
  requestTimeoutHandle = setTimeout(() => {
    cancelCurrentRequest('请求超时，已自动停止');
  }, REQUEST_TIMEOUT_MS);
}

function clearRequestTimeout() {
  if (requestTimeoutHandle) {
    clearTimeout(requestTimeoutHandle);
    requestTimeoutHandle = null;
  }
}

function clearInFlightRequestState() {
  clearRequestTimeout();
  currentAbortController = null;
  currentAssistantBubble = null;
  setSending(false);
}

function cancelCurrentRequest(reason = '请求已取消') {
  if (!currentAbortController) return;
  manualCancelReason = reason;
  currentAbortController.abort();
}

function clearLocalHistory() {
  if (isSending) {
    cancelCurrentRequest('已停止当前对话');
  }
  clearHistory('已清空，仅影响本地显示');
  setStatus('已清空本地对话');
}

function handleSendClick() {
  if (isSending) {
    cancelCurrentRequest('已手动停止');
  } else {
    ask();
  }
}

async function submitPasswordChange(newPwd, statusEl) {
  const token = localStorage.getItem('user_token');
  if (!token) { alert('请先登录'); return; }
  if (!newPwd || newPwd.length < 6) { statusEl.textContent = '新密码至少6位'; return; }
  try {
    const params = new URLSearchParams();
    params.append('new_password', newPwd);
    const resp = await fetch('/api/auth/change_password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Authorization': 'Bearer ' + token },
      body: params
    });
    if (!resp.ok) {
      const err = await resp.json();
      statusEl.textContent = '修改失败: ' + (err.detail || resp.statusText);
      return;
    }
    statusEl.textContent = '修改成功';
    localStorage.setItem('user_must_change', '0');
    return true;
  } catch (e) {
    statusEl.textContent = '异常: ' + e;
  }
}

function openPwdModal() {
  const modal = document.getElementById('pwdModal');
  if (modal) modal.classList.remove('hidden');
}

function closePwdModal() {
  const modal = document.getElementById('pwdModal');
  const statusEl = document.getElementById('modalPwdStatus');
  const newInput = document.getElementById('modalNewPwd');
  const confirmInput = document.getElementById('modalConfirmPwd');
  if (statusEl) statusEl.textContent = '';
  if (newInput) newInput.value = '';
  if (confirmInput) confirmInput.value = '';
  if (modal) modal.classList.add('hidden');
}

async function changePasswordFromModal() {
  const newPwd = document.getElementById('modalNewPwd').value;
  const confirmPwd = document.getElementById('modalConfirmPwd').value;
  const statusEl = document.getElementById('modalPwdStatus');
  if (!newPwd || newPwd.length < 6) {
    statusEl.textContent = '新密码至少6位';
    return;
  }
  if (newPwd !== confirmPwd) {
    statusEl.textContent = '两次输入不一致';
    return;
  }
  const ok = await submitPasswordChange(newPwd, statusEl);
  if (ok) closePwdModal();
}

function initPage() {
  ensureAuth();
  setupUserMenu('student');
  chatLogEl = document.getElementById('chatLog');
  chatStatusEl = document.getElementById('chatStatus');
  chatInputEl = document.getElementById('chatInput');
  sendBtnEl = document.getElementById('sendBtn');
  sessionListEl = document.getElementById('sessionList');
  sessionTitleEl = document.getElementById('sessionTitle');
  if (chatInputEl) {
    chatInputEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (!isSending) {
          ask();
        }
      }
    });
  }
  ensureSession().then(() => {
    refreshSessions();
    loadHistory();
  });
  const mustChange = localStorage.getItem('user_must_change');
  if (mustChange === '1') {
    openPwdModal();
  }
}

window.addEventListener('DOMContentLoaded', initPage);
