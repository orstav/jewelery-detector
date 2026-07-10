export const ALLOWED_DECISION_TYPES = new Set([
  'existing_product_images',
  'new_product_identity',
  'same_design_different_product',
  'photographer_corrected_image',
  'not_relevant',
  'duplicate',
  'not_sure',
  'split_group',
  'attach_to_session_bucket',
]);

const PRODUCT_FACT_FIELDS = ['price', 'material', 'metal', 'copy', 'description', 'gemstone', 'stone_size', 'productFacts'];

export function validatePacket(packet) {
  const errors = [];
  if (!packet || typeof packet !== 'object') return {valid: false, errors: ['packet must be object']};
  if (!ALLOWED_DECISION_TYPES.has(packet.decision_type)) errors.push(`unsupported decision_type: ${packet.decision_type}`);
  if (packet.decision_type === 'fix_existing_product') errors.push('fix_existing_product is banned; use photographer_corrected_image only for newer/differently edited photographer images');
  if (!Array.isArray(packet.photo_ids) || packet.photo_ids.length === 0) errors.push('photo_ids must be non-empty');
  if (!packet.displayed_assumption) errors.push('displayed_assumption is required');
  if (!packet.human_decision) errors.push('human_decision is required');
  for (const field of PRODUCT_FACT_FIELDS) if (Object.prototype.hasOwnProperty.call(packet, field)) errors.push(`product facts field is not allowed in identity packet: ${field}`);
  if (packet.decision_type === 'existing_product_images') {
    if (!packet.target_airtable_product && !packet.unresolved_target) errors.push('existing_product_images requires target_airtable_product or unresolved_target');
  }
  if (packet.decision_type === 'same_design_different_product') {
    if (!packet.reference_airtable_product && !packet.reference_bucket_id && !packet.unresolved_design_reference) errors.push('same_design_different_product requires a reference product/bucket or unresolved design reference');
    if (!packet.visible_difference) errors.push('same_design_different_product requires visible_difference');
  }
  if (packet.decision_type === 'photographer_corrected_image') {
    if (packet.relationship !== 'new_edit_of_existing_image') errors.push('photographer_corrected_image.relationship must be new_edit_of_existing_image');
    if (!['replace_old_version', 'add_alongside_existing', 'unresolved'].includes(packet.image_version_action)) errors.push('invalid image_version_action');
    if (!packet.target_airtable_product && !packet.target_bucket_id && !packet.unresolved_target) errors.push('photographer_corrected_image requires target product/bucket or unresolved marker');
  }
  return {valid: errors.length === 0, errors};
}

export function validatePackets(packets) {
  const errors = [];
  packets.forEach((packet, index) => {
    const result = validatePacket(packet);
    if (!result.valid) errors.push(...result.errors.map((error) => `packet[${index}]: ${error}`));
  });
  return {valid: errors.length === 0, errors};
}

export function validateCoverage(sourceAssets, packets) {
  const sourceIds = new Set((sourceAssets || []).map((asset) => asset.asset_id || asset.id));
  const accounted = new Map();
  const primary = new Map();
  const errors = [];
  for (const packet of packets || []) {
    if (packet.decision_type === 'split_group') continue;
    for (const id of packet.photo_ids || []) {
      accounted.set(id, (accounted.get(id) || 0) + 1);
      if (['existing_product_images', 'new_product_identity', 'same_design_different_product', 'photographer_corrected_image', 'attach_to_session_bucket'].includes(packet.decision_type)) {
        primary.set(id, (primary.get(id) || 0) + 1);
      }
    }
  }
  for (const id of sourceIds) if (!accounted.has(id)) errors.push(`missing source asset from terminal decisions: ${id}`);
  for (const id of accounted.keys()) if (!sourceIds.has(id)) errors.push(`packet references unknown photo_id: ${id}`);
  for (const [id, count] of primary) if (count > 1) errors.push(`photo assigned to more than one primary identity: ${id}`);
  return {valid: errors.length === 0, errors, accounted: accounted.size, expected: sourceIds.size};
}

