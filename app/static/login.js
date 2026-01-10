async function login() {
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;
  const statusEl = document.getElementById('loginStatus');
  if (!username || !password) {
    statusEl.textContent = '请输入账号和密码';
    return;
  }
  statusEl.textContent = '登录中...';
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
    localStorage.setItem('user_must_change', data.must_change_password ? '1' : '0');
    window.location.href = '/chat';
  } catch (e) {
    statusEl.textContent = '异常: ' + e;
  }
}

window.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    login();
  }
});
