import fs from 'node:fs';

const coverage = JSON.parse(fs.readFileSync(new URL('./public/batches/coverage.json', import.meta.url), 'utf8'));
const index = JSON.parse(fs.readFileSync(new URL('./public/batches/index.json', import.meta.url), 'utf8'));
const allowedLanes = new Set([
  'dalia_identity_review',
  'terminal_closed',
  'downstream_existing_workflow',
  'support_linked_to_web',
  'support_mapping_pending',
  'non_web_source_routed',
  'system_review_pending',
]);
if (!coverage.coverage_valid) throw new Error('Global source coverage is invalid.');
if (coverage.total_assets !== coverage.accounted_assets) throw new Error(`Global coverage mismatch ${coverage.accounted_assets}/${coverage.total_assets}`);
if ((coverage.assets || []).length !== coverage.total_assets) throw new Error('Coverage asset rows do not match total_assets.');
const ids = new Set();
for (const asset of coverage.assets || []) {
  if (!asset.asset_id || ids.has(asset.asset_id)) throw new Error(`Missing/duplicate global asset id: ${asset.asset_id}`);
  ids.add(asset.asset_id);
  if (!allowedLanes.has(asset.lane)) throw new Error(`Unknown coverage lane for ${asset.asset_id}: ${asset.lane}`);
  if (!asset.source_path || !asset.reason) throw new Error(`Coverage evidence missing for ${asset.asset_id}`);
}
const laneTotal = Object.values(coverage.lane_counts || {}).reduce((sum, value) => sum + value, 0);
if (laneTotal !== coverage.total_assets) throw new Error(`Lane count total mismatch ${laneTotal}/${coverage.total_assets}`);
if ((index.batches || []).length !== coverage.batches.length) throw new Error('Batch index mismatch.');
if (!coverage.review_batches || !coverage.reviewable_web_assets) throw new Error('Expected non-zero Dalia review queue.');
console.log(`Verified global coverage ${coverage.accounted_assets}/${coverage.total_assets}: ${coverage.reviewable_web_assets} Dalia-review photos across ${coverage.review_batches} batches; ${coverage.lane_counts.support_mapping_pending || 0} support assets remain in deterministic mapping queue.`);
