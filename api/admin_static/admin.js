const state = {
  config: null,
  status: null,
  fields: new Map(),
  localStatus: new Map(),
  modelOptions: [],
  oauthProviderStatus: {},
  gatewayAccounts: [],
};

const MASKED_SECRET = "********";
const HIDDEN_PROVIDERS = new Set([
  "wafer",
  "opencode",
  "github_models",
  "antigravity",
]);
const HIDDEN_FIELD_KEYS = new Set([
  "GITHUB_MODELS_API_KEY",
  "GEMINI_API_KEY",
  "GEMINI_BASE_URL",
  "GOOGLE_OAUTH_CLIENT_ID",
  "GOOGLE_OAUTH_CLIENT_SECRET",
  "GOOGLE_OAUTH_SCOPES",
  "GITHUB_OAUTH_CLIENT_ID",
  "GITHUB_OAUTH_CLIENT_SECRET",
]);
const OAUTH_ONLY_PROVIDERS = new Set([]);

const byId = (id) => document.getElementById(id);

function sourceLabel(source) {
  const labels = {
    default: "default",
    template: "template",
    repo_env: "repo .env",
    managed_env: "managed",
    explicit_env_file: "FCC_ENV_FILE",
    process: "process env",
  };
  return labels[source] || source;
}

function providerName(providerId) {
  const names = {
    nvidia_nim: "NVIDIA NIM",
    openai: "OpenAI",
    anthropic: "Anthropic",
    open_router: "OpenRouter",
    deepseek: "DeepSeek",
    lmstudio: "LM Studio",
    llamacpp: "llama.cpp",
    ollama: "Ollama",
    kimi: "Kimi",
    groq: "Groq",
    cerebras: "Cerebras",
    mistral: "Mistral",
    cohere: "Cohere",
    github_models: "GitHub Models",
    gemini: "Gemini API",
  };
  if (names[providerId]) return names[providerId];
  return providerId
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

async function quickAddProviderApiKey(providerId, providedKey = null) {
  if (OAUTH_ONLY_PROVIDERS.has(providerId)) {
    showMessage(`${providerName(providerId)} uses OAuth flow.`, "error");
    return;
  }
  const apiKey =
    (providedKey ?? window.prompt(`Enter API key for ${providerName(providerId)}`))
      ?.trim() || "";
  if (!apiKey) return;
  const now = Date.now();
  await api("/admin/api/gateway/accounts", {
    method: "POST",
    body: JSON.stringify({
      provider_id: providerId,
      account_key: `${providerId}-key-${now}`,
      auth_backend_key: "api_key_default",
      label: "default",
      api_key: apiKey,
      max_requests_per_day: null,
      max_tokens_per_day: null,
      enabled: true,
    }),
  });
  await safeRefreshGatewayDashboard({ silent: true });
  showMessage(`${providerName(providerId)} API key saved`, "ok");
}

function setSelectedApiProvider(providerId) {
  const input = byId("gaProvider");
  const hint = byId("gaProviderHint");
  if (!input || !hint) return;
  const normalized = String(providerId || "").trim();
  input.value = normalized;
  if (!normalized) {
    hint.textContent = "Click a provider card above to select provider.";
    return;
  }
  if (OAUTH_ONLY_PROVIDERS.has(normalized)) {
    hint.textContent = `${providerName(normalized)} uses OAuth. Use the Connect button instead of API key form.`;
    return;
  }
  hint.textContent = `Selected provider: ${providerName(normalized)}`;
}

function statusClass(status) {
  if (["configured", "reachable", "running"].includes(status)) return "ok";
  if (["missing_key", "missing_url", "unknown"].includes(status)) return "warn";
  if (["offline", "error"].includes(status)) return "error";
  return "neutral";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    cache: "no-store",
    ...options,
  });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = payload?.detail ? `: ${payload.detail}` : "";
    } catch {
      try {
        const text = await response.text();
        detail = text ? `: ${text.slice(0, 240)}` : "";
      } catch {
        detail = "";
      }
    }
    throw new Error(`${response.status} ${response.statusText}${detail}`);
  }
  return response.json();
}

