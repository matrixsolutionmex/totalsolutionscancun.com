(function (root, document) {
  "use strict";

  const ATTRIBUTION_KEY = "totalsolutions_quote_attribution_v1";
  const IDEMPOTENCY_KEY = "totalsolutions_quote_idempotency_v1";
  const CONVERSION_KEY_PREFIX = "totalsolutions_quote_conversion_v1:";
  const MARKETING_CONFIG_ENDPOINT = "/public/marketing-config";
  const REQUEST_TIMEOUT_MS = 30000;
  const WHATSAPP_URL = "https://wa.me/529984078199";
  const ALLOWED_ATTRIBUTION_KEYS = ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "wbraid", "gbraid"];
  let marketingConfig = {};
  let marketingReady = Promise.resolve();
  let pendingEvents = [];

  const trim = value => String(value || "").replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 120);
  const storage = (name) => {
    try { return root[name]; } catch (_error) { return null; }
  };
  const read = (store, key) => {
    try { return store?.getItem(key) || ""; } catch (_error) { return ""; }
  };
  const write = (store, key, value) => {
    try { store?.setItem(key, value); } catch (_error) { /* Storage is optional. */ }
  };
  const randomKey = () => {
    if (root.crypto?.randomUUID) return root.crypto.randomUUID();
    if (root.crypto?.getRandomValues) {
      const bytes = new Uint8Array(16);
      root.crypto.getRandomValues(bytes);
      return Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
    }
    return `quote-${Date.now()}`;
  };
  const safeAttribution = (search) => {
    const query = new URLSearchParams(search || root.location?.search || "");
    const values = {};
    for (const key of ALLOWED_ATTRIBUTION_KEYS) {
      const value = trim(query.get(key));
      if (value) values[key] = value;
    }
    return values;
  };
  const attributionDetail = values => Object.entries(values).map(([key, value]) => `${key}=${value}`).join(";").slice(0, 240);
  const originFor = values => values.gclid || values.wbraid || values.gbraid || values.utm_source?.toLowerCase() === "google" ? "GOOGLE_ADS" : "LANDING_PAGE";
  const normalizeMarketingConfig = config => {
    if (!config || typeof config !== "object") return {};
    const normalized = {};
    const ga4 = trim(config.ga4_measurement_id).toUpperCase();
    const ads = trim(config.google_ads_conversion_id).toUpperCase();
    const label = trim(config.google_ads_lead_conversion_label);
    const gtm = trim(config.gtm_container_id).toUpperCase();
    if (/^G-[A-Z0-9]+$/.test(ga4)) normalized.ga4_measurement_id = ga4;
    if (/^AW-[0-9]+$/.test(ads)) normalized.google_ads_conversion_id = ads;
    if (/^[A-Za-z0-9_-]+$/.test(label)) normalized.google_ads_lead_conversion_label = label;
    if (/^GTM-[A-Z0-9]+$/.test(gtm)) normalized.gtm_container_id = gtm;
    return normalized;
  };
  const eventParams = (eventName, params = {}, config = marketingConfig) => {
    const safeParams = { ...params };
    if (eventName === "generate_lead" && config.google_ads_conversion_id && config.google_ads_lead_conversion_label) {
      safeParams.send_to = `${config.google_ads_conversion_id}/${config.google_ads_lead_conversion_label}`;
    }
    return safeParams;
  };
  const isSuccessfulResponse = response => {
    if (!response) return false;
    const status = Number(response.status);
    if (Number.isFinite(status) && status > 0) return status >= 200 && status < 300;
    return response.ok === true;
  };
  const readSuccessfulPayload = async response => {
    if (!isSuccessfulResponse(response)) throw new Error("request_failed");
    if (typeof response.text === "function") {
      const raw = await response.text();
      if (!raw.trim()) return null;
      try { return JSON.parse(raw); } catch (_error) { return null; }
    }
    if (typeof response.json === "function") {
      try { return await response.json(); } catch (_error) { return null; }
    }
    return null;
  };
  const requestSuccessfulPayload = async (fetchImpl, endpoint, options) => {
    const response = await fetchImpl(endpoint, options);
    return readSuccessfulPayload(response);
  };
  const canStartSubmission = submit => Boolean(submit) && !submit.disabled;
  const dispatchTrackedEvent = (eventName, params) => {
    const payload = { event: eventName, ...params };
    try {
      if (typeof root.gtag === "function") root.gtag("event", eventName, params);
      if (Array.isArray(root.dataLayer)) root.dataLayer.push(payload);
    } catch (_error) { /* Analytics must never block the funnel. */ }
  };
  const track = (eventName, params = {}) => {
    const safeParams = eventParams(eventName, params);
    if (typeof root.gtag === "function" || Array.isArray(root.dataLayer)) dispatchTrackedEvent(eventName, safeParams);
    else pendingEvents.push({ eventName, params: safeParams });
  };
  const flushPendingEvents = () => {
    const queued = pendingEvents;
    pendingEvents = [];
    queued.forEach(item => dispatchTrackedEvent(item.eventName, item.params));
  };
  const loadScript = (src, id) => new Promise(resolve => {
    if (!document) return resolve(false);
    const existing = document.getElementById(id);
    if (existing) return resolve(true);
    const script = document.createElement("script");
    script.id = id;
    script.async = true;
    script.src = src;
    script.onload = () => resolve(true);
    script.onerror = () => resolve(false);
    document.head.appendChild(script);
  });
  const configureMarketing = async () => {
    try {
      const response = await root.fetch(MARKETING_CONFIG_ENDPOINT, { headers: { Accept: "application/json" }, cache: "no-store" });
      if (!response.ok) return;
      marketingConfig = normalizeMarketingConfig(await response.json());
      if (marketingConfig.gtm_container_id) {
        root.dataLayer = Array.isArray(root.dataLayer) ? root.dataLayer : [];
        root.dataLayer.push({ "gtm.start": Date.now(), event: "gtm.js" });
        await loadScript(`https://www.googletagmanager.com/gtm.js?id=${encodeURIComponent(marketingConfig.gtm_container_id)}`, "total-solutions-gtm");
      } else {
        const tagId = marketingConfig.ga4_measurement_id || marketingConfig.google_ads_conversion_id;
        if (tagId) {
          root.dataLayer = Array.isArray(root.dataLayer) ? root.dataLayer : [];
          root.gtag = root.gtag || function () { root.dataLayer.push(arguments); };
          await loadScript(`https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(tagId)}`, "total-solutions-gtag");
          root.gtag("js", new Date());
          if (marketingConfig.ga4_measurement_id) root.gtag("config", marketingConfig.ga4_measurement_id);
          if (marketingConfig.google_ads_conversion_id) root.gtag("config", marketingConfig.google_ads_conversion_id);
        }
      }
    } catch (_error) {
      marketingConfig = {};
    } finally {
      flushPendingEvents();
    }
  };
  const errorText = error => error?.name === "AbortError" ? "La solicitud tardó demasiado. Inténtalo nuevamente." : "No fue posible enviar la solicitud. Inténtalo nuevamente.";

  function init() {
    if (!document) return;
    const form = document.getElementById("quote-form");
    if (!form) return;
    const submit = document.getElementById("quote-submit");
    const status = document.getElementById("quote-status");
    const description = document.getElementById("quote-description");
    const session = storage("sessionStorage");
    marketingReady = configureMarketing();
    const persisted = read(session, ATTRIBUTION_KEY);
    let attribution = {};
    try { attribution = persisted ? JSON.parse(persisted) : safeAttribution(); } catch (_error) { attribution = safeAttribution(); }
    if (Object.keys(attribution).length) write(session, ATTRIBUTION_KEY, JSON.stringify(attribution));
    let formStarted = false;
    form.addEventListener("focusin", () => {
      if (!formStarted) { formStarted = true; track("form_start", { funnel: "quote" }); }
    });
    document.querySelectorAll("[data-whatsapp]").forEach(link => link.addEventListener("click", () => {
      track("whatsapp_click", { funnel: "quote" });
      const message = encodeURIComponent("Hola, quiero solicitar una cotización de mantenimiento en Cancún.");
      link.href = `${WHATSAPP_URL}?text=${message}`;
    }));
    document.querySelectorAll("[data-phone]").forEach(link => link.addEventListener("click", () => track("phone_click", { funnel: "quote" })));
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (!canStartSubmission(submit)) return;
      if (!form.reportValidity()) return;
      submit.disabled = true;
      submit.setAttribute("aria-busy", "true");
      status.textContent = "Enviando…";
      const body = new FormData(form);
      body.set("property_type", "Casa");
      body.set("address_line1", "Por confirmar");
      body.set("locality", "Cancún");
      body.set("administrative_area", "Quintana Roo");
      body.set("country_code", "MX");
      body.set("problem_description", description.value.trim() || "Solicitud de cotización desde landing");
      body.set("consent_privacy", "true");
      body.set("consent_images", "false");
      body.set("public_language", "es-MX");
      body.set("funnel", "quote");
      body.set("lead_origin", originFor(attribution));
      body.set("attribution", attributionDetail(attribution));
      let key = read(session, IDEMPOTENCY_KEY);
      if (!key) { key = randomKey(); write(session, IDEMPOTENCY_KEY, key); }
      body.set("idempotency_key", key);
      const controller = new AbortController();
      const timer = root.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      try {
        const data = await requestSuccessfulPayload(root.fetch, "/public/service-requests?funnel=quote", { method: "POST", body, headers: { Accept: "application/json" }, signal: controller.signal });
        const confirmation = root.TotalSolutionsConfirmation;
        if (confirmation && data && typeof confirmation.createState === "function" && typeof confirmation.write === "function") {
          try {
            const state = confirmation.createState(data, "es", Date.now(), root.location.origin);
            confirmation.write(storage("sessionStorage"), state, root.location.origin);
          } catch (_error) {
            // The backend already confirmed creation; optional confirmation storage must not re-submit.
          }
        }
        const conversionKey = `${CONVERSION_KEY_PREFIX}${key}`;
        if (!read(session, conversionKey)) {
          write(session, conversionKey, "1");
          await marketingReady;
          track("generate_lead", { funnel: "quote", method: "quote_form" });
        }
        try { session?.removeItem(IDEMPOTENCY_KEY); } catch (_error) { /* Storage is optional. */ }
        root.location.assign("/solicitud-enviada?lang=es");
      } catch (error) {
        status.textContent = errorText(error);
        submit.disabled = false;
        submit.removeAttribute("aria-busy");
      } finally {
        root.clearTimeout(timer);
      }
    });
  }

  if (typeof module === "object" && module.exports) module.exports = {
    safeAttribution,
    attributionDetail,
    originFor,
    normalizeMarketingConfig,
    eventParams,
    isSuccessfulResponse,
    readSuccessfulPayload,
    requestSuccessfulPayload,
    canStartSubmission,
  };
  if (document?.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})(typeof globalThis !== "undefined" ? globalThis : this, typeof document !== "undefined" ? document : null);
