const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");

function createHarness(fetchImpl) {
  const sourceStart = html.indexOf("    async function register(event) {");
  const sourceEnd = html.indexOf("    async function resendVerificationEmail()", sourceStart);
  assert.notEqual(sourceStart, -1);
  assert.notEqual(sourceEnd, -1);

  const elements = {
    registerButton: { disabled: false },
    registerPassword: { value: "qa-password" },
    registerPasswordConfirm: { value: "qa-password" },
    registerFullName: { value: "QA Security" },
    registerEmail: { value: "qa@example.invalid" },
    registerCompany: { value: "QA" },
    registerPhone: { value: "" },
    registerPlan: { value: "FREE" },
    registerForm: { reset() {} }
  };
  const statuses = [];
  const verificationViews = [];
  const context = {
    API_BASE: "",
    authPublicConfig: { public_signup_enabled: true },
    pendingInviteToken: "",
    pendingInviteData: null,
    registerInFlight: false,
    document: { getElementById: (id) => elements[id] },
    fetch: fetchImpl,
    setAuthStatus: (...args) => statuses.push(args),
    responseMessage: async (_response, fallback) => fallback,
    resetTurnstile() {},
    turnstileToken: () => "",
    showEmailVerificationForm: (payload) => verificationViews.push(payload),
    verificationDeliveryFailed: () => false,
    t: (key) => key
  };

  vm.runInNewContext(
    `${html.slice(sourceStart, sourceEnd)}\nglobalThis.register = register;`,
    context,
    { filename: "frontend/index.html" }
  );
  return { context, elements, statuses, verificationViews };
}

test("bloqueia clique duplo e restaura o botão após erro HTTP", async () => {
  let calls = 0;
  let resolveFetch;
  const pendingFetch = new Promise((resolve) => { resolveFetch = resolve; });
  const harness = createHarness(() => {
    calls += 1;
    return pendingFetch;
  });

  const event = { preventDefault() {} };
  const first = harness.context.register(event);
  const second = harness.context.register(event);

  assert.equal(calls, 1);
  assert.equal(harness.elements.registerButton.disabled, true);

  resolveFetch({ ok: false });
  await Promise.all([first, second]);

  assert.equal(calls, 1);
  assert.equal(harness.elements.registerButton.disabled, false);
  assert.deepEqual(harness.statuses.at(-1), ["registerStatus", "registrationError", "error"]);
});

test("libera a confirmação após resposta de sucesso", async () => {
  const harness = createHarness(async () => ({
    ok: true,
    async json() {
      return { masked_email: "q***@example.invalid", resend_after_seconds: 60 };
    }
  }));

  await harness.context.register({ preventDefault() {} });

  assert.equal(harness.verificationViews.length, 1);
  assert.equal(harness.elements.registerButton.disabled, false);
});
