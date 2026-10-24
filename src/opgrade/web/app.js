"use strict";

const state = { status: null, user: null, client: null, order: null, session: null, cardTypes: [], notice: null };
const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const STATE_LABELS = {
  await_front: "Recto attendu", await_back: "Verso attendu", ready_to_analyze: "Prêt à analyser",
  await_review: "Révision", completed: "Terminée", cancelled: "Annulée", error: "Erreur",
};
const CRITERIA_LABELS = { centering: "Centrage", corners: "Coins", edges: "Bordures", surface: "Surface" };

async function api(method, url, body) {
  const res = await fetch(url, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = res.status === 204 ? null : await res.json();
  if (res.status === 401 && !url.startsWith("/api/auth/")) {
    // Session expired (inactivity) or closed: back to the login screen.
    setUser(null);
    show("login");
    throw new Error("Session expirée : reconnectez-vous.");
  }
  if (!res.ok) {
    const detail = data && data.detail;
    if (Array.isArray(detail) && detail[0]) {  // validation error: show the first reason
      throw new Error(String(detail[0].msg || "").replace(/^Value error, /, ""));
    }
    throw new Error(typeof detail === "string" ? detail : "Requête refusée (" + res.status + ")");
  }
  return data;
}

function toast(msg, kind = "error") {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  t.classList.toggle("ok", kind === "ok");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), 5000);
}

function show(name) {
  document.querySelectorAll(".screen").forEach((s) => (s.hidden = s.id !== "screen-" + name));
  if (name === "home") { loadHistory(); loadOpenOrders(); }
  if (name === "client") searchClients("");
  if (name === "order-detail" && state.order) refreshOrder();
  if (name === "admin") loadAdmin().catch((err) => toast(err.message));
  if (name === "calibration") loadCalibration().catch((err) => toast(err.message));
}

// -- Comptes ----------------------------------------------------------------------------
const ROLE_LABELS = { operator: "Opérateur", admin: "Administrateur" };

function setUser(user) {
  state.user = user;
  $("#user-bar").hidden = !user;
  document.body.classList.toggle("role-admin", user?.role === "admin");
  document.body.classList.toggle("role-operator", user?.role === "operator");
  if (!user) return;
  $("#btn-account").textContent = `${user.display_name || user.username} · ${ROLE_LABELS[user.role]}`;
  $("#btn-admin").hidden = user.role !== "admin";
}

async function afterLogin(user) {
  setUser(user);
  state.notice = await api("GET", "/api/privacy/notice?lang=fr");
  renderConsentBlocks();
  show("home");
}

async function boot() {
  await loadStatus();
  const auth = await api("GET", "/api/auth/state");
  if (auth.setup_required) return show("setup");
  if (auth.user) return afterLogin(auth.user);
  show("login");
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    const user = await api("POST", "/api/auth/login", { username: f.username.value, password: f.password.value });
    f.reset();
    await afterLogin(user);
  } catch (err) { toast(err.message); f.password.value = ""; }
});

$("#setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  if (f.password.value !== f.password2.value) { toast("Les deux mots de passe ne correspondent pas."); return; }
  try {
    const user = await api("POST", "/api/auth/setup", {
      username: f.username.value, password: f.password.value, display_name: f.display_name.value || null,
    });
    f.reset();
    await afterLogin(user);
  } catch (err) { toast(err.message); }
});

$("#btn-logout").addEventListener("click", async () => {
  try { await api("POST", "/api/auth/logout"); } catch (err) { /* already logged out */ }
  state.client = state.order = state.session = null;
  setUser(null);
  show("login");
});

$("#btn-account").addEventListener("click", () => {
  const u = state.user;
  $("#account-info").textContent = `${u.username} — ${ROLE_LABELS[u.role]}`;
  show("account");
});

$("#password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  if (f.new_password.value !== f.new_password2.value) { toast("Les deux mots de passe ne correspondent pas."); return; }
  try {
    await api("POST", "/api/auth/password", { current_password: f.current_password.value, new_password: f.new_password.value });
    f.reset();
    toast("Mot de passe changé.", "ok");
    show("home");
  } catch (err) { toast(err.message); }
});

$("#btn-admin").addEventListener("click", () => show("admin"));

// -- Calibration ------------------------------------------------------------------------
$("#btn-calibration").addEventListener("click", () => show("calibration"));

const pct = (v) => `${(v * 100).toFixed(1)} %`;

