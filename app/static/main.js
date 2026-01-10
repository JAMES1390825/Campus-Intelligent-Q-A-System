async function fetchHealth() {
  const healthText = document.getElementById('healthText');
  try {
    const resp = await fetch('/health');
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    healthText.textContent = `状态 ${data.status} · 向量模型: ${data.embedding_model} · 已索引文档: ${data.docs_indexed}`;
  } catch (e) {
    healthText.textContent = '获取失败: ' + e;
  }
}

async function ask() {
  const query = document.getElementById('query').value.trim();
  const top_k = parseInt(document.getElementById('topk').value, 10);
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
        body: JSON.stringify({ query, top_k, streaming: true })
      });
      if (!resp.ok) {
        if (resp.status === 401) {
          localStorage.removeItem('user_token');
          result.innerHTML = '登录已失效，请重新登录';
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
        const sourcesHtml = meta.sources.map(s => `${s.source} (score ${s.score.toFixed(3)})`).join(' | ');
        const footer = `<div class="sources"><b>来源</b>: ${sourcesHtml}</div>`;
        result.innerHTML = `<div class="card"><div>${answer.replace(/\n/g, '<br/>')}</div>${footer}</div>`;
      }
    } else {
      const resp = await fetch('/api/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
        body: JSON.stringify({ query, top_k })
      });
      if (!resp.ok) {
        if (resp.status === 401) {
          localStorage.removeItem('user_token');
          result.innerHTML = '登录已失效，请重新登录';
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
      let html = `<div class="card"><div>${data.answer.replace(/\n/g, '<br/>')}</div>`;
      html += `<div class="sources"><b>来源</b>: ${data.sources.map(s => `${s.source} (score ${s.score.toFixed(3)})`).join(' | ')}</div>`;
      if (typeof data.latency_ms === 'number') {
        html += `<div class="sources">响应 ${Math.round(data.latency_ms)} ms</div>`;
      }
      html += `</div>`;
      result.innerHTML = html;
    }
  } catch (e) {
    result.innerHTML = '异常: ' + e;
  }
}

fetchHealth();

async function login() {
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;
  const statusEl = document.getElementById('loginStatus');
  if (!username || !password) { alert('请输入账号和密码'); return; }
  try {
    const params = new URLSearchParams();
    params.append('username', username);
    params.append('password', password);
    const resp = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: params
    });
    if (!resp.ok) {
      const err = await resp.json();
      statusEl.textContent = '登录失败: ' + (err.detail || resp.statusText);
      return;
    }
    const data = await resp.json();
    localStorage.setItem('user_token', data.token);
    statusEl.textContent = data.must_change_password ? '登录成功，请先修改密码' : '登录成功';
    if (data.must_change_password) {
      openPwdModal();
    }
  } catch (e) {
    statusEl.textContent = '异常: ' + e;
  }
}

async function changePassword() {
  const newPwd = document.getElementById('newPassword').value;
  const statusEl = document.getElementById('pwdStatus');
  await submitPasswordChange(newPwd, statusEl);
}

async function submitPasswordChange(newPwd, statusEl) {
  const token = localStorage.getItem('user_token');
  if (!token) { alert('请先登录'); return; }
  if (!newPwd || newPwd.length < 6) { alert('新密码至少6位'); return; }
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
