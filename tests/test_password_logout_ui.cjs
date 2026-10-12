// node tests/test_password_logout_ui.cjs <exported-dashboard.js>
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8').replace(/\r\n/g, '\n');
const start = source.indexOf("authForm.addEventListener('submit', async (ev) => {");
const end = source.indexOf('\nif (delegatedUserReset)', start);
assert(start >= 0 && end > start);

function fixture({password = 'new-password', ok = true, reauth = '1'} = {}) {
  let submit;
  const redirects = [], remembered = [], messages = [];
  let reloads = 0;
  const form = {
    password: {value: password}, password_confirm: {value: password}, current_password: {value: 'old-password'},
    addEventListener(type, fn) { submit = fn; },
  };
  const context = {
    authForm: form, authUsernameDraftDirty: true, authCaptchaDraftDirty: true,
    window: {location: {replace(url) { redirects.push(url); }}},
    FormData: class { constructor() { this.password = form.password.value; } *[Symbol.iterator]() { yield ['password', this.password]; } },
    URLSearchParams,
    setAuthMessage(...args) { messages.push(args); },
    setOwnerPasswordRememberEnabled(...args) { remembered.push(args); },
    ownerPasswordRememberEnabled() { return true; },
    restoreOwnerPasswordInput() {},
    async loadAuthMeta() { reloads++; },
    async fetch(url) {
      assert.equal(url, '/api/auth');
      return {ok, headers: {get() { return reauth; }}, async text() { return ok ? 'Saved' : 'Rejected'; }};
    },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  return {form, redirects, remembered, messages, reloads: () => reloads,
    submit: () => submit({currentTarget: form, preventDefault() {}, stopImmediatePropagation() {}})};
}

(async () => {
  const changed = fixture();
  await changed.submit();
  assert.deepEqual(changed.redirects, ['/login']);
  assert.deepEqual(changed.remembered, [[false]]);
  assert.equal(changed.form.password.value, '');
  assert.equal(changed.form.password_confirm.value, '');
  assert.equal(changed.reloads(), 0, 'do not reload protected APIs after logout');

  const fallback = fixture({reauth: null});
  await fallback.submit();
  assert.deepEqual(fallback.redirects, ['/login'], 'password POST also redirects when a proxy strips the reauth header');

  const rejected = fixture({ok: false});
  await rejected.submit();
  assert.deepEqual(rejected.redirects, []);
  assert.deepEqual(rejected.remembered, []);
  assert.equal(rejected.form.password.value, 'new-password');
  assert.equal(rejected.messages.at(-1)[1], true);

  const settings = fixture({password: '', reauth: null});
  await settings.submit();
  assert.deepEqual(settings.redirects, []);
  assert.equal(settings.reloads(), 1);
  console.log('PASSWORD_LOGOUT_UI_OK: success redirects immediately, rejection and settings preserve the page');
})().catch(error => { console.error(error); process.exitCode = 1; });