function refreshPreview() {
  $("#cal-preview").src = "/api/calibration/preview.jpg?t=" + Date.now();
}

function renderRun(run) {
  $("#cal-run").hidden = !run;
  if (!run) return;
  const enough = run.views >= run.min_views;
  $("#cal-progress").textContent =
    `${run.flat_done ? "✓ Vue à plat" : "✗ Vue à plat manquante"} · ${run.views} vue(s) ` +
    `(minimum ${run.min_views}, recommandé ${run.recommended_views}) · ` +
    `${run.white_done ? "✓ Balance des blancs" : "Balance des blancs facultative"}`;
  $("#cal-progress").className = "instruction" + (run.flat_done && enough ? " ok" : "");
  $("#cal-compute").disabled = !(run.flat_done && enough);
}

function calibrationSummary(c) {
  const warnings = c.warnings.length ? `<ul class="warn-list">${c.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "";
  return `<b>Calibration n° ${c.id}</b> ${c.active ? "(active)" : ""} — ${new Date(c.created_at).toLocaleString("fr-CA")} par ${esc(c.created_by)}<br>
    Erreur de reprojection : <b>${c.rms_px.toFixed(3)} px</b> sur ${c.views} vues ·
    échelle : <b>${c.px_per_mm.toFixed(2)} px/mm</b> (variation ${pct(c.scale_variation)})<br>
    Focale : ${c.focal_px.toFixed(0)} px · distorsion k1 = ${c.dist_coeffs[0].toFixed(3)} ·
    ${c.uniformity != null ? `uniformité de l'éclairage ${pct(c.uniformity)}` : "pas de balance des blancs"}${warnings}`;
}

async function loadCalibration() {
  const [st, history] = await Promise.all([api("GET", "/api/calibration/state"), api("GET", "/api/calibrations")]);
  $("#cal-active").innerHTML = st.active ? calibrationSummary(st.active)
    : '<b>Aucune calibration active</b> : les mesures ne sont pas corrigées de la distorsion de l\'objectif.';
  renderRun(st.run);
  $("#cal-sim").hidden = !state.status.simulation;
  $("#cal-history").innerHTML = history.map((c) => `
    <tr><td>${c.id}</td><td>${new Date(c.created_at).toLocaleString("fr-CA")}</td>
      <td>${c.rms_px.toFixed(3)} px</td><td>${c.px_per_mm.toFixed(2)}</td>
      <td>${c.active ? "<b>active</b>" : `<button data-activate="${c.id}">Activer</button>`}</td></tr>`).join("");
}

$("#cal-focus").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await api("GET", "/api/calibration/focus");
  $("#cal-focus-value").textContent = `netteté ${r.sharpness} · ${r.corners} coins de mire vus`;
  refreshPreview();
}));

$("#cal-start-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    renderRun(await api("POST", "/api/calibration/start", { square_mm: Number(e.target.square_mm.value) }));
    $("#cal-result").hidden = true; $("#cal-white-result").textContent = "";
  } catch (err) { toast(err.message); }
});

async function captureView(kind, btn) {
  await busy(btn, async () => {
    const r = await api("POST", "/api/calibration/capture", { kind });
    toast(r.message, r.accepted ? "ok" : "error");
    renderRun(r);
    refreshPreview();
  });
}
$("#cal-capture-flat").addEventListener("click", (e) => captureView("flat", e.target));
$("#cal-capture-view").addEventListener("click", (e) => captureView("view", e.target));

$("#cal-white").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await api("POST", "/api/calibration/white");
  $("#cal-white-result").innerHTML = `Uniformité de l'éclairage : <b>${pct(r.uniformity)}</b> ·
    gains B/V/R : ${r.gains_bgr.map((g) => g.toFixed(3)).join(" / ")}` +
    (r.problems.length ? `<ul class="warn-list">${r.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : "");
  renderRun(r);
  refreshPreview();
}));

$("#cal-compute").addEventListener("click", (e) => busy(e.target, async () => {
  const c = await api("POST", "/api/calibration/compute");
  $("#cal-result").innerHTML = calibrationSummary(c) +
    `<br><button class="primary" data-activate="${c.id}">Activer cette calibration</button>`;
  $("#cal-result").hidden = false;
  await loadCalibration();
}));

