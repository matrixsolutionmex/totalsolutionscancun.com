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
  const sharedMarketing = root.TotalSolutionsMarketing;

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
    const gtm = trim(config.gtm_container_id).toUpperCase();
    if (/^G-[A-Z0-9]+$/.test(ga4)) normalized.ga4_measurement_id = ga4;
    if (/^GTM-[A-Z0-9]+$/.test(gtm)) normalized.gtm_container_id = gtm;
    return normalized;
  };
  const eventParams = (eventName, params = {}, config = marketingConfig) => {
    return { ...params };
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
  const saveConfirmationState = (confirmation, store, data, baseUrl, now = Date.now()) => {
    if (!confirmation || !data || typeof confirmation.createState !== "function" || typeof confirmation.write !== "function") {
      const error = new Error("Confirmation state unavailable");
      error.code = "confirmation_unavailable";
      throw error;
    }
    try {
      const state = confirmation.createState(data, "es", now, baseUrl, "quote");
      confirmation.write(store, state, baseUrl);
      return state;
    } catch (_error) {
      const error = new Error("Confirmation state unavailable");
      error.code = "confirmation_unavailable";
      throw error;
    }
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
    if (sharedMarketing?.track) {
      sharedMarketing.track(root, eventName, safeParams);
      return;
    }
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
    if (sharedMarketing?.configure) {
      marketingConfig = await sharedMarketing.configure(root, document, MARKETING_CONFIG_ENDPOINT);
      return marketingConfig;
    }
    try {
      const response = await root.fetch(MARKETING_CONFIG_ENDPOINT, { headers: { Accept: "application/json" }, cache: "no-store" });
      if (!response.ok) return;
      marketingConfig = normalizeMarketingConfig(await response.json());
      if (marketingConfig.gtm_container_id) {
        root.dataLayer = Array.isArray(root.dataLayer) ? root.dataLayer : [];
        root.dataLayer.push({ "gtm.start": Date.now(), event: "gtm.js" });
        await loadScript(`https://www.googletagmanager.com/gtm.js?id=${encodeURIComponent(marketingConfig.gtm_container_id)}`, "total-solutions-gtm");
      } else {
        const tagId = marketingConfig.ga4_measurement_id;
        if (tagId) {
          root.dataLayer = Array.isArray(root.dataLayer) ? root.dataLayer : [];
          root.gtag = root.gtag || function () { root.dataLayer.push(arguments); };
          await loadScript(`https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(tagId)}`, "total-solutions-gtag");
          root.gtag("js", new Date());
          if (marketingConfig.ga4_measurement_id) root.gtag("config", marketingConfig.ga4_measurement_id);
        }
      }
    } catch (_error) {
      marketingConfig = {};
    } finally {
      flushPendingEvents();
    }
  };
  const errorText = error => error?.name === "AbortError" ? "La solicitud tardó demasiado. Inténtalo nuevamente." : "No fue posible enviar la solicitud. Inténtalo nuevamente.";
  const confirmationFailureText = error => error?.code === "confirmation_unavailable"
    ? "La solicitud fue registrada, pero no pudimos guardar la confirmación en esta pestaña. No la envíes nuevamente."
    : errorText(error);
  const waitForMarketingReady = (ready, timeoutMs = 3000) => new Promise(resolve => {
    let settled = false;
    let timer = null;
    const finish = status => {
      if (settled) return;
      settled = true;
      if (timer) root.clearTimeout(timer);
      resolve(status);
    };
    timer = root.setTimeout(() => finish("timeout"), Math.max(0, Number(timeoutMs) || 3000));
    Promise.resolve(ready).then(() => finish("ready"), () => finish("error"));
  });
  const navigateOnce = (state, navigate, url) => {
    if (!state || state.started) return false;
    state.started = true;
    navigate(url);
    return true;
  };

  function init() {
    if (!document) return;
    const form = document.getElementById("quote-form");
    if (!form) return;
    const submit = document.getElementById("quote-submit");
    const status = document.getElementById("quote-status");
    const description = document.getElementById("quote-description");
    const session = storage("sessionStorage");
    const navigationState = { started: false };
    marketingReady = configureMarketing();
    const persisted = read(session, ATTRIBUTION_KEY);
    let attribution = {};
    try { attribution = persisted ? JSON.parse(persisted) : safeAttribution(); } catch (_error) { attribution = safeAttribution(); }
    if (Object.keys(attribution).length) write(session, ATTRIBUTION_KEY, JSON.stringify(attribution));
    let formStarted = false;
    form.addEventListener("focusin", () => {
      if (!formStarted) { formStarted = true; track("form_start", { funnel: "cotizar" }); }
    });
    document.querySelectorAll("[data-whatsapp]").forEach(link => link.addEventListener("click", () => {
      track("whatsapp_click", { funnel: "cotizar" });
      const message = encodeURIComponent("Hola, quiero solicitar una cotización de mantenimiento en Cancún.");
      link.href = `${WHATSAPP_URL}?text=${message}`;
    }));
    document.querySelectorAll("[data-phone]").forEach(link => link.addEventListener("click", () => track("phone_click", { funnel: "cotizar" })));
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
        let confirmationError = null;
        try {
          saveConfirmationState(root.TotalSolutionsConfirmation, storage("sessionStorage"), data, root.location.origin);
        } catch (error) {
          confirmationError = error;
        }
        try {
          await waitForMarketingReady(marketingReady);
          if (sharedMarketing?.trackLeadOnceWithDelivery) {
            await sharedMarketing.trackLeadOnceWithDelivery(root, session, key, "cotizar", "quote_form");
          } else if (sharedMarketing?.trackLeadOnce) {
            sharedMarketing.trackLeadOnce(root, session, key, "cotizar", "quote_form");
          } else {
            const conversionKey = `${CONVERSION_KEY_PREFIX}${key}`;
            if (!read(session, conversionKey)) {
              write(session, conversionKey, "1");
              track("generate_lead", { funnel: "cotizar", method: "quote_form" });
            }
          }
        } catch (_error) { /* Measurement must never block a confirmed request. */ }
        if (confirmationError) throw confirmationError;
        try { session?.removeItem(IDEMPOTENCY_KEY); } catch (_error) { /* Storage is optional. */ }
        navigateOnce(navigationState, url => root.location.assign(url), "/solicitud-enviada?lang=es");
      } catch (error) {
        status.textContent = confirmationFailureText(error);
        if (error?.code === "confirmation_unavailable") {
          status.className = "quote-status quote-status-confirmed";
          submit.setAttribute("data-confirmed", "true");
          submit.removeAttribute("aria-busy");
          return;
        }
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
    saveConfirmationState,
    confirmationFailureText,
    waitForMarketingReady,
    navigateOnce,
    canStartSubmission,
  };
  if (document?.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})(typeof globalThis !== "undefined" ? globalThis : this, typeof document !== "undefined" ? document : null);
