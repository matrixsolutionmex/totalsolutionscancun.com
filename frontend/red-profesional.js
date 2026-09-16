(function () {
  const form = document.querySelector("#professionalApplicationForm");
  if (!form) return;
  document.title = "Únete a la Red Profesional de Total Solutions Cancún";
  const heroTitle = document.querySelector(".network-hero h1");
  if (heroTitle) heroTitle.textContent = "Únete a la Red Profesional de Total Solutions Cancún";
  const heroIntro = document.querySelector(".network-hero p:not(.public-kicker)");
  if (heroIntro) heroIntro.textContent = "Buscamos técnicos, especialistas, ingenieros y empresas de mantenimiento para formar nuestra red profesional.";
  const contactFields = form.querySelector(".network-form-section");
  if (contactFields && !form.elements.phone) {
    const wrapper = document.createElement("div");
    wrapper.className = "network-field";
    wrapper.innerHTML = '<label for="phone">Teléfono</label><input id="phone" name="phone" maxlength="40">';
    contactFields.querySelector(".network-fields").appendChild(wrapper);
  }
  const specialtyOptions = ["Refrigeración", "Impermeabilización", "Cisternas / tinacos / bombas", "Albañilería", "Tablaroca / drywall", "Herrería / soldadura", "Cerrajería", "Pisos / azulejos", "Vidrios / aluminio", "Electrodomésticos", "CCTV / seguridad", "Redes / internet", "Automatización", "Jardinería", "Limpieza técnica", "Ingeniería civil", "Ingeniería eléctrica", "Ingeniería mecánica", "Arquitectura", "Supervisión de obra", "Otro"];
  const specialtyGroup = form.querySelector("input[name=specialty]")?.parentElement?.parentElement;
  if (specialtyGroup) specialtyOptions.forEach(value => { if (![...form.querySelectorAll("input[name=specialty]")].some(input => input.value === value)) specialtyGroup.insertAdjacentHTML("beforeend", `<label class="network-option"><input type="checkbox" name="specialty" value="${value}">${value}</label>`); });
  const privacySection = form.querySelector(".network-privacy")?.parentElement;
  if (privacySection && !form.elements.consent_profile) privacySection.insertAdjacentHTML("afterbegin", '<label class="network-privacy"><input type="checkbox" name="consent_profile" value="true" required><span>Acepto que Total Solutions analice mi perfil profesional para evaluar oportunidades adecuadas.</span></label>');
  const status = document.querySelector("#applicationStatus");
  const button = form.querySelector("button[type=submit]");
  const submissionKey = sessionStorage.getItem("ts_professional_submission_key") || crypto.randomUUID();
  sessionStorage.setItem("ts_professional_submission_key", submissionKey);
  form.elements.submission_key.value = submissionKey;
  const show = (message, kind) => { status.textContent = message; status.className = `network-status is-visible ${kind}`; };
  form.addEventListener("submit", async event => {
    event.preventDefault();
    button.disabled = true;
    show("Enviando candidatura...", "");
    try {
      const response = await fetch("/public/professional-applications", { method: "POST", body: new FormData(form) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "No fue posible registrar la candidatura.");
      show(`Candidatura recibida. Tu código es ${data.public_code}. El equipo revisará la información y te contactará si existe una oportunidad adecuada.`, "success");
      form.reset();
      form.elements.submission_key.value = submissionKey;
    } catch (error) {
      show(error.message || "No fue posible registrar la candidatura.", "error");
    } finally { button.disabled = false; }
  });
})();