document.querySelector("#screen-calibration").addEventListener("click", (e) => {
  const id = e.target.dataset.activate;
  if (!id) return;
  busy(e.target, async () => {
    await api("POST", `/api/calibrations/${id}/activate`);
    toast("Calibration activée : elle s'applique aux prochaines captures.", "ok");
    $("#cal-result").hidden = true;
    await loadCalibration();
  });
});

$("#cal-sim").addEventListener("click", (e) => {
  const kind = e.target.dataset.target;
  if (!kind) return;
  busy(e.target, async () => { await api("POST", "/api/calibration/sim-target", { kind }); });
});

// -- Administration ---------------------------------------------------------------------
const AUDIT_LABELS = {
  setup: "Configuration initiale", login: "Connexion", login_failed: "Échec de connexion", logout: "Déconnexion",
  user_created: "Compte créé", user_updated: "Compte modifié", password_reset: "Mot de passe réinitialisé",
  password_changed: "Mot de passe changé", client_export: "Export de données client",
  client_anonymized: "Client anonymisé", retention_applied: "Conservation appliquée", card_type_created: "Type de carte ajouté",
  calibration_computed: "Calibration calculée", calibration_activated: "Calibration activée",
};

async function loadAdmin() {
  const [users, due, events] = await Promise.all([
    api("GET", "/api/users"), api("GET", "/api/privacy/retention"), api("GET", "/api/audit?limit=50"),
  ]);
  $("#admin-users").innerHTML = users.map((u) => `
    <tr><td>${esc(u.username)}${u.display_name ? ` <span class="muted">${esc(u.display_name)}</span>` : ""}</td>
      <td>${ROLE_LABELS[u.role]}</td><td>${u.active ? "Actif" : "Désactivé"}</td>
      <td><button data-user="${u.id}" data-act="role">${u.role === "admin" ? "→ Opérateur" : "→ Admin"}</button>
        <button data-user="${u.id}" data-act="active">${u.active ? "Désactiver" : "Réactiver"}</button>
        <button data-user="${u.id}" data-act="password">Mot de passe</button></td></tr>`).join("");
  $("#admin-users").users = users;
  $("#retention-info").textContent = due.length
    ? `${due.length} dossier(s) client sans activité depuis la durée de conservation : à anonymiser.`
    : "Aucun dossier client n'a dépassé la durée de conservation.";
  $("#btn-retention-apply").hidden = !due.length;
  $("#admin-audit").innerHTML = events.map((ev) => `
    <tr><td>${new Date(ev.at).toLocaleString("fr-CA")}</td><td>${esc(ev.username)}</td>
      <td>${esc(AUDIT_LABELS[ev.action] || ev.action)}</td><td>${esc(ev.target)}</td></tr>`).join("");
}

$("#admin-users").addEventListener("click", (e) => busy(e.target, async () => {
  const b = e.target.closest("button[data-user]"); if (!b) return;
  const user = $("#admin-users").users.find((u) => u.id === Number(b.dataset.user));
  if (b.dataset.act === "role") {
    await api("PATCH", `/api/users/${user.id}`, { role: user.role === "admin" ? "operator" : "admin" });
  } else if (b.dataset.act === "active") {
    await api("PATCH", `/api/users/${user.id}`, { active: !user.active });
  } else {
    // Temporary password typed by the admin and handed to the user, who changes it in « Mon compte ».
    const pw = prompt(`Nouveau mot de passe temporaire pour ${user.username} :`);
    if (!pw) return;
    await api("POST", `/api/users/${user.id}/password`, { new_password: pw });
    toast("Mot de passe réinitialisé : ses sessions ouvertes sont fermées.", "ok");
  }
  await loadAdmin();
}));

$("#user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("POST", "/api/users", {
      username: f.username.value, password: f.password.value, role: f.role.value, display_name: f.display_name.value || null,
    });
    f.reset();
    toast("Compte créé.", "ok");
    await loadAdmin();
  } catch (err) { toast(err.message); }
});

$("#card-type-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api("POST", "/api/card-types", {
      code: f.code.value.trim(), name: f.name.value.trim(), category: f.category.value,
      width_mm: Number(f.width_mm.value), height_mm: Number(f.height_mm.value), catalog: f.catalog.value || null,
    });
    f.reset();
    toast("Type de carte ajouté.", "ok");
    await loadAdmin();
  } catch (err) { toast(err.message); }
});

