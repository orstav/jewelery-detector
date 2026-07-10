import {validatePacket, validatePackets, validateCoverage, exportPacketBundle} from './src/packetSchema.js';

const base = {
  photo_ids: ['asset-1'],
  displayed_assumption: 'HAL showed this as possible corrected image for R212',
  human_decision: 'Dalia chose replace old version',
};

const allowed = [
  {...base, decision_type: 'existing_product_images', target_airtable_product: 'R212'},
  {...base, decision_type: 'new_product_identity', temporary_bucket_id: 'new-1'},
  {...base, decision_type: 'same_design_different_product', target_bucket_id: 'bucket-1', reference_airtable_product: 'R057', visible_difference: 'אבן / צבע אבן אחר'},
  {...base, decision_type: 'photographer_corrected_image', target_airtable_product: 'R212', target_bucket_id: null, image_version_action: 'replace_old_version', relationship: 'new_edit_of_existing_image', needs_whatsapp_followup: false},
  {...base, decision_type: 'not_relevant', exclusion_reason: 'not jewelry'},
  {...base, decision_type: 'duplicate', duplicate_reason: 'same source'},
  {...base, decision_type: 'not_sure', followup_reason_code: 'unclear_photographer_corrected_image_target', needs_whatsapp_followup: true},
  {...base, decision_type: 'split_group', clusters: [{photo_ids: ['asset-1']}]},
  {...base, decision_type: 'attach_to_session_bucket', target_bucket_id: 'bucket-1'},
];
for (const packet of allowed) {
  const result = validatePacket(packet);
  if (!result.valid) throw new Error(`${packet.decision_type} should validate: ${result.errors.join('; ')}`);
}

const banned = validatePacket({...base, decision_type: 'fix_existing_product'});
if (banned.valid) throw new Error('fix_existing_product must be rejected.');
const facty = validatePacket({...base, decision_type: 'photographer_corrected_image', target_airtable_product: 'R212', image_version_action: 'replace_old_version', relationship: 'new_edit_of_existing_image', price: 100});
if (facty.valid) throw new Error('product facts fields must be rejected.');
const unresolvedCorrected = validatePacket({...base, decision_type: 'photographer_corrected_image', unresolved_target: true, image_version_action: 'unresolved', relationship: 'new_edit_of_existing_image'});
if (!unresolvedCorrected.valid) throw new Error(`unresolved corrected-image packet should validate: ${unresolvedCorrected.errors.join('; ')}`);

const sourceAssets = [{asset_id: 'asset-1'}, {asset_id: 'asset-2'}];
const coverageOk = validateCoverage(sourceAssets, [
  {...base, photo_ids: ['asset-1'], decision_type: 'duplicate'},
  {...base, photo_ids: ['asset-2'], decision_type: 'not_sure'},
]);
if (!coverageOk.valid) throw new Error(`coverage should pass: ${coverageOk.errors.join('; ')}`);
const coverageMissing = validateCoverage(sourceAssets, [{...base, photo_ids: ['asset-1'], decision_type: 'duplicate'}]);
if (coverageMissing.valid) throw new Error('coverage must catch missing source images.');
const coverageDup = validateCoverage(sourceAssets, [
  {...base, photo_ids: ['asset-1'], decision_type: 'existing_product_images', target_airtable_product: 'R1'},
  {...base, photo_ids: ['asset-1'], decision_type: 'new_product_identity'},
  {...base, photo_ids: ['asset-2'], decision_type: 'not_sure'},
]);
if (coverageDup.valid) throw new Error('coverage must catch duplicate primary assignments.');

const bundle = exportPacketBundle({datasetVersion: 'test', batchId: 'dropbox-2025-03-19-web', sourceAssets, buckets: [], decisions: [
  {taskId: 't1', outcome: 'duplicate', payload: {photoIds: ['asset-1'], displayedAssumption: 'a', humanDecision: 'd'}, decidedAt: 'now'},
  {taskId: 't2', outcome: 'not_sure', payload: {photoIds: ['asset-2'], displayedAssumption: 'a', humanDecision: 'd'}, decidedAt: 'now'},
]});
if (!bundle.dry_run.no_airtable_writes || !bundle.dry_run.no_drive_writes || !bundle.dry_run.no_shopify_writes || !bundle.dry_run.no_whatsapp_sends) {
  throw new Error('dry-run safety flags must all be true.');
}
if (!bundle.validation.packets.valid || !bundle.validation.coverage.valid) throw new Error('exported bundle should validate.');

console.log('Verified packet schema, coverage validator, corrected-image constraints, and dry-run safety.');
