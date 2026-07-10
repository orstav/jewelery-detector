#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import {exportPacketBundle} from '../src/packetSchema.js';

const args = process.argv.slice(2);
function arg(name, fallback) {
  const index = args.indexOf(name);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
}
const base = arg('--base', 'https://mobile-clustering-prototype.vercel.app');
const outPath = arg('--out', 'qa/batch-session-status.json');
const sessionPrefix = arg('--session-prefix', 'stav-');
const index = JSON.parse(fs.readFileSync(new URL('../public/batches/index.json', import.meta.url), 'utf8'));

async function fetchBatch(batch) {
  const sessionId = `${sessionPrefix}${batch.batch_id}`;
  const url = new URL('/api/identity-session', base);
  url.searchParams.set('session_id', sessionId);
  url.searchParams.set('batch_id', batch.batch_id);
  let response;
  try {
    response = await fetch(url, {headers: {accept: 'application/json'}});
  } catch (error) {
    return {...batch, session_id: sessionId, status: 'backend_unreachable', blocker: error?.message || String(error)};
  }
  if (response.status === 404) return {...batch, session_id: sessionId, status: 'not_started', decisions: 0};
  if (!response.ok) return {...batch, session_id: sessionId, status: 'backend_error', http_status: response.status, blocker: await response.text()};
  const record = await response.json();
  const state = record.state || {};
  if (state.batchId && state.batchId !== batch.batch_id) return {...batch, session_id: sessionId, status: 'invalid_session', blocker: `state batchId ${state.batchId} does not match`};
  if (!Array.isArray(state.tasks) || !Array.isArray(state.decisions) || !Array.isArray(state.buckets)) return {...batch, session_id: sessionId, status: 'invalid_session', blocker: 'tasks/decisions/buckets missing'};
  const sourceAssets = state.sourceAssets || [];
  const bundle = exportPacketBundle({datasetVersion: state.datasetVersion || 'backend-session', batchId: batch.batch_id, sourceAssets, decisions: state.decisions, buckets: state.buckets});
  const completedIds = new Set(state.decisions.map((decision) => decision.taskId));
  const remaining = state.tasks.filter((task) => !completedIds.has(task.id)).length;
  const valid = bundle.validation.packets.valid && (remaining > 0 || bundle.validation.coverage.valid);
  return {
    ...batch,
    session_id: sessionId,
    status: !valid ? 'invalid_session' : remaining ? 'in_progress' : 'complete_validated',
    started: Boolean(state.started),
    tasks: state.tasks.length,
    decisions: state.decisions.length,
    remaining,
    buckets: state.buckets.length,
    updated_at: record.updatedAt,
    validation: bundle.validation,
    bundle: remaining ? null : bundle,
  };
}

const batches = [];
for (const batch of index.batches || []) batches.push(await fetchBatch(batch));
const counts = batches.reduce((acc, batch) => { acc[batch.status] = (acc[batch.status] || 0) + 1; return acc; }, {});
const completeBundles = batches.filter((batch) => batch.status === 'complete_validated').map((batch) => batch.bundle);
const report = {
  generated_at: new Date().toISOString(),
  base,
  no_live_writes: true,
  counts,
  total_batches: batches.length,
  total_reviewable_assets: batches.reduce((sum, batch) => sum + (batch.reviewable_assets || 0), 0),
  completed_reviewable_assets: batches.filter((batch) => batch.status === 'complete_validated').reduce((sum, batch) => sum + (batch.reviewable_assets || 0), 0),
  complete_bundles: completeBundles,
  batches: batches.map(({bundle, ...batch}) => batch),
};
fs.mkdirSync(path.dirname(outPath), {recursive: true});
fs.writeFileSync(outPath, JSON.stringify(report, null, 2));
console.log(JSON.stringify({output: outPath, counts, completed_reviewable_assets: report.completed_reviewable_assets, total_reviewable_assets: report.total_reviewable_assets, no_live_writes: true}, null, 2));
if (batches.some((batch) => ['backend_unreachable', 'backend_error', 'invalid_session'].includes(batch.status))) process.exitCode = 1;