$("#btn-retention-apply").addEventListener("click", (e) => busy(e.target, async () => {
  if (!confirm("Anonymiser IRRÉVERSIBLEMENT tous les dossiers échus ?")) return;
  const r = await api("POST", "/api/privacy/retention/apply");
  toast(`${r.anonymized} dossier(s) anonymisé(s).`, "ok");
  await loadAdmin();
}));

const ORDER_STATUS = {
  received: "Reçue", in_progress: "En cours", graded: "Gradée", returned: "Retournée", cancelled: "Annulée",
};
const CHANNELS = { counter: "Comptoir", store: "Boutique", mail: "Envoi postal" };
const clientName = (c) => `${c.first_name} ${c.last_name}`;
const orderLine = (o) =>
  `<b>${esc(o.number)}</b> · ${esc(o.client_name)} · ${CHANNELS[o.channel]} · ${ORDER_STATUS[o.status]} · ${o.graded_count}/${o.cards_count} gradée(s)`;

function gradeOptions(select, allowEmpty) {
  select.innerHTML = allowEmpty ? '<option value="">(mesure automatique)</option>' : '<option value="">—</option>';
  for (let g = 10; g >= 1; g -= 0.5) select.insertAdjacentHTML("beforeend", `<option value="${g}">${g}</option>`);
}

// -- Accueil ----------------------------------------------------------------------------
async function loadStatus() {
  state.status = await api("GET", "/api/status");
  const pill = $("#machine-status");
  pill.textContent = (state.status.simulation ? "SIMULATION · " : "Prête · ") + state.status.machine_id;
  pill.classList.toggle("sim", state.status.simulation);
  $("#privacy-warning").hidden = state.status.privacy_configured;
}

async function loadOpenOrders() {
  const orders = await api("GET", "/api/orders?status=open");
  $("#open-orders").innerHTML = orders.length
    ? orders.map((o) => `<li data-id="${o.id}">${orderLine(o)}</li>`).join("")
    : '<li class="muted">Aucune commande en cours.</li>';
}

$("#open-orders").addEventListener("click", async (e) => {
  const li = e.target.closest("li[data-id]"); if (!li) return;
  openOrder(await api("GET", "/api/orders/" + li.dataset.id));
});

// -- Consentement (Loi 25) --------------------------------------------------------------
function renderConsentBlocks() {
  const n = state.notice;
  document.querySelectorAll(".consent-block").forEach((block) => {
    block.innerHTML = `
      <div class="notice"><b>Avis de confidentialité</b> <span class="muted">(version ${esc(n.version)})</span>
        <p>${esc(n.notice).replace(/\n/g, "<br>")}</p></div>
      <label class="check"><input type="checkbox" name="consent_service" required> ${esc(n.consent_service)}</label>
      <label class="check"><input type="checkbox" name="consent_marketing"> ${esc(n.consent_marketing)}</label>
      <label>Consentement recueilli
        <select name="consent_method">
          <option value="touch">à l'écran</option><option value="paper">sur papier signé</option>
          <option value="verbal">verbalement</option><option value="email">par courriel</option>
        </select></label>`;
  });
}

function consentFields(form) {
  return {
    consent_service: form.consent_service.checked,
    consent_marketing: form.consent_marketing.checked,
    consent_method: form.consent_method.value,
    consent_policy_version: state.notice.version,
  };
}

$("#consent-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = consentFields(e.target);
  const base = { policy_version: f.consent_policy_version, method: f.consent_method };
  try {
    await api("POST", `/api/clients/${state.client.id}/consent`, { ...base, kind: "service", granted: true });
    const client = await api("POST", `/api/clients/${state.client.id}/consent`,
      { ...base, kind: "marketing", granted: f.consent_marketing });
    e.target.reset();
    selectClient(client);
  } catch (err) { toast(err.message); }
});

async function loadHistory() {
  const sessions = await api("GET", "/api/sessions?limit=20");
  $("#history").innerHTML = sessions.map((s) => `
    <tr><td>${s.id}${s.simulated ? " (sim)" : ""}</td><td>carte #${s.card_id}</td>
    <td>${STATE_LABELS[s.state] || s.state}</td><td>${s.final_grade ?? "—"}</td>
    <td><button data-open="${s.id}">Ouvrir</button></td></tr>`).join("");
}

$("#history").addEventListener("click", async (e) => {
  const id = e.target.dataset.open; if (!id) return;
  openSession(await api("GET", "/api/sessions/" + id));
});

