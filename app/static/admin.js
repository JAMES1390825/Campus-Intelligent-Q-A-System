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

async function uploadDoc() {
  const token = localStorage.getItem(TOKEN_KEY);
  const statusEl = document.getElementById('uploadStatus');
  const fileInput = document.getElementById('fileInput');
  if (!token) { alert('请先登录'); return; }
  if (!fileInput.files.length) { alert('请选择 .txt 文件'); return; }
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
      try {
        const err = await resp.json();
        errText = err.detail || errText;
      } catch (_) {}
      setStatus(statusEl, '错误: ' + errText);
      return;
    }
    const data = await resp.json();
    setStatus(statusEl, `上传成功: ${data.saved}，当前切片数 ${data.docs_count}`);
  } catch (e) {
    setStatus(statusEl, '异常: ' + e);
  }
}

async function batchRegister() {
  const token = localStorage.getItem(TOKEN_KEY);
  const statusEl = document.getElementById('registerStatus');
  const idsText = document.getElementById('studentIds').value.trim();
  if (!token) { alert('请先登录'); return; }
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
