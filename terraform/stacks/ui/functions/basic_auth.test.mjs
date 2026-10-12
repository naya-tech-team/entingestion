// node --test terraform/stacks/ui/functions/ (make test-scripts). Runs basic_auth.js with a
// stand-in for CloudFront's `cloudfront` module; Node's crypto stands in for CloudFront's.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./basic_auth.js', import.meta.url), 'utf8');
const IMPORT = "import cf from 'cloudfront';";

function load(store) {
  assert.ok(source.includes(IMPORT), 'the function imports the cloudfront module');
  const cf = {
    kvs: () => ({
      get: async (key) => {
        if (!(key in store)) throw new Error(`key ${key} not found`);
        return store[key];
      },
    }),
  };
  const body = `${source.replace(IMPORT, '')}\nreturn handler;`;
  return new Function('cf', 'require', body)(cf, createRequire(import.meta.url));
}

const basic = (user, password) => `Basic ${Buffer.from(`${user}:${password}`).toString('base64')}`;
const sha256 = (value) => createHash('sha256').update(value).digest('hex');
const event = (authorization) => ({
  request: {
    method: 'GET',
    uri: '/index.html',
    headers: authorization === undefined ? {} : { authorization: { value: authorization } },
  },
});

const STORE = { auth: sha256(basic('ops', 'correct horse')) };

test('the right credentials pass the request through unchanged', async () => {
  const handler = load(STORE);
  const e = event(basic('ops', 'correct horse'));
  assert.equal(await handler(e), e.request);
});

for (const [name, authorization] of [
  ['no header', undefined],
  ['empty header', ''],
  ['wrong password', basic('ops', 'wrong')],
  ['wrong user', basic('admin', 'correct horse')],
  ['the hash itself', STORE.auth],
  ['bearer token', 'Bearer abc'],
]) {
  test(`401 with a challenge: ${name}`, async () => {
    const response = await load(STORE)(event(authorization));
    assert.equal(response.statusCode, 401);
    assert.equal(
      response.headers['www-authenticate'].value,
      'Basic realm="entingestion", charset="UTF-8"',
    );
    assert.equal(response.headers['cache-control'].value, 'no-store');
  });
}

test('401 until a password is set', async () => {
  const response = await load({})(event(basic('ops', 'correct horse')));
  assert.equal(response.statusCode, 401);
});

test('an empty stored value never matches', async () => {
  const response = await load({ auth: '' })(event(basic('', '')));
  assert.equal(response.statusCode, 401);
});
