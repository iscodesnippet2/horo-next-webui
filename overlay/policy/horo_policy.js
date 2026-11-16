// Admin policy, front end (horo-next-webui). The server enforces it; this shows it.
//
// What the policy locks comes with GET /api/settings as `horo_policy`, and is
// cached for the next page load, whose first paint comes before that request.
// A 503 that carries `horo_policy_error` means no usable policy: the page is
// covered with the reason instead of upstream's error toasts.
(function(){
  const KEY = 'horo-policy';
  let policy = null;
  try { policy = JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (_e) { policy = null; }

  const list = (field) => (policy && Array.isArray(policy[field]) ? policy[field] : []);
  const hidesTab = (name) => list('hide_tabs').includes(name);

  // kind 'tab' or 'setting'.
  window._horoPolicyLocked = function(kind, name){
    if (kind === 'tab') return hidesTab(name);
    if (kind === 'setting') return list('settings').includes(name);
    return false;
  };
  // Upstream keeps chat and settings always visible; the policy may hide settings.
  window._horoAlwaysVisible = (name) => name === 'chat' || (name === 'settings' && !hidesTab('settings'));
  // A panel the policy hides has no way in: rail, shortcut, ?panel= or a button calling switchPanel.
  window._horoPolicyRefusesPanel = (name) => name !== 'chat' && hidesTab(name);

  // Upstream takes hidden_tabs from its localStorage mirror at boot and asks the
  // server again only on a profile switch or in the settings panel, which the
  // policy may hide. The server's list already carries the policy's tabs.
  function applyTabs(hidden){
    if (!Array.isArray(hidden) || typeof _setHiddenTabs !== 'function' || typeof _applyTabVisibility !== 'function') return;
    _setHiddenTabs(hidden.filter((x) => typeof x === 'string' && x.trim()));
    _applyTabVisibility(_getHiddenTabs());
  }

  function rerender(){
    try {
      if (typeof _renderTabVisibilityChips === 'function') _renderTabVisibilityChips();
      if (typeof _renderComposerControlChips === 'function') _renderComposerControlChips();
      if (typeof _renderComposerSituationalControlChips === 'function') _renderComposerSituationalControlChips();
      if (typeof _currentPanel !== 'undefined' && window._horoPolicyRefusesPanel(_currentPanel)
          && typeof switchPanel === 'function') switchPanel('chat');
    } catch (_e) { /* the next settings load renders again */ }
  }

  function update(settings){
    const view = settings && settings.horo_policy;
    if (!view || view.error) return;
    policy = {path: view.path, settings: view.settings || [], hide_tabs: view.hide_tabs || [],
              block_features: view.block_features || []};
    try { localStorage.setItem(KEY, JSON.stringify(policy)); } catch (_e) {}
    try { applyTabs(settings.hidden_tabs); } catch (_e) {}
    rerender();
  }

  function showUnavailable(info){
    let box = document.getElementById('horoPolicyBlocked');
    if (!box) {
      box = document.createElement('div');
      box.id = 'horoPolicyBlocked';
      box.setAttribute('role', 'alertdialog');
      box.setAttribute('aria-modal', 'true');
      box.setAttribute('aria-labelledby', 'horoPolicyBlockedTitle');
      box.innerHTML = '<div class="horo-policy-card"><h2 id="horoPolicyBlockedTitle">WebUI 暫停使用</h2>'
        + '<p>管理員政策（admin policy）無法使用，所以 WebUI 不提供服務。請聯絡管理員。</p>'
        + '<dl><dt>政策檔</dt><dd class="horo-policy-path"></dd><dt>原因</dt><dd class="horo-policy-reason"></dd></dl>'
        + '<p class="horo-policy-hint">政策修好後重新整理此頁即可，不必重新啟動 WebUI。</p>'
        + '<button type="button" class="horo-policy-reload">重新整理</button></div>';
      box.querySelector('.horo-policy-reload').onclick = () => location.reload();
      (document.body || document.documentElement).appendChild(box);
    }
    box.querySelector('.horo-policy-path').textContent = info.path || '';
    box.querySelector('.horo-policy-reason').textContent = info.reason || '';
    document.documentElement.classList.add('horo-policy-unavailable');
  }

  const isSettingsGet = (url, init, req) => {
    const method = String((init && init.method) || (req && req.method) || 'GET').toUpperCase();
    try { return method === 'GET' && new URL(url, location.href).pathname.replace(/\/+$/, '').endsWith('/api/settings'); }
    catch (_e) { return false; }
  };

  const realFetch = window.fetch.bind(window);
  window.fetch = async function(input, init){
    const req = (typeof Request !== 'undefined' && input instanceof Request) ? input : null;
    const url = req ? req.url : String(input);
    const res = await realFetch(input, init);
    if (res.status === 503) {
      try {
        const body = await res.clone().json();
        if (body && body.horo_policy_error) showUnavailable(body.horo_policy_error);
      } catch (_e) {}
    } else if (res.ok && isSettingsGet(url, init, req)) {
      // Before the caller sees the response, so its own rendering already has the locks.
      try { update(await res.clone().json()); } catch (_e) {}
    }
    return res;
  };
})();