// -- Client -----------------------------------------------------------------------------
async function searchClients(q) {
  const clients = await api("GET", "/api/clients" + (q ? "?q=" + encodeURIComponent(q) : ""));
  $("#client-list").innerHTML = clients.map((c) =>
    `<li data-id="${c.id}">${esc(c.first_name)} ${esc(c.last_name)}${c.email ? " · " + esc(c.email) : ""}</li>`).join("");
  $("#client-list").clients = clients;
}
$("#client-search").addEventListener("input", (e) => searchClients(e.target.value.trim()));
$("#client-list").addEventListener("click", (e) => {
  const id = Number(e.target.dataset.id); if (!id) return;
  selectClient($("#client-list").clients.find((c) => c.id === id));
});
$("#client-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {};
  for (const k of ["first_name", "last_name", "email", "phone"]) body[k] = f[k].value.trim() || null;
  if (!body.email && !body.phone) { toast("Un moyen de contact est requis : courriel ou téléphone."); return; }
  Object.assign(body, consentFields(f));
  try { selectClient(await api("POST", "/api/clients", body)); f.reset(); } catch (err) { toast(err.message); }
});

function selectClient(client) {
  state.client = client;
  document.querySelectorAll(".client-name").forEach((el) => (el.textContent = clientName(client)));
  if (!client.consent_valid) return show("consent");
  openOrderScreen();
}

// -- Commande ---------------------------------------------------------------------------
const orderForm = $("#order-form");

function toggleMailFields() {
  const mail = orderForm.channel.value === "mail";
  orderForm.tracking_in.hidden = orderForm.return_address.hidden = !mail;
  orderForm.return_address.required = mail;
}
orderForm.channel.addEventListener("change", toggleMailFields);

async function openOrderScreen() {
  const orders = await api("GET", `/api/orders?status=open&client_id=${state.client.id}`);
  $("#client-orders").innerHTML = orders.length
    ? orders.map((o) => `<li data-id="${o.id}">${orderLine(o)}</li>`).join("")
    : '<li class="muted">Aucune commande en cours.</li>';
  orderForm.reset(); toggleMailFields();
  show("order");
}

$("#client-orders").addEventListener("click", async (e) => {
  const li = e.target.closest("li[data-id]"); if (!li) return;
  openOrder(await api("GET", "/api/orders/" + li.dataset.id));
});

orderForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { client_id: state.client.id, channel: orderForm.channel.value };
  for (const k of ["tracking_in", "return_address", "notes"]) body[k] = orderForm[k].value.trim() || null;
  try { openOrder(await api("POST", "/api/orders", body)); } catch (err) { toast(err.message); }
});

async function openOrder(order) {
  state.order = order;
  if (!state.client || state.client.id !== order.client_id) {
    state.client = await api("GET", "/api/clients/" + order.client_id);
  }
  document.querySelectorAll(".client-name").forEach((el) => (el.textContent = clientName(state.client)));
  renderOrder();
  show("order-detail");
}

async function refreshOrder() {
  state.order = await api("GET", "/api/orders/" + state.order.id);
  renderOrder();
}

function renderOrder() {
  const o = state.order;
  const open = ["received", "in_progress", "graded"].includes(o.status);
  $("#order-number").textContent = o.number;
  $("#order-summary").innerHTML = `
    Client : <b>${esc(o.client_name)}</b> · ${CHANNELS[o.channel]} · <b>${ORDER_STATUS[o.status]}</b>
    · ${o.graded_count}/${o.cards_count} carte(s) gradée(s)
    ${o.tracking_in ? `<br>Suivi réception : ${esc(o.tracking_in)}` : ""}
    ${o.tracking_out ? `<br>Suivi retour : ${esc(o.tracking_out)}` : ""}
    ${o.return_address ? `<br>Adresse de retour : ${esc(o.return_address)}` : ""}
    ${o.notes ? `<br>Notes : ${esc(o.notes)}` : ""}`;
  $("#order-cards").innerHTML = o.cards.length ? o.cards.map((c) => `
    <tr><td>${esc(c.name)}${c.set_name ? ` <span class="muted">${esc(c.set_name)} ${esc(c.card_number)}</span>` : ""}</td>
      <td>${c.session_state ? STATE_LABELS[c.session_state] : "À grader"}</td>
      <td>${c.final_grade ?? "—"}${c.session_state === "completed"
        ? ` <a href="/api/sessions/${c.session_id}/report.pdf" target="_blank">PDF</a>` : ""}</td>
      <td>${open ? `<button data-card="${c.id}" data-session="${c.session_id ?? ""}" data-state="${c.session_state ?? ""}">
        ${c.session_state === "completed" ? "Voir" : c.session_state && c.session_state !== "cancelled" ? "Continuer" : "Grader"}</button>` : ""}</td></tr>`).join("")
    : '<tr><td colspan="4" class="muted">Aucune carte pour l\'instant.</td></tr>';
  $("#btn-add-card").hidden = !open;
  $("#btn-cancel-order").hidden = !open;
  $("#btn-return-order").hidden = o.status !== "graded";
}

