import fs from 'node:fs';
import vm from 'node:vm';
import {validatePackets, validateCoverage} from './src/packetSchema.js';

function loadFixture() {
  const source = fs.readFileSync(new URL('./data.js', import.meta.url), 'utf8');
  const sandbox = {window: {}};
  vm.runInNewContext(source, sandbox, {filename: 'data.js'});
  return {
    sourceAssets: sandbox.window.STAV_SOURCE_ASSETS || [],
    groups: sandbox.window.STAV_REAL_GROUPS || [],
    stats: sandbox.window.STAV_REAL_DATASET_STATS || {},
  };
}

function defaultPacketForCard(card) {
  return {
    decision_id: `${card.id}-dry-run-not-sure`,
    task_id: card.id,
    decision_type: 'not_sure',
    photo_ids: (card.photos || []).map((photo) => photo.id),
    displayed_assumption: card.halAssumption || 'HAL showed this source image as an identity decision',
    human_decision: 'dry-run placeholder: Dalia has not reviewed yet',
    followup_reason_code: 'awaiting_dalia_identity_sort',
    needs_whatsapp_followup: true,
  };
}

function actionFor(packet) {
  const base = {
    packet_id: packet.decision_id,
    decision_type: packet.decision_type,
    photo_ids: packet.photo_ids,
    writes: {airtable: false, drive: false, shopify: false, whatsapp: false},
  };
  switch (packet.decision_type) {
    case 'existing_product_images':
      return {...base, airtable_plan: `link image ownership to existing product ${packet.target_airtable_product || '[unresolved]'}`, drive_plan: 'prepare add/link-to-product-folder proposal', whatsapp_plan: packet.unresolved_target ? 'ask Dalia/Eyal which existing product' : 'none unless approval/facts needed'};
    case 'new_product_identity':
      return {...base, airtable_plan: 'prepare candidate product create proposal', drive_plan: 'prepare new product image folder proposal', whatsapp_plan: 'ask facts/price/material only after Or-approved next step'};
    case 'same_design_different_product':
      return {...base, airtable_plan: 'prepare candidate under related design/reference', drive_plan: 'prepare separate product image ownership proposal', whatsapp_plan: 'ask distinguishing fact only if needed'};
    case 'photographer_corrected_image':
      return {...base, airtable_plan: 'prepare existing image version update/readback proposal', drive_plan: `compare old/new image; ${packet.image_version_action || 'unresolved'} plan only`, whatsapp_plan: packet.unresolved_target ? 'ask which existing image/product this replaces/adds to' : 'none unless approval needed'};
    case 'duplicate':
      return {...base, airtable_plan: 'none', drive_plan: 'prepare duplicate/exclusion record only', whatsapp_plan: 'none'};
    case 'not_relevant':
      return {...base, airtable_plan: 'none', drive_plan: 'prepare not-relevant exclusion record only', whatsapp_plan: 'none'};
    case 'attach_to_session_bucket':
      return {...base, airtable_plan: `defer to session bucket ${packet.target_bucket_id}`, drive_plan: 'defer to bucket action', whatsapp_plan: 'defer'};
    default:
      return {...base, airtable_plan: 'none until human identity decision', drive_plan: 'keep source covered, no move/write', whatsapp_plan: 'ask Dalia only after packet review if still unsure'};
  }
}

const args = process.argv.slice(2);
const outFlag = args.indexOf('--out');
const outPath = outFlag >= 0 ? args[outFlag + 1] : undefined;
const inputPath = args.find((arg, index) => arg !== '--out' && index !== outFlag + 1);
const fixture = loadFixture();
let bundle;
if (inputPath) {
  bundle = JSON.parse(fs.readFileSync(inputPath, 'utf8'));
} else {
  bundle = {
    batch_id: fixture.stats.batch_id || 'dropbox-2025-03-19-web',
    exported_at: new Date().toISOString(),
    mode: 'pre_review_placeholder',
    source_asset_count: fixture.sourceAssets.length,
    packets: fixture.groups.map(defaultPacketForCard),
  };
}
const packetResult = validatePackets(bundle.packets || []);
const coverageResult = validateCoverage(fixture.sourceAssets, bundle.packets || []);
const report = {
  batch_id: bundle.batch_id || fixture.stats.batch_id || 'dropbox-2025-03-19-web',
  generated_at: new Date().toISOString(),
  source_asset_count: fixture.sourceAssets.length,
  packet_count: (bundle.packets || []).length,
  validation: {packets: packetResult, coverage: coverageResult},
  safety: {no_airtable_writes: true, no_drive_writes: true, no_shopify_writes: true, no_whatsapp_sends: true},
  actions: (bundle.packets || []).map(actionFor),
};
if (!packetResult.valid || !coverageResult.valid) {
  console.error(JSON.stringify(report, null, 2));
  process.exit(2);
}
if (outPath) fs.writeFileSync(outPath, JSON.stringify(report, null, 2));
console.log(JSON.stringify({batch_id: report.batch_id, source_asset_count: report.source_asset_count, packet_count: report.packet_count, coverage: coverageResult, safety: report.safety, output: outPath || null}, null, 2));
