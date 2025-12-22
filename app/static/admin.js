const TOKEN_KEY = 'admin_token';

function setStatus(el, msg) {
  el.textContent = msg;
}

async function login() {
  const username = document.getElementById('adminUsername').value.trim();
  const pwd = document.getElementById('adminPassword').value;
  const statusEl = document.getElementById('loginStatus');
  if (!username || !pwd) { alert('请输入账号和密码'); return; }
  try {
    const params = new URLSearchParams();
    params.append('username', username);
    params.append('password', pwd);
    const resp = await fetch('/api/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: params
    });
    if (!resp.ok) {
      const err = await resp.json();
      setStatus(statusEl, '登录失败: ' + (err.detail || resp.statusText));
      return;
    }
    const data = await resp.json();
    localStorage.setItem(TOKEN_KEY, data.token);
    setStatus(statusEl, '登录成功');
  } catch (e) {
    setStatus(statusEl, '异常: ' + e);
  }
}

function openPwdModal() {
  const modal = document.getElementById('pwdModal');
  if (modal) modal.classList.remove('hidden');
}

function closePwdModal() {
  const modal = document.getElementById('pwdModal');
  const statusEl = document.getElementById('pwdStatus');
  const newInput = document.getElementById('newPassword');
  const confirmInput = document.getElementById('confirmPassword');
  if (statusEl) statusEl.textContent = '';
  if (newInput) newInput.value = '';
  if (confirmInput) confirmInput.value = '';
  if (modal) modal.classList.add('hidden');
}

async function changePassword() {
  const token = localStorage.getItem(TOKEN_KEY);
  const newPwd = document.getElementById('newPassword').value;
  const confirmPwd = document.getElementById('confirmPassword').value;
  const statusEl = document.getElementById('pwdStatus');
  if (!token) { setStatus(statusEl, '请先登录'); return; }
  if (!newPwd || newPwd.length < 6) { setStatus(statusEl, '新密码至少6位'); return; }
  if (newPwd !== confirmPwd) { setStatus(statusEl, '两次输入不一致'); return; }
  try {
    const params = new URLSearchParams();
    params.append('new_password', newPwd);
    const resp = await fetch('/api/auth/change_password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Authorization': 'Bearer ' + token },
      body: params
    });
    if (!resp.ok) {
      let errText = resp.statusText;
      try { const err = await resp.json(); errText = err.detail || errText; } catch (_) {}
      setStatus(statusEl, '修改失败: ' + errText);
      return;
    }
    setStatus(statusEl, '修改成功');
    closePwdModal();
  } catch (e) {
    setStatus(statusEl, '异常: ' + e);
  }
}

async function uploadDoc() {
  const token = localStorage.getItem(TOKEN_KEY);
  const statusEl = document.getElementById('uploadStatus');
  const fileInput = document.getElementById('fileInput');
  if (!token) { window.location.href = '/admin'; return; }
  if (!fileInput.files.length) { alert('请选择支持的文件 (.txt/.md/.pdf/.docx/.xlsx)'); return; }
  const file = fileInput.files[0];
  const form = new FormData();
  form.append('file', file);
  setStatus(statusEl, '上传中...');
  try {
    const resp = await fetch('/api/docs/upload', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + token },
      body: form
    });
    if (!resp.ok) {
      let errText = resp.statusText;
      if (resp.status === 401) {
        localStorage.removeItem(TOKEN_KEY);
        setStatus(statusEl, '登录已失效，请重新登录');
        return;
      }
      try {
        const err = await resp.json();
        errText = err.detail || errText;
      } catch (_) {}
      setStatus(statusEl, '错误: ' + errText);
      return;
    }
    const data = await resp.json();
    setStatus(statusEl, `上传成功: ${data.saved}，当前切片数 ${data.docs_count}`);
    await loadDocs();
  } catch (e) {
    setStatus(statusEl, '异常: ' + e);
  }
}

async function batchRegister() {
  const token = localStorage.getItem(TOKEN_KEY);
  const statusEl = document.getElementById('registerStatus');
  const idsText = document.getElementById('studentIds').value.trim();
  if (!token) { window.location.href = '/admin'; return; }
  if (!idsText) { alert('请输入学号，每行一个'); return; }
  const student_ids = idsText.split(/\n+/).map(s => s.trim()).filter(Boolean);
  statusEl.textContent = '提交中...';
  try {
    const resp = await fetch('/api/admin/users/batch_register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
      body: JSON.stringify(student_ids)
    });
    if (!resp.ok) {
      let errText = resp.statusText;
      if (resp.status === 401) {
        localStorage.removeItem(TOKEN_KEY);
        statusEl.textContent = '登录已失效，请重新登录';
        return;
      }
      try { const err = await resp.json(); errText = err.detail || errText; } catch (_) {}
      statusEl.textContent = '错误: ' + errText;
      return;
    }
    const data = await resp.json();
    statusEl.textContent = `已创建 ${data.created.length} 个，已存在 ${data.skipped.length} 个，初始密码: hziee+学号`;
  } catch (e) {
    statusEl.textContent = '异常: ' + e;
  }
}

window.addEventListener('DOMContentLoaded', () => {
  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) {
    window.location.href = '/admin';
  }
  loadDocs();
});

async function loadDocs() {
  const token = localStorage.getItem(TOKEN_KEY);
  const listEl = document.getElementById('docsList');
  if (!token || !listEl) return;
  listEl.textContent = '加载中...';
  try {
    const resp = await fetch('/api/admin/docs', {
      headers: { 'Authorization': 'Bearer ' + token }
    });
    if (!resp.ok) {
      listEl.textContent = '加载失败';
      return;
    }
    const data = await resp.json();
    if (!data.docs || !data.docs.length) {
      listEl.textContent = '暂无文档';
      return;
    }
    listEl.innerHTML = '';
    data.docs.forEach(item => {
      const row = document.createElement('div');
      row.className = 'list-row';
      const link = document.createElement('a');
      link.href = 'javascript:void(0)';
      link.textContent = item.name + ` (${Math.round(item.size/1024)} KB)`;
      link.onclick = () => viewDoc(item.name);
      row.appendChild(link);
      listEl.appendChild(row);
    });
  } catch (e) {
    listEl.textContent = '异常: ' + e;
  }
}

async function viewDoc(name) {
  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) { window.location.href = '/admin'; return; }
  const titleEl = document.getElementById('docModalTitle');
  const contentEl = document.getElementById('docModalContent');
  const modal = document.getElementById('docModal');
  if (titleEl) titleEl.textContent = name;
  if (contentEl) contentEl.textContent = '加载中...';
  if (modal) modal.classList.remove('hidden');
  try {
    const resp = await fetch(`/api/admin/docs/${encodeURIComponent(name)}`, {
      headers: { 'Authorization': 'Bearer ' + token }
    });
    if (!resp.ok) {
      const errText = await resp.text();
      contentEl.textContent = '加载失败: ' + errText;
      return;
    }
    const text = await resp.text();
    contentEl.textContent = text;
  } catch (e) {
    contentEl.textContent = '异常: ' + e;
  }
}

function closeDocModal() {
  const modal = document.getElementById('docModal');
  if (modal) modal.classList.add('hidden');
}
