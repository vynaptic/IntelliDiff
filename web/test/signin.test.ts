// What the sign-in page says beyond the server's answers.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { NOT_KEPT, TOKEN_HINT, addressHint } from '../src/signin.ts';

const REFUSED = 'cross-origin request refused';
const SAID = 'DeltaSleuth takes a sign-in from one address only, the one in its settings (auth.public_url).';
const at = (url: string) => new URL(url); // what window.location gives: protocol, hostname, port, origin

test('the token hint and the dropped-cookie message say what to do', () => {
  assert.equal(
    TOKEN_HINT,
    'An API token comes from the command line of the machine that runs DeltaSleuth: whoever administers it runs "ds token create" there and passes the token on. It is shown once. This page cannot create one.',
  );
  assert.equal(
    NOT_KEPT,
    'The token was accepted, but this browser did not send the session cookie back, so you are not signed in. Allow cookies for this address, or use another browser.',
  );
});

test('a refused origin on a loopback page names the other loopback addresses, with its scheme and port', () => {
  assert.equal(
    addressHint(REFUSED, at('http://127.0.0.1:8000/#/signin')),
    `${SAID} This page was opened at http://127.0.0.1:8000. On the machine that runs it, try http://localhost:8000 instead; if that is refused too, the settings name another address.`,
  );
  assert.equal(
    addressHint(REFUSED, at('http://localhost:18000/')),
    `${SAID} This page was opened at http://localhost:18000. On the machine that runs it, try http://127.0.0.1:18000 instead; if that is refused too, the settings name another address.`,
  );
  assert.equal(
    addressHint(REFUSED, at('http://[::1]:8000/')),
    `${SAID} This page was opened at http://[::1]:8000. On the machine that runs it, try http://localhost:8000 or http://127.0.0.1:8000 instead; if that is refused too, the settings name another address.`,
  );
  assert.equal(
    addressHint(REFUSED, at('https://localhost/#/signin')), // the scheme's own port: none is written
    `${SAID} This page was opened at https://localhost. On the machine that runs it, try https://127.0.0.1 instead; if that is refused too, the settings name another address.`,
  );
});

test('elsewhere it says whom to ask, and names no address of its own making', () => {
  assert.equal(
    addressHint(REFUSED, at('https://ds.bank.example/#/signin?error=x')),
    `${SAID} This page was opened at https://ds.bank.example. Ask whoever runs it for the address.`,
  );
  assert.equal(
    addressHint(REFUSED, at('http://10.1.2.3:8000/')),
    `${SAID} This page was opened at http://10.1.2.3:8000. Ask whoever runs it for the address.`,
  );
});

test('no other refusal gets the hint', () => {
  for (const detail of [undefined, '', 'cross-site request refused', 'invalid or expired API token', 'unexpected Host header', NOT_KEPT])
    assert.equal(addressHint(detail, at('http://127.0.0.1:8000/')), undefined, detail);
});

test('the module is pure: nothing in it comes from the server', () => {
  const text = readFileSync(new URL('../src/signin.ts', import.meta.url), 'utf8');
  for (const banned of ['import', 'fetch', 'XMLHttpRequest', 'document.']) assert.ok(!text.includes(banned), banned);
});