export function makePacketFromDecision(decision) {
  const payload = decision.payload || {};
  const photo_ids = payload.photoIds || [];
  const base = {
    decision_id: `${decision.taskId}-${decision.decidedAt}`,
    task_id: decision.taskId,
    decision_type: decision.outcome,
    photo_ids,
    displayed_assumption: payload.displayedAssumption || 'HAL showed this as an identity decision card',
    human_decision: payload.humanDecision || payload.label || decision.outcome,
    needs_whatsapp_followup: Boolean(payload.needsWhatsappFollowup),
  };
  if (decision.outcome === 'existing_product_images') return {...base, target_airtable_product: payload.productId || payload.targetProductId || null, target_bucket_id: payload.bucketId || null, unresolved_target: Boolean(payload.unresolvedTarget || (!payload.productId && !payload.targetProductId)), catalog_source: payload.catalogSource || null, catalog_image_name: payload.imageName || null, detector_source: payload.detectorSource || payload.detectorEvidence?.source || null, detector_score: payload.detectorScore ?? payload.detectorEvidence?.score ?? null, detector_rank: payload.detectorRank ?? payload.detectorEvidence?.rank ?? null, detector_margin: payload.detectorMargin ?? payload.detectorEvidence?.margin ?? null, detector_evidence: payload.detectorEvidence || null};
  if (decision.outcome === 'new_product_identity') return {...base, temporary_bucket_id: payload.bucketId, product_type_hint: payload.productType};
  if (decision.outcome === 'same_design_different_product') return {...base, target_bucket_id: payload.bucketId, visible_difference: payload.visibleDifference, reference_airtable_product: payload.referenceProductId || null, reference_bucket_id: payload.referenceBucketId || null, reference_label: payload.referenceLabel || payload.designReference?.label || null, unresolved_design_reference: Boolean(payload.unresolvedDesignReference), create_design_if_missing: Boolean(payload.createDesignIfMissing), relationship: 'new_product_under_existing_or_new_design'};
  if (decision.outcome === 'photographer_corrected_image') return {...base, target_airtable_product: payload.productId || payload.targetProductId, target_bucket_id: payload.bucketId || null, unresolved_target: payload.unresolvedTarget || (!payload.productId && !payload.targetProductId && !payload.bucketId), image_version_action: payload.imageVersionAction || 'unresolved', relationship: 'new_edit_of_existing_image'};
  if (decision.outcome === 'duplicate') return {...base, duplicate_reason: payload.reason || 'reviewer_marked_duplicate'};
  if (decision.outcome === 'not_relevant') return {...base, exclusion_reason: payload.reason || 'reviewer_marked_not_relevant'};
  if (decision.outcome === 'attach_to_session_bucket') return {...base, target_bucket_id: payload.bucketId};
  return {...base, decision_type: 'not_sure', followup_reason_code: payload.reason || 'reviewer_unsure', needs_whatsapp_followup: true};
}

export function exportPacketBundle({datasetVersion, batchId, sourceAssets, decisions, buckets}) {
  const packets = (decisions || []).map(makePacketFromDecision);
  const packetValidation = validatePackets(packets);
  const coverageValidation = validateCoverage(sourceAssets || [], packets);
  const dry_run_actions = packets.map((packet) => {
    const action = {packet_id: packet.decision_id, decision_type: packet.decision_type, photo_ids: packet.photo_ids, writes: {airtable: false, drive: false, shopify: false, whatsapp: false}};
    if (packet.decision_type === 'existing_product_images') action.plan = 'attach/link image plan for existing product';
    else if (packet.decision_type === 'new_product_identity') action.plan = 'Airtable candidate plan + WhatsApp facts follow-up';
    else if (packet.decision_type === 'same_design_different_product') action.plan = 'new candidate under related design/reference';
    else if (packet.decision_type === 'photographer_corrected_image') action.plan = 'old/new image comparison + replace/add plan';
    else if (packet.decision_type === 'not_relevant') action.plan = 'structured exclusion';
    else if (packet.decision_type === 'duplicate') action.plan = 'duplicate record/exclusion';
    else action.plan = 'human review / WhatsApp follow-up';
    return action;
  });
  return {
    batch_id: batchId,
    datasetVersion,
    exported_at: new Date().toISOString(),
    source_asset_count: (sourceAssets || []).length,
    buckets: buckets || [],
    packets,
    validation: {packets: packetValidation, coverage: coverageValidation},
    dry_run: {no_airtable_writes: true, no_drive_writes: true, no_shopify_writes: true, no_whatsapp_sends: true, actions: dry_run_actions},
  };
}
