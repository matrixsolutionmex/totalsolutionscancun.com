"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const uploadSource = html.split("function uploadEvidenceWithProgress", 2)[1].split("function renderEvidenceUploadArea", 1)[0];

test("document upload sends session credentials and the current CSRF header", () => {
  assert.match(uploadSource, /xhr\.withCredentials\s*=\s*true/);
  assert.match(uploadSource, /const authenticatedHeaders = authHeaders\(\)/);
  assert.match(uploadSource, /authenticatedHeaders\.forEach\(\(value, name\) => xhr\.setRequestHeader\(name, value\)\)/);
  assert.match(html, /authenticatedHeaders\.set\("X-CSRF-Token", decodeURIComponent\(csrfToken\)\)/);
  assert.match(html, /csrfToken = readCookie\("ts_csrf"\) \|\| csrfToken/);
});

test("document upload still uses the protected lead endpoint", () => {
  assert.match(uploadSource, /xhr\.open\("POST", `\$\{API_BASE\}\/leads\/\$\{leadId\}\/documents`\)/);
  assert.doesNotMatch(uploadSource, /csrf|CSRF/);
});