$("#order-cards").addEventListener("click", (e) => busy(e.target, async () => {
  const b = e.target.closest("button[data-card]"); if (!b) return;
  if (b.dataset.session && b.dataset.state !== "cancelled") {
    openSession(await api("GET", "/api/sessions/" + b.dataset.session));
  } else {
    openSession(await api("POST", "/api/sessions", { card_id: Number(b.dataset.card) }));
  }
}));

$("#btn-add-card").addEventListener("click", () => prepareCardScreen());

$("#btn-return-order").addEventListener("click", (e) => busy(e.target, async () => {
  let tracking = null;
  if (state.order.channel === "mail") {
    tracking = prompt("Numéro de suivi de l'envoi de retour (facultatif) :");
    if (tracking === null) return;
  } else if (!confirm("Confirmer que les cartes ont été remises au client ?")) return;
  state.order = await api("POST", `/api/orders/${state.order.id}/return`, { tracking_out: tracking || null });
  renderOrder();
}));

$("#btn-cancel-order").addEventListener("click", (e) => busy(e.target, async () => {
  if (!confirm("Annuler cette commande ?")) return;
  state.order = await api("POST", `/api/orders/${state.order.id}/cancel`);
  renderOrder();
}));

$("#btn-export-client").addEventListener("click", () => {
  // Access request: the file is downloaded and the request is logged by the server.
  window.location.href = `/api/clients/${state.order.client_id}/export`;
});

$("#btn-anonymize-client").addEventListener("click", (e) => busy(e.target, async () => {
  const reason = prompt(
    "Anonymisation IRRÉVERSIBLE : nom, coordonnées, adresses et notes seront effacés ; " +
    "les résultats de gradation sont conservés.\n\nMotif (ex. « Demande du client ») :");
  if (!reason) return;
  await api("POST", `/api/clients/${state.order.client_id}/anonymize`, { reason, confirm: true });
  toast("Client anonymisé.", "ok");
  state.client = null;
  await refreshOrder();
}));

// -- Carte ------------------------------------------------------------------------------
async function prepareCardScreen() {
  state.cardTypes = await api("GET", "/api/card-types");
  $("#card-type").innerHTML = '<option value="">Type de carte…</option>' +
    state.cardTypes.map((t) => `<option value="${t.id}">${esc(t.name)} (${t.width_mm}×${t.height_mm} mm)</option>`).join("");
  $("#card-order").textContent = state.order.number;
  cardForm.reset(); clearIdentity(); identifyMessage(""); $("#candidates").innerHTML = "";
  $("#btn-identify").hidden = $("#btn-identify-camera").hidden = $("#sim-identify").hidden = true;
  show("card");
}

const cardForm = $("#card-form");
const IDENTITY_FIELDS = ["rarity", "external_source", "external_id", "reference_image_url"];

function selectedCardType() {
  return state.cardTypes.find((t) => t.id === Number(cardForm.card_type_id.value));
}

function identifyMessage(text) {
  const m = $("#identify-message"); m.textContent = text || ""; m.hidden = !text;
}

function clearIdentity() {
  IDENTITY_FIELDS.forEach((n) => (cardForm[n].value = ""));
  $("#identified").hidden = true;
}

const CONFIDENCE_LABELS = { high: "Très probable", medium: "Possible", low: "Peu probable" };

$("#card-type").addEventListener("change", () => {
  const type = selectedCardType();
  clearIdentity(); $("#candidates").innerHTML = "";
  $("#btn-identify").hidden = $("#btn-identify-camera").hidden = !(type && type.catalog);
  $("#sim-identify").hidden = !(state.status.simulation && type && type.catalog);
  identifyMessage(type && !type.catalog ? "Pas de catalogue en ligne pour ce type : saisie manuelle." : "");
  refreshSim();
});

