(function () {
  const view = document.getElementById("view-professional-network");
  if (!view) return;
  const api = (path, options = {}) => window.apiFetch ? window.apiFetch(path, options) : fetch(path, { ...options, headers: { ...(options.headers || {}), Authorization: `Bearer ${localStorage.getItem("totalsolutions_access_token") || ""}` } });
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const qs = id => document.getElementById(id);
  const renderKpis = items => { const counts = {}; items.forEach(item => { counts[item.status] = (counts[item.status] || 0) + 1; }); qs("professionalNetworkKpis").innerHTML = [["Candidaturas recibidas", items.length], ["En análisis", counts.UNDER_REVIEW || 0], ["En validación", counts.TECHNICAL_VALIDATION || 0], ["Aprobados", counts.APPROVED || 0], ["Activos", counts.ACTIVE || 0]].map(([label, value]) => `<div class="professional-network-kpi"><strong>${value}</strong><span>${label}</span></div>`).join(""); };
  const renderCoverage = data => { qs("professionalNetworkCoverage").innerHTML = data.specialties.map(item => `<button type="button" data-network-specialty="${esc(item.name)}"><strong>${esc(item.name)} · ${item.approved_count}/${item.target}</strong><small>${esc(item.coverage_status)}</small></button>`).join(""); };
  const renderRows = items => { qs("professionalNetworkList").innerHTML = items.length ? items.map(item => `<div class="professional-network-row"><strong>${esc(item.public_code)}<br>${esc(item.name)}</strong><span>${esc(item.professional_type)}<br>${esc(item.professional_level)}</span><span>${item.specialties.map(esc).join(", ")}</span><span>${esc(item.city)} / ${esc(item.zone)}</span><span>${esc(item.status)}<br>${esc(item.experience)}</span><button type="button" data-network-application="${item.public_code}">Ver perfil</button></div>`).join("") : "<p>No hay candidaturas para estos filtros.</p>"; };
  async function load() { const params = new URLSearchParams(); ["professionalNetworkSearch", "professionalNetworkStatus", "professionalNetworkSpecialty"].forEach((id, i) => { const value = qs(id).value.trim(); if (value) params.set(["search", "status", "specialty"][i], value); }); const [listResponse, coverageResponse] = await Promise.all([api(`/professional-network/applications?${params}`), api("/professional-network/coverage")]); if (!listResponse.ok || !coverageResponse.ok) throw new Error("No fue posible cargar Red Profesional"); const list = await listResponse.json(); renderKpis(list.items); renderRows(list.items); renderCoverage(await coverageResponse.json()); }
  async function detail(publicCode) { const listResponse = await api(`/professional-network/applications?search=${encodeURIComponent(publicCode)}`); const match = (await listResponse.json()).items[0]; if (!match) return; const response = await api(`/professional-network/applications/${match.id}`); if (!response.ok) return; const item = await response.json(); qs("professionalNetworkDetail").classList.remove("hidden"); qs("professionalNetworkDetail").innerHTML = `<div class="panel-header"><div><p class="eyebrow">${esc(item.public_code)}</p><h3>${esc(item.name)}</h3></div><button type="button" data-network-close>Cerrar</button></div><div class="professional-network-detail-grid"><div><strong>Contacto</strong><br>${esc(item.email)}<br>${esc(item.whatsapp)}<br>${esc(item.phone || "")}</div><div><strong>Perfil</strong><br>${esc(item.professional_type)} · ${esc(item.professional_level)}<br>${esc(item.experience)}<br>${esc(item.city)} / ${esc(item.zone)}</div><div><strong>Operación</strong><br>Vehículo: ${item.has_vehicle ? "Sí" : "No"}<br>Herramientas: ${item.has_tools ? "Sí" : "No"}<br>Factura: ${item.can_invoice ? "Sí" : "No"}<br>Emergencias: ${item.attention_emergencies ? "Sí" : "No"}</div><div><strong>Presentación</strong><br>${esc(item.presentation || "Sin presentación")}</div></div><h4>Validación por especialidad</h4>${item.skills.map(skill => `<div class="professional-network-skill"><span>${esc(skill.specialty)}</span><select data-network-skill="${esc(skill.specialty)}" data-network-id="${match.id}"><option ${skill.validation_status === "DECLARED" ? "selected" : ""}>DECLARED</option><option ${skill.validation_status === "UNDER_REVIEW" ? "selected" : ""}>UNDER_REVIEW</option><option ${skill.validation_status === "VALIDATED" ? "selected" : ""}>VALIDATED</option><option ${skill.validation_status === "REJECTED" ? "selected" : ""}>REJECTED</option></select></div>`).join("")}<h4>Cambiar etapa</h4><select id="networkStatusUpdate"><option>${esc(item.status)}</option><option>UNDER_REVIEW</option><option>CONTACTED</option><option>INTERVIEW</option><option>DOCUMENTATION</option><option>TECHNICAL_VALIDATION</option><option>APPROVED</option><option>REJECTED</option></select><button type="button" data-network-status-id="${match.id}">Guardar etapa</button>`; }
  qs("professionalNetworkRefresh")?.addEventListener("click", () => load().catch(error => { qs("professionalNetworkList").textContent = error.message; })); ["professionalNetworkSearch", "professionalNetworkStatus", "professionalNetworkSpecialty"].forEach(id => qs(id)?.addEventListener("change", () => load())); qs("professionalNetworkList")?.addEventListener("click", event => { const button = event.target.closest("[data-network-application]"); if (button) detail(button.dataset.networkApplication); }); qs("professionalNetworkCoverage")?.addEventListener("click", () => {}); qs("professionalNetworkDetail")?.addEventListener("click", async event => { if (event.target.matches("[data-network-close]")) qs("professionalNetworkDetail").classList.add("hidden"); const statusButton = event.target.closest("[data-network-status-id]"); if (statusButton) { const response = await api(`/professional-network/applications/${statusButton.dataset.networkStatusId}/status`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: qs("networkStatusUpdate").value }) }); if (response.ok) load(); } }); qs("professionalNetworkDetail")?.addEventListener("change", async event => { const select = event.target.closest("[data-network-skill]"); if (!select) return; await api(`/professional-network/applications/${select.dataset.networkId}/skills/${encodeURIComponent(select.dataset.networkSkill)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ validation_status: select.value }) }); load(); }); document.querySelector('[data-view-target="professional-network"]')?.addEventListener("click", () => load());
})();

(function () {
  const detail = document.getElementById("professionalNetworkDetail");
  const coverage = document.getElementById("professionalNetworkCoverage");
  const token = () => localStorage.getItem("access_token") || localStorage.getItem("token") || "";
  const request = (path, options = {}) => fetch(path, { ...options, headers: { Authorization: `Bearer ${token()}`, ...(options.headers || {}) } });

  coverage?.addEventListener("click", event => {
    const button = event.target.closest("[data-network-specialty]");
    if (!button) return;
    const input = document.getElementById("professionalNetworkSpecialty");
    if (input) input.value = button.dataset.networkSpecialty;
    document.getElementById("professionalNetworkRefresh")?.click();
  });

  const enhanceDetail = async () => {
    if (!detail || detail.classList.contains("hidden") || detail.dataset.enhanced === "1") return;
    const statusButton = detail.querySelector("[data-network-status-id]");
    if (!statusButton) return;
    const response = await request(`/professional-network/applications/${statusButton.dataset.networkStatusId}`);
    if (!response.ok) return;
    const item = await response.json();
    detail.dataset.enhanced = "1";
    const files = (item.uploads || []).map(file => `<li><a href="${file.url}" target="_blank" rel="noreferrer">${file.name}</a></li>`).join("") || "<li>Sin archivos registrados</li>";
    const notes = (item.notes || []).map(note => `<li>${note.note}</li>`).join("") || "<li>Sin notas internas</li>";
    detail.insertAdjacentHTML("beforeend", `<div class="professional-network-extra"><h4>Archivos y notas internas</h4><ul>${files}</ul><ul>${notes}</ul><textarea id="professionalNetworkNote" placeholder="Nota interna"></textarea><button type="button" data-network-note-id="${statusButton.dataset.networkStatusId}">Guardar nota</button></div>`);
  };

  detail && new MutationObserver(() => { enhanceDetail().catch(() => {}); }).observe(detail, { childList: true });
  detail?.addEventListener("click", async event => {
    const button = event.target.closest("[data-network-note-id]");
    if (!button) return;
    const note = document.getElementById("professionalNetworkNote")?.value.trim();
    if (!note) return;
    const response = await request(`/professional-network/applications/${button.dataset.networkNoteId}/notes`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ note }) });
    if (response.ok) { detail.dataset.enhanced = ""; document.getElementById("professionalNetworkRefresh")?.click(); }
  });
})();
