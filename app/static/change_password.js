(function () {
  const params = new URLSearchParams(window.location.search);
  const role = params.get('role') === 'admin' ? 'admin' : 'student';
  const tokenKey = role === 'admin' ? 'admin_token' : 'user_token';
  const loginUrl = role === 'admin' ? '/admin' : '/';
  const backUrl = role === 'admin' ? '/admin/dashboard' : '/chat';
  const token = localStorage.getItem(tokenKey);
  const roleTitleEl = document.getElementById('roleTitle');
  const subtitleEl = document.getElementById('pageSubtitle');
  const changeForm = document.getElementById('changePwdForm');
  const statusEl = document.getElementById('changeStatus');
  const cancelBtn = document.getElementById('cancelChangeBtn');

  if (roleTitleEl) {
    roleTitleEl.textContent = role === 'admin' ? '管理员账号' : '学生账号';
  }
  if (subtitleEl) {
    subtitleEl.textContent = role === 'admin'
      ? '管理员修改密码后将返回登录页'
      : '学生修改密码后将返回登录页';
  }

  if (!token) {
    window.location.href = loginUrl;
    return;
  }

  if (cancelBtn) {
    cancelBtn.addEventListener('click', () => {
      window.location.href = backUrl;
    });
  }

  if (changeForm) {
    changeForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      await submitPasswordChange(role, tokenKey, loginUrl, statusEl);
    });
  }
})();

async function submitPasswordChange(role, tokenKey, loginUrl, statusEl) {
  const token = localStorage.getItem(tokenKey);
  if (!token) {
    window.location.href = loginUrl;
    return;
  }
  const newPwdInput = document.getElementById('newPwd');
  const confirmPwdInput = document.getElementById('confirmPwd');
  if (!newPwdInput || !confirmPwdInput) return;
  const newPwd = newPwdInput.value.trim();
  const confirmPwd = confirmPwdInput.value.trim();
  if (!newPwd || newPwd.length < 6) {
    statusEl.textContent = '新密码至少6位';
    return;
  }
  if (newPwd !== confirmPwd) {
    statusEl.textContent = '两次输入不一致';
    return;
  }
  statusEl.textContent = '提交中...';
  try {
    const params = new URLSearchParams();
    params.append('new_password', newPwd);
    const resp = await fetch('/api/auth/change_password', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Authorization': 'Bearer ' + token,
      },
      body: params,
    });
    if (!resp.ok) {
      let errText = resp.statusText;
      try {
        const err = await resp.json();
        errText = err.detail || errText;
      } catch (_) {}
      statusEl.textContent = '修改失败: ' + errText;
      return;
    }
    statusEl.textContent = '修改成功，正在跳转...';
    localStorage.removeItem(tokenKey);
    if (role === 'student') {
      localStorage.removeItem('user_must_change');
    }
    setTimeout(() => {
      window.location.href = loginUrl;
    }, 1500);
  } catch (error) {
    statusEl.textContent = '异常: ' + error;
  }
}
