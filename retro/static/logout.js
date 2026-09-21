(function (root) {
  async function logout(request = root.fetch.bind(root), navigate = url => root.location.assign(url)) {
    const response = await request('/api/session/logout', {method: 'POST'});
    if (!response.ok) throw new Error('Не удалось завершить сессию.');
    navigate('/login');
  }

  function bind() {
    const button = root.document?.getElementById('logout');
    if (!button) return;
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await logout();
      } catch (error) {
        button.disabled = false;
        button.textContent = 'Повторить выход';
        button.title = error.message;
      }
    });
  }

  root.RetroLogout = {logout, bind};
  if (root.document?.readyState === 'loading') {
    root.document.addEventListener('DOMContentLoaded', bind, {once: true});
  } else {
    bind();
  }
})(globalThis);