async function load() {
  showMessage("Loading admin config");
  const [config, status] = await Promise.all([
    api("/admin/api/config"),
    api("/admin/api/status"),
  ]);
  state.config = config;
  state.status = status;
  state.fields = new Map(config.fields.map((field) => [field.key, field]));
  updateHeader(status);
  renderNav(config.sections);
  renderProviders(config.provider_status);
  setSelectedApiProvider(byId("gaProvider")?.value || "");
  renderSections(config.sections, config.fields);
  byId("configPath").textContent = config.paths.managed;
  await validate(false);
  await refreshLocalStatus();
  try {
    await refreshGatewayDashboard();
  } catch {
    // Gateway runtime can be unavailable on bare test apps without lifespan startup.
  }
  updateDirtyState();
  showMessage("");
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function updateHeader(status) {
  const serverStatus = byId("serverStatus");
  serverStatus.textContent = "Running";
  serverStatus.className = "status-pill ok";
  byId("modelBadge").textContent = status.model || "";
}

function renderNav(sections) {
  const nav = byId("sectionNav");
  nav.innerHTML = "";
  sections.forEach((section, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `nav-link${index === 0 ? " active" : ""}`;
    button.textContent = section.label;
    button.addEventListener("click", () => {
      document.querySelectorAll(".nav-link").forEach((link) => {
        link.classList.remove("active");
      });
      button.classList.add("active");
      byId(`section-${section.id}`).scrollIntoView({ behavior: "smooth" });
    });
    nav.appendChild(button);
  });
}

function renderProviders(providerStatus) {
  const grid = byId("providerGrid");
  grid.innerHTML = "";
  providerStatus
    .filter((provider) => !HIDDEN_PROVIDERS.has(provider.provider_id))
    .forEach((provider) => {
    const card = document.createElement("article");
    card.className = "provider-card";
    card.dataset.provider = provider.provider_id;

    const title = document.createElement("div");
    title.className = "provider-title";
    title.innerHTML = `<strong>${providerName(provider.provider_id)}</strong>`;

    const pill = document.createElement("span");
    pill.className = `status-pill ${statusClass(provider.status)}`;
    pill.textContent = provider.label;
    title.appendChild(pill);

    const meta = document.createElement("div");
    meta.className = "provider-meta";
    meta.textContent =
      provider.kind === "local"
        ? provider.base_url || "No local URL configured"
        : provider.credential_env;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "test-button";
    button.textContent = provider.kind === "local" ? "Test" : "Refresh models";
    button.addEventListener("click", () => testProvider(provider.provider_id, button));

    card.style.cursor = "pointer";
    card.addEventListener("click", async (e) => {
      if (e.target.tagName === "BUTTON") return;
      setSelectedApiProvider(provider.provider_id);
      await quickAddProviderApiKey(provider.provider_id);
    });

    card.append(title, meta, button);
      grid.appendChild(card);
    });
}

function updateProviderCard(providerId, status, label, metaText) {
  const card = document.querySelector(`[data-provider="${providerId}"]`);
  if (!card) return;
  const pill = card.querySelector(".status-pill");
  pill.className = `status-pill ${statusClass(status)}`;
  pill.textContent = label;
  if (metaText) {
    card.querySelector(".provider-meta").textContent = metaText;
  }
}

function renderSections(sections, fields) {
  const container = byId("formSections");
  container.innerHTML = "";
  const bySection = new Map();
  sections.forEach((section) => bySection.set(section.id, []));
  fields.forEach((field) => {
    if (HIDDEN_FIELD_KEYS.has(field.key)) return;
    if (!bySection.has(field.section)) bySection.set(field.section, []);
    bySection.get(field.section).push(field);
  });

  sections.forEach((section) => {
    const sectionEl = document.createElement("section");
    sectionEl.className = "settings-section";
    sectionEl.id = `section-${section.id}`;

    const heading = document.createElement("div");
    heading.className = "section-heading";
    heading.innerHTML = `<div><h3>${section.label}</h3><p>${section.description}</p></div>`;
    sectionEl.appendChild(heading);

    const grid = document.createElement("div");
    grid.className = "field-grid";
    bySection.get(section.id).forEach((field) => {
      grid.appendChild(renderField(field));
    });
    sectionEl.appendChild(grid);

    if (section.id === "agents") {
      const panel = document.createElement("div");
      panel.className = "gateway-card glass-panel";
      panel.style.marginTop = "12px";
      panel.innerHTML = `
        <h4 style="margin-top:0;">Agents Control Center</h4>
        <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px;">
          <button class="secondary-button" type="button" onclick="window.agentsImportAll(true)">Import All Skills</button>
          <button class="secondary-button" type="button" onclick="window.agentsImportAll(false)">Disable All</button>
          <button class="secondary-button" type="button" onclick="window.agentsSyncEnabled()">Sync Enabled To Claude</button>
          <button class="secondary-button" type="button" onclick="window.agentsRescan()">Rescan</button>
        </div>
        <div id="agentsConfigMeta" class="field-description">Loading agents catalog…</div>
        <div id="agentsConfigRoles" style="margin-top:10px;"></div>
      `;
      sectionEl.appendChild(panel);
    }

    if (bySection.get(section.id).some((field) => field.advanced)) {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "ghost-button advanced-toggle";
      toggle.textContent = "Show advanced";
      toggle.addEventListener("click", () => {
        const showing = sectionEl.classList.toggle("show-advanced");
        toggle.textContent = showing ? "Hide advanced" : "Show advanced";
      });
      sectionEl.appendChild(toggle);
    }

    container.appendChild(sectionEl);
  });
}

function renderAgentsConfigPanel(rows, summary) {
  const meta = byId("agentsConfigMeta");
  const rolesRoot = byId("agentsConfigRoles");
  if (!meta || !rolesRoot) return;
  const syncField = state.fields.get("AGENTS_SYNC_TARGETS");
  const syncTargets = (syncField?.value || "").trim() || "~/.claude/agents (default)";
  meta.textContent = `Catalog: ${Number(summary.total_catalog || 0)} · Installed: ${Number(summary.installed || 0)} · Enabled: ${Number(summary.enabled || 0)} · Synced: ${Number(summary.synced || 0)} · Claude sync targets: ${syncTargets}`;

  const groups = new Map();
  rows.forEach((row) => {
    const key = String(row.role || row.category || "general");
    const bucket = groups.get(key) || [];
    bucket.push(row);
    groups.set(key, bucket);
  });
  const blocks = [...groups.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([role, items]) => {
      const enabled = items.filter((x) => x.enabled).length;
      const encodedRole = encodeURIComponent(role);
      const sub = items
        .slice(0, 10)
        .map((x) => {
          const stateLabel = x.enabled ? "ON" : "OFF";
          return `<div style="display:flex; justify-content:space-between; gap:8px; padding:4px 0;">
            <span>${esc(x.sub_role || x.title)}</span>
            <button class="ghost-button" onclick="window.agentToggle('${x.agent_key}', ${x.enabled ? "false" : "true"})">${stateLabel}</button>
          </div>`;
        })
        .join("");
      const more = items.length > 10 ? `<div class="field-description">+${items.length - 10} more sub agents (use Gateway Agents table for full list)</div>` : "";
      return `<article class="gateway-card glass-panel" style="margin-bottom:10px;">
        <div style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
          <strong>${esc(role)}</strong>
          <span class="field-description">${enabled}/${items.length} enabled</span>
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap; margin:8px 0;">
          <button class="test-button btn-enable" onclick="window.agentToggleRole('${encodedRole}', true)">Enable Role</button>
          <button class="test-button btn-disable" onclick="window.agentToggleRole('${encodedRole}', false)">Disable Role</button>
        </div>
        ${sub}
        ${more}
      </article>`;
    })
    .join("");
  rolesRoot.innerHTML = blocks || `<div class="field-description">No agents discovered yet. Click "Rescan".</div>`;
}

function renderField(field) {
  const wrapper = document.createElement("div");
  wrapper.className = `field${field.advanced ? " advanced-field" : ""}`;
  wrapper.dataset.key = field.key;

  const label = document.createElement("label");
  label.htmlFor = `field-${field.key}`;
  label.innerHTML = `<span>${field.label}</span><span class="field-source">${sourceLabel(
    field.source,
  )}${field.locked ? " locked" : ""}</span>`;

  const input = inputForField(field);
  input.id = `field-${field.key}`;
  input.dataset.key = field.key;
  input.dataset.original = field.value || "";
  input.dataset.secret = field.secret ? "true" : "false";
  input.dataset.configured = field.configured ? "true" : "false";
  input.disabled = field.locked;
  input.addEventListener("input", updateDirtyState);
  input.addEventListener("change", updateDirtyState);

  wrapper.append(label, input);
  if (field.description) {
    const description = document.createElement("div");
    description.className = "field-description";
    description.textContent = field.description;
    wrapper.appendChild(description);
  }
  return wrapper;
}

function inputForField(field) {
  if (field.type === "boolean") {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = String(field.value).toLowerCase() === "true";
    input.dataset.original = input.checked ? "true" : "false";
    return input;
  }

  if (field.type === "tri_boolean") {
    const select = document.createElement("select");
    [
      ["", "Inherit"],
      ["true", "Enabled"],
      ["false", "Disabled"],
    ].forEach(([value, label]) => select.appendChild(option(value, label)));
    select.value = field.value || "";
    return select;
  }

  if (field.type === "select") {
    const select = document.createElement("select");
    field.options.forEach((value) => select.appendChild(option(value, value)));
    select.value = field.value || field.options[0] || "";
    return select;
  }

  if (field.type === "textarea") {
    const textarea = document.createElement("textarea");
    textarea.value = field.value || "";
    return textarea;
  }

  const input = document.createElement("input");
  input.type = field.type === "number" ? "number" : "text";
  if (field.type === "secret") {
    input.type = "password";
    input.placeholder = field.configured
      ? "Configured - enter a new value to replace"
      : "Not configured";
    input.value = "";
    input.autocomplete = "off";
  } else {
    input.value = field.value || "";
  }
  if (field.key.startsWith("MODEL")) {
    input.setAttribute("list", "model-options");
  }
  return input;
}

function option(value, label) {
  const optionEl = document.createElement("option");
  optionEl.value = value;
  optionEl.textContent = label;
  return optionEl;
}

function readFieldValue(input) {
  if (input.type === "checkbox") return input.checked ? "true" : "false";
  if (input.dataset.secret === "true" && input.dataset.configured === "true") {
    return input.value ? input.value : MASKED_SECRET;
  }
  return input.value;
}

function changedValues() {
  const values = {};
  document.querySelectorAll("[data-key]").forEach((input) => {
    if (input.disabled || !input.matches("input, select, textarea")) return;
    const value = readFieldValue(input);
    if (value !== input.dataset.original) {
      values[input.dataset.key] = value;
    }
  });
  return values;
}

function updateDirtyState() {
  const count = Object.keys(changedValues()).length;
  byId("dirtyState").textContent =
    count === 0 ? "No changes" : `${count} unsaved change${count === 1 ? "" : "s"}`;
  byId("applyButton").disabled = count === 0;
}

async function validate(showResult = true) {
  const result = await api("/admin/api/config/validate", {
    method: "POST",
    body: JSON.stringify({ values: changedValues() }),
  });
  byId("envPreview").textContent = result.env_preview || "";
  if (showResult) {
    showValidationResult(result);
  }
  return result;
}

function showValidationResult(result) {
  if (result.valid) {
    showMessage("Config shape is valid", "ok");
  } else {
    showMessage(result.errors.join("; "), "error");
  }
}

async function apply() {
  const result = await api("/admin/api/config/apply", {
    method: "POST",
    body: JSON.stringify({ values: changedValues() }),
  });
  byId("envPreview").textContent = result.env_preview || "";
  if (!result.applied) {
    showValidationResult(result);
    return;
  }
  const restart = result.restart || {};
  if (restart.required && restart.automatic) {
    showMessage("Applied. Restarting server...", "ok");
    byId("applyButton").disabled = true;
    setTimeout(() => {
      window.location.href = restart.admin_url || "/admin";
    }, 1600);
    return;
  }
  const pending = restart.required ? restart.fields || [] : result.pending_fields || [];
  await load();
  showMessage(
    pending.length
      ? `Applied. Restart fcc-server to use: ${pending.join(", ")}`
      : "Applied",
    "ok",
  );
}

async function refreshLocalStatus() {
  const result = await api("/admin/api/providers/local-status");
  result.providers.forEach((provider) => {
    state.localStatus.set(provider.provider_id, provider);
    const meta = provider.status_code
      ? `${provider.base_url} returned HTTP ${provider.status_code}`
      : provider.base_url;
    updateProviderCard(provider.provider_id, provider.status, provider.label, meta);
  });
  syncLocalStatusWithGatewayAccounts();
}

function syncLocalStatusWithGatewayAccounts() {
  if (!Array.isArray(state.gatewayAccounts) || !state.gatewayAccounts.length) return;
  const enabledByProvider = new Map();
  for (const row of state.gatewayAccounts) {
    if (!row || !row.provider_id) continue;
    const current = enabledByProvider.get(row.provider_id) || 0;
    if (row.enabled) enabledByProvider.set(row.provider_id, current + 1);
  }
  enabledByProvider.forEach((count, providerId) => {
    if (count <= 0) return;
    const existing = state.localStatus.get(providerId);
    if (existing && existing.status === "configured") return;
    updateProviderCard(providerId, "configured", "Configured", `${count} account(s) in gateway`);
  });
}

async function testProvider(providerId, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Testing";
  try {
    const result = await api(`/admin/api/providers/${providerId}/test`, {
      method: "POST",
      body: "{}",
    });
    if (result.ok) {
      updateProviderCard(
        providerId,
        "reachable",
        `${result.models.length} models`,
        result.models.slice(0, 3).join(", ") || "No models returned",
      );
      state.modelOptions = Array.from(
        new Set([...state.modelOptions, ...result.models.map((model) => `${providerId}/${model}`)]),
      ).sort();
      syncModelDatalist();
    } else {
      updateProviderCard(providerId, "offline", result.error_type, result.error_type);
    }
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function refreshGatewayDashboard() {
  const data = await api("/admin/api/gateway/dashboard");
  const providers = data.providers || [];
  state.gatewayAccounts = data.accounts || [];
  const recentRequests = data.recent_requests || [];
  const activeProviderSet = new Set(
    providers
      .filter((provider) => provider.enabled && !HIDDEN_PROVIDERS.has(provider.provider_id))
      .map((provider) => provider.provider_id),
  );
  const recentWindowCutoff = (Date.now() / 1000) - (6 * 3600);
  const realtimeRequests = recentRequests.filter(
    (row) =>
      activeProviderSet.has(row.provider_id) &&
      Number(row.created_at || 0) >= recentWindowCutoff,
  );

  renderGatewayProviders(data.providers || []);
  renderGatewayAccounts(state.gatewayAccounts);
  renderGatewayUsage(data.daily_usage || []);
  renderGatewayCosts(data.cost_analytics || {});
  renderGatewayLiveGraphs(realtimeRequests);
  renderGatewayFlowViz(realtimeRequests);
  renderGatewayRotationUI(data.accounts || []);
  renderGatewayRequests(data.recent_requests || []);
  renderGatewayCircuits(data.circuit_breakers || {});
  renderGatewayBenchmarks(data.benchmarks || []);
  renderGatewayQueue(data.queue || {});
  renderGatewayCapabilityProbes(data.capability_probes || []);
  renderGatewayTraces(data.traces || []);
  renderGatewayConfigVersions(data.config_versions || []);
  renderGatewayAuthBackends(data.auth_backends || []);
  renderGatewayOAuthAccounts(data.oauth_accounts || []);
  renderGatewayOAuthSessions(data.oauth_sessions || []);
  renderGatewayAgents(data.agents || [], data.agent_summary || {});
  renderAgentsConfigPanel(data.agents || [], data.agent_summary || {});
  renderOAuthEcosystems(
    state.gatewayAccounts,
    data.oauth_accounts || [],
    data.oauth_provider_status || {},
  );
  syncLocalStatusWithGatewayAccounts();
  
  // Modern Dashboard rendering
  renderDashActiveModels(providers, realtimeRequests);
  renderDashModelAccuracy(data.benchmarks || [], realtimeRequests);
  renderDashModelMetrics(realtimeRequests);
  renderDashModelStatusCards(realtimeRequests);
}

let _refreshInFlight = false;
async function safeRefreshGatewayDashboard({ silent = false } = {}) {
  if (_refreshInFlight) return;
  _refreshInFlight = true;
  try {
    await refreshGatewayDashboard();
    if (!silent) {
      showMessage(`Gateway refreshed at ${new Date().toLocaleTimeString()}`, "ok");
    }
  } catch (error) {
    showMessage(`Gateway refresh failed: ${error.message}`, "error");
    throw error;
  } finally {
    _refreshInFlight = false;
  }
}

window.showActiveModelsModal = function() {
  if (!window._currentProviders || !window._currentRequests) return;
  const activeProviders = window._currentProviders.filter(p => !HIDDEN_PROVIDERS.has(p.provider_id) && p.enabled);
  const modelsByProvider = {};
  window._currentRequests.forEach(r => {
    if(!modelsByProvider[r.provider_id]) modelsByProvider[r.provider_id] = new Set();
    if(r.gateway_model) modelsByProvider[r.provider_id].add(r.gateway_model);
  });
  
  const modal = document.createElement("div");
  modal.style.position = "fixed";
  modal.style.top = "0"; modal.style.left = "0"; modal.style.width = "100%"; modal.style.height = "100%";
  modal.style.backgroundColor = "rgba(0,0,0,0.8)";
  modal.style.display = "flex"; modal.style.alignItems = "center"; modal.style.justifyContent = "center";
  modal.style.zIndex = "9999";
  
  let html = `<div class="glass-panel" style="padding: 24px; width: 400px; border-radius: 8px; background: var(--panel-strong); border: 1px solid var(--line);">`;
  html += `<h3 style="margin-top: 0; color: var(--accent); border-bottom: 1px solid var(--line); padding-bottom: 10px;">Active Models</h3>`;
  html += `<ul style="list-style: none; padding: 0; margin: 0; max-height: 300px; overflow-y: auto;">`;
  activeProviders.forEach(p => {
    const models = modelsByProvider[p.provider_id] ? Array.from(modelsByProvider[p.provider_id]).join(", ") : "Running";
    html += `<li style="padding: 12px 0; border-bottom: 1px solid var(--line); color: var(--text); display: flex; flex-direction: column;">
               <div style="display: flex; justify-content: space-between; align-items: center;">
                 <span style="font-weight: bold;">${providerName(p.provider_id)}</span>
                 <span style="color: var(--ok); font-size: 12px;">● Online</span>
               </div>
               <div style="font-size: 11px; color: var(--muted); margin-top: 4px;">Models: ${models}</div>
             </li>`;
  });
  if(activeProviders.length === 0) {
    html += `<li style="padding: 12px 0; color: var(--muted);">No active models found.</li>`;
  }
  html += `</ul>`;
  html += `<button onclick="this.parentElement.parentElement.remove()" style="margin-top: 20px; padding: 10px 20px; width: 100%; background: var(--btn-bg); color: #fff; font-weight: bold; border: none; border-radius: 4px; cursor: pointer;">Close</button>`;
  html += `</div>`;
  modal.innerHTML = html;
  document.body.appendChild(modal);
}

function renderDashActiveModels(providers, requests) {
  window._currentProviders = providers;
  window._currentRequests = requests;
  const container = byId("dashActiveModels");
  if (!container) return;
  const active = providers.filter(p => !HIDDEN_PROVIDERS.has(p.provider_id) && p.enabled).length;
  
  container.innerHTML = `
    <div onclick="window.showActiveModelsModal()" style="cursor: pointer; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%;">
      <div style="font-size: 64px; font-weight: bold; color: var(--accent); line-height: 1;">${active}</div>
      <div style="margin-top: 10px; color: var(--muted); font-size: 14px; text-decoration: underline;">Online Providers</div>
    </div>
  `;
}

function renderDashModelAccuracy(benchmarks, requests) {
  const container = byId("dashModelAccuracy");
  if (!container) return;
  const total = requests.length || 0;
  if (total === 0) {
    container.innerHTML = `
      <div class="accuracy-text">-</div>
      <div class="accuracy-bar-container">
        <div class="accuracy-bar-fill" style="width: 0%;"></div>
      </div>
      <div style="margin-top: 10px; color: var(--muted); font-size: 14px;">No recent requests</div>
    `;
    return;
  }
  const success = requests.filter(r => r.success).length;
  const accuracy = ((success / total) * 100).toFixed(1);
  
  container.innerHTML = `
    <div class="accuracy-text">${accuracy}%</div>
    <div class="accuracy-bar-container">
      <div class="accuracy-bar-fill" style="width: ${accuracy}%;"></div>
    </div>
    <div style="margin-top: 10px; color: var(--muted); font-size: 14px;">Recent Global Accuracy</div>
  `;
}

function renderDashModelMetrics(requests) {
  const container = byId("dashModelMetrics");
  if (!container) return;
  
  let latencies = requests.map(r => r.latency_ms || 0).filter(l => l > 0).slice(0, 20).reverse();
  if (latencies.length === 0) {
    container.innerHTML = `
      <div style="width:100%; text-align: left; font-size: 18px; font-weight: bold; color: var(--accent);">-</div>
      <div style="font-size: 12px; color: var(--muted); align-self: flex-start;">No latency samples</div>
    `;
    return;
  }
  while(latencies.length < 20) latencies.unshift(latencies[0]);
  const avg = (latencies.reduce((a,b)=>a+b,0)/latencies.length).toFixed(0);
  const max = Math.max(...latencies) || 100;
  
  let pathStr = "M 0 40 ";
  for (let i = 0; i < latencies.length; i++) {
    const x = i * 5;
    const y = 40 - ((latencies[i] / max) * 40);
    pathStr += `L ${x} ${y} `;
  }
  pathStr += "L 100 40 Z";
  const lineStr = pathStr.replace("L 100 40 Z", "").replace("M 0 40 L", "M");

  container.innerHTML = `
    <div style="width:100%; text-align: left; font-size: 18px; font-weight: bold; color: var(--accent);">${avg}ms</div>
    <div style="font-size: 12px; color: var(--muted); align-self: flex-start;">Avg Latency</div>
    <div style="position: relative; width: 100%; height: 80px; margin-top: 10px; border: 1px solid var(--accent); background: rgba(220,38,38,0.05);">
      <div style="position: absolute; top: 25%; width: 100%; height: 1px; background: rgba(220,38,38,0.2);"></div>
      <div style="position: absolute; top: 50%; width: 100%; height: 1px; background: rgba(220,38,38,0.2);"></div>
      <div style="position: absolute; top: 75%; width: 100%; height: 1px; background: rgba(220,38,38,0.2);"></div>
      <div style="position: absolute; left: 25%; width: 1px; height: 100%; background: rgba(220,38,38,0.2);"></div>
      <div style="position: absolute; left: 50%; width: 1px; height: 100%; background: rgba(220,38,38,0.2);"></div>
      <div style="position: absolute; left: 75%; width: 1px; height: 100%; background: rgba(220,38,38,0.2);"></div>
      <svg viewBox="0 0 100 40" preserveAspectRatio="none" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; overflow: visible;">
        <path d="${pathStr}" fill="rgba(220,38,38,0.2)"/>
        <path d="${lineStr}" fill="none" stroke="var(--accent)" stroke-width="1.5"/>
      </svg>
    </div>
  `;
}

function renderDashModelStatusCards(requests) {
  const container = byId("dashModelStatusCards");
  if (!container) return;
  
  const modelStats = {};
  requests.forEach(r => {
    if(!r.gateway_model) return;
    if(!modelStats[r.gateway_model]) modelStats[r.gateway_model] = { total: 0, success: 0, latSum: 0, latCount: 0 };
    modelStats[r.gateway_model].total++;
    if(r.success) modelStats[r.gateway_model].success++;
    if(r.latency_ms > 0) {
      modelStats[r.gateway_model].latSum += r.latency_ms;
      modelStats[r.gateway_model].latCount++;
    }
  });
  
  const models = Object.keys(modelStats).map(m => {
    const s = modelStats[m];
    const acc = ((s.success / s.total) * 100).toFixed(1);
    const lat = s.latCount > 0 ? Math.round(s.latSum / s.latCount) : 0;
    const status = acc > 95 ? 'ok' : (acc > 80 ? 'warn' : 'error');
    return { name: m, lat: lat + "ms", acc: acc + "%", status: status, total: s.total };
  }).sort((a,b) => b.total - a.total).slice(0, 4);

  if(models.length === 0) {
    container.innerHTML = `<p class="field-description">No model activity in the last 6 hours.</p>`;
    return;
  }
  
  let html = '';
  for (const m of models) {
    const color = m.status === 'ok' ? 'var(--ok)' : (m.status === 'warn' ? 'var(--warn)' : 'var(--error)');
    html += `
      <div class="model-status-card">
        <div class="model-status-header">
          <span class="model-status-name">${m.name}</span>
          <div class="model-status-indicator" style="background: ${color}; color: ${color};"></div>
        </div>
        <div class="model-status-stats">
          <span>Lat: ${m.lat}</span>
          <span>Acc: ${m.acc}</span>
        </div>
      </div>
    `;
  }
  container.innerHTML = html;
}

function renderGatewayCosts(costs) {
  const total = costs.total || {};
  const providers = costs.providers || [];
  const top = providers.slice(0, 5).map((row) => [
    providerName(row.provider_id),
    (row.requests || 0).toLocaleString(),
    Number(row.input_tokens || 0).toLocaleString(),
    Number(row.output_tokens || 0).toLocaleString(),
    `$${Number(row.estimated_cost_usd || 0).toFixed(4)}`,
  ]);
  byId("gatewayCosts").innerHTML = `
    <div style="display:grid; gap:10px; margin-bottom:12px;">
      <div style="display:flex; gap:10px; flex-wrap:wrap;">
        <span class="status-pill neutral">Window: ${Number(costs.window_days || 0)}d</span>
        <span class="status-pill neutral">Req: ${Number(total.requests || 0).toLocaleString()}</span>
        <span class="status-pill neutral">Daily est: $${Number(total.estimated_cost_usd || 0).toFixed(4)}</span>
        <span class="status-pill warn">Monthly est: $${Number(total.projected_monthly_cost_usd || 0).toFixed(4)}</span>
        <span class="status-pill ok">Avg/req: $${Number(total.average_request_cost_usd || 0).toFixed(6)}</span>
      </div>
    </div>
    ${tableHtml(["Provider", "Requests", "Input", "Output", "Cost"], top)}
  `;
}

function renderGatewayProviders(providers) {
  byId("gatewayProviders").innerHTML = tableHtml(
    ["Provider", "Enabled", "Priority", "Actions"],
    providers
      .filter((provider) => !HIDDEN_PROVIDERS.has(provider.provider_id))
      .map((provider) => [
        providerName(provider.provider_id),
        provider.enabled ? "yes" : "no",
        provider.priority,
        `<button class=\"test-button ${provider.enabled ? "btn-disable" : "btn-enable"}\" onclick=\"window.gatewayToggleProvider('${provider.provider_id}', ${provider.enabled ? "false" : "true"})\">${provider.enabled ? "Disable" : "Enable"}</button>`,
      ]),
  );
}

function renderGatewayAccounts(accounts) {
  byId("gatewayAccounts").innerHTML = tableHtml(
    ["ID", "Provider", "Backend", "Label", "Type", "Enabled", "Req/day", "Tok/day", "Health", "Cooldown", "Actions"],
    accounts
      .filter((account) => !HIDDEN_PROVIDERS.has(account.provider_id))
      .map((account) => [
        account.account_id,
        providerName(account.provider_id),
        esc(account.metadata?.auth_backend_key || "-"),
        esc(account.label),
        esc(account.account_type),
        account.enabled ? "yes" : "no",
        account.used_requests_today + (account.max_requests_per_day ? ` / ${account.max_requests_per_day}` : ""),
        account.used_tokens_today + (account.max_tokens_per_day ? ` / ${account.max_tokens_per_day}` : ""),
        Number(account.health_score || 0).toFixed(2),
        account.cooldown_until ? new Date(account.cooldown_until * 1000).toLocaleTimeString() : "-",
        `<button class=\"test-button ${account.enabled ? "btn-disable" : "btn-enable"}\" onclick=\"window.gatewayToggleAccount(${account.account_id}, ${account.enabled ? "false" : "true"})\">${account.enabled ? "Disable" : "Enable"}</button>
<button class=\"ghost-button\" onclick=\"window.gatewayDeleteAccount(${account.account_id})\">Delete</button>`,
      ]),
  );
}

function renderGatewayUsage(rows) {
  const visibleRows = rows.filter((row) => !HIDDEN_PROVIDERS.has(row.provider_id));
  const maxRequests = Math.max(1, ...visibleRows.map(r => r.requests || 0));
  const maxTokens = Math.max(1, ...visibleRows.map(r => r.tokens || 0));

  byId("gatewayUsage").innerHTML = tableHtml(
    ["Day", "Provider", "Account", "Requests", "Tokens", "Failures", "Retries", "Fallbacks"],
    visibleRows.map((row) => [
      row.day,
      providerName(row.provider_id),
      row.account_id ?? "-",
      `<div class="usage-bar-container">
         <span style="min-width: 40px">${(row.requests || 0).toLocaleString()}</span>
         <div class="usage-bar"><div class="usage-bar-fill" style="width: ${((row.requests || 0) / maxRequests) * 100}%; background: var(--accent)"></div></div>
       </div>`,
      `<div class="usage-bar-container">
         <span style="min-width: 50px; color: var(--info)">${(row.tokens || 0).toLocaleString()}</span>
         <div class="usage-bar"><div class="usage-bar-fill" style="width: ${((row.tokens || 0) / maxTokens) * 100}%; background: var(--info)"></div></div>
       </div>`,
      `<span style="color: ${row.failures > 0 ? 'var(--error)' : 'inherit'}; font-weight: ${row.failures > 0 ? 'bold' : 'normal'}">${row.failures || 0}</span>`,
      row.retries || 0,
      row.fallback_events || 0,
    ]),
  );
}

function renderGatewayLiveGraphs(requests) {
  const container = byId("gatewayLiveGraphs");
  if (!container) return;
  if (!requests.length) {
    container.innerHTML = `<p class="field-description">No traffic in the last 6 hours.</p>`;
    return;
  }
  
  const buckets = Array(21).fill(0);
  const now = Date.now() / 1000;
  requests.forEach(r => {
    const ageSeconds = now - r.created_at;
    const bucketIdx = 20 - Math.floor(ageSeconds / 60);
    if(bucketIdx >= 0 && bucketIdx <= 20) {
      buckets[bucketIdx]++;
    }
  });
  const maxReq = Math.max(...buckets) || 10;
  
  let pathStr = "M 0 100 ";
  for (let i = 0; i <= 20; i++) {
    const x = i * 5;
    const y = 100 - ((buckets[i] / maxReq) * 100);
    pathStr += `L ${x} ${y} `;
  }
  pathStr += "L 100 100 Z";
  const lineStr = pathStr.replace("L 100 100 Z", "").replace("M 0 100 L", "M");

  container.innerHTML = `
    <div style="display: flex; width: 100%; height: 240px; margin-top: 10px;">
      <!-- Y-Axis Labels -->
      <div style="display: flex; flex-direction: column; justify-content: space-between; align-items: flex-end; padding-right: 10px; color: var(--muted); font-size: 11px; height: 100%;">
        <span>${maxReq} req/m</span>
        <span>${Math.round(maxReq * 0.75)}</span>
        <span>${Math.round(maxReq * 0.5)}</span>
        <span>${Math.round(maxReq * 0.25)}</span>
        <span>0</span>
      </div>
      <!-- Chart Area -->
      <div style="flex: 1; position: relative; border: 1px solid var(--accent); background: rgba(220,38,38,0.05);">
        <!-- Grid lines horizontal -->
        <div style="position: absolute; top: 25%; width: 100%; height: 1px; background: rgba(220,38,38,0.2);"></div>
        <div style="position: absolute; top: 50%; width: 100%; height: 1px; background: rgba(220,38,38,0.2);"></div>
        <div style="position: absolute; top: 75%; width: 100%; height: 1px; background: rgba(220,38,38,0.2);"></div>
        <!-- Grid lines vertical -->
        <div style="position: absolute; left: 10%; width: 1px; height: 100%; background: rgba(220,38,38,0.2);"></div>
        <div style="position: absolute; left: 20%; width: 1px; height: 100%; background: rgba(220,38,38,0.2);"></div>
        <div style="position: absolute; left: 30%; width: 1px; height: 100%; background: rgba(220,38,38,0.2);"></div>
        <div style="position: absolute; left: 40%; width: 1px; height: 100%; background: rgba(220,38,38,0.2);"></div>
        <div style="position: absolute; left: 50%; width: 1px; height: 100%; background: rgba(220,38,38,0.2);"></div>
        <div style="position: absolute; left: 60%; width: 1px; height: 100%; background: rgba(220,38,38,0.2);"></div>
        <div style="position: absolute; left: 70%; width: 1px; height: 100%; background: rgba(220,38,38,0.2);"></div>
        <div style="position: absolute; left: 80%; width: 1px; height: 100%; background: rgba(220,38,38,0.2);"></div>
        <div style="position: absolute; left: 90%; width: 1px; height: 100%; background: rgba(220,38,38,0.2);"></div>
        
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; overflow: visible;">
          <path d="${pathStr}" fill="rgba(220,38,38,0.25)"/>
          <path d="${lineStr}" fill="none" stroke="var(--accent)" stroke-width="1.5"/>
        </svg>
      </div>
    </div>
  `;
}

function renderGatewayFlowViz(requests) {
  const container = byId("gatewayFlowViz");
  if (!container) return;
  if (!requests.length) {
    container.innerHTML = `<p class="field-description">No recent routing decisions to visualize.</p>`;
    return;
  }
  const fallbacks = requests.filter(r => r.fallback_count > 0);
  const total = requests.length || 1;
  const fallbackPct = Math.round((fallbacks.length / total) * 100);
  
  container.innerHTML = `
    <div style="display: flex; align-items: center; justify-content: space-between; padding: 24px; background: rgba(17,17,17,0.4); border-radius: 8px; border: 1px solid var(--line);">
      <div style="text-align: center; z-index: 2;">
        <div style="width: 48px; height: 48px; background: var(--panel-strong); border: 2px solid var(--info); border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 8px;">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--info)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M23 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path></svg>
        </div>
        <div style="font-weight: bold; color: var(--text);">Client</div>
        <div style="font-size: 11px; color: var(--muted);">${requests.length} Requests</div>
      </div>
      
      <div style="flex: 1; height: 2px; background: var(--line); margin: 0 -12px; position: relative;">
         <div style="position: absolute; top: -4px; left: 50%; width: 10px; height: 10px; background: var(--accent); border-radius: 50%; box-shadow: 0 0 10px var(--accent);"></div>
      </div>
      
      <div style="text-align: center; z-index: 2;">
        <div style="width: 48px; height: 48px; background: var(--panel-strong); border: 2px solid var(--accent); border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 8px; box-shadow: 0 0 15px rgba(220, 38, 38, 0.2);">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>
        </div>
        <div style="font-weight: bold; color: var(--accent);">Primary Provider</div>
        <div style="font-size: 11px; color: var(--ok);">${100 - fallbackPct}% Success</div>
      </div>
      
      <div style="flex: 1; height: 2px; background: repeating-linear-gradient(90deg, transparent, transparent 4px, var(--warn) 4px, var(--warn) 8px); margin: 0 -12px; position: relative;">
      </div>
      
      <div style="text-align: center; z-index: 2;">
        <div style="width: 48px; height: 48px; background: var(--panel-strong); border: 2px solid var(--warn); border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 8px;">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--warn)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.5 2v6h-6M2.13 15.57a9 9 0 1 0 3.84-10.36L2 8"></path></svg>
        </div>
        <div style="font-weight: bold; color: var(--warn);">Fallback Pool</div>
        <div style="font-size: 11px; color: var(--muted);">${fallbackPct}% Routed</div>
      </div>
    </div>
  `;
}

function renderGatewayRotationUI(accounts) {
  const container = byId("gatewayRotationUI");
  if (!container) return;
  
  if (!accounts || accounts.length === 0) {
    container.innerHTML = `<p class="field-description">No accounts configured for rotation.</p>`;
    return;
  }
  
  const exhaustedCount = accounts.filter(a => a.enabled && a.max_requests_per_day && a.used_requests_today >= a.max_requests_per_day).length;
  const coolingCount = accounts.filter(a => a.enabled && a.cooldown_until && a.cooldown_until * 1000 > Date.now()).length;
  
  // Active is anything enabled that is neither cooling down nor exhausted.
  const activeCount = accounts.filter(a => 
    a.enabled && 
    (!a.cooldown_until || a.cooldown_until * 1000 <= Date.now()) && 
    (!a.max_requests_per_day || a.used_requests_today < a.max_requests_per_day)
  ).length;
  
  container.innerHTML = `
    <div style="display: flex; gap: 16px; flex-wrap: wrap;">
      <div style="flex: 1; min-width: 120px; padding: 16px; background: rgba(220, 38, 38, 0.05); border: 1px solid rgba(220, 38, 38, 0.2); border-radius: 8px; display: flex; align-items: center; gap: 16px;">
        <div style="width: 40px; height: 40px; border-radius: 50%; background: rgba(220, 38, 38, 0.1); display: flex; align-items: center; justify-content: center;">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><polyline points="20 6 9 17 4 12"></polyline></svg>
        </div>
        <div>
          <div style="font-size: 24px; color: var(--accent); font-weight: bold; line-height: 1;">${activeCount}</div>
          <div style="font-size: 12px; color: var(--muted); margin-top: 4px;">Active Keys</div>
        </div>
      </div>
      
      <div style="flex: 1; min-width: 120px; padding: 16px; background: rgba(245, 158, 11, 0.05); border: 1px solid rgba(245, 158, 11, 0.2); border-radius: 8px; display: flex; align-items: center; gap: 16px;">
        <div style="width: 40px; height: 40px; border-radius: 50%; background: rgba(245, 158, 11, 0.1); display: flex; align-items: center; justify-content: center;">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--warn)" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
        </div>
        <div>
          <div style="font-size: 24px; color: var(--warn); font-weight: bold; line-height: 1;">${coolingCount}</div>
          <div style="font-size: 12px; color: var(--muted); margin-top: 4px;">In Cooldown</div>
        </div>
      </div>
      
      <div style="flex: 1; min-width: 120px; padding: 16px; background: rgba(220, 38, 38, 0.05); border: 1px solid rgba(220, 38, 38, 0.2); border-radius: 8px; display: flex; align-items: center; gap: 16px;">
        <div style="width: 40px; height: 40px; border-radius: 50%; background: rgba(220, 38, 38, 0.1); display: flex; align-items: center; justify-content: center;">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--error)" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>
        </div>
        <div>
          <div style="font-size: 24px; color: var(--error); font-weight: bold; line-height: 1;">${exhaustedCount}</div>
          <div style="font-size: 12px; color: var(--muted); margin-top: 4px;">Quota Exhausted</div>
        </div>
      </div>
    </div>
  `;
}

function renderGatewayRequests(rows) {
  byId("gatewayRequests").innerHTML = tableHtml(
    ["Request", "Model", "Provider", "Account", "OK", "Latency(ms)", "Retries", "Fallbacks", "When"],
    rows
      .filter((row) => !HIDDEN_PROVIDERS.has(row.provider_id))
      .slice(0, 50)
      .map((row) => [
      esc(row.request_id),
      esc(row.gateway_model),
      providerName(row.provider_id),
      row.account_id ?? "-",
      row.success ? "yes" : "no",
      Number(row.latency_ms || 0).toFixed(1),
      row.retries,
      row.fallback_count,
      new Date((row.created_at || 0) * 1000).toLocaleTimeString(),
      ]),
  );
}

function renderGatewayCircuits(snapshot) {
  const rows = Object.entries(snapshot).map(([key, value]) => [
    key,
    value.failures ?? 0,
    value.opened_until
      ? new Date(value.opened_until * 1000).toLocaleTimeString()
      : "-",
    value.half_open ? "yes" : "no",
  ]);
  byId("gatewayCircuits").innerHTML = tableHtml(
    ["Key", "Failures", "Opened Until", "Half-open"],
    rows,
  );
}

function renderGatewayBenchmarks(rows) {
  byId("gatewayBenchmarks").innerHTML = tableHtml(
    ["Provider", "Latency(ms)", "Models", "Success", "Error", "When"],
    rows
      .filter((row) => !HIDDEN_PROVIDERS.has(row.provider_id))
      .map((row) => [
        providerName(row.provider_id),
        Number(row.latency_ms || 0).toFixed(1),
        row.model_count,
        row.success ? "yes" : "no",
        row.error_type || "-",
        new Date((row.created_at || 0) * 1000).toLocaleTimeString(),
      ]),
  );
}

function renderGatewayQueue(queue) {
  const rows = [[
    queue.queued ?? 0,
    queue.inflight ?? 0,
    queue.max_queued ?? 0,
    queue.max_inflight ?? 0,
    queue.rejected ?? 0,
  ]];
  byId("gatewayQueue").innerHTML = tableHtml(
    ["Queued", "Inflight", "Max Queued", "Max Inflight", "Rejected"],
    rows,
  );
}

function renderGatewayCapabilityProbes(rows) {
  byId("gatewayCapabilityProbes").innerHTML = tableHtml(
    ["Provider", "Status", "Required", "Detail", "When"],
    rows
      .filter((row) => !HIDDEN_PROVIDERS.has(row.provider_id))
      .slice(0, 50)
      .map((row) => [
        providerName(row.provider_id),
        esc(row.status),
        esc((row.required_capabilities || []).join(", ")),
        esc(JSON.stringify(row.detail || {})),
        new Date((row.created_at || 0) * 1000).toLocaleTimeString(),
      ]),
  );
}

function renderGatewayTraces(rows) {
  byId("gatewayTraces").innerHTML = tableHtml(
    ["Trace", "Request", "Phase", "When"],
    rows.slice(0, 50).map((row) => [
      row.trace_id,
      esc(row.request_id),
      esc(row.phase),
      new Date((row.created_at || 0) * 1000).toLocaleTimeString(),
    ]),
  );
}

function renderGatewayConfigVersions(rows) {
  byId("gatewayConfigVersions").innerHTML = tableHtml(
    ["Version", "Reason", "When"],
    rows.map((row) => [
      row.version_id,
      esc(row.reason),
      new Date((row.created_at || 0) * 1000).toLocaleString(),
    ]),
  );
}

function renderGatewayAuthBackends(rows) {
  byId("gatewayAuthBackends").innerHTML = tableHtml(
    ["Backend ID", "Provider", "Ecosystem", "Type", "Backend Key", "Label", "Enabled"],
    rows
      .filter((row) => !HIDDEN_PROVIDERS.has(row.provider_id))
      .map((row) => [
        row.backend_id,
        providerName(row.provider_id),
        esc(row.ecosystem_id),
        esc(row.backend_type),
        esc(row.backend_key),
        esc(row.label),
        row.enabled ? "yes" : "no",
      ]),
  );
}

function renderGatewayOAuthAccounts(rows) {
  byId("gatewayOAuthAccounts").innerHTML = tableHtml(
    ["OAuth ID", "Provider", "Backend", "External Account", "Provider Account", "Health", "Token Expires"],
    rows
      .filter((row) => !HIDDEN_PROVIDERS.has(row.provider_id))
      .map((row) => [
        row.oauth_account_id,
        providerName(row.provider_id),
        esc(row.backend_key),
        esc(row.external_account_id),
        row.provider_account_id ?? "-",
        Number(row.health_score || 0).toFixed(2),
        row.token_expires_at ? new Date(row.token_expires_at * 1000).toLocaleString() : "-",
      ]),
  );
}

function renderGatewayOAuthSessions(rows) {
  byId("gatewayOAuthSessions").innerHTML = tableHtml(
    ["Session", "Provider", "Backend", "Status", "Expires", "Created"],
    rows
      .filter((row) => !HIDDEN_PROVIDERS.has(row.provider_id))
      .map((row) => [
        row.session_id,
        providerName(row.provider_id),
        esc(row.backend_key),
        esc(row.status),
        new Date((row.expires_at || 0) * 1000).toLocaleString(),
        new Date((row.created_at || 0) * 1000).toLocaleString(),
      ]),
  );
}

function renderGatewayAgents(rows, summary) {
  const search = (byId("agentsSearch")?.value || "").trim().toLowerCase();
  const filtered = rows.filter((row) => {
    if (!search) return true;
    return (
      String(row.title || "").toLowerCase().includes(search) ||
      String(row.agent_key || "").toLowerCase().includes(search) ||
      String(row.category || "").toLowerCase().includes(search) ||
      String(row.role || "").toLowerCase().includes(search) ||
      String(row.sub_role || "").toLowerCase().includes(search)
    );
  });
  const meta = `<div style="margin-bottom:8px; display:flex; gap:8px; flex-wrap:wrap;">
    <span class="status-pill neutral">Catalog: ${Number(summary.total_catalog || 0)}</span>
    <span class="status-pill neutral">Installed: ${Number(summary.installed || 0)}</span>
    <span class="status-pill ok">Enabled: ${Number(summary.enabled || 0)}</span>
    <span class="status-pill warn">Synced: ${Number(summary.synced || 0)}</span>
  </div>`;
  const groups = new Map();
  for (const row of filtered) {
    const key = String(row.role || row.category || "general");
    const bucket = groups.get(key) || [];
    bucket.push(row);
    groups.set(key, bucket);
  }
  const cards = [...groups.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([role, items]) => {
      const encodedRole = encodeURIComponent(role);
      const enabledCount = items.filter((item) => item.enabled).length;
      const syncedCount = items.filter((item) => item.synced).length;
      const roleHeader = `
        <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:8px;">
          <div>
            <strong>${esc(role)}</strong>
            <div style="font-size:12px;color:var(--muted);">${enabledCount}/${items.length} enabled · ${syncedCount} synced</div>
          </div>
          <div style="display:flex; gap:6px; flex-wrap:wrap;">
            <button class="test-button btn-enable" onclick="window.agentToggleRole('${encodedRole}', true)">Enable Role</button>
            <button class="test-button btn-disable" onclick="window.agentToggleRole('${encodedRole}', false)">Disable Role</button>
          </div>
        </div>
      `;
      const table = tableHtml(
        ["Sub Agent", "Route", "Installed", "Enabled", "Synced", "Actions"],
        items.map((row) => [
          `<strong>${esc(row.sub_role || row.title)}</strong><br /><span style="font-size:11px;color:var(--muted);">${esc(row.description || "")}</span>`,
          esc(
            [
              row.runtime_preferences?.preferred_provider || row.preferred_provider || "-",
              row.runtime_preferences?.preferred_model || row.preferred_model || "-",
            ].join(" / "),
          ),
          row.installed ? "yes" : "no",
          row.enabled ? "yes" : "no",
          row.synced ? "yes" : "no",
          `<button class="test-button" onclick="window.agentInstall('${row.agent_key}')">Install</button>
<button class="test-button ${row.enabled ? "btn-disable" : "btn-enable"}" onclick="window.agentToggle('${row.agent_key}', ${row.enabled ? "false" : "true"})">${row.enabled ? "Off" : "On"}</button>
<button class="ghost-button" onclick="window.agentSync('${row.agent_key}')">Sync</button>`,
        ]),
      );
      return `<article class="gateway-card glass-panel" style="margin-bottom:10px;">${roleHeader}${table}</article>`;
    })
    .join("");

  byId("gatewayAgents").innerHTML = meta + cards;
}

function renderOAuthEcosystems(accounts, oauthAccounts, providerStatus) {
  state.oauthProviderStatus = providerStatus || {};
  const ecosystemSpec = [
    { providerId: "gemini", summaryId: "oauthGoogleSummary", authLabel: "Google / Gemini" },
  ];
  const googleCfg = providerStatus.google || {};
  const googleBtn = byId("oauthGoogleLoginBtn");
  if (googleBtn) {
    const googleReady = Boolean(googleCfg.configured);
    googleBtn.disabled = false;
    googleBtn.title = googleCfg.configured
      ? "Open Google OAuth login flow"
      : "Configure GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET first";
    googleBtn.dataset.configured = googleReady ? "yes" : "no";
  }
  for (const spec of ecosystemSpec) {
    const summary = byId(spec.summaryId);
    if (!summary) continue;
    const providerAccounts = accounts.filter((row) => row.provider_id === spec.providerId);
    const providerOauth = oauthAccounts.filter((row) => row.provider_id === spec.providerId);
    const studentDetected = providerOauth.some(
      (row) => row.metadata?.github_student_eligible === true,
    );
    const active = providerAccounts.filter(
      (row) =>
        row.enabled &&
        (!row.cooldown_until || Number(row.cooldown_until) * 1000 <= Date.now()),
    ).length;
    const cooldown = providerAccounts.filter(
      (row) =>
        row.enabled &&
        row.cooldown_until &&
        Number(row.cooldown_until) * 1000 > Date.now(),
    ).length;
    const totalReq = providerAccounts.reduce(
      (sum, row) => sum + Number(row.used_requests_today || 0),
      0,
    );
    const totalTok = providerAccounts.reduce(
      (sum, row) => sum + Number(row.used_tokens_today || 0),
      0,
    );
    const status = googleCfg;
    const callbackUrl = status.callback_url || "-";
    const configuredText = status.configured ? "configured" : "missing setup";
    const suggestedModels = Array.isArray(status.suggested_models)
      ? status.suggested_models.slice(0, 3).join(", ")
      : "-";
    const healthText =
      status.setup?.client_id_set && status.setup?.client_secret_set ? "client configured" : "client missing";
    summary.innerHTML = `
      <div class="ecosystem-row"><span>Auth Backend</span><strong>${esc(spec.authLabel)}</strong></div>
      <div class="ecosystem-row"><span>OAuth Setup</span><strong>${esc(configuredText)}</strong></div>
      <div class="ecosystem-row"><span>Callback URL</span><strong>${esc(callbackUrl)}</strong></div>
      <div class="ecosystem-row"><span>Provider Health</span><strong>${esc(healthText)}</strong></div>
      <div class="ecosystem-row"><span>Suggested Models</span><strong>${esc(suggestedModels)}</strong></div>
      <div class="ecosystem-row"><span>Connected Accounts</span><strong>${providerOauth.length}</strong></div>
      <div class="ecosystem-row"><span>Rotation State</span><strong>${active} active / ${cooldown} cooldown</strong></div>
      <div class="ecosystem-row"><span>Today Usage</span><strong>${totalReq.toLocaleString()} req · ${totalTok.toLocaleString()} tok</strong></div>
    `;
  }
}

function tableHtml(headers, rows) {
  if (!rows.length) return "<p class=\"field-description\">No data yet.</p>";
  const head = `<thead><tr>${headers.map((header) => `<th>${esc(header)}</th>`).join("")}</tr></thead>`;
  const body = `<tbody>${rows
    .map((columns) => `<tr>${columns.map((column) => `<td>${column}</td>`).join("")}</tr>`)
    .join("")}</tbody>`;
  return `<table class=\"gateway-table\">${head}${body}</table>`;
}

async function addApiAccount(event) {
  event.preventDefault();
  const providerId = byId("gaProvider").value.trim();
  const apiKey = byId("gaSecret").value.trim();
  if (!providerId) {
    showMessage("Select a provider first by clicking a provider card.", "error");
    return;
  }
  if (OAUTH_ONLY_PROVIDERS.has(providerId)) {
    showMessage(`${providerName(providerId)} requires OAuth login. Use Connect button above.`, "error");
    return;
  }
  if (!apiKey) {
    showMessage("API key is required.", "error");
    return;
  }
  await quickAddProviderApiKey(providerId, apiKey);
  event.target.reset();
  setSelectedApiProvider(providerId);
}

async function importAgent(event) {
  event.preventDefault();
  await api("/admin/api/agents/import", {
    method: "POST",
    body: JSON.stringify({
      title: byId("agentImportTitle").value.trim(),
      category: byId("agentImportCategory").value.trim(),
      content: byId("agentImportContent").value,
    }),
  });
  event.target.reset();
  await refreshGatewayDashboard();
  showMessage("Agent imported", "ok");
}

async function oauthGoogleLogin() {
  const googleCfg = state.oauthProviderStatus.google || {};
  if (!googleCfg.configured) {
    showMessage("Google OAuth is not configured. Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET.", "error");
    document.querySelector('[data-key="GOOGLE_OAUTH_CLIENT_ID"]')?.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  window.location.assign(`/admin/oauth/google/start?account_key=${encodeURIComponent(`google-${Date.now()}`)}`);
}

window.gatewayToggleProvider = async (providerId, enabled) => {
  await api(`/admin/api/gateway/providers/${providerId}/toggle`, {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });
  await refreshGatewayDashboard();
};

window.gatewayToggleAccount = async (accountId, enabled) => {
  await api(`/admin/api/gateway/accounts/${accountId}/toggle`, {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });
  await refreshGatewayDashboard();
};

window.gatewayDeleteAccount = async (accountId) => {
  await api(`/admin/api/gateway/accounts/${accountId}`, {
    method: "DELETE",
  });
  await refreshGatewayDashboard();
};

window.agentInstall = async (agentKey) => {
  await api(`/admin/api/agents/${encodeURIComponent(agentKey)}/install`, {
    method: "POST",
    body: "{}",
  });
  await refreshGatewayDashboard();
};

window.agentToggle = async (agentKey, enabled) => {
  await api(`/admin/api/agents/${encodeURIComponent(agentKey)}/toggle`, {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });
  await refreshGatewayDashboard();
};

window.agentSync = async (agentKey) => {
  await api(`/admin/api/agents/${encodeURIComponent(agentKey)}/sync`, {
    method: "POST",
    body: "{}",
  });
  await refreshGatewayDashboard();
};

window.agentToggleRole = async (encodedRole, enabled) => {
  await api(`/admin/api/agents/categories/${encodedRole}/toggle`, {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });
  await refreshGatewayDashboard();
};

window.agentsRescan = async () => {
  await api("/admin/api/agents/rescan", { method: "POST", body: "{}" });
  await refreshGatewayDashboard();
};

window.agentsImportAll = async (enable) => {
  await api("/admin/api/agents/import-all", {
    method: "POST",
    body: JSON.stringify({ enable, sync: false }),
  });
  await refreshGatewayDashboard();
};

window.agentsSyncEnabled = async () => {
  await api("/admin/api/agents/sync-enabled", { method: "POST", body: "{}" });
  await refreshGatewayDashboard();
};

function connectGatewayMetricsSocket() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${protocol}://${window.location.host}/admin/ws/metrics`);
  ws.onmessage = (_event) => {
    safeRefreshGatewayDashboard({ silent: true }).catch(() => {});
  };
  ws.onclose = () => {
    setTimeout(connectGatewayMetricsSocket, 2000);
  };
}

function syncModelDatalist() {
  let datalist = byId("model-options");
  if (!datalist) {
    datalist = document.createElement("datalist");
    datalist.id = "model-options";
    document.body.appendChild(datalist);
  }
  datalist.innerHTML = "";
  state.modelOptions.forEach((model) => datalist.appendChild(option(model, model)));
}

function showMessage(message, kind = "") {
  const area = byId("messageArea");
  area.textContent = message;
  area.className = `message-area ${kind}`.trim();
}

byId("validateButton").addEventListener("click", () => validate(true));
byId("applyButton").addEventListener("click", apply);
byId("refreshLocal").addEventListener("click", refreshLocalStatus);
byId("refreshGateway").addEventListener("click", () => {
  safeRefreshGatewayDashboard().catch(() => {});
});
byId("addApiAccountForm").addEventListener("submit", addApiAccount);
byId("importAgentForm")?.addEventListener("submit", (event) => {
  importAgent(event).catch((error) => showMessage(error.message, "error"));
});
byId("agentsRescanBtn")?.addEventListener("click", () => {
  api("/admin/api/agents/rescan", { method: "POST", body: "{}" })
    .then(() => refreshGatewayDashboard())
    .catch((error) => showMessage(error.message, "error"));
});
byId("agentsImportAllBtn")?.addEventListener("click", () => {
  api("/admin/api/agents/import-all", {
    method: "POST",
    body: JSON.stringify({ enable: true, sync: false }),
  })
    .then(() => refreshGatewayDashboard())
    .catch((error) => showMessage(error.message, "error"));
});
byId("agentsSyncEnabledBtn")?.addEventListener("click", () => {
  api("/admin/api/agents/sync-enabled", { method: "POST", body: "{}" })
    .then(() => refreshGatewayDashboard())
    .catch((error) => showMessage(error.message, "error"));
});
byId("agentsSearch")?.addEventListener("input", () => {
  safeRefreshGatewayDashboard({ silent: true }).catch(() => {});
});
byId("oauthGoogleLoginBtn")?.addEventListener("click", () => {
  oauthGoogleLogin().catch((error) => showMessage(error.message, "error"));
});
byId("openAgentsPageBtn")?.addEventListener("click", () => {
  window.open("/admin?tab=agents", "_blank", "noopener,noreferrer");
});
byId("backToDashboardBtn")?.addEventListener("click", () => {
  const params = new URLSearchParams(window.location.search);
  if (params.get("tab") === "agents") {
    window.location.assign("/admin");
    return;
  }
  byId("agentsPage").style.display = "none";
  byId("formSections").style.display = "";
});

(() => {
  const params = new URLSearchParams(window.location.search);
  if (params.get("tab") === "agents") {
    byId("agentsPage").style.display = "";
    byId("formSections").style.display = "none";
  }
})();

load().catch((error) => {
  byId("serverStatus").textContent = "Error";
  byId("serverStatus").className = "status-pill error";
  showMessage(error.message, "error");
});

connectGatewayMetricsSocket();
setInterval(() => {
  safeRefreshGatewayDashboard({ silent: true }).catch(() => {});
}, 5000);
