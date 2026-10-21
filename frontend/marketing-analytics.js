(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TotalSolutionsMarketing = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const ATTRIBUTION_KEYS = ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "wbraid", "gbraid"];
  const CONFIG_ENDPOINT = "/public/marketing-config";
  const CONVERSION_KEY_PREFIX = "totalsolutions_marketing_conversion_v2:";
  const MARKETING_INIT_TIMEOUT_MS = 3000;
  const EVENT_CALLBACK_TIMEOUT_MS = 1200;
  let configPromise = null;
  let pendingEvents = [];
  const inMemoryConversions = new Set();

  const trim = value => String(value || "").replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 120);
  const read = (storage, key) => {
    try { return storage?.getItem(key) || ""; } catch (_error) { return ""; }
  };
  const write = (storage, key, value) => {
    try { storage?.setItem(key, value); } catch (_error) { /* Storage is optional. */ }
  };

  const randomKey = root => {
    if (root?.crypto?.randomUUID) return root.crypto.randomUUID();
    if (root?.crypto?.getRandomValues) {
      const bytes = new Uint8Array(16);
      root.crypto.getRandomValues(bytes);
      return Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
    }
    return `request-${Date.now()}`;
  };

  const safeAttribution = search => {
    const query = new URLSearchParams(search || "");
    const values = {};
    for (const key of ATTRIBUTION_KEYS) {
      const value = trim(query.get(key));
      if (value) values[key] = value;
    }
    return values;
  };

  const captureAttribution = (search, storage, key) => {
    let values = {};
    try {
      const persisted = read(storage, key);
      values = persisted ? JSON.parse(persisted) : safeAttribution(search);
    } catch (_error) {
      values = safeAttribution(search);
    }
    if (values && typeof values === "object" && Object.keys(values).length) write(storage, key, JSON.stringify(values));
    return values && typeof values === "object" ? values : {};
  };

  const attributionDetail = values => Object.entries(values || {})
    .filter(([key]) => ATTRIBUTION_KEYS.includes(key))
    .map(([key, value]) => `${key}=${trim(value)}`)
    .join(";")
    .slice(0, 240);

  const originFor = values => values?.gclid || values?.wbraid || values?.gbraid || values?.utm_source?.toLowerCase() === "google"
    ? "GOOGLE_ADS"
    : "LANDING_PAGE";

  const normalizeMarketingConfig = config => {
    if (!config || typeof config !== "object") return {};
    const normalized = {};
    const ga4 = trim(config.ga4_measurement_id).toUpperCase();
    const gtm = trim(config.gtm_container_id).toUpperCase();
    if (/^G-[A-Z0-9]+$/.test(ga4)) normalized.ga4_measurement_id = ga4;
    if (/^GTM-[A-Z0-9]+$/.test(gtm)) normalized.gtm_container_id = gtm;
    return normalized;
  };

  const isBearerTrackingPath = pathname => {
    const path = String(pathname || "");
    return path.startsWith("/acompanhar/") || path.startsWith("/seguimiento/");
  };

  const shouldConfigureForPath = pathname => !isBearerTrackingPath(pathname);

  const loadScript = (document, src, id) => new Promise(resolve => {
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

  const dispatch = (root, eventName, params, onComplete = null) => {
    let settled = false;
    let timer = null;
    const complete = outcome => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      if (typeof onComplete === "function") onComplete(outcome);
    };
    const tagParams = typeof onComplete === "function"
      ? { ...params, event_callback: () => complete("callback"), event_timeout: EVENT_CALLBACK_TIMEOUT_MS }
      : params;
    const payload = typeof onComplete === "function"
      ? { event: eventName, ...params, eventCallback: () => complete("callback"), eventTimeout: EVENT_CALLBACK_TIMEOUT_MS }
      : { event: eventName, ...params };
    try {
      if (typeof root?.gtag === "function") root.gtag("event", eventName, tagParams);
      else if (Array.isArray(root?.dataLayer)) root.dataLayer.push(payload);
      else complete("queued");
    } catch (_error) {
      complete("error");
    }
    if (typeof onComplete === "function" && !settled) {
      timer = setTimeout(() => complete("timeout"), EVENT_CALLBACK_TIMEOUT_MS);
    }
  };

  const flush = root => {
    const queued = pendingEvents;
    pendingEvents = [];
    queued.forEach(item => {
      if (item.cancelled) return;
      dispatch(root, item.eventName, item.params, item.onComplete);
    });
  };

  const configure = (root = globalThis, document = root?.document, endpoint = CONFIG_ENDPOINT) => {
    if (configPromise) return configPromise;
    const request = typeof root?.fetch === "function"
      ? Promise.resolve(root.fetch(endpoint, { headers: { Accept: "application/json" }, cache: "no-store" }))
        .then(async response => response.ok ? normalizeMarketingConfig(await response.json()) : {})
        .catch(() => ({}))
      : Promise.resolve({});
    const timeout = new Promise(resolve => {
      const timer = typeof root?.setTimeout === "function" ? root.setTimeout : setTimeout;
      timer(() => resolve({}), MARKETING_INIT_TIMEOUT_MS);
    });
    configPromise = Promise.race([request, timeout]).then(async config => {
      const normalized = config || {};
      if (normalized.gtm_container_id) {
        root.dataLayer = Array.isArray(root.dataLayer) ? root.dataLayer : [];
        root.dataLayer.push({ "gtm.start": Date.now(), event: "gtm.js" });
        await loadScript(document, `https://www.googletagmanager.com/gtm.js?id=${encodeURIComponent(normalized.gtm_container_id)}`, "total-solutions-gtm");
      } else if (normalized.ga4_measurement_id) {
        root.dataLayer = Array.isArray(root.dataLayer) ? root.dataLayer : [];
        root.gtag = root.gtag || function () { root.dataLayer.push(arguments); };
        await loadScript(document, `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(normalized.ga4_measurement_id)}`, "total-solutions-gtag");
        root.gtag("js", new Date());
        root.gtag("config", normalized.ga4_measurement_id);
      }
      return normalized;
    }).catch(() => ({})).finally(() => flush(root));
    return configPromise;
  };

  const track = (root, eventName, params = {}) => {
    if (typeof root?.gtag === "function" || Array.isArray(root?.dataLayer)) dispatch(root, eventName, { ...params });
    else pendingEvents.push({ eventName, params: { ...params } });
  };

  const getOrCreateRequestKey = (storage, key, root = globalThis) => {
    const existing = read(storage, key);
    if (existing) return existing;
    const value = randomKey(root);
    write(storage, key, value);
    return value;
  };

  const trackLeadOnce = (root, storage, requestKey, funnel, method) => {
    const key = String(requestKey || "").trim();
    const funnelName = String(funnel || "").trim();
    if (!key || !funnelName) return false;
    const conversionKey = `${CONVERSION_KEY_PREFIX}${funnelName}:${key}`;
    if (read(storage, conversionKey) || inMemoryConversions.has(conversionKey)) return false;
    inMemoryConversions.add(conversionKey);
    write(storage, conversionKey, "1");
    try {
      track(root, "generate_lead", { funnel: funnelName, method: String(method || "form") });
    } catch (_error) { /* Analytics must never block the confirmed request. */ }
    return true;
  };

  const trackLeadOnceWithDelivery = (root, storage, requestKey, funnel, method, timeoutMs = EVENT_CALLBACK_TIMEOUT_MS) => {
    const key = String(requestKey || "").trim();
    const funnelName = String(funnel || "").trim();
    if (!key || !funnelName) return Promise.resolve({ status: "invalid" });
    const conversionKey = `${CONVERSION_KEY_PREFIX}${funnelName}:${key}`;
    if (read(storage, conversionKey) || inMemoryConversions.has(conversionKey)) {
      return Promise.resolve({ status: "duplicate" });
    }
    inMemoryConversions.add(conversionKey);
    write(storage, conversionKey, "1");

    return new Promise(resolve => {
      let settled = false;
      let timer = null;
      const item = {
        eventName: "generate_lead",
        params: { funnel: funnelName, method: String(method || "form") },
        cancelled: false,
        onComplete: null,
      };
      const settle = status => {
        if (settled) return;
        settled = true;
        item.cancelled = true;
        if (timer) clearTimeout(timer);
        resolve({ status });
      };
      item.onComplete = settle;
      timer = setTimeout(() => settle("timeout"), Math.max(0, Number(timeoutMs) || EVENT_CALLBACK_TIMEOUT_MS));
      if (typeof root?.gtag === "function" || Array.isArray(root?.dataLayer)) {
        dispatch(root, item.eventName, item.params, settle);
      } else {
        pendingEvents.push(item);
      }
    });
  };

  return {
    ATTRIBUTION_KEYS,
    CONFIG_ENDPOINT,
    safeAttribution,
    captureAttribution,
    attributionDetail,
    originFor,
    normalizeMarketingConfig,
    isBearerTrackingPath,
    shouldConfigureForPath,
    configure,
    track,
    getOrCreateRequestKey,
    trackLeadOnce,
    trackLeadOnceWithDelivery,
    MARKETING_INIT_TIMEOUT_MS,
    EVENT_CALLBACK_TIMEOUT_MS,
  };
});
