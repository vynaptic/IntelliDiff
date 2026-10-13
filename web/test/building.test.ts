// What the comparison builder and the Targets screen build in the browser (docs/mvp/CONTRACTS.md §7): the starter,
// a form's body, a curl command read into a side's rows with its credential headers set aside by the loader's
// rules (one fixture, which tests/unit/spec/test_rules.py reads too) and DeltaSleuth's own cookies left out, an
// endpoint's body, the credential handed to the target a command reached, and that a credential and a sample key
// stay in memory only.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { parseExact, stringifyExact, type SpecForm, type Target } from '../src/api.ts';
import {
  NOT_JSON, STARTER, blankEndpoint, carried, draftOf, endpointBody, endpointOf, filled, forget, formOf, fromCurl, handing,
  memory, reached, rowsOf, sentLine, type Rows,
} from '../src/building.ts';

const source = (file: string) => readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');
const sent = (form: SpecForm) => JSON.parse(stringifyExact(form)); // what goes over the wire
const rows = (fields: Partial<Rows>): Rows => ({ ...rowsOf(), ...fields });
const refusal = (command: string) => {
  try {
    fromCurl(command);
  } catch (e) {
    return (e as Error).message;
  }
  return undefined;
};

test('a header of a curl command goes to the target, not into the rows, exactly when the loader would refuse it', () => {
  const { cases } = JSON.parse(readFileSync(new URL('credential-headers.json', import.meta.url), 'utf8')) as {
    cases: { header: string; value: string; credential: boolean }[];
  };
  assert.ok(cases.some((c) => c.credential) && cases.some((c) => !c.credential));
  for (const { header, value, credential } of cases) {
    const found = fromCurl(`curl https://svc.example/x -H '${header}: ${value}'`);
    assert.deepEqual(found.credentials, credential ? [[header, value.trim()]] : [], header);
    assert.equal(found.headers, credential ? '' : `${header}: ${value}`, header);
  }
});

test('a curl command copied for bash fills a side: method, path, query, headers and body', () => {
  const found = fromCurl(
    "curl 'https://shop.example:8443/api/v2/orders/A-17/lines?expand=items&expand=prices&q=a%20b' \\\n" +
      "  -H 'accept: application/json' \\\n  -H 'Content-Type: application/json' -H \"X-Note: say \\\"hi\\\" \\$5\" \\\n" +
      "  -H 'User-Agent: Mozilla/5.0' -H 'sec-ch-ua: \"x\"' -H 'Origin: https://shop.example' -H 'Referer: https://shop.example/' \\\n" +
      "  -H 'Host: shop.example' -H 'Accept-Encoding: gzip' -H 'Connection: keep-alive' \\\n" +
      "  -b 'sid=abc; theme=dark' -H 'X-Auth-Token: t0k' \\\n  --data-raw '{\"id\": 12345678901234567890, \"rate\": 1.10}' --compressed -s -L",
  );
  assert.deepEqual(found, {
    origin: 'https://shop.example:8443',
    under: '', // no base path of a target was taken off
    method: 'POST', // a body without -X
    path: '/api/v2/orders/A-17/lines',
    query: 'expand=items\nexpand=prices\nq=a b',
    headers: 'accept: application/json\nX-Note: say "hi" $5', // what the client writes itself is dropped
    body: '{"id": 12345678901234567890, "rate": 1.10}', // as typed: its numbers stay exact
    credentials: [['Cookie', 'sid=abc; theme=dark'], ['X-Auth-Token', 't0k']], // every credential header, -b as Cookie
  });
  assert.equal(carried(found), 'The command carried a credential (Cookie, X-Auth-Token). It belongs to the target, not to a spec.');
  // Other spellings: no scheme, unquoted words, -X joined to its value, long options, a body by --json.
  const plain = fromCurl('curl -XGET localhost:9001/v1/things/7 --header Accept:text/plain --silent --location');
  assert.deepEqual([plain.origin, plain.method, plain.path, plain.headers], ['http://localhost:9001', 'GET', '/v1/things/7', 'Accept: text/plain']);
  assert.equal(fromCurl('curl --request POST --url http://h.example/q --json \'{"a": 1}\' --cookie a=b').credentials[0][0], 'Cookie');
  assert.equal(fromCurl('curl http://h.example/a\\ b').path, '/a%20b'); // a backslash keeps the space in the word
});

