"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const funnel = require("./cotizar.js");

test("keeps only approved attribution parameters", () => {
  const values = funnel.safeAttribution("?utm_source=google&utm_campaign=cancun&gclid=synthetic-click&email=private@example.invalid");
  assert.deepEqual(values, {
    utm_source: "google",
    utm_campaign: "cancun",
    gclid: "synthetic-click",
  });
  assert.equal(funnel.attributionDetail(values), "utm_source=google;utm_campaign=cancun;gclid=synthetic-click");
});

test("maps Google click identifiers to the CRM Google Ads origin", () => {
  assert.equal(funnel.originFor({ gclid: "synthetic-click" }), "GOOGLE_ADS");
  assert.equal(funnel.originFor({ utm_source: "google" }), "GOOGLE_ADS");
  assert.equal(funnel.originFor({ utm_source: "website" }), "LANDING_PAGE");
});

test("normalizes GA4 and GTM identifiers without accepting URLs", () => {
  assert.deepEqual(funnel.normalizeMarketingConfig({
    ga4_measurement_id: "g-ABC123",
    google_ads_conversion_id: "AW-123456",
    google_ads_lead_conversion_label: "Lead_Label-01",
    gtm_container_id: "gtm-ABC123",
  }), {
    ga4_measurement_id: "G-ABC123",
    gtm_container_id: "GTM-ABC123",
  });
  assert.deepEqual(funnel.normalizeMarketingConfig({
    ga4_measurement_id: "https://analytics.example",
    google_ads_conversion_id: "AW-not-numeric",
    google_ads_lead_conversion_label: "label with spaces",
    gtm_container_id: "https://tag.example",
  }), {});
});

test("keeps lead conversion events on the GA4 import path", () => {
  assert.deepEqual(funnel.eventParams("generate_lead", { funnel: "quote" }, {
    google_ads_conversion_id: "AW-123456",
    google_ads_lead_conversion_label: "Lead_Label-01",
  }), { funnel: "quote" });
});

test("quote success requires and persists the complete confirmation before navigation", () => {
  const writes = [];
  const confirmation = {
    createState: (data, language, now, baseUrl, funnel) => ({ data, language, now, baseUrl, funnel }),
    write: (_storage, state) => writes.push(state),
  };
  const payload = {
    order_number: "TS-2026-000001",
    tracking_token: "synthetic-token",
    tracking_url: "/seguimiento/synthetic-token",
    status: "Solicitud recibida",
    pricing: { currency: "MXN" },
  };
  const state = funnel.saveConfirmationState(confirmation, {}, payload, "https://totalsolutionscancun.com", 1000);
  assert.equal(state.funnel, "quote");
  assert.deepEqual(writes, [state]);
});

test("quote success does not proceed when confirmation state cannot be saved", () => {
  assert.throws(
    () => funnel.saveConfirmationState(null, {}, { order_number: "synthetic" }, "https://totalsolutionscancun.com", 1000),
    error => error.code === "confirmation_unavailable",
  );
});

test("confirmation preparation failure is explicit and does not invite a resend", () => {
  const message = funnel.confirmationFailureText({ code: "confirmation_unavailable" });
  assert.match(message, /solicitud fue registrada/);
  assert.match(message, /No la envíes nuevamente/);
});

test("accepts JSON success responses across the full 2xx range", async () => {
  for (const status of [200, 201, 204, 299]) {
    const payload = await funnel.readSuccessfulPayload({
      status,
      ok: true,
      text: async () => status === 204 ? "" : JSON.stringify({ accepted: true }),
    });
    assert.deepEqual(payload, status === 204 ? null : { accepted: true });
  }
});

test("accepts a successful empty response without requiring JSON", async () => {
  assert.equal(await funnel.readSuccessfulPayload({ status: 201, ok: true, text: async () => "" }), null);
});

test("rejects non-2xx responses before parsing or tracking success", async () => {
  await assert.rejects(
    funnel.readSuccessfulPayload({ status: 422, ok: false, text: async () => JSON.stringify({ detail: "invalid" }) }),
    /request_failed/,
  );
  assert.equal(funnel.isSuccessfulResponse({ status: 503, ok: false }), false);
});

test("does not convert a network failure into a successful response", async () => {
  await assert.rejects(
    funnel.requestSuccessfulPayload(async () => { throw new Error("network"); }, "/public/service-requests", {}),
    /network/,
  );
});

test("locks the submit control while the first request is pending", () => {
  const submit = { disabled: false };
  assert.equal(funnel.canStartSubmission(submit), true);
  submit.disabled = true;
  assert.equal(funnel.canStartSubmission(submit), false);
});

test("waits for analytics initialization with a bounded timeout", async () => {
  const never = new Promise(() => {});
  const started = Date.now();
  assert.equal(await funnel.waitForMarketingReady(never, 5), "timeout");
  assert.ok(Date.now() - started < 500);
  assert.equal(await funnel.waitForMarketingReady(Promise.reject(new Error("blocked")), 50), "error");
  assert.equal(await funnel.waitForMarketingReady(Promise.resolve(), 50), "ready");
});

test("navigates only once when analytics completion races its timeout", () => {
  const sequence = [];
  const state = { started: false };
  const navigate = url => sequence.push(url);
  assert.equal(funnel.navigateOnce(state, navigate, "/solicitud-enviada?lang=es"), true);
  assert.equal(funnel.navigateOnce(state, navigate, "/solicitud-enviada?lang=es"), false);
  assert.deepEqual(sequence, ["/solicitud-enviada?lang=es"]);
});
