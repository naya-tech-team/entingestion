// HTTP basic auth for the upload UI (CloudFront Functions, cloudfront-js-2.0, viewer request).
//
// The associated KeyValueStore holds one key, "auth": the lowercase hex SHA-256 of the whole
// Authorization header value, "Basic " + base64("user:password"). `make ui-password` sets it, so
// no password is in this code, in Terraform or in git. A missing key or any other header is a 401.
import cf from 'cloudfront';

const crypto = require('crypto');

const kvs = cf.kvs();

function unauthorized() {
  return {
    statusCode: 401,
    statusDescription: 'Unauthorized',
    headers: {
      'www-authenticate': { value: 'Basic realm="entingestion", charset="UTF-8"' },
      'cache-control': { value: 'no-store' },
    },
  };
}

async function handler(event) {
  const request = event.request;
  const header = request.headers.authorization;
  if (!header || !header.value) {
    return unauthorized();
  }
  let expected;
  try {
    expected = await kvs.get('auth');
  } catch (err) {
    return unauthorized(); // no password set yet
  }
  const actual = crypto.createHash('sha256').update(header.value).digest('hex');
  if (!expected || actual !== expected) {
    return unauthorized();
  }
  return request;
}