const shownNumber = (c) => c.printed_number || c.number;

function showCandidates(r) {
  identifyMessage(r.message || `${r.candidates.length} résultat(s) — touchez la bonne carte.`);
  $("#candidates").innerHTML = r.candidates.map((c, i) => `
    <li data-i="${i}">${c.image_url ? `<img src="${esc(c.image_url)}" alt="" loading="lazy">` : ""}
      <b>${esc(c.name)}</b><br>${esc(c.set_name)}${shownNumber(c) ? " · " + esc(shownNumber(c)) : ""}${c.year ? " · " + c.year : ""}
      ${c.confidence ? `<br><span class="conf ${c.confidence}">${CONFIDENCE_LABELS[c.confidence]}</span>` : ""}
      ${c.number_match ? `<br><span class="conf high">Numéro confirmé ✓</span>` : ""}</li>`).join("");
  $("#candidates").results = r.candidates;
}

async function searchByName() {
  const name = cardForm.name.value.trim();
  if (name.length < 2) { identifyMessage("Tapez au moins 2 lettres du nom de la carte."); return; }
  const params = new URLSearchParams({ card_type_id: cardForm.card_type_id.value, name });
  if (cardForm.card_number.value.trim()) params.set("number", cardForm.card_number.value.trim());
  if (cardForm.set_name.value.trim()) params.set("set_name", cardForm.set_name.value.trim());
  identifyMessage("Recherche…");
  showCandidates(await api("GET", "/api/identify?" + params));
}

$("#btn-identify").addEventListener("click", (e) => busy(e.target, searchByName));

$("#btn-identify-camera").addEventListener("click", (e) => busy(e.target, async () => {
  identifyMessage("Placez la carte RECTO vers le haut… capture en cours.");
  showCandidates(await api("POST", "/api/identify/image", { card_type_id: Number(cardForm.card_type_id.value) }));
}));

$("#sim-place-ref").addEventListener("click", (e) => busy(e.target, async () => {
  const type = selectedCardType();
  await api("POST", "/api/sim/place-reference", { catalog: type.catalog });
  refreshSim();
}));

$("#candidates").addEventListener("click", async (e) => {
  const li = e.target.closest("li"); if (!li) return;
  const c = $("#candidates").results[Number(li.dataset.i)];
  if (!c.set_name && !c.number) {
    // Artwork recognised but not the printing (e.g. Yu-Gi-Oh!): list printings by name.
    cardForm.name.value = c.name;
    try { await searchByName(); } catch (err) { toast(err.message); }
    return;
  }
  cardForm.name.value = c.name;
  cardForm.set_name.value = c.set_name || "";
  cardForm.card_number.value = shownNumber(c) || "";
  cardForm.year.value = c.year || "";
  cardForm.rarity.value = c.rarity || "";
  cardForm.external_source.value = c.source;
  cardForm.external_id.value = c.external_id;
  cardForm.reference_image_url.value = c.image_url || "";
  const img = $("#identified-img");
  if (c.image_url) img.src = c.image_url; else img.removeAttribute("src");
  $("#identified-text").textContent = [c.name, c.set_name, shownNumber(c), c.year].filter(Boolean).join(" · ");
  $("#identified").hidden = false;
  $("#candidates").innerHTML = ""; identifyMessage("");
});
$("#btn-unidentify").addEventListener("click", clearIdentity);

$("#card-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target)); for (const k in body) if (!body[k]) body[k] = null;
  body.client_id = state.order.client_id; body.order_id = state.order.id;
  body.card_type_id = Number(body.card_type_id);
  if (body.year) body.year = Number(body.year);
  try {
    const card = await api("POST", "/api/cards", body);
    openSession(await api("POST", "/api/sessions", { card_id: card.id }));
  } catch (err) { toast(err.message); }
});

// -- Capture ----------------------------------------------------------------------------
function openSession(session, message, accepted = true) {
  state.session = session;
  if (session.state === "await_review") return renderReview();
  if (["completed", "cancelled", "error"].includes(session.state)) return renderResult();
  const ins = $("#instruction");
  ins.textContent = message || session.last_message;
  ins.className = "instruction" + (accepted ? "" : " error");
  for (const side of ["front", "back"]) {
    const cap = session.captures.find((c) => c.side === side);
    const img = $("#img-" + side);
    if (cap) img.src = `/api/captures/${cap.id}/image?t=${cap.id}`; else img.removeAttribute("src");
  }
  $("#btn-capture").hidden = session.state === "ready_to_analyze";
  $("#btn-analyze").hidden = session.state !== "ready_to_analyze";
  $("#sim-panel").hidden = !state.status.simulation;
  refreshSim();
  show("capture");
}

