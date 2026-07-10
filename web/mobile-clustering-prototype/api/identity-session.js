import fs from 'node:fs/promises';
import path from 'node:path';
import {del as blobDel, get as blobGet, put as blobPut} from '@vercel/blob';

const KEY_PREFIX = 'stav-identity-session:';
const MAX_BODY_BYTES = 2 * 1024 * 1024;
const MAX_TASKS = 500;
const MAX_DECISIONS = 1000;
const MAX_BUCKETS = 1000;

function json(res, status, body) {
  res.statusCode = status;
  res.setHeader('content-type', 'application/json; charset=utf-8');
  res.setHeader('cache-control', 'no-store');
  res.end(JSON.stringify(body));
}

function safeId(value) {
  return String(value || '').replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 100);
}

function stateValidationError(state, batchId) {
  if (!state || typeof state !== 'object' || !Array.isArray(state.tasks) || !Array.isArray(state.decisions) || !Array.isArray(state.buckets)) return 'tasks_decisions_or_buckets_missing';
  if (state.tasks.length > MAX_TASKS || state.decisions.length > MAX_DECISIONS || state.buckets.length > MAX_BUCKETS) return 'session_collection_limit_exceeded';
  if (state.batchId && safeId(state.batchId) !== batchId) return 'state_batch_id_mismatch';
  if (state.datasetVersion != null && (typeof state.datasetVersion !== 'string' || state.datasetVersion.length > 240)) return 'invalid_dataset_version';
  if (state.sourceAssets != null && (!Array.isArray(state.sourceAssets) || state.sourceAssets.length > 1000)) return 'invalid_source_assets';
  const taskIds = state.tasks.map((task) => task?.id).filter(Boolean);
  if (taskIds.length !== state.tasks.length || new Set(taskIds).size !== taskIds.length) return 'missing_or_duplicate_task_id';
  if (state.decisions.some((decision) => !decision?.taskId || !taskIds.includes(decision.taskId))) return 'decision_task_not_in_session';
  return null;
}

function storeKey(batchId, sessionId) {
  return `${KEY_PREFIX}${batchId}:${sessionId}`;
}

function blobPath(batchId, sessionId) {
  return `identity-sessions/${batchId}/${sessionId}.json`;
}

function blobConfigured() {
  return Boolean(process.env.BLOB_READ_WRITE_TOKEN || (process.env.VERCEL_OIDC_TOKEN && process.env.BLOB_STORE_ID));
}

async function streamToText(stream) {
  if (!stream) return '';
  return new Response(stream).text();
}

async function readBody(req) {
  const declared = Number(req.headers?.['content-length'] || 0);
  if (declared > MAX_BODY_BYTES) {
    const error = new Error('request_body_too_large');
    error.statusCode = 413;
    throw error;
  }
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    const buffer = Buffer.from(chunk);
    size += buffer.length;
    if (size > MAX_BODY_BYTES) {
      const error = new Error('request_body_too_large');
      error.statusCode = 413;
      throw error;
    }
    chunks.push(buffer);
  }
  return Buffer.concat(chunks).toString('utf8');
}

async function kvRequest(command) {
  const base = process.env.KV_REST_API_URL || process.env.VERCEL_KV_REST_API_URL;
  const token = process.env.KV_REST_API_TOKEN || process.env.VERCEL_KV_REST_API_TOKEN;
  if (!base || !token) return {configured: false};
  const response = await fetch(base.replace(/\/$/, ''), {
    method: 'POST',
    headers: {'authorization': `Bearer ${token}`, 'content-type': 'application/json'},
    body: JSON.stringify(command),
    cache: 'no-store',
  });
  if (!response.ok) throw new Error(`kv_${response.status}`);
  return {configured: true, payload: await response.json()};
}

async function filePath(batchId, sessionId) {
  const dir = process.env.STAV_IDENTITY_SESSION_DIR;
  if (!dir) return null;
  await fs.mkdir(dir, {recursive: true});
  return path.join(dir, `${batchId}__${sessionId}.json`);
}