test('the path is the part under the base path of the target, where the caller may read it', () => {
  const command = "curl 'http://localhost:9001/shop/api/v1/things/7?x=1'";
  const read = (cmd: string, base?: string) => [fromCurl(cmd, base).path, fromCurl(cmd, base).under]; // the path, and what was taken off
  assert.deepEqual(read(command), ['/shop/api/v1/things/7', '']);
  assert.deepEqual(read(command, 'http://host.docker.internal:9001/shop/api/'), ['/v1/things/7', '/shop/api']); // whatever the host
  assert.deepEqual(read(command, 'http://localhost:9001'), ['/shop/api/v1/things/7', '']);
  assert.deepEqual(read(command, 'http://localhost:9001/sh'), ['/shop/api/v1/things/7', '']); // whole segments only
  assert.deepEqual(read("curl 'http://localhost:9001/shop/api'", 'http://localhost:9001/shop/api'), ['/', '/shop/api']);
  assert.deepEqual(read(command, 'no address'), ['/shop/api/v1/things/7', '']);
});

test("DeltaSleuth's own session and sign-in cookies are left out of a command, by name", () => {
  // A browser keeps cookies by host, not by port: a request to an application on localhost, copied from the browser
  // a trial is open in, carries the trial's session cookie beside the application's own, or alone.
  const read = (option: string) => fromCurl(`curl 'http://localhost:3000/api/items/K1' -H 'Accept: application/json' ${option}`);
  const beside = read("-b 'ds_session_8000=S3CRET; sid=abc; theme=dark'");
  assert.deepEqual(beside.credentials, [['Cookie', 'sid=abc; theme=dark']]);
  assert.equal(handing(beside).secret, 'sid=abc; theme=dark');
  for (const own of ['ds_session_8000=S3CRET', 'ds_session=S3CRET', '__Host-ds_session=S3CRET', 'ds_login_18700=x.y.z', '__Host-ds_login=x.y.z']) {
    for (const option of [`-b '${own}'`, `--cookie '${own}; ds_login_8000=x'`, `-H 'Cookie: ${own}'`, `-H 'cookie:  ${own} ;'`]) {
      const alone = read(option); // no pair is left: the command carried no credential, and the rows get no Cookie
      assert.deepEqual([alone.credentials, alone.headers], [[], 'Accept: application/json'], option);
    }
    assert.deepEqual(read(`-H 'COOKIE: a=1;${own};b=2'`).credentials, [['COOKIE', 'a=1; b=2']], own);
    assert.deepEqual(read(`-b 'a=1; ${own.replace('=', ' =')}'`).credentials, [['Cookie', 'a=1']], own); // as a server reads the name
  }
  // Another cookie's name, and DeltaSleuth's own under another header, are the service's.
  const others = 'ds_session_x=1; my_ds_session=2; ds_sessions=3; ds_session_8000x=4; DS_SESSION=5';
  assert.deepEqual(read(`-b '${others}'`).credentials, [['Cookie', others]]);
  assert.deepEqual(read("-H 'X-Auth-Token: ds_session_8000=S3CRET'").credentials, [['X-Auth-Token', 'ds_session_8000=S3CRET']]);
});

test('what a browser adds for its own cache is dropped: with it a service answers 304, without a body', () => {
  const found = fromCurl(
    "curl 'http://localhost:3000/etag/items/K1' -H 'Accept: application/json' -H 'If-None-Match: W/\"1a2-abc\"' " +
      "-H 'If-Modified-Since: Wed, 07 Oct 2026 10:00:00 GMT' -H 'if-match: \"x\"' -H 'If-Unmodified-Since: Wed, 07 Oct 2026 10:00:00 GMT' " +
      "-H 'IF-RANGE: \"x\"' -H 'Cache-Control: no-cache' -H 'If-Different: kept'",
  );
  assert.equal(found.headers, 'Accept: application/json\nCache-Control: no-cache\nIf-Different: kept');
  assert.deepEqual(found.credentials, []);
});