async function busy(btn, fn) {
  btn.disabled = true;
  try { await fn(); } catch (err) { toast(err.message); } finally { btn.disabled = false; }
}

$("#btn-capture").addEventListener("click", (e) => busy(e.target, async () => {
  const r = await api("POST", `/api/sessions/${state.session.id}/capture`);
  openSession(r.session, r.message, r.accepted);
}));
$("#btn-analyze").addEventListener("click", (e) => busy(e.target, async () => {
  openSession(await api("POST", `/api/sessions/${state.session.id}/analyze`));
}));
$("#btn-cancel").addEventListener("click", (e) => busy(e.target, async () => {
  if (!confirm("Annuler cette gradation ?")) return;
  await api("POST", `/api/sessions/${state.session.id}/cancel`);
  show(state.order ? "order-detail" : "home");
}));

async function refreshSim() {
  if (!state.status.simulation) return;
  const t = await api("GET", "/api/sim/table");
  $("#sim-state").textContent = t.card_present ? (t.side_up === "front" ? "recto visible" : "verso visible") : "vide";
  document.querySelectorAll(".sim-label").forEach((el) => (el.textContent = t.label ? "« " + t.label + " »" : "carte synthétique"));
}
$("#sim-flip").addEventListener("click", async () => { await api("POST", "/api/sim/flip"); refreshSim(); });
$("#sim-random").addEventListener("click", async () => {
  const r = () => Math.round(40 + Math.random() * 25);
  await api("POST", "/api/sim/place", { front_left_share: r(), front_top_share: r(), back_left_share: r(), back_top_share: r() });
  refreshSim();
});

// -- Révision ---------------------------------------------------------------------------
function renderReview() {
  const s = state.session;
  $("#review-instruction").textContent = s.last_message;
  const c = s.subgrades.find((g) => g.criterion === "centering");
  $("#centering-info").innerHTML = c
    ? `<b>Centrage mesuré : ${c.value}/10</b> (confiance ${(c.confidence * 100).toFixed(0)} %)<br>
       Recto : ${c.details.front}<br>Verso : ${c.details.back}` : "";
  const form = $("#review-form");
  gradeOptions(form.centering_override, true);
  ["corners", "edges", "surface"].forEach((n) => gradeOptions(form[n], false));
  show("review");
}

$("#review-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = { corners: Number(f.corners.value), edges: Number(f.edges.value), surface: Number(f.surface.value) };
  if (f.centering_override.value) body.centering_override = Number(f.centering_override.value);
  try { state.session = await api("POST", `/api/sessions/${state.session.id}/review`, body); renderResult(); }
  catch (err) { toast(err.message); }
});

// -- Résultat ---------------------------------------------------------------------------
function renderResult() {
  const s = state.session;
  $("#final-grade").textContent = s.final_grade ?? (STATE_LABELS[s.state] || s.state);
  $("#sim-badge").hidden = !s.simulated;
  $("#subgrades").innerHTML = s.subgrades.map((g) =>
    `<tr><td>${CRITERIA_LABELS[g.criterion] || g.criterion}</td><td>${g.value}</td>
     <td class="muted">${g.source === "auto" ? "mesure machine" : "opérateur"}</td></tr>`).join("");
  $("#explanation").innerHTML = (s.explanation || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const done = s.state === "completed";
  $("#certificate").hidden = $("#btn-report").hidden = !done;
  // Gradings finished before certificates existed get their number with their first report.
  if (done) $("#certificate").textContent = s.certificate_number ? "Certificat n° " + s.certificate_number : "";
  $("#result-order-actions").hidden = !state.order;
  $("#btn-result-home").hidden = !!state.order;
  show("result");
}

$("#btn-report").addEventListener("click", () => {
  window.open(`/api/sessions/${state.session.id}/report.pdf`, "_blank");
});

$("#btn-next-card").addEventListener("click", async () => {
  await refreshOrder();
  if (["returned", "cancelled"].includes(state.order.status)) return show("order-detail");
  prepareCardScreen();
});

document.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => show(b.dataset.go)));
boot().catch((err) => toast(err.message));
