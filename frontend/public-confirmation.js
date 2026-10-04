(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TotalSolutionsConfirmation = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const STORAGE_KEY = "totalsolutions_request_confirmation_v1";
  const STATE_VERSION = 1;
  const STATE_TTL_MS = 30 * 60 * 1000;
  const REQUEST_TIMEOUT_MS = 30000;

  const normalizeLanguage = (value) => String(value || "").toLowerCase().startsWith("pt")
    ? "pt-BR"
    : String(value || "").toLowerCase().startsWith("en") ? "en" : "es";

  const languageQuery = (value) => normalizeLanguage(value) === "pt-BR" ? "pt" : normalizeLanguage(value);

  const requiredText = (value, field) => {
    if (typeof value !== "string" || !value.trim()) throw new Error(`Invalid confirmation field: ${field}`);
    return value.trim();
  };

  const optionalText = (value) => typeof value === "string" && value.trim() ? value.trim() : null;
  const optionalNumber = (value) => value === null || value === undefined || value === ""
    ? null
    : Number.isFinite(Number(value)) ? Number(value) : null;

  function safeTrackingUrl(value, trackingToken, baseUrl) {
    const rawUrl = requiredText(value, "tracking_url");
    let parsed;
    try {
      parsed = new URL(rawUrl, baseUrl || "http://localhost");
    } catch (_error) {
      throw new Error("Invalid confirmation field: tracking_url");
    }
    if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("Invalid confirmation field: tracking_url");
    if (parsed.origin !== new URL(baseUrl || parsed.origin).origin || parsed.search || parsed.hash) {
      throw new Error("Invalid confirmation field: tracking_url");
    }
    const segments = parsed.pathname.split("/").filter(Boolean);
    const route = segments.at(-2);
    const token = decodeURIComponent(segments.at(-1) || "");
    if (!["seguimiento", "acompanhar"].includes(route) || token !== trackingToken) {
      throw new Error("Invalid confirmation field: tracking_url");
    }
    return rawUrl.startsWith("/") ? parsed.pathname : parsed.href;
  }

  function sanitizePricing(value) {
    const pricing = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    return {
      currency: optionalText(pricing.currency) || "MXN",
      visit_calculated_price: optionalNumber(pricing.visit_calculated_price),
      zone: optionalText(pricing.zone),
      urgency_multiplier: optionalNumber(pricing.urgency_multiplier),
      market_reference_min: optionalNumber(pricing.market_reference_min),
      market_reference_max: optionalNumber(pricing.market_reference_max),
    };
  }

  function createState(data, language, now, baseUrl) {
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Invalid confirmation response");
    const trackingToken = requiredText(data.tracking_token, "tracking_token");
    const createdAt = Number.isFinite(now) ? now : Date.now();
    return {
      version: STATE_VERSION,
      created_at: createdAt,
      expires_at: createdAt + STATE_TTL_MS,
      language: normalizeLanguage(language),
      confirmation: {
        order_number: requiredText(data.order_number, "order_number"),
        tracking_token: trackingToken,
        status: requiredText(data.status, "status"),
        tracking_url: safeTrackingUrl(data.tracking_url, trackingToken, baseUrl),
        pricing: sanitizePricing(data.pricing),
      },
    };
  }

  function validState(value, now, baseUrl) {
    if (!value || value.version !== STATE_VERSION || !Number.isFinite(value.created_at) || !Number.isFinite(value.expires_at)) return null;
    const currentTime = Number.isFinite(now) ? now : Date.now();
    if (value.created_at > currentTime + 60000 || value.expires_at <= currentTime || value.expires_at - value.created_at !== STATE_TTL_MS) return null;
    try {
      return createState(value.confirmation, value.language, value.created_at, baseUrl);
    } catch (_error) {
      return null;
    }
  }

  function clear(storage) {
    try {
      storage.removeItem(STORAGE_KEY);
    } catch (_error) {
      // Storage may be unavailable; callers still fail closed.
    }
  }

  function read(storage, now, baseUrl) {
    try {
      const state = validState(JSON.parse(storage.getItem(STORAGE_KEY)), now, baseUrl);
      if (!state) clear(storage);
      return state;
    } catch (_error) {
      clear(storage);
      return null;
    }
  }

  function write(storage, state, baseUrl) {
    storage.setItem(STORAGE_KEY, JSON.stringify(state));
    const saved = read(storage, state.created_at, baseUrl);
    if (!saved) throw new Error("Confirmation storage unavailable");
    return saved;
  }

  async function submit(options) {
    const {
      endpoint, body, language, storage, fetchImpl, navigate, baseUrl,
      timeoutMs = REQUEST_TIMEOUT_MS, now = Date.now(),
      AbortControllerImpl = typeof AbortController !== "undefined" ? AbortController : null,
      setTimer = setTimeout, clearTimer = clearTimeout,
    } = options;
    clear(storage);
    if (!AbortControllerImpl) throw new Error("Request timeout protection unavailable");
    const controller = new AbortControllerImpl();
    const timer = setTimer(() => controller.abort(), timeoutMs);
    try {
      const response = await fetchImpl(endpoint, {
        method: "POST",
        body,
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data?.detail || "No fue posible registrar la solicitud");
      const state = createState(data, language, now, baseUrl);
      write(storage, state, baseUrl);
      navigate(`/solicitud-enviada?lang=${languageQuery(language)}`);
      return state;
    } catch (error) {
      clear(storage);
      if (error?.name === "AbortError") throw new Error("La solicitud excedió el tiempo de espera");
      throw error;
    } finally {
      clearTimer(timer);
    }
  }

  return {
    STORAGE_KEY,
    STATE_TTL_MS,
    REQUEST_TIMEOUT_MS,
    normalizeLanguage,
    languageQuery,
    createState,
    validState,
    read,
    clear,
    submit,
  };
});
