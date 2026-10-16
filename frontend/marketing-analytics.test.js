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

test("tracks only the cotizar funnel while preserving the operational form flow", () => {
  const quote = fs.readFileSync(path.join(__dirname, "cotizar.js"), "utf8");
  const operational = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");

  assert.match(quote, /funnel: "cotizar"/);
  assert.match(quote, /trackLeadOnce/);
  assert.match(operational, /funnel: "solicitar_servico"/);
  assert.match(operational, /onConfirmed/);
  assert.doesNotMatch(operational, /trackLeadOnce/);
});
