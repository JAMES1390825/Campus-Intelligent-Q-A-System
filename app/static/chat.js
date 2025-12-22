let lastPreviewName = null;

function ensureAuth() {
  const token = localStorage.getItem('user_token');
  if (!token) {
    window.location.href = '/';
  }
}

function renderAnswerWithSources(answer, sources, metaInfo) {
  const chips = (sources || []).map(s => {
    const score = s.score !== undefined ? ` (${s.score.toFixed(3)})` : '';
    return `<span class="chip" data-doc="${s.source}">${s.source}${score}</span>`;
  }).join(' ');
  const meta = metaInfo || {};
  const footer = `<div class="sources"><b>来源</b>: ${chips || '无'}</div>` +
                 `<div class="sources"><b>意图</b>: ${meta.intent || ''}; 工具: ${(meta.used_tools || []).join(', ') || '无'}${meta.latency ? '; 延迟: ' + Math.round(meta.latency) + ' ms' : ''}</div>`;
  return `<div class="card"><div>${answer.replace(/\n/g, '<br/>')}</div>${footer}</div>`;
}

function bindChipClicks(container) {
  container.querySelectorAll('.chip').forEach(el => {
    el.onclick = () => previewDoc(el.getAttribute('data-doc'));
  });
}

async function previewDoc(name) {
  if (!name) return;
  lastPreviewName = name;
  const token = localStorage.getItem('user_token');
  const modal = document.getElementById('docPreviewModal');
  const titleEl = document.getElementById('docPreviewTitle');
  const bodyEl = document.getElementById('docPreviewBody');
  if (titleEl) titleEl.textContent = name;
  if (bodyEl) bodyEl.textContent = '加载中...';
  if (modal) modal.classList.remove('hidden');
  try {
    const resp = await fetch(`/api/docs/${encodeURIComponent(name)}`, {
      headers: token ? { 'Authorization': 'Bearer ' + token } : {}
    });
    if (!resp.ok) {
      const err = await resp.text();
      if (bodyEl) bodyEl.textContent = '加载失败: ' + err;
      return;
    }
    const text = await resp.text();
    if (bodyEl) bodyEl.textContent = text;
  } catch (e) {
    if (bodyEl) bodyEl.textContent = '异常: ' + e;
  }
}

function closeDocPreview() {
  const modal = document.getElementById('docPreviewModal');
  if (modal) modal.classList.add('hidden');
}

async function downloadDoc() {
  if (!lastPreviewName) return;
  const token = localStorage.getItem('user_token');
  try {
    const resp = await fetch(`/api/docs/${encodeURIComponent(lastPreviewName)}/download`, {
      headers: token ? { 'Authorization': 'Bearer ' + token } : {}
    });
    if (!resp.ok) {
      const err = await resp.text();
      alert('下载失败: ' + err);
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = lastPreviewName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('下载异常: ' + e);
  }
}

async function ask() {
  ensureAuth();
  const query = document.getElementById('query').value.trim();
  const top_k = parseInt(document.getElementById('topk').value, 10);
  const need_tool = document.getElementById('needTool').checked;
  const streaming = document.getElementById('streaming').checked;
  const result = document.getElementById('result');
  const token = localStorage.getItem('user_token');
  if (!query) { alert('请输入问题'); return; }
  if (!token) { alert('请先登录'); return; }
  result.innerHTML = '思考中...';
  try {
    if (streaming) {
      const resp = await fetch('/api/query/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
        body: JSON.stringify({ query, top_k, need_tool, streaming: true })
      });
      if (!resp.ok) {
        if (resp.status === 401) {
          localStorage.removeItem('user_token');
          window.location.href = '/';
          return;
        }
        if (resp.status === 403) {
          localStorage.setItem('user_must_change', '1');
          result.innerHTML = '需先修改密码后再使用';
          openPwdModal();
          return;
        }
        const err = await resp.text();
        result.innerHTML = '错误: ' + err;
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let answer = '';
      let meta = null;
      result.innerHTML = '流式生成中...';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });
        const pieces = chunk.split('\n');
        for (const piece of pieces) {
          if (!piece) continue;
          if (piece.startsWith('__META__')) {
            meta = JSON.parse(piece.replace('__META__', ''));
            continue;
          }
          answer += piece;
          result.innerHTML = `<div class="card"><div>${answer.replace(/\n/g, '<br/>')}</div></div>`;
        }
      }
      if (meta) {
        const html = renderAnswerWithSources(answer, meta.sources, { intent: meta.intent, used_tools: meta.used_tools });
        result.innerHTML = html;
        bindChipClicks(result);
      }
    } else {
      const resp = await fetch('/api/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
        body: JSON.stringify({ query, top_k, need_tool })
      });
      if (!resp.ok) {
        if (resp.status === 401) {
          localStorage.removeItem('user_token');
          window.location.href = '/';
          return;
        }
        if (resp.status === 403) {
          localStorage.setItem('user_must_change', '1');
          result.innerHTML = '需先修改密码后再使用';
          openPwdModal();
          return;
        }
        const err = await resp.json();
        result.innerHTML = '错误: ' + err.detail;
        return;
      }
      const data = await resp.json();
      const html = renderAnswerWithSources(data.answer, data.sources, { intent: data.intent, used_tools: data.used_tools, latency: data.latency_ms });
      result.innerHTML = html;
      bindChipClicks(result);
    }
  } catch (e) {
    result.innerHTML = '异常: ' + e;
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
  const mustChange = localStorage.getItem('user_must_change');
  if (mustChange === '1') {
    openPwdModal();
  }
}

window.addEventListener('DOMContentLoaded', initPage);
