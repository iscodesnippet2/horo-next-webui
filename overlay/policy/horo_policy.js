// Admin policy, front end (horo-next-webui). The server enforces it; this shows it.
//
// What the policy locks comes with GET /api/settings as `horo_policy`, and is
// cached for the next page load, whose first paint comes before that request.
// A 503 that carries `horo_policy_error` means no usable policy: the page is
// covered with the reason instead of upstream's error toasts.
//
// Slash commands: with allow_commands only the listed ones run (and /help);
// the others leave the dropdown, and typing one shows a local message instead
// of reaching the agent. The server refuses the ones with an endpoint of their own.
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

  // Spellings folded into the name the policy uses (api/horo_policy.py COMMAND_ALIASES).
  const ALIASES = {compact: 'compress', use: 'skills', resume: 'sessions', reload_mcp: 'reload-mcp',
                   reload_skills: 'reload-skills', codex_runtime: 'codex-runtime'};
  const commandName = (name, source) => {
    if (source === 'skill' || source === 'bundle') return 'skills';
    if (source === 'plugin') return 'plugins';
    const n = String(name || '').trim().toLowerCase();
    return ALIASES[n] || n;
  };
  // source as getMatchingCommands tags its rows: builtin, agent, plugin, skill, bundle.
  window._horoCommandAllowed = function(name, source){
    const n = commandName(name, source);
    if (n === 'help') return true;
    if (!policy) return true;  // the page asks the server before running one (_horoRefuseBlockedCommand)
    if (list('deny_commands').includes(n)) return false;
    return !Array.isArray(policy.allow_commands) || policy.allow_commands.includes(n);
  };
  const markYolo = () => document.documentElement.classList.toggle('horo-no-yolo', !window._horoCommandAllowed('yolo'));
  markYolo();

  // What a typed /name runs, as {name, source}; null when nothing does, so the
  // text is sent as written ("/etc/hosts").
  async function commandFor(name){
    const n = String(name || '').trim().toLowerCase();
    if (!n) return null;
    if (typeof COMMANDS !== 'undefined' && COMMANDS.some((c) => c.name === n)) return {name: n, source: 'builtin'};
    // The agent commands send() runs itself, known with or without the agent's registry.
    const webui = [typeof _WEBUI_DISPATCHABLE_AGENT_COMMANDS !== 'undefined' && _WEBUI_DISPATCHABLE_AGENT_COMMANDS,
                   typeof _AGENT_COMMANDS_RUN_ON_WEBUI !== 'undefined' && _AGENT_COMMANDS_RUN_ON_WEBUI];
    if (n === 'resume' || webui.some((set) => set && set.has(n))) return {name: n, source: 'agent'};
    const meta = typeof getAgentCommandMetadata === 'function' ? await getAgentCommandMetadata(n) : null;
    if (meta) return {name: String(meta.name || n), source: meta.category === 'Plugin' ? 'plugin' : 'agent'};
    // Skills and bundles matter only when the policy refuses them.
    if (window._horoCommandAllowed('skills')) return null;
    const find = (rows) => (Array.isArray(rows) ? rows : []).find((r) => String(r && r.name || '').toLowerCase() === n);
    if (typeof loadBundleCommands === 'function' && find(await loadBundleCommands())) return {name: n, source: 'bundle'};
    if (typeof loadSkillCommands === 'function' && find(await loadSkillCommands())) return {name: n, source: 'skill'};
    return null;
  }

  // Called by send() before any command runs. True when it refused the text.
  window._horoRefuseBlockedCommand = async function(text){
    const parsed = typeof parseCommand === 'function' ? parseCommand(String(text || '')) : null;
    if (!parsed || !parsed.name) return false;
    if (!policy || !Array.isArray(policy.deny_commands)) {
      try { if (typeof api === 'function') await api('/api/settings'); } catch (_e) {}
    }
    const hit = await commandFor(parsed.name);
    if (!hit) return false;
    // No policy even now: the server refuses everything else, so refuse this too.
    const allowed = policy && window._horoCommandAllowed(hit.name, hit.source);
    if (allowed) return false;
    if (!S.session && typeof newSession === 'function') await newSession();
    S.messages.push({role: 'user', content: String(text), _ts: Date.now() / 1000});
    S.messages.push({role: 'assistant', content: policy ? `此指令已被管理員停用：\`/${parsed.name}\``
      : '管理員政策（admin policy）無法載入，指令暫停使用。', _ts: Date.now() / 1000});
    renderMessages();
    $('msg').value = '';
    if (typeof autoResize === 'function') autoResize();
    if (typeof hideCmdDropdown === 'function') hideCmdDropdown();
    return true;
  };

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
              block_features: view.block_features || [],
              allow_commands: Array.isArray(view.allow_commands) ? view.allow_commands : null,
              deny_commands: view.deny_commands || []};
    try { localStorage.setItem(KEY, JSON.stringify(policy)); } catch (_e) {}
    markYolo();
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
