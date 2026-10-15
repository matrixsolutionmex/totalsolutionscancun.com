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

test("normalizes runtime measurement identifiers without accepting URLs", () => {
  assert.deepEqual(funnel.normalizeMarketingConfig({
    ga4_measurement_id: "g-ABC123",
    google_ads_conversion_id: "AW-123456",
    google_ads_lead_conversion_label: "Lead_Label-01",
    gtm_container_id: "gtm-ABC123",
  }), {
    ga4_measurement_id: "G-ABC123",
    google_ads_conversion_id: "AW-123456",
    google_ads_lead_conversion_label: "Lead_Label-01",
    gtm_container_id: "GTM-ABC123",
  });
  assert.deepEqual(funnel.normalizeMarketingConfig({
    ga4_measurement_id: "https://analytics.example",
    google_ads_conversion_id: "AW-not-numeric",
    google_ads_lead_conversion_label: "label with spaces",
    gtm_container_id: "https://tag.example",
  }), {});
});

test("adds Google Ads send_to only to the deduplicated lead conversion", () => {
  assert.deepEqual(funnel.eventParams("generate_lead", { funnel: "quote" }, {
    google_ads_conversion_id: "AW-123456",
    google_ads_lead_conversion_label: "Lead_Label-01",
  }), { funnel: "quote", send_to: "AW-123456/Lead_Label-01" });
});