test('what the import does not read is refused in words that say what to change', () => {
  assert.equal(refusal("curl -k 'https://h.example/x'"), 'DeltaSleuth always verifies TLS: remove -k.');
  assert.equal(refusal("curl --insecure 'https://h.example/x'"), 'DeltaSleuth always verifies TLS: remove -k.');
  assert.equal(refusal("curl 'https://h.example/x' -u me:pw"), 'Import does not read -u: remove it.');
  assert.equal(refusal("curl 'https://h.example/x' --data-urlencode a=b"), 'Import does not read --data-urlencode: remove it.');
  assert.equal(refusal("curl 'https://h.example/x' -d @body.json"), 'Import does not read -d @file: remove it.');
  assert.equal(refusal("curl 'https://h.example/x' 'https://h.example/y'"), 'Import does not read a second URL: remove it.');
  // What the shell reads, not curl: the command has one URL, and the sentence names what to remove.
  const alone = (operator: string) => `Paste the curl command alone: remove ${operator} and what follows it.`;
  assert.equal(refusal("curl -s 'http://localhost:9001/x/1' | jq ."), alone('|'));
  assert.equal(refusal("curl 'http://localhost:9001/x/1'|jq ."), alone('|'));
  assert.equal(refusal("curl 'http://localhost:9001/x/1' > out.json"), alone('>'));
  assert.equal(refusal("curl 'http://localhost:9001/x/1' 2>/dev/null"), alone('>'));
  assert.equal(refusal("curl -d @- 'http://localhost:9001/x/1' < body.json"), alone('<'));
  assert.equal(refusal("curl 'http://localhost:9001/x/1'; echo done"), alone(';'));
  assert.equal(refusal("curl 'http://localhost:9001/x/1' && echo done"), alone('&&'));
  const quoted = fromCurl(`curl 'http://localhost:9001/x/1?a=1|2&&b=<3>' -H "X-Note: a;b > c" -H X-Other:\\;`); // quoted, they are text
  assert.deepEqual([quoted.query, quoted.headers], ['a=1|2\nb=<3>', 'X-Note: a;b > c\nX-Other: ;']);
  assert.equal(fromCurl('curl http://localhost:9001/x/1?a=1&b=2').query, 'a=1\nb=2'); // one & of a URL left unquoted
  // curl reads a -b value without = as the name of a file: no cookie, and never a credential for the target.
  for (const file of ['-b cookies.txt', '--cookie cookies.txt', '-bcookies.txt', "-b ''"]) {
    assert.equal(refusal(`curl 'http://localhost:9001/x/1' ${file}`), 'Import does not read a cookie file: remove it.', file);
  }
  assert.deepEqual(fromCurl("curl 'http://localhost:9001/x/1' -H 'Cookie: cookies.txt'").credentials, [['Cookie', 'cookies.txt']]); // a header says what it sends
  assert.equal(refusal("curl 'https://me:pw@h.example/x'"), 'Import does not read a user name in the URL: remove it.');
  const bash = "Copy the command for bash: $'…' and ^ quoting are not read.";
  assert.equal(refusal("curl 'https://h.example/x' -H $'X-A: b\\'c'"), bash);
  assert.equal(refusal('curl ^"https://h.example/x^" ^\n  -H ^"accept: */*^"'), bash); // copied for cmd
  assert.equal(refusal("curl 'https://h.example/x"), bash); // a quote left open
  assert.equal(refusal('Invoke-WebRequest -Uri "https://h.example/x"'), bash);
  assert.equal(refusal(''), bash);
  const only = 'Only GET, and POST with a JSON body, are sent.';
  assert.equal(refusal("curl -X PUT 'https://h.example/x' -d '{}'"), only);
  assert.equal(refusal("curl -X DELETE 'https://h.example/x'"), only);
  assert.equal(refusal("curl 'https://h.example/x' -d 'a=1' -d 'b=2'"), only); // a form, not JSON
  assert.equal(refusal("curl -X GET 'https://h.example/x' -d '{}'"), only);
  assert.equal(refusal('curl -H "Accept: */*"'), 'The command has no URL that can be read.');
  assert.equal(refusal("curl 'ftp://h.example/x'"), 'The command has no URL that can be read.');
  assert.equal(refusal("curl -X POST 'https://h.example/x'"), undefined); // a POST without a body is one
});