async function readState(batchId, sessionId) {
  const key = storeKey(batchId, sessionId);
  const kv = await kvRequest(['get', key]);
  if (kv.configured) {
    const raw = kv.payload?.result;
    return raw ? JSON.parse(raw) : null;
  }
  if (blobConfigured()) {
    const blob = await blobGet(blobPath(batchId, sessionId), {access: 'private', useCache: false});
    if (!blob) return null;
    return JSON.parse(await streamToText(blob.stream));
  }
  const fp = await filePath(batchId, sessionId);
  if (fp) {
    try { return JSON.parse(await fs.readFile(fp, 'utf8')); } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }
  return undefined;
}

async function writeState(batchId, sessionId, state) {
  const record = {state, updatedAt: new Date().toISOString(), no_live_writes: true};
  const key = storeKey(batchId, sessionId);
  const kv = await kvRequest(['set', key, JSON.stringify(record)]);
  if (kv.configured) return record;
  if (blobConfigured()) {
    await blobPut(blobPath(batchId, sessionId), JSON.stringify(record), {
      access: 'private',
      addRandomSuffix: false,
      allowOverwrite: true,
      contentType: 'application/json',
      cacheControlMaxAge: 60,
    });
    return record;
  }
  const fp = await filePath(batchId, sessionId);
  if (fp) { await fs.writeFile(fp, JSON.stringify(record, null, 2)); return record; }
  return undefined;
}

async function deleteState(batchId, sessionId) {
  const key = storeKey(batchId, sessionId);
  const kv = await kvRequest(['del', key]);
  if (kv.configured) return true;
  if (blobConfigured()) { await blobDel(blobPath(batchId, sessionId)); return true; }
  const fp = await filePath(batchId, sessionId);
  if (fp) { try { await fs.unlink(fp); } catch (error) { if (error.code !== 'ENOENT') throw error; } return true; }
  return undefined;
}

export default async function handler(req, res) {
  const url = new URL(req.url, 'http://localhost');
  const sessionId = safeId(url.searchParams.get('session_id'));
  const batchId = safeId(url.searchParams.get('batch_id'));
  if (!sessionId || !batchId) return json(res, 400, {error: 'missing_session_id_or_batch_id'});
  try {
    if (req.method === 'GET') {
      const record = await readState(batchId, sessionId);
      if (record === undefined) return json(res, 501, {error: 'backend_store_not_configured', required: 'Configure Vercel KV, Vercel Blob, or STAV_IDENTITY_SESSION_DIR.'});
      if (!record) return json(res, 404, {error: 'session_not_found', sessionId, batchId});
      return json(res, 200, {sessionId, batchId, ...record});
    }
    if (req.method === 'PUT') {
      const contentType = String(req.headers?.['content-type'] || '');
      if (!contentType.toLowerCase().includes('application/json')) return json(res, 415, {error: 'content_type_must_be_application_json'});
      const payload = JSON.parse(await readBody(req) || '{}');
      const validationError = stateValidationError(payload.state, batchId);
      if (validationError) return json(res, 422, {error: 'invalid_identity_session_state', reason: validationError});
      const record = await writeState(batchId, sessionId, {...payload.state, sessionId, batchId, no_live_writes: true});
      if (record === undefined) return json(res, 501, {error: 'backend_store_not_configured', required: 'Configure Vercel KV, Vercel Blob, or STAV_IDENTITY_SESSION_DIR.'});
      return json(res, 200, {sessionId, batchId, updatedAt: record.updatedAt, no_live_writes: true});
    }
    if (req.method === 'DELETE') {
      const deleted = await deleteState(batchId, sessionId);
      if (deleted === undefined) return json(res, 501, {error: 'backend_store_not_configured'});
      return json(res, 200, {sessionId, batchId, deleted: true, no_live_writes: true});
    }
    return json(res, 405, {error: 'method_not_allowed'});
  } catch (error) {
    if (error?.statusCode === 413) return json(res, 413, {error: 'request_body_too_large', max_bytes: MAX_BODY_BYTES});
    if (error instanceof SyntaxError) return json(res, 400, {error: 'invalid_json_body'});
    return json(res, 500, {error: 'identity_session_backend_error', message: error?.message || String(error)});
  }
}
