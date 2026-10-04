"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const confirmation = require("./public-confirmation.js");

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
  removeItem(key) { this.values.delete(key); }
}

const responsePayload = (overrides = {}) => ({
  order_number: "OS-LOCAL-001",
  tracking_token: "synthetic-token-001",
  status: "Solicitud recibida",
  tracking_url: "/seguimiento/synthetic-token-001",
  pricing: {
    currency: "MXN",
    visit_calculated_price: 850,
    zone: "Cancun Centro",
    urgency_multiplier: 1,
    market_reference_min: 1200,
    market_reference_max: 1800,
    customer_budget_min: 5000,
  },
  requester_name: "Synthetic Person",
  requester_email: "synthetic@example.invalid",
  requester_phone: "+52 000 000 0000",
  address_line1: "Synthetic address",
  ...overrides,
});

const okResponse = (payload = responsePayload()) => ({ ok: true, json: async () => payload });

test("successful confirmed response stores only display data and navigates with a real success URL", async () => {
  const storage = new MemoryStorage();
  const navigations = [];
  let request;
  await confirmation.submit({
    endpoint: "https://example.test/public/service-requests",
    body: "synthetic-form",
    language: "pt-BR",
    storage,
    fetchImpl: async (url, options) => { request = { url, options }; return okResponse(); },
    navigate: (url) => navigations.push(url),
    baseUrl: "https://example.test",
    now: 1000,
  });

  assert.equal(request.url, "https://example.test/public/service-requests");
  assert.equal(request.options.method, "POST");
  assert.deepEqual(navigations, ["/solicitud-enviada?lang=pt"]);
  const serialized = storage.getItem(confirmation.STORAGE_KEY);
  assert.match(serialized, /OS-LOCAL-001/);
  for (const sensitive of ["Synthetic Person", "synthetic@example.invalid", "+52 000", "Synthetic address", "customer_budget_min"]) {
    assert.equal(serialized.includes(sensitive), false);
  }
});

test("API error, invalid JSON and incomplete confirmations fail closed without navigation", async () => {
  const cases = [
    { ok: false, json: async () => ({ detail: "Synthetic API error" }) },
    { ok: true, json: async () => { throw new SyntaxError("invalid json"); } },
    okResponse(responsePayload({ order_number: null })),
    okResponse(responsePayload({ tracking_token: "" })),
    okResponse(responsePayload({ tracking_url: "/seguimiento/wrong-token" })),
    okResponse(responsePayload({ tracking_url: "https://untrusted.example/seguimiento/synthetic-token-001" })),
  ];

  for (const response of cases) {
    const storage = new MemoryStorage();
    let navigated = false;
    await assert.rejects(confirmation.submit({
      endpoint: "/public/service-requests",
      body: "synthetic-form",
      language: "es",
      storage,
      fetchImpl: async () => response,
      navigate: () => { navigated = true; },
      baseUrl: "https://totalsolutionscancun.com",
    }));
    assert.equal(navigated, false);
    assert.equal(storage.getItem(confirmation.STORAGE_KEY), null);
  }
});

test("timeout aborts the request and remains fail closed", async () => {
  const storage = new MemoryStorage();
  let navigated = false;
  const fetchImpl = (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => {
      const error = new Error("aborted");
      error.name = "AbortError";
      reject(error);
    });
  });
  await assert.rejects(confirmation.submit({
    endpoint: "/public/service-requests",
    body: "synthetic-form",
    language: "en",
    storage,
    fetchImpl,
    navigate: () => { navigated = true; },
    baseUrl: "https://totalsolutionscancun.com",
    timeoutMs: 5,
  }), /tiempo de espera/);
  assert.equal(navigated, false);
  assert.equal(storage.getItem(confirmation.STORAGE_KEY), null);
});

test("direct access requires a valid unexpired confirmation and reload does not submit", () => {
  const storage = new MemoryStorage();
  assert.equal(confirmation.read(storage, 1000, "https://totalsolutionscancun.com"), null);

  const state = confirmation.createState(responsePayload(), "en", 1000, "https://totalsolutionscancun.com");
  storage.setItem(confirmation.STORAGE_KEY, JSON.stringify(state));
  assert.equal(confirmation.read(storage, 1001, "https://totalsolutionscancun.com").confirmation.order_number, "OS-LOCAL-001");
  assert.equal(confirmation.read(storage, 1000 + confirmation.STATE_TTL_MS, "https://totalsolutionscancun.com"), null);
});

test("ES, EN and PT-BR generate only supported language query values", () => {
  assert.equal(confirmation.languageQuery("es"), "es");
  assert.equal(confirmation.languageQuery("en"), "en");
  assert.equal(confirmation.languageQuery("pt-BR"), "pt");
  assert.equal(confirmation.normalizeLanguage("pt"), "pt-BR");
});

test("clearing the state supports creating another request", () => {
  const storage = new MemoryStorage();
  storage.setItem(confirmation.STORAGE_KEY, "synthetic");
  confirmation.clear(storage);
  assert.equal(storage.getItem(confirmation.STORAGE_KEY), null);
});
