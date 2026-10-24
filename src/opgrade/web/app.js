"use strict";

const state = { status: null, client: null, session: null, cardTypes: [] };
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
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : "Requête refusée (" + res.status + ")");
  }
  return data;
}

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), 5000);
}

function show(name) {
  document.querySelectorAll(".screen").forEach((s) => (s.hidden = s.id !== "screen-" + name));
  if (name === "home") loadHistory();
  if (name === "client") searchClients("");
}

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
}

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
  const body = Object.fromEntries(new FormData(e.target)); for (const k in body) if (!body[k]) body[k] = null;
  try { selectClient(await api("POST", "/api/clients", body)); e.target.reset(); } catch (err) { toast(err.message); }
});

async function selectClient(client) {
  state.client = client;
  $("#card-client").textContent = client.first_name + " " + client.last_name;
  state.cardTypes = await api("GET", "/api/card-types");
  $("#card-type").innerHTML = '<option value="">Type de carte…</option>' +
    state.cardTypes.map((t) => `<option value="${t.id}">${esc(t.name)} (${t.width_mm}×${t.height_mm} mm)</option>`).join("");
  show("card");
}

// -- Carte ------------------------------------------------------------------------------
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

function showCandidates(r) {
  identifyMessage(r.message || `${r.candidates.length} résultat(s) — touchez la bonne carte.`);
  $("#candidates").innerHTML = r.candidates.map((c, i) => `
    <li data-i="${i}">${c.image_url ? `<img src="${esc(c.image_url)}" alt="" loading="lazy">` : ""}
      <b>${esc(c.name)}</b><br>${esc(c.set_name)}${c.number ? " · " + esc(c.number) : ""}${c.year ? " · " + c.year : ""}
      ${c.confidence ? `<br><span class="conf ${c.confidence}">${CONFIDENCE_LABELS[c.confidence]}</span>` : ""}</li>`).join("");
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
  cardForm.card_number.value = c.number || "";
  cardForm.year.value = c.year || "";
  cardForm.rarity.value = c.rarity || "";
  cardForm.external_source.value = c.source;
  cardForm.external_id.value = c.external_id;
  cardForm.reference_image_url.value = c.image_url || "";
  const img = $("#identified-img");
  if (c.image_url) img.src = c.image_url; else img.removeAttribute("src");
  $("#identified-text").textContent = [c.name, c.set_name, c.number, c.year].filter(Boolean).join(" · ");
  $("#identified").hidden = false;
  $("#candidates").innerHTML = ""; identifyMessage("");
});
$("#btn-unidentify").addEventListener("click", clearIdentity);

$("#card-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target)); for (const k in body) if (!body[k]) body[k] = null;
  body.client_id = state.client.id; body.card_type_id = Number(body.card_type_id);
  if (body.year) body.year = Number(body.year);
  try {
    const card = await api("POST", "/api/cards", body);
    openSession(await api("POST", "/api/sessions", { card_id: card.id }));
    e.target.reset(); clearIdentity(); identifyMessage("");
    $("#btn-identify").hidden = $("#btn-identify-camera").hidden = $("#sim-identify").hidden = true;
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
  await api("POST", `/api/sessions/${state.session.id}/cancel`); show("home");
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
  show("result");
}

document.querySelectorAll("[data-go]").forEach((b) => b.addEventListener("click", () => show(b.dataset.go)));
loadStatus().then(() => show("home")).catch((err) => toast(err.message));