test('a new comparison starts from the smallest spec, without the sides\' target and request, under a scratch name', () => {
  assert.deepEqual(STARTER.sides, { a: {}, b: {} });
  assert.match(STARTER.text, /^metadata: \{name: scratch-comparison, /m);
  assert.ok(STARTER.text.includes("pattern: '^[^\\s/]{1,200}$'"));
  assert.doesNotMatch(STARTER.text, /^\s*(target|request):/m);
  const d = draftOf(STARTER);
  assert.deepEqual([d.key, d.text, d.a, d.b], ['', STARTER.text, rowsOf(), rowsOf()]);
  assert.deepEqual(sent(formOf(d)), { text: STARTER.text, sides: {} }); // no target yet: no side is sent
});

test('a form sends a side only when its rows name a target, and a stored request as it was until a row is edited', () => {
  const stored = parseExact('{"method":"POST","path":"/q","query":{"limit":50,"tag":["a","b"],"exact":1.10},"headers":{"Accept":"application/json"},"body":{"id":"{{ key }}","n":12345678901234567890}}');
  const opened = draftOf({ text: 'kind: ComparisonSpec\n', sides: { a: { target: 'svc-a', request: stored }, b: { target: 'svc-b' } } });
  assert.deepEqual(
    { ...opened.a, stored: undefined },
    { target: 'svc-a', method: 'POST', path: '/q', query: 'limit=50\ntag=a\ntag=b\nexact=1.10', headers: 'Accept: application/json',
      body: '{\n  "id": "{{ key }}",\n  "n": 12345678901234567890\n}', stored: undefined },
  );
  // Untouched, it goes back as stored (a number stays a number, exactly); a target without rows has no request.
  assert.equal(
    stringifyExact(formOf(opened)),
    `{"text":"kind: ComparisonSpec\\n","sides":{"a":{"target":"svc-a","request":${stringifyExact(stored)}},"b":{"target":"svc-b"}}}`,
  );
  // Another target is no edit of the request; an edited row builds the request from the rows.
  assert.equal(sent(formOf({ ...opened, a: { ...opened.a!, target: 'svc-c' } })).sides.a.request.query.limit, 50);
  const edited = formOf({ ...opened, a: { ...opened.a!, path: '/q/{{ key }}', stored: undefined } });
  assert.equal(
    stringifyExact(edited.sides.a),
    '{"target":"svc-a","request":{"method":"POST","path":"/q/{{ key }}","query":{"limit":"50","tag":["a","b"],"exact":"1.10"},' +
      '"headers":{"Accept":"application/json"},"body":{"id":"{{ key }}","n":12345678901234567890}}}',
  );
  // Typed rows: empty ones are left out, a GET has no body, and a table side (null) has no form.
  const typed = formOf({ text: 't', a: rows({ target: 'svc-a', path: ' /v1/things/{{ key }} ', body: '{"left": "over"}' }), b: null });
  assert.deepEqual(sent(typed), { text: 't', sides: { a: { target: 'svc-a', request: { method: 'GET', path: '/v1/things/{{ key }}' } } } });
  assert.deepEqual(sent(formOf({ text: 't', a: rows({ path: '/typed/without/a/target' }), b: null })), { text: 't', sides: {} });
  assert.deepEqual(
    sent(formOf({ text: 't', a: null, b: rows({ target: 'svc-b', method: 'POST', path: '/q', headers: 'X-A:  b: c \n\nAccept:*/*', query: 'flag' }) })).sides.b.request,
    { method: 'POST', path: '/q', query: { flag: '' }, headers: { 'X-A': 'b: c', Accept: '*/*' } },
  );
  assert.throws(() => formOf({ text: 't', a: rows({ target: 'svc-a', method: 'POST', path: '/q', body: '{not json' }), b: null }), { message: NOT_JSON });
});

test('text pasted whole fills the rows it left empty, and a side the answer leaves out is a table side', () => {
  const mine = rows({ target: 'svc-a', path: '/mine' });
  const d = { key: 'K1', text: 'pasted', a: mine, b: rowsOf() };
  const answer: SpecForm = { text: 'rendered', sides: { a: { target: 'other', request: { method: 'GET', path: '/theirs' } }, b: { target: 'svc-b', request: { method: 'GET', path: '/b' } } } };
  const got = filled(d, answer);
  assert.deepEqual([got.key, got.text, got.a, got.b?.target, got.b?.path], ['K1', 'pasted', mine, 'svc-b', '/b']); // the text stays as typed
  assert.equal(filled({ ...d, b: rowsOf() }, { text: '', sides: { a: {} } }).b, null); // side b holds a table
  assert.equal(filled({ ...d, b: null }, answer).b?.path, '/b'); // and is a service side again
});

test('a Send says its status, time, attempts and size, and nothing of the address', () => {
  assert.equal(sentLine({ status: 200, ms: 143.2, attempts: 1, pages: 1, bytes: 12345 }), 'HTTP 200, 143 ms, 1 attempt, 12,345 bytes');
  assert.equal(sentLine({ status: 200, ms: 20, attempts: 1, pages: 3, bytes: 900 }), 'HTTP 200, 20 ms, 1 attempt, 3 pages, 900 bytes');
  assert.equal(sentLine({ status: null, ms: null, attempts: 0, pages: 1, bytes: null }), 'No answer, 0 attempts');
  const exact = parseExact('{"status":404,"ms":7.0,"attempts":1,"pages":1,"bytes":2}') as Parameters<typeof sentLine>[0];
  assert.equal(sentLine(exact), 'HTTP 404, 7 ms, 1 attempt, 2 bytes'); // a number kept as its text
});

test('an endpoint sends no credential left empty, each further header by its name, and no budget left empty', () => {
  const body = (fields: Partial<ReturnType<typeof blankEndpoint>>) => JSON.parse(JSON.stringify(endpointBody({ ...blankEndpoint(), ...fields })));
  assert.deepEqual(body({ base_url: ' http://localhost:9001 ' }), {
    base_url: 'http://localhost:9001', allow_private_network: false, allow_insecure_http: false, auth: { kind: 'none' }, absent_on_404: false,
  });
  assert.deepEqual(
    body({ base_url: 'http://h', private_network: true, insecure_http: true, absent_on_404: true, kind: 'header', header: ' Cookie ', secret: 'sid=1',
      more: [{ header: ' X-CSRF-Token ', secret: 'abc' }, { header: 'X-Kept', secret: '' }], rate_per_s: '2.5', max_concurrency: '1' }),
    { base_url: 'http://h', allow_private_network: true, allow_insecure_http: true, absent_on_404: true, secret: 'sid=1',
      auth: { kind: 'header', header: 'Cookie', more: [{ header: 'X-CSRF-Token' }, { header: 'X-Kept' }] },
      more_secrets: { 'X-CSRF-Token': 'abc' }, rate_per_s: 2.5, max_concurrency: 1 },
  );
  // A bearer has no header and no further ones; auth none sends no credential, whatever was typed before.
  assert.deepEqual(body({ base_url: 'h', kind: 'bearer', header: 'Cookie', secret: 't', more: [{ header: 'X', secret: 'y' }] }).auth, { kind: 'bearer' });
  const none = body({ base_url: 'h', kind: 'none', secret: 'typed', more: [{ header: 'X', secret: 'y' }] });
  assert.deepEqual([none.auth, none.secret, none.more_secrets], [{ kind: 'none' }, undefined, undefined]);
  // Changing a target starts from what it was declared with, every credential empty: left so, each is kept.
  const listed: Target = {
    target_id: 'orders-legacy', engine: null, declared_in: 'product', base_url: 'http://h/api', allow_private_network: true,
    allow_insecure_http: true, auth: { kind: 'header', header: 'Cookie', more: ['X-CSRF-Token'] }, has_secret: true, rate_per_s: 5,
    max_concurrency: 1, absent_on_404: true,
  };
  assert.deepEqual(body(endpointOf(listed)), {
    base_url: 'http://h/api', allow_private_network: true, allow_insecure_http: true, absent_on_404: true,
    auth: { kind: 'header', header: 'Cookie', more: [{ header: 'X-CSRF-Token' }] }, rate_per_s: 5, max_concurrency: 1,
  });
  assert.equal(endpointOf(listed).id, 'orders-legacy');
});

test('the import hands the Targets form the address and every credential header, a lone bearer as a token', () => {
  const both = handing(fromCurl("curl 'http://localhost:9001/api/x/1' -b 'sid=abc' -H 'X-Auth-Token: t0k' -H 'Authorization: Bearer abc.def'"));
  assert.deepEqual(both, {
    ...blankEndpoint(), base_url: 'http://localhost:9001', kind: 'header', header: 'Cookie', secret: 'sid=abc',
    more: [{ header: 'X-Auth-Token', secret: 't0k' }, { header: 'Authorization', secret: 'Bearer abc.def' }],
  });
  assert.equal(both.private_network || both.insecure_http, false); // the consents are the person's to give
  const bearer = handing(fromCurl("curl 'https://api.example/v1/x/1' -H 'authorization: Bearer abc.def'"));
  assert.deepEqual([bearer.base_url, bearer.kind, bearer.header, bearer.secret, bearer.more], ['https://api.example', 'bearer', '', 'abc.def', []]);
  assert.equal(handing(fromCurl("curl 'https://api.example/x' -H 'Authorization: Basic dTpw'")).kind, 'header');
});

test('the credential a command carried goes to the target the side names, which keeps its address, consents and budget', () => {
  // An endpoint declared first, with a base path, no credential yet, its consents and a budget of its own.
  const legacy: Target = {
    target_id: 'legacy', engine: null, declared_in: 'product', base_url: 'http://localhost:9001/api', allow_private_network: true,
    allow_insecure_http: true, auth: { kind: 'none', header: null, more: [] }, has_secret: false, rate_per_s: 2, max_concurrency: 1, absent_on_404: true,
  };
  const command = "curl 'http://localhost:9001/api/secure/records/K1' -b 'sid=abc' -H 'X-Auth-Token: t0k'";
  const found = fromCurl(command, legacy.base_url);
  assert.deepEqual([found.path, found.under], ['/secure/records/K1', '/api']);
  // The form is `Change legacy`: only how it authenticates is laid over what it was declared with.
  assert.equal(reached(found.origin, legacy), legacy);
  const change = handing(found, legacy);
  assert.deepEqual(change, { ...endpointOf(legacy), kind: 'header', header: 'Cookie', secret: 'sid=abc', more: [{ header: 'X-Auth-Token', secret: 't0k' }] });
  assert.deepEqual(JSON.parse(JSON.stringify(endpointBody(change))), {
    base_url: 'http://localhost:9001/api', allow_private_network: true, allow_insecure_http: true, absent_on_404: true,
    auth: { kind: 'header', header: 'Cookie', more: [{ header: 'X-Auth-Token' }] }, secret: 'sid=abc', more_secrets: { 'X-Auth-Token': 't0k' },
    rate_per_s: 2, max_concurrency: 1,
  });
  assert.deepEqual(handing({ ...found, credentials: [['Authorization', 'Bearer abc.def']] }, { ...legacy, auth: { kind: 'header', header: 'Cookie', more: ['X-Old'] } }),
    { ...endpointOf(legacy), kind: 'bearer', header: '', secret: 'abc.def', more: [] }); // the command's headers, none of the old ones
  // Reached: the same scheme and port, and the same host or two names of this machine (a container's name for it).
  const at = (base_url?: string, declared_in: Target['declared_in'] = 'product') => !!reached('http://localhost:9001', { ...legacy, base_url, declared_in });
  assert.deepEqual(
    ['http://localhost:9001', 'http://host.docker.internal:9001/api', 'http://127.0.0.1:9001/x', 'http://[::1]:9001', 'http://app.localhost:9001'].map((base) => at(base)),
    [true, true, true, true, true],
  );
  assert.deepEqual(
    ['http://localhost:9002/api', 'https://localhost:9001', 'http://localhost', 'http://shop.example:9001', 'http://10.1.2.3:9001', 'no address', undefined].map((base) => at(base)),
    [false, false, false, false, false, false, false],
  );
  assert.equal(at(legacy.base_url, 'file'), false); // a target of the targets file is changed there, and shows no address
  assert.equal(reached('http://localhost:9001'), undefined); // the side names no target
  assert.equal(!!reached('https://shop.example', { ...legacy, base_url: 'https://shop.example:443/api' }), true);
  assert.equal(!!reached('https://shop.example', { ...legacy, base_url: 'https://host.docker.internal/api' }), false); // no name of this machine
  // No target reached: a new endpoint at the address the rows' path is under, so the same request is sent.
  const fresh = handing(found);
  assert.deepEqual([fresh.id, fresh.base_url, fresh.private_network, fresh.rate_per_s], ['', 'http://localhost:9001/api', false, '']);
  assert.equal(handing(fromCurl(command)).base_url, 'http://localhost:9001'); // nothing was taken off the path
  // The Targets screen lays it over the side's target at once, and over a row whose `Change` is pressed when the
  // command reached that one; before another row's form drops the credential, the person is asked.
  const targets = source('Targets.tsx');
  assert.match(targets, /useState\(\(\) => handed && reached\(handed\.origin, memory\.handed\?\.target\)\);/);
  assert.match(targets, /useState<Endpoint>\(\(\) => \(handed \? handing\(handed, stored\) : blankEndpoint\(\)\)\);/);
  assert.match(targets, /const mine = handed && reached\(handed\.origin, t\);\n.*\n\s+if \(dropped && !confirm\(.*\)\) return;\n\s+setE\(mine \? handing\(handed, mine\) : /);
});

test('a credential and a sample key stay in memory only, and go when the person signed in changes', () => {
  memory.drafts.set('new', draftOf(STARTER));
  memory.handed = { found: fromCurl("curl 'http://localhost:9001/x' -b 'sid=abc'"), back: '#/specs/new' };
  forget();
  assert.deepEqual([memory.drafts.size, memory.handed], [0, undefined]);
  assert.equal(source('main.tsx').match(/forget\(\);/g)?.length, 2); // at a sign-in and at a sign-out
  for (const file of ['Builder.tsx', 'Targets.tsx', 'building.ts']) {
    const text = source(file);
    for (const banned of ['Storage', 'document.cookie', 'location.search', '<form', 'onSubmit', 'console.']) assert.ok(!text.includes(banned), `${file}: ${banned}`);
  }
  // Every credential box is masked and kept from the browser's own stores; the sample key is no autofill entry.
  const targets = source('Targets.tsx');
  assert.equal(targets.match(/<input type="password" autoComplete="off"/g)?.length, 1);
  assert.equal(targets.match(/\{credential\(/g)?.length, 2); // the first credential and each further header's
  assert.match(source('Builder.tsx'), /Sample key\{' '\}\s*<input value=\{d\.key\}.*autoComplete="off"/);
  // The builder leaves for Targets with the endpoint in memory, and the form there reads it once.
  assert.match(source('Builder.tsx'), /memory\.handed = \{ found: found!, target: items\.find\(\(t\) => t\.target_id === rows\.target\), back: location\.hash \};/);
  assert.match(targets, /memory\.handed = undefined;/);
});
