"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const frontend = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const backend = fs.readFileSync(path.join(__dirname, "..", "backend", "app", "routes", "lead_document_routes.py"), "utf8");

function extractTypes(source, pattern) {
  return [...source.matchAll(pattern)].map(match => match[1]);
}

test("document picker sends only backend canonical categories", () => {
  const catalogStart = frontend.indexOf("const evidenceCategories = [");
  const catalogEnd = frontend.indexOf("\n    ];", catalogStart);
  assert.notEqual(catalogStart, -1);
  assert.notEqual(catalogEnd, -1);

  const catalog = frontend.slice(catalogStart, catalogEnd);
  const uiTypes = extractTypes(catalog, /type: "([A-Z_]+)"/g);

  const allowedStart = backend.indexOf("ALLOWED_DOCUMENT_TYPES = {");
  const allowedEnd = backend.indexOf("\n}", allowedStart);
  assert.notEqual(allowedStart, -1);
  assert.notEqual(allowedEnd, -1);

  const allowed = backend.slice(allowedStart, allowedEnd);
  const backendTypes = extractTypes(allowed, /"([A-Z_]+)"/g);

  assert.deepEqual(uiTypes, [
    "ANTES_SERVICIO",
    "DURANTE_SERVICIO",
    "DESPUES_SERVICIO",
    "PRESUPUESTO",
    "NOTA_FISCAL",
    "GARANTIA",
    "VIDEO",
    "OTROS",
  ]);
  assert.deepEqual(new Set(uiTypes), new Set(backendTypes));
  assert.match(frontend, /formData\.append\("document_type", item\.documentType\)/);
});
