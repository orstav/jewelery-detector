#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import {exportPacketBundle} from '../src/packetSchema.js';

const sessionPath = process.argv[2];
const outputPath = process.argv[3] || 'qa/identity-backend-packets.json';
if (!sessionPath) {
  console.error('Usage: node tools/hal_export_identity_session.mjs <session-state-or-record.json> [output.json]');
  process.exit(2);
}
const raw = JSON.parse(fs.readFileSync(sessionPath, 'utf8'));
const state = raw.state || raw;
const bundle = exportPacketBundle({
  datasetVersion: state.datasetVersion || state.version || 'backend-session',
  batchId: state.batchId || state.batch_id || 'backend-session',
  sourceAssets: state.sourceAssets || [],
  decisions: state.decisions || [],
  buckets: state.buckets || [],
});
const report = {source: sessionPath, generatedAt: new Date().toISOString(), no_live_writes: true, bundle};
fs.mkdirSync(path.dirname(outputPath), {recursive: true});
fs.writeFileSync(outputPath, JSON.stringify(report, null, 2));
console.log(JSON.stringify({output: outputPath, packets: bundle.packets.length, coverage: bundle.validation.coverage, no_live_writes: true}, null, 2));
