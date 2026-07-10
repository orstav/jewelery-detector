import {Readable} from 'node:stream';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import handler from './api/identity-session.js';

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'stav-identity-api-'));
process.env.STAV_IDENTITY_SESSION_DIR = tmp;
delete process.env.KV_REST_API_URL;
delete process.env.KV_REST_API_TOKEN;
delete process.env.VERCEL_KV_REST_API_URL;
delete process.env.VERCEL_KV_REST_API_TOKEN;

function req(method, url, body, headers = {}) {
  const raw = body == null ? '' : (typeof body === 'string' ? body : JSON.stringify(body));
  const stream = Readable.from(raw ? [raw] : []);
  stream.method = method;
  stream.url = url;
  stream.headers = {'content-type': 'application/json', 'content-length': Buffer.byteLength(raw), ...headers};
  return stream;
}

function res() {
  return {
    statusCode: 200,
    headers: {},
    body: '',
    setHeader(key, value) { this.headers[key.toLowerCase()] = value; },
    end(chunk = '') { this.body += chunk; },
  };
}

async function call(method, url, body, headers = {}) {
  const response = res();
  await handler(req(method, url, body, headers), response);
  return {status: response.statusCode, body: JSON.parse(response.body || '{}')};
}

const state = {started: true, tasks: [{id: 't1', photos: []}], decisions: [], buckets: [], datasetVersion: 'test', sourceAssets: [], no_live_writes: true};
let out = await call('PUT', '/api/identity-session?session_id=test-session&batch_id=batch-1', {state: {...state, batchId: 'other-batch'}});
if (out.status !== 422 || out.body.reason !== 'state_batch_id_mismatch') throw new Error(`batch mismatch must fail closed ${JSON.stringify(out)}`);
out = await call('PUT', '/api/identity-session?session_id=test-session&batch_id=batch-1', '{bad json');
if (out.status !== 400 || out.body.error !== 'invalid_json_body') throw new Error(`invalid JSON must return 400 ${JSON.stringify(out)}`);
out = await call('PUT', '/api/identity-session?session_id=test-session&batch_id=batch-1', {state}, {'content-length': String(2 * 1024 * 1024 + 1)});
if (out.status !== 413 || out.body.error !== 'request_body_too_large') throw new Error(`oversized body must return 413 ${JSON.stringify(out)}`);
out = await call('PUT', '/api/identity-session?session_id=test-session&batch_id=batch-1', {state});
if (out.status !== 200 || !out.body.no_live_writes) throw new Error(`PUT failed ${JSON.stringify(out)}`);
out = await call('GET', '/api/identity-session?session_id=test-session&batch_id=batch-1');
if (out.status !== 200 || out.body.state.tasks[0].id !== 't1') throw new Error(`GET failed ${JSON.stringify(out)}`);
out = await call('GET', '/api/identity-session?session_id=test-session&batch_id=batch-2');
if (out.status !== 404) throw new Error(`session state leaked across batches: ${JSON.stringify(out)}`);
out = await call('DELETE', '/api/identity-session?session_id=test-session&batch_id=batch-1');
if (out.status !== 200 || !out.body.deleted) throw new Error(`DELETE failed ${JSON.stringify(out)}`);
out = await call('GET', '/api/identity-session?session_id=test-session&batch_id=batch-1');
if (out.status !== 404) throw new Error(`expected 404 after delete, got ${JSON.stringify(out)}`);
console.log('Verified identity-session API with file-backed shared store.');
