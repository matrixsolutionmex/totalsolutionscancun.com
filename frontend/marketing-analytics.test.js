"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const marketing = require("./marketing-analytics.js");

class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(key, String(value)); }
}

class UnavailableStorage {
  getItem() { throw new Error("storage unavailable"); }
  setItem() { throw new Error("storage unavailable"); }
}

test("captures only approved click attribution keys", () => {
  const values = marketing.safeAttribution("?utm_source=google&gclid=synthetic&email=private@example.invalid");
  assert.deepEqual(values, { utm_source: "google", gclid: "synthetic" });
  assert.equal(marketing.attributionDetail(values), "utm_source=google;gclid=synthetic");
});

test("keeps attribution out of lead event parameters", () => {
  const storage = new MemoryStorage();
  const root = { dataLayer: [] };
  const values = marketing.captureAttribution("?utm_campaign=cancun&wbraid=synthetic", storage, "attribution");
  marketing.track(root, "generate_lead", { funnel: "cotizar", method: "quote_form" });
  assert.deepEqual(JSON.parse(storage.getItem("attribution")), values);
  assert.deepEqual(root.dataLayer, [{ event: "generate_lead", funnel: "cotizar", method: "quote_form" }]);
  assert.equal(JSON.stringify(root.dataLayer).includes("cancun"), false);
});

test("tracks one conversion per confirmed request key and permits another request", () => {
  const storage = new MemoryStorage();
  const root = { dataLayer: [] };
  assert.equal(marketing.trackLeadOnce(root, storage, "request-a", "cotizar", "quote_form"), true);
  assert.equal(marketing.trackLeadOnce(root, storage, "request-a", "cotizar", "quote_form"), false);
  assert.equal(marketing.trackLeadOnce(root, storage, "request-b", "cotizar", "quote_form"), true);
  assert.equal(root.dataLayer.length, 2);
});

test("uses an in-memory guard when session storage is unavailable", () => {
  const root = { dataLayer: [] };
  const storage = new UnavailableStorage();
  assert.equal(marketing.trackLeadOnce(root, storage, "request-storage-unavailable", "cotizar", "quote_form"), true);
  assert.equal(marketing.trackLeadOnce(root, storage, "request-storage-unavailable", "cotizar", "quote_form"), false);
  assert.equal(root.dataLayer.length, 1);
});

test("rejects invalid public measurement identifiers", () => {
  assert.deepEqual(marketing.normalizeMarketingConfig({
    ga4_measurement_id: "https://analytics.example",
    gtm_container_id: "GTM bad",
  }), {});
});

test("never configures analytics after direct or internal navigation to a bearer path", () => {
  assert.equal(marketing.shouldConfigureForPath("/solicitar-servico"), true);
  assert.equal(marketing.shouldConfigureForPath("/seguimiento/synthetic-bearer"), false);
  assert.equal(marketing.shouldConfigureForPath("/acompanhar/synthetic-bearer"), false);

  const operational = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
  assert.match(operational, /href="\$\{escapeHtml\(trackingUrl\)\}"/);
  assert.doesNotMatch(operational, /pushState\([^\n]*trackingUrl/);
});

test("tracks only the cotizar funnel while preserving the operational form flow", () => {
  const quote = fs.readFileSync(path.join(__dirname, "cotizar.js"), "utf8");
  const operational = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");

  assert.match(quote, /funnel: "cotizar"/);
  assert.match(quote, /trackLeadOnce/);
  assert.match(operational, /funnel: "solicitar_servico"/);
  assert.match(operational, /onConfirmed/);
  assert.doesNotMatch(operational, /trackLeadOnce/);
});

test("uses the tag callback and resolves one delivery result", async () => {
  const calls = [];
  const root = {
    dataLayer: [],
    gtag(_type, event, params) { calls.push({ event, params }); setTimeout(params.event_callback, 5); },
  };
  const storage = new MemoryStorage();
  const result = await marketing.trackLeadOnceWithDelivery(root, storage, "request-callback", "cotizar", "quote_form", 50);
  assert.equal(result.status, "callback");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].event, "generate_lead");
  assert.equal(calls[0].params.funnel, "cotizar");
  assert.equal(root.dataLayer.length, 0);
});

test("uses the GTM dataLayer callback without declaring queueing as delivery", async () => {
  const dataLayer = [];
  dataLayer.push = item => {
    Array.prototype.push.call(dataLayer, item);
    setTimeout(item.eventCallback, 5);
    return dataLayer.length;
  };
  const root = { dataLayer };
  const storage = new MemoryStorage();
  const result = await marketing.trackLeadOnceWithDelivery(root, storage, "request-gtm", "cotizar", "quote_form", 50);
  assert.equal(result.status, "callback");
  assert.equal(root.dataLayer.length, 1);
  assert.equal(root.dataLayer[0].event, "generate_lead");
  assert.equal(typeof root.dataLayer[0].eventCallback, "function");
  assert.equal(root.dataLayer[0].eventTimeout, 1200);
});

test("bounds a blocked tag without a callback and does not retry", async () => {
  let calls = 0;
  const root = { gtag() { calls += 1; } };
  const storage = new MemoryStorage();
  const result = await marketing.trackLeadOnceWithDelivery(root, storage, "request-timeout", "cotizar", "quote_form", 5);
  assert.equal(result.status, "timeout");
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(calls, 1);
  assert.equal((await marketing.trackLeadOnceWithDelivery(root, storage, "request-timeout", "cotizar", "quote_form", 5)).status, "duplicate");
});

test("does not block a confirmed request when analytics is unavailable", async () => {
  const root = {};
  const storage = new MemoryStorage();
  const result = await marketing.trackLeadOnceWithDelivery(root, storage, "request-unavailable", "cotizar", "quote_form", 5);
  assert.equal(result.status, "timeout");
  assert.equal((await marketing.trackLeadOnceWithDelivery(root, storage, "request-unavailable", "cotizar", "quote_form", 5)).status, "duplicate");
});
