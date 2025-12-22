const TOKEN_KEY = 'admin_token';

async function login() {
  const username = document.getElementById('adminUsername').value.trim();
  const pwd = document.getElementById('adminPassword').value;
  const statusEl = document.getElementById('loginStatus');
  if (!username || !pwd) { statusEl.textContent = '请输入账号和密码'; return; }
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
      statusEl.textContent = '登录失败: ' + (err.detail || resp.statusText);
      return;
    }
    const data = await resp.json();
    localStorage.setItem(TOKEN_KEY, data.token);
    statusEl.textContent = '登录成功，跳转中...';
    window.location.href = '/admin/dashboard';
  } catch (e) {
    statusEl.textContent = '异常: ' + e;
  }
}

window.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') login();
});
