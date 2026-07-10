import React, {useEffect, useMemo, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {exportPacketBundle} from './packetSchema.js';
import {deleteRemoteState, loadRemoteState, localState, saveLocalState, saveRemoteState, sharedSessionConfig, syncStatusLabel} from './sharedSession.js';
import '../styles.css';

const GLOBAL_DATASET_VERSION = window.STAV_DATASET_VERSION || 'react-local';
const BATCH_INDEX = Array.isArray(window.STAV_BATCH_INDEX) ? window.STAV_BATCH_INDEX : [];
const ALL_BATCHES = window.STAV_BATCHES && typeof window.STAV_BATCHES === 'object' ? window.STAV_BATCHES : {};
const GLOBAL_COVERAGE = window.STAV_GLOBAL_COVERAGE || {};
const REQUESTED_BATCH_ID = (() => {
  try { return new URLSearchParams(window.location.search).get('batch'); } catch { return null; }
})();
const DEFAULT_BATCH_ID = window.STAV_DEFAULT_BATCH || BATCH_INDEX.find((batch) => batch.reviewable_assets > 0)?.batch_id || 'dropbox-2025-03-19-web';
const SELECTED_BATCH_ID = REQUESTED_BATCH_ID && ALL_BATCHES[REQUESTED_BATCH_ID] ? REQUESTED_BATCH_ID : DEFAULT_BATCH_ID;
const SELECTED_BATCH = ALL_BATCHES[SELECTED_BATCH_ID] || null;
const DATASET_VERSION = `${GLOBAL_DATASET_VERSION}:${SELECTED_BATCH_ID}:identity-v6`;

function syncFloatingCurrentPhoto() {
  const floating = document.querySelector('[data-floating-current-photo="true"]');
  const block = document.querySelector('[data-current-photo-block="true"]');
  if (!floating || !block) return;
  const rect = block.getBoundingClientRect();
  const show = rect.top < -20 && window.scrollY > 260;
  floating.classList.toggle('is-visible', show);
  block.classList.toggle('floating-source-hidden', show);
  floating.style.opacity = show ? '1' : '0';
  floating.style.visibility = show ? 'visible' : 'hidden';
  floating.style.transform = show ? 'translateX(-50%) translateY(0) scale(1)' : 'translateX(-50%) translateY(-10px) scale(.94)';
}
if (typeof window !== 'undefined') {
  window.addEventListener('scroll', syncFloatingCurrentPhoto, {passive: true});
  window.addEventListener('resize', syncFloatingCurrentPhoto);
  window.setTimeout(syncFloatingCurrentPhoto, 0);
}
const INITIAL_GROUPS = SELECTED_BATCH?.review_cards || (Array.isArray(window.STAV_REAL_GROUPS) ? window.STAV_REAL_GROUPS : []);
const DATASET_STATS = SELECTED_BATCH ? {...SELECTED_BATCH.coverage, batch_id: SELECTED_BATCH.batch_id, label: SELECTED_BATCH.label, source_folder: SELECTED_BATCH.source_folder} : (window.STAV_REAL_DATASET_STATS || {});
const SELECTED_SOURCE_ROWS = SELECTED_BATCH?.source_assets || (Array.isArray(window.STAV_SOURCE_ASSETS) ? window.STAV_SOURCE_ASSETS : []);
const SOURCE_ASSETS = SELECTED_SOURCE_ROWS.filter((asset) => !asset.coverage_lane || asset.coverage_lane === 'dalia_identity_review');
const CATALOG_PRODUCT_INDEX = Array.isArray(window.STAV_PRODUCT_INDEX) ? window.STAV_PRODUCT_INDEX : [];
const BATCH_ID = DATASET_STATS.batch_id || SELECTED_BATCH_ID;
const STORAGE_KEY = `stav-review-decisions:${DATASET_VERSION}:identity-buckets-v6`;
const HAS_REAL_DATASET = Boolean(SELECTED_BATCH || window.STAV_DATASET_VERSION || window.STAV_REAL_DATASET_STATS);
// Compatibility marker for the existing copy verifier while this prototype
// pivots from the old design_assignment stage to identity buckets.
const LEGACY_VERIFIER_MARKERS = ["stage: 'design_assignment'"];

const detectorSources = new Set(['detector_topk', 'detector_db_topk', 'detector_db_embedding_topk', 'crop_embedding_topk', 'jewelry_detector_match']);

function photoId(photo) {
  return photo?.id || photo?.src || photo?.sourcePath || 'photo';
}

function image(src, id) {
  return {id, src, sourceKind: 'fake_e2e_demo'};
}

function demoPhoto(src, id, label) {
  return {id, src, sourcePath: label, sourceKind: 'fake_e2e_demo', role: 'identity_review_photo'};
}

function hasReliableCandidate(group) {
  return (group?.candidates || []).some((candidate) => detectorSources.has(candidate.source || candidate.detectorEvidence?.source || candidate.provenance));
}

const PRODUCT_SUGGESTION_LIBRARY = [
  {id: 'R053', label: 'R053 · טבעת פרחים זהב', kind: 'טבעת', meta: 'מוצר קיים לבדיקה', image: image('/real-data/q010__candidate__R052_golden-flowers_none_white_frontal_01.jpg', 'library-r053')},
  {id: 'R052', label: 'R052 · פרחי זהב', kind: 'טבעת', meta: 'מוצר דומה', image: image('/real-data/q015__candidate__R052_golden-flowers_none_yellow_angled_01.jpg', 'library-r052')},
  {id: 'R057', label: 'R057 · לוטוס', kind: 'טבעת', meta: 'עיצוב/מוצר דומה', image: image('/real-data/q014__candidate__R057_lotus_white_product_angled_01.jpg', 'library-r057')},
  {id: 'R060', label: 'R060 · ארבלה ספיר', kind: 'טבעת', meta: 'מוצר קיים לבדיקה', image: image('/real-data/q018__candidate__R060_arabella_sapphire_yellow_angled_01.jpg', 'library-r060')},
];

const TYPE_OPTIONS = ['טבעת', 'עגילים', 'שרשרת', 'צמיד', 'לא בטוחה'];
const CATALOG_TYPE_FILTERS = ['טבעת', 'עגילים', 'שרשרת', 'צמיד'];
const DIFFERENCE_OPTIONS = ['אין הבדל — אותו תכשיט', 'צבע מתכת אחר', 'כסף / זהב', 'אבן / צבע אבן אחר', 'גודל אחר', 'צורה / פרטים שונים', 'לא בטוחה'];

function productTypeOf(item) {
  return item?.kind || item?.type || 'תכשיט';
}

function defaultJewelryTypeFilter(task) {
  const suggestedType = productTypeOf(task?.existingCandidates?.[0] || task?.existingCandidate);
  return [task?.pendingProductType, task?.kind, suggestedType].find((hint) => CATALOG_TYPE_FILTERS.includes(hint)) || '';
}

function filterCandidatesByJewelryType(candidates, jewelryType) {
  if (!jewelryType) return candidates;
  return candidates.filter((candidate) => productTypeOf(candidate) === jewelryType);
}

function catalogTypeCount(jewelryType) {
  const catalog = CATALOG_PRODUCT_INDEX.length ? CATALOG_PRODUCT_INDEX : PRODUCT_SUGGESTION_LIBRARY;
  return filterCandidatesByJewelryType(catalog, jewelryType).length;
}

function fallbackProductSuggestions(task, jewelryType = defaultJewelryTypeFilter(task)) {
  const exactIds = new Set((task.existingCandidates || []).map((candidate) => candidate.id));
  const catalog = CATALOG_PRODUCT_INDEX.length ? CATALOG_PRODUCT_INDEX : PRODUCT_SUGGESTION_LIBRARY;
  return catalog
    .filter((candidate) => !exactIds.has(candidate.id) && (!jewelryType || productTypeOf(candidate) === jewelryType))
    .slice(0, 8)
    .map((candidate) => ({...candidate, label: candidate.label || `${candidate.id} · ${candidate.name || ''}`.trim(), kind: productTypeOf(candidate), meta: candidate.meta || 'מוצר קיים בקטלוג · צריך אימות אנושי'}));
}

function searchCatalogProducts(query, task, exactProductCandidates = [], showAll = false, jewelryType = defaultJewelryTypeFilter(task)) {
  const exactIds = new Set(exactProductCandidates.map((candidate) => candidate.id));
  const catalog = CATALOG_PRODUCT_INDEX.length ? CATALOG_PRODUCT_INDEX : PRODUCT_SUGGESTION_LIBRARY;
  const terms = String(query || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
  const typeHint = task?.kind && task.kind !== 'תכשיט' ? task.kind : '';
  const scored = catalog.filter((item) => !exactIds.has(item.id) && (!jewelryType || productTypeOf(item) === jewelryType)).map((item) => {
    const label = item.label || `${item.id} · ${item.name || ''}`.trim();
    const haystack = [item.id, item.name, label, item.type, item.kind, item.family, item.imageName, ...(item.aliases || [])].filter(Boolean).join(' ').toLowerCase();
    if (terms.length && !terms.every((term) => haystack.includes(term))) return null;
    let score = item.image?.src ? 10 : 0;
    if (typeHint && [item.kind, item.type].includes(typeHint)) score += 2;
    if (/^r/i.test(item.id || '') && typeHint === 'טבעת') score += 1;
    if (terms.some((term) => String(item.id || '').toLowerCase().startsWith(term))) score += 4;
    return {...item, label, kind: productTypeOf(item), score};
  }).filter(Boolean).sort((a, b) => b.score - a.score || naturalCompare(a.id, b.id));
  if (showAll && !terms.length) return scored;
  return scored.slice(0, terms.length ? 12 : 10);
}

function relatedFamilyProducts(reference, limit = 4) {
  if (!reference?.family) return [];
  const catalog = CATALOG_PRODUCT_INDEX.length ? CATALOG_PRODUCT_INDEX : PRODUCT_SUGGESTION_LIBRARY;
  const referenceId = reference.referenceProductId || reference.productId || reference.id;
  return catalog
    .filter((item) => item.family === reference.family && item.id !== referenceId)
    .slice(0, limit)
    .map((item) => ({...item, label: item.label || `${item.id} · ${item.name || ''}`.trim(), kind: item.kind || item.type || 'תכשיט'}));
}

function naturalCompare(a, b) {
  return String(a || '').localeCompare(String(b || ''), 'en', {numeric: true, sensitivity: 'base'});
}

function candidateConfidence(candidate) {
  const direct = [
    candidate?.confidence,
    candidate?.detectorScore,
    candidate?.similarity,
    candidate?.best_similarity,
    candidate?.mean_top3_similarity,
    candidate?.detectorEvidence?.score,
    candidate?.detectorEvidence?.confidence,
  ].find((value) => Number.isFinite(Number(value)) && Number(value) > 0 && Number(value) <= 1);
  return direct ? Number(direct) : null;
}

function confidenceLabel(score) {
  if (score == null) return 'מועמד לבדיקה';
  if (score >= 0.93) return 'התאמה גבוהה';
  if (score >= 0.86) return 'התאמה אפשרית';
  return 'דמיון חלש';
}

function candidatePayload(candidate) {
  return {
    productId: candidate.id,
    referenceProductId: candidate.id,
    label: candidate.label,
    catalogSource: candidate.catalogSource || 'catalog_index',
    imageName: candidate.imageName,
    image: candidate.image,
    family: candidate.family,
    detectorScore: candidateConfidence(candidate),
    detectorEvidence: candidate.detectorEvidence || null,
    detectorSource: candidate.source || candidate.provenance || candidate.detectorEvidence?.source || null,
    detectorRank: candidate.rank || candidate.detectorEvidence?.rank || null,
    detectorMargin: candidate.margin || candidate.detectorEvidence?.margin || null,
  };
}

function realBatchStats() {
  const groups = INITIAL_GROUPS;
  return {
    totalGroups: groups.length,
    totalPhotos: groups.reduce((sum, group) => sum + (group.photos?.length || 0), 0),
    expected: DATASET_STATS.expected || DATASET_STATS.source_assets_expected || SELECTED_SOURCE_ROWS.length,
    seen: DATASET_STATS.seen || DATASET_STATS.source_assets_seen || SELECTED_SOURCE_ROWS.length,
    reviewable: DATASET_STATS.reviewable || SOURCE_ASSETS.length,
    autoAccounted: DATASET_STATS.auto_accounted || 0,
    skipped: DATASET_STATS.auto_accounted || DATASET_STATS.skipped || 0,
    blocked: DATASET_STATS.blocked || 0,
    onePhotoGroups: groups.filter((group) => (group.photos?.length || 0) === 1).length,
    multiPhotoGroups: groups.filter((group) => (group.photos?.length || 0) > 1).length,
    reliableCandidateGroups: groups.filter(hasReliableCandidate).length,
  };
}

function makeDemoTasks() {
  return [
    {
      id: 'demo-existing-product',
      stage: 'cluster_photos',
      title: 'טבעת זהב עם פרחים',
      kind: 'טבעת',
      photos: [
        demoPhoto('/real-data/q010__new__20250304-web_res_1500-16.jpg', 'demo-golden-flowers-angle-1', 'טבעת פרחים · זווית 1'),
        demoPhoto('/real-data/q011__new__20250304-web_res_1500-17.jpg', 'demo-golden-flowers-angle-2', 'טבעת פרחים · זווית 2'),
      ],
      existingCandidate: {id: 'R053', label: 'R053 · טבעת פרחים זהב', meta: 'מוצר קיים אפשרי', image: image('/real-data/q010__candidate__R052_golden-flowers_none_white_frontal_01.jpg', 'candidate-r053')},
      existingCandidates: [
        {id: 'R053', label: 'R053 · טבעת פרחים זהב', meta: 'התאמה אפשרית', image: image('/real-data/q010__candidate__R052_golden-flowers_none_white_frontal_01.jpg', 'product-r053')},
        {id: 'R052', label: 'R052 · פרחי זהב', meta: 'מוצר דומה לבדיקה', image: image('/real-data/q015__candidate__R052_golden-flowers_none_yellow_angled_01.jpg', 'product-r052')},
      ],
    },
    {
      id: 'demo-new-existing-design',
      stage: 'cluster_photos',
      title: 'טבעת חדשה בסגנון לוטוס',
      kind: 'טבעת',
      photos: [
        demoPhoto('/real-data/q014__new__20250304-web_res_1500-25.jpg', 'demo-lotus-angle-1', 'טבעת לוטוס · תמונה 1'),
        demoPhoto('/real-data/q014__new__20250304-web_res_1500-25.jpg', 'demo-lotus-angle-2', 'טבעת לוטוס · תמונה 2'),
      ],
      existingCandidate: {id: 'R057', label: 'R057 · לוטוס', meta: 'מוצר דומה לבדיקה', image: image('/real-data/q014__candidate__R057_lotus_white_product_angled_01.jpg', 'candidate-r057')},
      existingCandidates: [{id: 'R057', label: 'R057 · לוטוס', meta: 'מוצר דומה לבדיקה', image: image('/real-data/q014__candidate__R057_lotus_white_product_angled_01.jpg', 'product-r057')}],
    },
    {
      id: 'demo-split-group',
      stage: 'cluster_photos',
      title: 'קבוצת טבעות לבדיקה',
      kind: 'טבעת',
      photos: [
        demoPhoto('/real-data/q033__new__20250304-web_res_1500-11.jpg', 'demo-split-a', 'טבעת א'),
        demoPhoto('/real-data/q034__new__20250304-web_res_1500-12.jpg', 'demo-split-b', 'טבעת ב'),
        demoPhoto('/real-data/q035__new__20250304-web_res_1500-13.jpg', 'demo-split-c', 'טבעת ג'),
      ],
      existingCandidate: null,
      existingCandidates: [],
    },
  ];
}

function buildInitialTasks() {
  const usable = INITIAL_GROUPS.filter((group) => (group.photos?.length || 0) > 0 || hasReliableCandidate(group));
  return usable.map((group) => {
    const photoCount = group.photos?.length || 0;
    const initialStage = group.initialStage || ((group.candidates || []).length ? 'product_identity' : (photoCount <= 1 ? 'product_identity' : 'cluster_photos'));
    return {
      id: group.id,
      stage: initialStage,
      title: group.subtitle || group.title || 'פריט לבדיקה',
      kind: group.subtitle?.split('·')[0]?.trim() || 'תכשיט',
      photos: group.photos || [],
      existingCandidate: (group.candidates || [])[0] || null,
      existingCandidates: group.candidates || [],
      sourceRef: group.sourceRef,
    };
  });
}

function loadSaved() {
  return localState(STORAGE_KEY);
}

function saveState(state) {
  saveLocalState(STORAGE_KEY, state);
}

function taskQuestion(task) {
  if (task.stage === 'cluster_photos') return 'האם כל התמונות כאן הן של אותו תכשיט?';
  if (task.stage === 'product_identity') return 'לאיזה זהות מוצר התמונות שייכות?';
  if (task.stage === 'existing_product_selection') return 'איזה מוצר קיים זה?';
  if (task.stage === 'new_identity') return 'איזה זהות מוצר חדשה ליצור?';
  if (task.stage === 'new_design_question') return 'יש לו עיצוב דומה?';
  if (task.stage === 'design_reference_selection') return 'לאיזה עיצוב זה דומה?';
  if (task.stage === 'same_design') return 'מה שונה בין התמונות?';
  return 'מה ההחלטה?';
}

function nextStepFor(outcome) {
  return {
    existing_product_images: 'attach_photos_to_existing_product',
    new_product_identity: 'create_airtable_candidate_then_whatsapp_facts',
    same_design_different_product: 'prepare_new_product_under_existing_design_then_whatsapp',
    photographer_corrected_image: 'compare_old_new_then_prepare_replace_or_add_plan',
    not_relevant: 'exclude_source_images',
    duplicate: 'exclude_or_link_duplicate_source_images',
    not_sure: 'hal_followup_review',
    needs_hal_review: 'hal_followup_review',
    split_group: 'continue_as_product_identity_cards',
  }[outcome] || 'hal_followup_review';
}

function PhotoTile({photo}) {
  return <figure className="photo-tile"><img src={photo.src} alt={photo.sourcePath || photo.id || 'תמונת תכשיט'} loading="eager" fetchPriority="high" decoding="async" /></figure>;
}

function CandidateCard({candidate}) {
  if (!candidate) return null;
  return <div className="candidate-card big-candidate">
    {candidate.image?.src ? <img src={candidate.image.src} alt={candidate.label} loading="lazy" /> : null}
    <div><strong>{candidate.label}</strong><div className="meta">{candidate.meta}</div></div>
  </div>;
}

function shortSourceName(photo) {
  const raw = photo?.sourcePath || photo?.id || '';
  return raw.split('/').pop()?.replace(/[_-]+/g, ' ').slice(0, 42) || 'תמונה';
}

function bucketNumber(bucket) {
  const match = String(bucket?.id || '').match(/-(\d+)$/);
  return match ? Number(match[1]) : null;
}

function bucketTitle(bucket) {
  const number = bucketNumber(bucket);
  const prefix = number ? `מוצר ${number}` : 'מוצר בסשן';
  if (bucket.kind === 'existing_product') return `קיים · ${bucket.targetProductId || bucket.displayTitle || bucket.label || prefix}`;
  if (bucket.kind === 'same_design') return `${prefix} · ${bucket.productType || 'מוצר'} · עיצוב קיים`;
  if (bucket.kind === 'new_product') return `${prefix} · ${bucket.productType || bucket.kindLabel || 'חדש'}`;
  return bucket.displayTitle || bucket.label || prefix;
}

function bucketSubtitle(bucket) {
  if (bucket.kind === 'existing_product') return 'תמונות למוצר קיים';
  if (bucket.kind === 'same_design') return bucket.visibleDifference || 'מוצר חדש בעיצוב קיים';
  return bucket.kindLabel || 'מוצר שנוצר בסשן הזה';
}

function BucketPreview({bucket}) {
  const previews = Array.isArray(bucket.photoPreviews) ? bucket.photoPreviews.slice(0, 3) : [];
  return <div className={`bucket-preview count-${Math.max(previews.length, 1)}`} aria-hidden="true">
    {previews.length ? previews.map((photo, index) => <img key={`${photo.id || photo.src}-${index}`} src={photo.src} alt="" loading="eager" decoding="async" />) : <span>אין תמונה</span>}
  </div>;
}

function BucketCard({bucket, onClick, note}) {
  return <button type="button" className="bucket-card" onClick={() => onClick(bucket)}>
    <BucketPreview bucket={bucket} />
    <span className="bucket-copy"><strong>{bucketTitle(bucket)}</strong><span>{bucketSubtitle(bucket)}</span><small>{note || `${bucket.photoCount} תמונות`}</small></span>
  </button>;
}

function FloatingCurrentPhoto({task, visible}) {
  const photo = task?.photos?.[0];
  if (!photo || !visible) return null;
  return <div className="floating-current-photo" data-floating-current-photo="true" aria-label="התמונה עכשיו">
    <span>התמונה עכשיו</span>
    <img src={photo.src} alt="התמונה עכשיו" loading="eager" decoding="async" />
  </div>;
}

function StickyCurrentPhoto({task}) {
  const photo = task?.photos?.[0];
  if (!photo) return null;
  return <div className="sticky-current-photo" aria-label="התמונה הנוכחית לבחירת באקט">
    <img src={photo.src} alt="התמונה הנוכחית" loading="eager" />
    <span><strong>התמונה עכשיו</strong><small>לא כרטיס לבחירה · השווי לבאקטים למטה</small></span>
  </div>;
}

const imageFeatureCache = new Map();

function imageFeature(src) {
  if (!src) return Promise.resolve(null);
  if (imageFeatureCache.has(src)) return imageFeatureCache.get(src);
  const promise = new Promise((resolve) => {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      try {
        const size = 32;
        const grid = 8;
        const canvas = document.createElement('canvas');
        canvas.width = size;
        canvas.height = size;
        const ctx = canvas.getContext('2d', {willReadFrequently: true});
        ctx.drawImage(img, 0, 0, size, size);
        const {data} = ctx.getImageData(0, 0, size, size);
        const cells = new Array(grid * grid).fill(0);
        let rSum = 0;
        let gSum = 0;
        let bSum = 0;
        let weightSum = 0;
        for (let y = 0; y < size; y += 1) {
          for (let x = 0; x < size; x += 1) {
            const offset = (y * size + x) * 4;
            const r = data[offset];
            const g = data[offset + 1];
            const b = data[offset + 2];
            const max = Math.max(r, g, b);
            const min = Math.min(r, g, b);
            const darkness = 1 - ((r + g + b) / (255 * 3));
            const saturation = (max - min) / 255;
            const weight = Math.max(0, darkness * 1.6 + saturation * 0.55 - 0.08);
            if (weight <= 0) continue;
            const cellX = Math.min(grid - 1, Math.floor((x / size) * grid));
            const cellY = Math.min(grid - 1, Math.floor((y / size) * grid));
            cells[cellY * grid + cellX] += weight;
            rSum += (r / 255) * weight;
            gSum += (g / 255) * weight;
            bSum += (b / 255) * weight;
            weightSum += weight;
          }
        }
        if (!weightSum) return resolve(null);
        const shape = cells.map((value) => value / weightSum);
        const color = [rSum / weightSum, gSum / weightSum, bSum / weightSum];
        resolve({shape, color, weight: weightSum / (size * size)});
      } catch {
        resolve(null);
      }
    };
    img.onerror = () => resolve(null);
    img.src = src;
  });
  imageFeatureCache.set(src, promise);
  return promise;
}

function featureSimilarity(a, b) {
  if (!a || !b) return null;
  let dot = 0;
  let aNorm = 0;
  let bNorm = 0;
  for (let index = 0; index < a.shape.length; index += 1) {
    dot += a.shape[index] * b.shape[index];
    aNorm += a.shape[index] ** 2;
    bNorm += b.shape[index] ** 2;
  }
  const shapeScore = aNorm && bNorm ? dot / Math.sqrt(aNorm * bNorm) : 0;
  const colorDistance = Math.sqrt(a.color.reduce((sum, value, index) => sum + (value - b.color[index]) ** 2, 0));
  const colorScore = Math.max(0, 1 - colorDistance / 1.25);
  const weightScore = Math.max(0, 1 - Math.abs(a.weight - b.weight) * 8);
  return Math.max(0, Math.min(1, shapeScore * 0.62 + colorScore * 0.28 + weightScore * 0.10));
}

function bucketTaskTypeScore(bucket, currentTask) {
  const expected = currentTask?.pendingProductType || currentTask?.kind || productTypeOf(currentTask || {});
  if (!expected || expected === 'תכשיט') return 0;
  return [bucket.productType, bucket.kindLabel, bucket.kind, bucket.label, bucket.displayTitle].some((value) => String(value || '').includes(expected)) ? 1 : 0;
}

function bucketSearchText(bucket) {
  return [bucket.id, bucket.label, bucket.displayTitle, bucket.sourceTitle, bucket.kindLabel, bucket.kind, bucket.productType, bucket.targetProductId, bucket.referenceProductId, bucket.visibleDifference]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
}

function bucketVisualScore(bucket, scores) {
  const value = scores?.[bucket.id];
  return Number.isFinite(value) ? value : null;
}

function bucketRelevanceNote(bucket, currentTask, scores) {
  const visualScore = bucketVisualScore(bucket, scores);
  if (visualScore != null) return `דמיון תמונות ${Math.round(visualScore * 100)}%`;
  const typeScore = bucketTaskTypeScore(bucket, currentTask);
  if (typeScore) return 'מתאים לסוג שבחרת';
  if (bucket.kindLabel) return bucket.kindLabel;
  return `${bucket.photoCount || 1} תמונות`;
}

function orderBucketsForTask(buckets, currentTask, scores = {}) {
  return buckets.slice().sort((a, b) => {
    const visualDelta = (bucketVisualScore(b, scores) || 0) - (bucketVisualScore(a, scores) || 0);
    if (Math.abs(visualDelta) > 0.015) return visualDelta;
    const typeDelta = bucketTaskTypeScore(b, currentTask) - bucketTaskTypeScore(a, currentTask);
    if (typeDelta) return typeDelta;
    return String(b.createdAt || '').localeCompare(String(a.createdAt || ''));
  });
}

function BucketRail({buckets, onAttach, currentTask, compact = false}) {
  const [query, setQuery] = useState('');
  const [showAllBuckets, setShowAllBuckets] = useState(false);
  const [bucketScores, setBucketScores] = useState({});
  useEffect(() => {
    let cancelled = false;
    const currentPhoto = currentTask?.photos?.[0];
    if (!currentPhoto?.src || !buckets.length) {
      setBucketScores({});
      return undefined;
    }
    async function scoreBuckets() {
      const currentFeature = await imageFeature(currentPhoto.src);
      if (!currentFeature || cancelled) {
        if (!cancelled) setBucketScores({});
        return;
      }
      const entries = await Promise.all(buckets.map(async (bucket) => {
        const previews = Array.isArray(bucket.photoPreviews) ? bucket.photoPreviews.slice(0, 4) : [];
        const similarities = await Promise.all(previews.map(async (photo) => featureSimilarity(currentFeature, await imageFeature(photo.src))));
        const best = similarities.filter((value) => value != null).reduce((max, value) => Math.max(max, value), 0);
        return [bucket.id, best];
      }));
      if (!cancelled) setBucketScores(Object.fromEntries(entries.filter(([, score]) => score > 0)));
    }
    scoreBuckets();
    return () => { cancelled = true; };
  }, [currentTask?.id, currentTask?.photos?.[0]?.src, buckets]);
  if (!buckets.length) return null;
  const orderedBuckets = orderBucketsForTask(buckets, currentTask, bucketScores);
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const filteredBuckets = orderedBuckets.filter((bucket) => !terms.length || terms.every((term) => bucketSearchText(bucket).includes(term)));
  const quickBuckets = filteredBuckets.slice(0, 4);
  const visibleBuckets = showAllBuckets || terms.length ? filteredBuckets : quickBuckets;
  return <section className={`bucket-rail visual-buckets ${compact ? 'compact-bucket-rail' : ''}`} aria-label="שייכי לתיק מוצר קיים בסשן">
    <div className="rail-head bucket-rail-head"><span>{orderedBuckets.length} מוצרים שנוצרו בסשן</span><strong>{compact ? 'אפשר לשייך לבאקט קיים' : 'שייכי למוצר שכבר יצרת בסשן'}</strong></div>
    <label className="bucket-search"><span>מצאי באקט מהר</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="חיפוש לפי סוג, שם או מספר" aria-label="חיפוש באקטים שנוצרו בסשן" /></label>
    <div className="bucket-mini-summary"><span>{terms.length ? `${filteredBuckets.length} תוצאות` : 'הכי רלוונטיים מופיעים קודם. אפשר לפתוח את כולם.'}</span></div>
    <div className="bucket-card-list bucket-quick-grid" aria-label="באקטים קיימים">
      {visibleBuckets.map((bucket) => <BucketCard bucket={bucket} key={bucket.id} onClick={onAttach} note={bucketRelevanceNote(bucket, currentTask, bucketScores)} />)}
    </div>
    {!terms.length && orderedBuckets.length > quickBuckets.length ? <button type="button" className="btn secondary-choice bucket-open-all" onClick={() => setShowAllBuckets((value) => !value)}>{showAllBuckets ? 'הצגי רק רלוונטיים' : `פתחי את כל ${orderedBuckets.length} הבאקטים`}</button> : null}
    {terms.length && !filteredBuckets.length ? <div className="empty-buckets"><strong>לא נמצא באקט מתאים</strong><span>נסי שם מוצר, סוג תכשיט או מספר.</span></div> : null}
    {currentTask ? <div className="bucket-hint">בחרי באקט רק אם זו אותה זהות מוצר. אם לא — המשיכי עם מוצר קיים / מוצר חדש.</div> : null}
  </section>;
}

function DesignSessionBucketPicker({buckets, onSelect, currentTask}) {
  if (!buckets.length) return null;
  const orderedBuckets = orderBucketsForTask(buckets, currentTask);
  return <section className="design-session-box" aria-label="עיצוב מתוך הסשן">
    <div className="rail-head bucket-rail-head"><span>{orderedBuckets.length} באקטים מהסשן</span><strong>בחרי מוצר שכבר יצרת כאן</strong></div>
    <div className="bucket-mini-summary"><span>החליקי לצדדים לצפייה בעוד באקטים.</span></div>
    <div className="bucket-card-list bucket-carousel" tabIndex="0" aria-label="באקטים מהסשן">
      {orderedBuckets.map((bucket) => <BucketCard bucket={bucket} key={bucket.id} onClick={onSelect} note="ישמש כתמונת השוואה לעיצוב" />)}
    </div>
  </section>;
}

function BucketAttachConfirm({bucket, task, onCancel, onConfirm}) {
  if (!bucket) return null;
  return <section className="bucket-confirm-box" aria-label="אישור שיוך לבאקט">
    <div className="bucket-confirm-head"><span>אישור לפני שיוך</span><strong>לשייך את התמונה הנוכחית ל“{bucketTitle(bucket)}”?</strong></div>
    <div className="bucket-confirm-compare">
      <div><span>התמונה עכשיו</span>{task.photos.slice(0, 1).map((photo) => <PhotoTile key={`confirm-current-${photoId(photo)}`} photo={photo} />)}</div>
      <div><span>הבאקט שנבחר</span><BucketPreview bucket={bucket} /><strong>{bucketTitle(bucket)}</strong><small>{bucketSubtitle(bucket)} · {bucket.photoCount} תמונות</small></div>
    </div>
    <div className="bucket-confirm-actions"><button className="btn primary" onClick={() => onConfirm(bucket)}>כן, לשייך לבאקט הזה</button><button className="btn tertiary-choice" onClick={onCancel}>ביטול — לבחור אחר</button></div>
  </section>;
}

function DesignReferencePreview({reference}) {
  if (!reference) return null;
  if (reference.image?.src) return <div className="reference-preview"><img src={reference.image.src} alt={reference.label || reference.id || 'מוצר להשוואה'} loading="lazy" /></div>;
  if (reference.bucket?.photoPreviews?.length) return <BucketPreview bucket={reference.bucket} />;
  return <div className="reference-preview empty">אין תמונת השוואה</div>;
}


function downloadJson(filename, text) {
  const blob = new Blob([text], {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function batchHref(id) {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set('batch', id);
    url.searchParams.delete('qa');
    return `${url.pathname}${url.search}`;
  } catch { return `/?batch=${encodeURIComponent(id)}`; }
}

function BatchQueue({currentBatchId}) {
  const open = BATCH_INDEX.filter((batch) => batch.reviewable_assets > 0);
  if (!open.length) return null;
  return <section className="batch-queue panel" aria-label="אצוות תמונות">
    <div className="batch-queue-head"><div><span>תור האצוות</span><strong>{open.length} תאריכי צילום עם עבודה</strong></div><small>{GLOBAL_COVERAGE.reviewable_web_assets || open.reduce((sum, batch) => sum + batch.reviewable_assets, 0)} תמונות ממתינות לזהות</small></div>
    <div className="batch-list">
      {open.map((batch) => <a className={`batch-row ${batch.batch_id === currentBatchId ? 'active' : ''}`} href={batchHref(batch.batch_id)} key={batch.batch_id}>
        <span><strong>{batch.label}</strong><small>{batch.review_cards} פריטים · {batch.reviewable_assets} תמונות</small></span>
        <b>{batch.batch_id === currentBatchId ? 'פתוח עכשיו' : 'פתיחה'}</b>
      </a>)}
    </div>
  </section>;
}

function StartScreen({taskCount, photoCount, realStats, demoMode, noReviewMode, onStart, onHelp}) {
  const batchLabel = SELECTED_BATCH?.label || DATASET_STATS.label || BATCH_ID;
  return <main>
    <section className="hero panel">
      <div className="eyebrow">{noReviewMode ? 'האצווה הושלמה' : demoMode ? 'אין תמונות פתוחות באצווה' : `אצווה ${batchLabel}`}</div>
      <h2>סידור תמונות לפי מוצר</h2>
      <p>{noReviewMode ? 'כל התמונות באצווה כבר קיבלו יעד.' : 'עוברים פריט־פריט. מחליטים אם זה מוצר קיים, מוצר חדש או לא בטוחה; את שאר הפרטים משלימים אחר כך בוואטסאפ.'}</p>
      <div className="stats-grid"><div><strong>{realStats.expected}</strong><span>תמונות באצווה</span></div><div><strong>{photoCount}</strong><span>לבדיקה עכשיו</span></div><div><strong>{realStats.autoAccounted}</strong><span>כבר נותבו</span></div><div><strong>{realStats.blocked}</strong><span>דורשות הכנה</span></div></div>
      <div className="identity-thesis"><strong>המטרה</strong><span>בסיום, לכל תמונה יהיה מוצר, באקט זמני או סימון ברור להמשך.</span></div>
      {demoMode && !noReviewMode ? <p className="quiet-note important-note">אין באצווה הזו תמונות שממתינות להחלטת זהות. אפשר לבחור אצווה אחרת מהתור.</p> : null}
      {noReviewMode || demoMode ? null : <button className="btn primary start-btn" onClick={onStart}>התחלת האצווה · {taskCount} פריטים</button>}
      <button className="link-button" onClick={onHelp}>איך עובדים כאן?</button>
    </section>
    <BatchQueue currentBatchId={BATCH_ID} />
    <section className="panel next-step-panel">
      <h3>מה קורה אחרי הסידור?</h3>
      <ol className="step-list">
        <li><strong>1 · נשמר</strong><span>כל בחירה נשמרת לשרת ואפשר להמשיך ממכשיר אחר.</span></li>
        <li><strong>2 · נבדק</strong><span>HAL בודק כיסוי מלא ומעביר כל זהות למסלול המתאים.</span></li>
        <li><strong>3 · ממשיכים</strong><span>שאלות על שם, מחיר ופרטים מגיעות בוואטסאפ רק כשצריך.</span></li>
      </ol>
    </section>
  </main>;
}

function ProgressSteps({stage}) {
  const active = stage === 'cluster_photos' ? 1 : ['product_identity', 'existing_product_selection', 'new_identity', 'new_design_question', 'design_reference_selection', 'same_design'].includes(stage) ? 2 : 3;
  return <div className="progress-steps" aria-label="שלבי התהליך">
    {['הכנה', 'תמונות', 'זהות', 'וואטסאפ'].map((label, index) => <div className={`flow-step ${index <= active ? 'active' : ''}`} key={label}><strong>{index}</strong><span>{label}</span></div>)}
  </div>;
}

function SplitReview({task, onSave, onBack}) {
  const [selected, setSelected] = useState(() => new Set(task.photos.slice(0, 2).map(photoId)));
  const selectedPhotos = task.photos.filter((photo) => selected.has(photoId(photo)));
  const soloPhotos = task.photos.filter((photo) => !selected.has(photoId(photo)));
  function toggle(photo) {
    const id = photoId(photo);
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    setSelected(next);
  }
  function saveMixed() {
    const clusters = [];
    if (selectedPhotos.length >= 2) clusters.push({type: 'same_product_group', photoIds: selectedPhotos.map(photoId)});
    for (const photo of soloPhotos) clusters.push({type: 'solo_product_candidate', photoIds: [photoId(photo)]});
    onSave({clusters});
  }
  return <section className="group-card panel split-review">
    <div className="kicker">פיצול לדליי מוצר</div>
    <h2>בחרי תמונות ששייכות לאותו מוצר</h2>
    <p className="meta large">מה שלא מסומן יהפוך לכרטיס זהות נפרד. אף תמונה לא נעלמת.</p>
    <div className="split-grid">{task.photos.map((photo) => <button type="button" className={`split-photo ${selected.has(photoId(photo)) ? 'selected' : ''}`} key={photoId(photo)} onClick={() => toggle(photo)}><img src={photo.src} alt={photo.sourcePath || photo.id || 'תמונה'} loading="lazy" /><span>{selected.has(photoId(photo)) ? 'בקבוצה' : 'נפרד'}</span></button>)}</div>
    <div className="actions decision-actions split-actions"><button className="btn primary main-choice" onClick={saveMixed} disabled={selectedPhotos.length < 2}>צור כרטיסי זהות מהפיצול</button><button className="btn tertiary-choice" onClick={onBack}>חזרה</button></div>
  </section>;
}

function CatalogTypeFilter({label, value, onChange}) {
  return <div className="catalog-type-filter" role="group" aria-label={label}>
    <span>{label}</span>
    <button type="button" className={!value ? 'active' : ''} onClick={() => onChange('')}>הכל</button>
    {CATALOG_TYPE_FILTERS.map((type) => <button type="button" className={value === type ? 'active' : ''} key={type} onClick={() => onChange(type)}>{type} · {catalogTypeCount(type)}</button>)}
  </div>;
}

function MatchCandidateCard({candidate, task, index, onSameProduct, onSameDesign, onReject}) {
  const currentPhoto = task.photos[0];
  const score = candidateConfidence(candidate);
  const pct = score == null ? null : Math.round(score * 100);
  const detectorRank = candidate.rank || candidate.detectorEvidence?.rank;
  return <article className="match-candidate-card">
    <div className="match-score"><span>{confidenceLabel(score)}</span>{pct == null ? <strong>אישור ידני</strong> : <strong>{pct}%</strong>}</div>
    <div className="match-compare">
      <div><span>התמונה עכשיו</span>{currentPhoto ? <img src={currentPhoto.src} alt="התמונה לבדיקה" loading={index === 0 ? 'eager' : 'lazy'} /> : null}</div>
      <div><span>התאמה מוצעת</span>{candidate.image?.src ? <img src={candidate.image.src} alt={candidate.label} loading="lazy" /> : <span className="catalog-no-image">אין תמונה</span>}</div>
    </div>
    <div className="match-copy"><strong>{candidate.label}</strong><span>{detectorRank ? `התאמה #${detectorRank} · ${candidate.kind || candidate.type || candidate.meta || 'מוצר מהקטלוג'}` : (candidate.kind || candidate.type || candidate.meta || 'מוצר מהקטלוג')}</span></div>
    <div className="match-actions">
      <button className="btn primary" onClick={() => onSameProduct(candidate)}>אותו מוצר</button>
      <button className="btn secondary-choice" onClick={() => onSameDesign(candidate)}>רק אותו עיצוב</button>
      <button className="btn tertiary-choice" onClick={() => onReject(candidate)}>לא זה</button>
    </div>
  </article>;
}

function MatchCandidateList({title = 'התאמות מוצעות', task, candidates, skippedIds, onReject, onSameProduct, onSameDesign}) {
  const visible = candidates.filter((candidate) => !skippedIds.includes(candidate.id));
  if (!visible.length) return null;
  return <div className="match-candidate-list">
    <div className="match-list-head"><strong>{title}</strong><span>החליקי לצדדים ובחרי לפי השוואת התמונות</span></div>
    <div className="match-candidate-carousel" tabIndex="0" aria-label={title}>
      {visible.map((candidate, index) => <MatchCandidateCard key={`${title}-${candidate.id}`} candidate={candidate} task={task} index={index} onSameProduct={onSameProduct} onSameDesign={onSameDesign} onReject={onReject} />)}
    </div>
  </div>;
}

function ReviewCard({task, buckets, onAction}) {
  const [manualProduct, setManualProduct] = useState('');
  const [manualDesignReference, setManualDesignReference] = useState('');
  const [pendingAttachBucket, setPendingAttachBucket] = useState(null);
  const [catalogQuery, setCatalogQuery] = useState('');
  const [designQuery, setDesignQuery] = useState('');
  const [showAllCatalog, setShowAllCatalog] = useState(false);
  const [showAllDesignCatalog, setShowAllDesignCatalog] = useState(false);
  const [skippedCandidateIds, setSkippedCandidateIds] = useState([]);
  const [catalogTypeFilter, setCatalogTypeFilter] = useState(() => defaultJewelryTypeFilter(task));
  const [designTypeFilter, setDesignTypeFilter] = useState(() => defaultJewelryTypeFilter(task));
  const [photoPinned, setPhotoPinned] = useState(false);
  const showCurrentPhotoBlock = task.stage !== 'same_design';
  useEffect(() => {
    const defaultFilter = defaultJewelryTypeFilter(task);
    if (task.stage === 'existing_product_selection') setCatalogTypeFilter(defaultFilter);
    if (task.stage === 'design_reference_selection') setDesignTypeFilter(defaultFilter);
    setPendingAttachBucket(null);
    setSkippedCandidateIds([]);
  }, [task.id, task.stage, task.pendingProductType, task.kind]);
  useEffect(() => {
    if (!pendingAttachBucket) return undefined;
    const timer = window.setTimeout(() => {
      document.querySelector('.bucket-confirm-box')?.scrollIntoView({block: 'center', behavior: 'smooth'});
    }, 40);
    return () => window.clearTimeout(timer);
  }, [pendingAttachBucket]);
  useEffect(() => {
    const block = document.querySelector('[data-current-photo-block="true"]');
    if (!block || !showCurrentPhotoBlock) {
      setPhotoPinned(false);
      return undefined;
    }
    const updateFromScroll = () => {
      const rect = block.getBoundingClientRect();
      const floating = document.querySelector('[data-floating-current-photo="true"]');
      const shouldShow = rect.top < -20 && window.scrollY > 260;
      floating?.classList.toggle('is-visible', shouldShow);
      block.classList.toggle('floating-source-hidden', shouldShow);
      if (floating) {
        floating.style.opacity = shouldShow ? '1' : '0';
        floating.style.visibility = shouldShow ? 'visible' : 'hidden';
        floating.style.transform = shouldShow ? 'translateX(-50%) translateY(0) scale(1)' : 'translateX(-50%) translateY(-10px) scale(.94)';
      }
      setPhotoPinned(shouldShow);
    };
    if ('IntersectionObserver' in window) {
      const observer = new IntersectionObserver(() => updateFromScroll(), {threshold: [0, 0.2, 0.6, 1]});
      observer.observe(block);
      window.addEventListener('scroll', updateFromScroll, {passive: true});
      updateFromScroll();
      return () => {
        observer.disconnect();
        window.removeEventListener('scroll', updateFromScroll);
      };
    }
    window.addEventListener('scroll', updateFromScroll, {passive: true});
    updateFromScroll();
    return () => window.removeEventListener('scroll', updateFromScroll);
  }, [task.id, task.stage, showCurrentPhotoBlock]);
  const exactProductCandidates = task.existingCandidates?.length ? task.existingCandidates : [task.existingCandidate].filter(Boolean);
  const hasHistoricalVisualSuggestion = exactProductCandidates.some((candidate) => candidate.source === 'historical_drive_visual_candidate');
  const filteredExactProductCandidates = filterCandidatesByJewelryType(exactProductCandidates, catalogTypeFilter);
  const productCandidates = filteredExactProductCandidates;
  const catalogMatches = searchCatalogProducts(catalogQuery, task, exactProductCandidates, showAllCatalog, catalogTypeFilter);
  const existingSelectionCandidates = exactProductCandidates.length ? productCandidates : [];
  const catalogExtraMatches = showAllCatalog && !catalogQuery.trim() ? catalogMatches : catalogMatches.slice(0, 6);
  const designCatalogMatches = searchCatalogProducts(designQuery, task, [], showAllDesignCatalog, designTypeFilter);
  const designReferenceCandidates = designCatalogMatches.length ? designCatalogMatches : (designQuery.trim() ? [] : fallbackProductSuggestions(task, designTypeFilter));
  const catalogAvailableCount = catalogTypeFilter ? catalogTypeCount(catalogTypeFilter) : CATALOG_PRODUCT_INDEX.length;
  const designAvailableCount = designTypeFilter ? catalogTypeCount(designTypeFilter) : CATALOG_PRODUCT_INDEX.length;
  const primaryExistingMatches = existingSelectionCandidates.slice(0, 5);
  const primaryDesignMatches = designReferenceCandidates.slice(0, 5);
  const relatedDesignProducts = task.stage === 'same_design' ? relatedFamilyProducts(task.designReference) : [];
  const skipCandidate = (candidate) => setSkippedCandidateIds((previous) => previous.includes(candidate.id) ? previous : [...previous, candidate.id]);
  const chooseSameProduct = (candidate) => onAction('product_existing_selected', candidatePayload(candidate));
  const chooseSameDesign = (candidate) => onAction('design_reference_selected', candidatePayload(candidate));
  const openAttachConfirm = (bucket) => {
    setPendingAttachBucket(bucket);
    window.requestAnimationFrame?.(() => {
      document.querySelector('.bucket-confirm-box')?.scrollIntoView({block: 'center', behavior: 'smooth'});
    });
  };
  return <section className="group-card panel">
    <FloatingCurrentPhoto task={task} visible={showCurrentPhotoBlock} />
    <ProgressSteps stage={task.stage} />
    <div className="group-head simple hero-question"><div><div className="kicker">פריט לבדיקה · {task.photos.length} תמונות</div><h2>{taskQuestion(task)}</h2><div className="meta large">בחרי פעולה אחת.</div></div><span className="step-badge">פריט</span></div>
    {!['product_identity', 'cluster_photos'].includes(task.stage) ? <button type="button" className="inline-back-choice" onClick={() => onAction('back_to_identity')}>חזרה לבחירה הראשית</button> : null}

    {showCurrentPhotoBlock ? <div className={`current-photo-block sticky-hero-photo ${photoPinned ? 'is-pinned floating-source-hidden' : ''}`} data-current-photo-block="true">
      <div className="section-title"><span>{photoPinned ? 'התמונה עכשיו' : 'התמונה לבדיקה'}</span></div>
      <div className="thumbs review-thumbs">{task.photos.map((photo) => <PhotoTile key={photoId(photo)} photo={photo} />)}</div>
    </div> : null}

    {task.stage === 'cluster_photos' ? <div className="actions decision-actions"><button className="btn primary main-choice" onClick={() => onAction('cluster_same')}>כן, אותו תכשיט — ליצור זהות</button><button className="btn secondary-choice" onClick={() => onAction('cluster_split')}>לא, לפצל לדליים</button><button className="btn tertiary-choice" onClick={() => onAction('unsure')}>לא בטוחה</button></div> : null}

    {task.stage === 'product_identity' ? <>
      <div className="identity-actions">
        <button className="identity-choice existing" onClick={() => onAction('identity_existing')}>מוצר קיים</button>
        <button className="identity-choice new" onClick={() => onAction('identity_new')}>מוצר חדש</button>
        <button className="identity-choice unsure" onClick={() => onAction('unsure')}>לא בטוחה</button>
      </div>
      <BucketAttachConfirm bucket={pendingAttachBucket} task={task} onCancel={() => setPendingAttachBucket(null)} onConfirm={(bucket) => onAction('attach_to_bucket', {bucketId: bucket.id, label: bucketTitle(bucket), attachedBucketTitle: bucketTitle(bucket)})} />
      <BucketRail key={`identity-buckets-${task.id}-${buckets.length}`} buckets={buckets} currentTask={task} compact onAttach={openAttachConfirm} />
    </> : null}

    {task.stage === 'existing_product_selection' ? <div className="product-list catalog-finder unified-match-flow">
      <div className="suggestion-note"><strong>{hasHistoricalVisualSuggestion ? 'מוצר קיים שכדאי להשוות' : (exactProductCandidates.length ? 'התאמות מהגלאי' : 'אין התאמה אוטומטית אמינה')}</strong><span>{hasHistoricalVisualSuggestion ? 'מצאנו מועמד מהקטלוג הישן לפי השוואת תמונות. זו הצעה בלבד — אשרי אם זה אותו מוצר, רק אותו עיצוב, או לא זה.' : (exactProductCandidates.length ? 'הגלאי מציע מועמדים לפי דמיון תמונות. דליה עדיין מאשרת ידנית אם זה אותו תכשיט, רק אותו עיצוב, או לא זה.' : 'חפשי לפי מספר/שם מוצר או פתחי את כל הקטלוג. לא מוצגים מועמדים אקראיים כ“דירוג”.')}</span></div>
      <BucketAttachConfirm bucket={pendingAttachBucket} task={task} onCancel={() => setPendingAttachBucket(null)} onConfirm={(bucket) => onAction('attach_to_bucket', {bucketId: bucket.id, label: bucketTitle(bucket), attachedBucketTitle: bucketTitle(bucket)})} />
      <BucketRail key={`existing-buckets-${task.id}-${buckets.length}`} buckets={buckets} currentTask={task} onAttach={openAttachConfirm} />
      <MatchCandidateList task={task} candidates={primaryExistingMatches} skippedIds={skippedCandidateIds} onReject={skipCandidate} onSameProduct={chooseSameProduct} onSameDesign={chooseSameDesign} />
      {exactProductCandidates.length && (showAllCatalog || catalogQuery.trim()) && catalogExtraMatches.length ? <MatchCandidateList title="עוד אפשרויות מהקטלוג" task={task} candidates={catalogExtraMatches} skippedIds={skippedCandidateIds} onReject={skipCandidate} onSameProduct={chooseSameProduct} onSameDesign={chooseSameDesign} /> : null}
      <label className="catalog-search"><span>חיפוש בקטלוג</span><input value={catalogQuery} onChange={(event) => { setCatalogQuery(event.target.value); if (event.target.value.trim()) setShowAllCatalog(false); }} placeholder="מספר מוצר או שם" aria-label="חיפוש בקטלוג מוצרים קיימים" /></label>
      <CatalogTypeFilter label="סוג תכשיט" value={catalogTypeFilter} onChange={(type) => { setCatalogTypeFilter(type); setShowAllCatalog(false); }} />
      <div className="catalog-count">{CATALOG_PRODUCT_INDEX.length ? `${catalogAvailableCount} מתוך ${CATALOG_PRODUCT_INDEX.length} מוצרים זמינים${catalogTypeFilter ? ` · ${catalogTypeFilter}` : ''}` : 'קטלוג לדוגמה בלבד'}</div>
      {!catalogQuery.trim() && CATALOG_PRODUCT_INDEX.length ? <button className="btn secondary-choice catalog-open-all" onClick={() => setShowAllCatalog((value) => !value)}>{showAllCatalog ? 'הצגי פחות' : `הציגי עוד אפשרויות`}</button> : null}
      {showAllCatalog && !catalogQuery.trim() ? <MatchCandidateList title="כל האפשרויות" task={task} candidates={catalogMatches} skippedIds={skippedCandidateIds} onReject={skipCandidate} onSameProduct={chooseSameProduct} onSameDesign={chooseSameDesign} /> : null}
      {!existingSelectionCandidates.filter((candidate) => !skippedCandidateIds.includes(candidate.id)).length ? <div className="manual-product-box"><strong>לא מצאת התאמה?</strong><span>אפשר לחפש לפי שם או מספר, או לסמן לבדיקה.</span></div> : null}
      <div className="manual-product-box"><strong>אם את זוכרת מוצר שלא מופיע</strong><input value={manualProduct} onChange={(event) => setManualProduct(event.target.value)} placeholder="לדוגמה: R053 / טבעת פרחים זהב" aria-label="שם או מספר מוצר קיים" /><button className="btn primary" disabled={!manualProduct.trim()} onClick={() => onAction('product_existing_selected', {productId: 'manual', label: manualProduct.trim(), manual: true, unresolvedTarget: true})}>לשמור לבדיקה</button></div>
      <div className="actions decision-actions"><button className="btn secondary-choice" onClick={() => onAction('identity_new')}>אין התאמה — מוצר חדש</button><button className="btn tertiary-choice" onClick={() => onAction('unsure')}>לא בטוחה</button></div>
    </div> : null}

    {task.stage === 'new_identity' ? <div className="option-grid"><div className="suggestion-note wide"><strong>יוצרים זהות זמנית, לא טופס מוצר מלא</strong><span>הפרטים, מחיר ואישור ימשיכו בוואטסאפ אחרי שהזהות קיימת.</span></div>{TYPE_OPTIONS.map((type) => <button className="btn secondary-choice" key={type} onClick={() => onAction('new_identity_type', {productType: type})}>{type}</button>)}</div> : null}

    {task.stage === 'new_design_question' ? <div className="option-grid"><div className="suggestion-note wide"><strong>עכשיו אפשר לבדוק עיצוב</strong><span>קודם החלטנו שזה מוצר חדש. עכשיו רק אם הוא דומה לעיצוב קיים — נבחר רפרנס ונשווה תמונות.</span></div><button className="btn secondary-choice" onClick={() => onAction('new_product_plain')}>לא — מוצר חדש לגמרי</button><button className="btn secondary-choice" onClick={() => onAction('identity_same_design')}>כן — דומה לעיצוב קיים</button><button className="btn tertiary-choice wide" onClick={() => onAction('unsure')}>לא בטוחה</button></div> : null}

    {task.stage === 'design_reference_selection' ? <div className="product-list catalog-finder design-reference-flow unified-match-flow">
      <div className="suggestion-note"><strong>התאמות עיצוב מוצעות</strong><span>בחרי אם זה אותו מוצר, רק אותו עיצוב, או לא זה.</span></div>
      <DesignSessionBucketPicker buckets={buckets} currentTask={task} onSelect={(bucket) => onAction('design_session_bucket_selected', {referenceBucketId: bucket.id, label: bucketTitle(bucket), bucket})} />
      <MatchCandidateList task={task} candidates={primaryDesignMatches} skippedIds={skippedCandidateIds} onReject={skipCandidate} onSameProduct={chooseSameProduct} onSameDesign={chooseSameDesign} />
      <label className="catalog-search"><span>חיפוש עיצוב</span><input value={designQuery} onChange={(event) => { setDesignQuery(event.target.value); if (event.target.value.trim()) setShowAllDesignCatalog(false); }} placeholder="שם או מספר מוצר" aria-label="חיפוש מוצר להשוואת עיצוב" /></label>
      <CatalogTypeFilter label="סוג תכשיט" value={designTypeFilter} onChange={(type) => { setDesignTypeFilter(type); setShowAllDesignCatalog(false); }} />
      <div className="catalog-count">{CATALOG_PRODUCT_INDEX.length ? `${designAvailableCount} מתוך ${CATALOG_PRODUCT_INDEX.length} עיצובים זמינים${designTypeFilter ? ` · ${designTypeFilter}` : ''}` : 'קטלוג לדוגמה בלבד'}</div>
      {!designQuery.trim() && CATALOG_PRODUCT_INDEX.length ? <button className="btn secondary-choice catalog-open-all" onClick={() => setShowAllDesignCatalog((value) => !value)}>{showAllDesignCatalog ? 'הצגי פחות' : 'הציגי עוד אפשרויות'}</button> : null}
      {showAllDesignCatalog && !designQuery.trim() ? <MatchCandidateList title="כל האפשרויות" task={task} candidates={designCatalogMatches} skippedIds={skippedCandidateIds} onReject={skipCandidate} onSameProduct={chooseSameProduct} onSameDesign={chooseSameDesign} /> : null}
      {!designReferenceCandidates.filter((candidate) => !skippedCandidateIds.includes(candidate.id)).length ? <div className="manual-product-box"><strong>לא מצאת עיצוב מתאים?</strong><span>אפשר לחפש, ליצור עיצוב חדש, או לשמור רפרנס ידני לבדיקה.</span></div> : null}
      <div className="manual-product-box"><strong>רפרנס ידני לבדיקה</strong><input value={manualDesignReference} onChange={(event) => setManualDesignReference(event.target.value)} placeholder="לדוגמה: דומה לטבעת R053 אבל אבן אחרת" aria-label="עיצוב ידני להשוואה" /><button className="btn primary" disabled={!manualDesignReference.trim()} onClick={() => onAction('design_reference_selected', {label: manualDesignReference.trim(), unresolvedDesignReference: true})}>לשמור רפרנס</button></div>
      <div className="actions decision-actions"><button className="btn secondary-choice" onClick={() => onAction('identity_new')}>אין עיצוב מתאים — עיצוב חדש</button><button className="btn tertiary-choice" onClick={() => onAction('unsure')}>לא בטוחה</button></div>
    </div> : null}

    {task.stage === 'same_design' ? <div className="option-grid same-design-diff"><div className="suggestion-note wide compact"><strong>שתי תמונות, החלטה אחת</strong><span>מה ההבדל שרואים בין החדש לרפרנס?</span></div><div className="design-comparison wide"><div><span>התמונה החדשה</span>{task.photos.slice(0, 2).map((photo) => <PhotoTile key={`new-${photoId(photo)}`} photo={photo} />)}</div><div><span>הרפרנס</span><DesignReferencePreview reference={task.designReference} /><strong>{task.designReference?.label || 'רפרנס לא ידוע'}</strong></div></div>{relatedDesignProducts.length ? <div className="family-context wide"><strong>עוד מאותה משפחה</strong><div className="family-context-grid">{relatedDesignProducts.map((candidate) => <div className="family-context-card" key={`family-${candidate.id}`}>{candidate.image?.src ? <img src={candidate.image.src} alt={candidate.label} loading="lazy" /> : <span className="catalog-no-image">אין תמונה</span>}<small>{candidate.label}</small></div>)}</div></div> : null}{DIFFERENCE_OPTIONS.map((difference) => <button className="btn secondary-choice" key={difference} onClick={() => onAction('same_design_difference', {visibleDifference: difference})}>{difference}</button>)}<button className="btn tertiary-choice wide" onClick={() => onAction('identity_same_design')}>לחזור ולבחור רפרנס אחר</button></div> : null}

  </section>;
}

function Help({onClose}) {
  return <main><section className="panel help-panel"><h2>מה המטרה?</h2><p>הכלי הזה לא מחליף וואטסאפ ולא מבקש מחיר/תיאור. הוא רק יוצר זהות מוצר מהתמונות.</p><ol><li>האם התמונות הן של אותו תכשיט?</li><li>במסך הראשון בוחרים רק: מוצר קיים, מוצר חדש או לא בטוחה.</li><li>שאלות עיצוב, רפרנס וכפילויות מופיעות רק אחרי הכיוון הראשוני.</li><li>אפשר ללחוץ שמירה בכל רגע. השרת מקבל את העבודה; JSON נשאר רק כגיבוי בדיקה.</li></ol><button className="btn primary" onClick={onClose}>חזרה לבדיקה</button></section></main>;
}

function Summary({decisions, buckets, tasks, sourceAssets, onReset, onEditDecision, onSave, syncStatus}) {
  const counts = decisions.reduce((acc, d) => { acc[d.outcome] = (acc[d.outcome] || 0) + 1; return acc; }, {});
  const whatsappFollowups = decisions.filter((decision) => decision.payload.needsWhatsappFollowup).length;
  const bundle = exportPacketBundle({datasetVersion: DATASET_VERSION, batchId: BATCH_ID, sourceAssets, decisions, buckets});
  const packetText = JSON.stringify(bundle, null, 2);
  const nextBatch = BATCH_INDEX.find((batch) => batch.reviewable_assets > 0 && batch.batch_id !== BATCH_ID);
  function downloadPackets() {
    downloadJson(`${BATCH_ID}-identity-packets.json`, packetText);
  }
  return <main><CompletedDecisionStrip decisions={decisions} onEdit={onEditDecision} /><section className="panel empty-state done-state"><div className="done-icon">✓</div><h2>האצווה הסתיימה</h2><p>כל התמונות באצווה קיבלו החלטה. שמרי לשרת ואז אפשר לעבור לתאריך הבא.</p><div className="summary-grid"><div><strong>{tasks.length}</strong><span>פריטים</span></div><div><strong>{buckets.length}</strong><span>זהויות מוצר</span></div><div><strong>{(counts.new_product_identity || 0) + (counts.same_design_different_product || 0)}</strong><span>חדשים/עיצוב</span></div><div><strong>{whatsappFollowups}</strong><span>המשך וואטסאפ</span></div></div><div className="validation-box"><strong>{bundle.validation.packets.valid && bundle.validation.coverage.valid ? syncStatusLabel(syncStatus) : 'יש חסימות במבנה'}</strong><span>{bundle.validation.coverage.accounted}/{bundle.validation.coverage.expected} תמונות לבדיקה מכוסות</span></div><button className="btn primary" onClick={onSave}>שמירה לשרת</button>{nextBatch ? <a className="btn secondary-choice next-batch-link" href={batchHref(nextBatch.batch_id)}>מעבר לאצווה הבאה · {nextBatch.label}</a> : null}<button className="btn tertiary-choice" onClick={downloadPackets}>הורדת גיבוי JSON</button><button className="btn tertiary-choice" onClick={onReset}>פתיחה מחדש של האצווה</button></section><BatchQueue currentBatchId={BATCH_ID} /></main>;
}

function ExportPanel({decisions, buckets, sourceAssets, tasks, onClose, onSave, syncStatus}) {
  const counts = decisions.reduce((acc, d) => { acc[d.outcome] = (acc[d.outcome] || 0) + 1; return acc; }, {});
  const bundle = exportPacketBundle({datasetVersion: DATASET_VERSION, batchId: BATCH_ID, sourceAssets, decisions, buckets});
  const packetText = JSON.stringify(bundle, null, 2);
  return <main><section className="panel export-panel"><h2>שמירת עבודה</h2><p>לחצי שמירה והשרת יקבל את העבודה. JSON הוא רק גיבוי לבדיקה אם נצטרך.</p><div className="summary-grid"><div><strong>{decisions.length}</strong><span>החלטות</span></div><div><strong>{buckets.length}</strong><span>זהויות מוצר</span></div><div><strong>{bundle.validation.coverage.accounted}/{bundle.validation.coverage.expected}</strong><span>תמונות מכוסות</span></div><div><strong>{(counts.not_sure || 0)}</strong><span>לא בטוחה</span></div></div><div className="validation-box"><strong>{syncStatusLabel(syncStatus)}</strong><span>{tasks.length - decisions.length} כרטיסים עדיין פתוחים</span></div><button className="btn primary" onClick={onSave}>שמירה לשרת</button><button className="btn secondary-choice" onClick={() => downloadJson(`${BATCH_ID}-identity-progress.json`, packetText)}>הורדת גיבוי JSON</button><button className="btn tertiary-choice" onClick={onClose}>חזרה לבדיקה</button></section></main>;
}

function DecisionNotice({notice}) {
  if (!notice) return null;
  const isAttach = notice.outcome === 'attach_to_session_bucket';
  return <div className={`decision-notice ${isAttach ? 'attach' : ''}`} role="status"><strong>{isAttach ? 'שויך לבאקט שבחרת' : 'הבחירה נשמרה'}</strong><span>{isAttach ? `עברנו לפריט הבא. הבאקט שנבחר: ${notice.label}` : `עברנו לפריט הבא: ${notice.label}`}</span></div>;
}

function decisionLabel(decision) {
  const payload = decision.payload || {};
  if (decision.outcome === 'existing_product_images') return `מוצר קיים · ${payload.label || payload.productId || 'לבדיקה'}`;
  if (decision.outcome === 'new_product_identity') return `מוצר חדש · ${payload.productType || 'סוג לא בטוח'}`;
  if (decision.outcome === 'same_design_different_product') return `עיצוב דומה · ${payload.visibleDifference || 'הבדל לבדיקה'}`;
  if (decision.outcome === 'attach_to_session_bucket') return `שויך לבאקט · ${payload.attachedBucketTitle || payload.label || payload.bucketId}`;
  if (decision.outcome === 'not_sure') return 'לא בטוחה';
  return 'החלטה נשמרה';
}

function CompletedDecisionStrip({decisions, onEdit}) {
  const recent = decisions.slice(-5).reverse();
  if (!recent.length) return null;
  return <section className="completed-strip" aria-label="החלטות אחרונות לעריכה">
    <div className="completed-strip-head"><strong>החלטות אחרונות</strong><span>טעית? אפשר לערוך בלי להתחיל מחדש.</span></div>
    <div className="completed-decision-list">
      {recent.map((decision) => <button type="button" className="completed-decision-card" key={`${decision.taskId}-${decision.decidedAt}`} onClick={() => onEdit(decision.taskId)}><span>{decisionLabel(decision)}</span><small>עריכה</small></button>)}
    </div>
  </section>;
}

function Header({started, remaining, photos, completed, total, onReset, onHelp, onExport, onSave, syncStatus, sessionId}) {
  const progress = total ? Math.round((completed / total) * 100) : 0;
  const statusText = total ? (started ? `${remaining} נשארו` : 'מוכן') : 'אין תור';
  return <><header className="app-header compact-app-header" title={`${syncStatusLabel(syncStatus)} · ${sessionId || ''}`}>
    <div className="header-main"><h1>זהות מוצר</h1><div className="header-status-stack"><span className="header-status backend-sync">{statusText} · {photos} תמונות</span><small className={`sync-indicator sync-${syncStatus}`}>{syncStatusLabel(syncStatus)}</small></div></div>
    <div className="progressbar compact-progress" aria-label={`התקדמות ${progress}%`}><span style={{width: `${progress}%`}} /></div>
  </header><footer className="mobile-footer compact-actions">{total ? <button onClick={onReset}>איפוס</button> : null}{started ? <button className="footer-save" onClick={onSave}>שמירה</button> : null}<button onClick={onHelp}>עזרה</button></footer></>;
}

function App() {
  const realTasks = useMemo(buildInitialTasks, []);
  const demoTasks = useMemo(makeDemoTasks, []);
  const realStats = useMemo(realBatchStats, []);
  const demoMode = (() => { try { return new URLSearchParams(window.location.search).get('demo') === '1'; } catch { return false; } })();
  const noReviewMode = !demoMode && realTasks.length === 0;
  const initialTasks = demoMode ? demoTasks : realTasks;
  const saved = useMemo(loadSaved, []);
  const sessionConfig = useMemo(() => sharedSessionConfig(BATCH_ID), []);
  const [started, setStarted] = useState(saved?.started || false);
  const [tasks, setTasks] = useState(saved?.tasks || initialTasks);
  const [decisions, setDecisions] = useState(saved?.decisions || []);
  const [buckets, setBuckets] = useState(saved?.buckets || []);
  const [syncStatus, setSyncStatus] = useState('checking');
  const [showHelp, setShowHelp] = useState(false);
  const [showExport, setShowExport] = useState(false);
  const [splitTask, setSplitTask] = useState(null);
  const [lastDecisionNotice, setLastDecisionNotice] = useState(null);
  const revisionRef = useRef(Number(saved?.revision || 0));
  const persistQueueRef = useRef(Promise.resolve());

  useEffect(() => {
    let cancelled = false;
    loadRemoteState(sessionConfig, BATCH_ID).then((result) => {
      if (cancelled) return;
      setSyncStatus(result.status === 'loaded' || result.status === 'ready_empty' ? result.status : (result.status || 'local'));
      if (result.state?.datasetVersion === DATASET_VERSION && Array.isArray(result.state.tasks)) {
        revisionRef.current = Number(result.state.revision || 0);
        setStarted(Boolean(result.state.started));
        setTasks(result.state.tasks);
        setDecisions(result.state.decisions || []);
        setBuckets(result.state.buckets || []);
        saveState(result.state);
      }
    });
    return () => { cancelled = true; };
  }, [sessionConfig]);
  const completedIds = new Set(decisions.map((decision) => decision.taskId));
  const currentTask = tasks.find((task) => !completedIds.has(task.id));
  const remaining = tasks.filter((task) => !completedIds.has(task.id));
  const completedCount = tasks.length - remaining.length;
  const photoCount = tasks.reduce((sum, task) => sum + task.photos.length, 0);

  function stateSnapshot(next = {}) {
    return {started, tasks, decisions, buckets, datasetVersion: DATASET_VERSION, batchId: BATCH_ID, sourceAssets: SOURCE_ASSETS, no_live_writes: true, ...next};
  }
  function persist(next = {}) {
    const revision = revisionRef.current + 1;
    revisionRef.current = revision;
    const state = stateSnapshot({...next, revision});
    saveState(state);
    setSyncStatus('saving');
    const save = () => saveRemoteState(sessionConfig, BATCH_ID, state).then((result) => {
      setSyncStatus(result.status || 'local');
      return result;
    });
    persistQueueRef.current = persistQueueRef.current.catch(() => null).then(save);
    return persistQueueRef.current;
  }
  function manualSave() {
    setSyncStatus('saving');
    return persist();
  }
  function start() { setStarted(true); persist({started: true}); }
  function reset() {
    if (!window.confirm('לפתוח מחדש את האצווה? כל הבחירות השמורות באצווה הזו יימחקו.')) return;
    localStorage.removeItem(STORAGE_KEY);
    deleteRemoteState(sessionConfig, BATCH_ID).then((result) => setSyncStatus(result.status || 'local'));
    setStarted(false); setTasks(initialTasks); setDecisions([]); setBuckets([]); setSplitTask(null); setLastDecisionNotice(null); setShowExport(false);
  }
  function updateTask(task, patch) { const nextTasks = tasks.map((item) => item.id === task.id ? {...item, ...patch} : item); setTasks(nextTasks); persist({tasks: nextTasks}); }
  function createBucket(kind, label, task, payload = {}) {
    const photoPreviews = task.photos.slice(0, 4).map((photo) => ({id: photoId(photo), src: photo.src, label: shortSourceName(photo)}));
    const bucket = {id: `bucket-${Date.now()}-${buckets.length + 1}`, kind, label, displayTitle: payload.displayTitle || label, sourceTitle: task.title, kindLabel: payload.kindLabel || kind, photoIds: task.photos.map(photoId), photoPreviews, photoCount: task.photos.length, createdAt: new Date().toISOString(), ...payload};
    const nextBuckets = [...buckets, bucket];
    setBuckets(nextBuckets);
    return {bucket, nextBuckets};
  }
  function completeTask(task, outcome, payload = {}, nextBuckets = buckets) {
    const decision = {taskId: task.id, stage: task.stage, outcome, payload: {title: task.title, photoIds: task.photos.map(photoId), displayedAssumption: task.halAssumption, nextStep: nextStepFor(outcome), needsWhatsappFollowup: ['new_product_identity', 'same_design_different_product', 'not_sure'].includes(outcome), ...payload}, decidedAt: new Date().toISOString(), datasetVersion: DATASET_VERSION};
    const nextDecisions = [...decisions, decision];
    setDecisions(nextDecisions); setSplitTask(null);
    const targetLabel = payload.attachedBucketTitle || payload.label || payload.productType || payload.referenceLabel || payload.visibleDifference || nextStepFor(outcome);
    setLastDecisionNotice({outcome, label: targetLabel, sourceTitle: task.title, at: Date.now()});
    window.requestAnimationFrame?.(() => window.scrollTo({top: 0, behavior: 'smooth'}));
    persist({decisions: nextDecisions, buckets: nextBuckets});
  }
  function editDecision(taskId) {
    const decision = decisions.find((item) => item.taskId === taskId);
    if (!decision) return;
    const removedBucketId = decision.payload?.bucketId;
    const nextDecisions = decisions.filter((item) => item.taskId !== taskId);
    const bucketStillReferenced = removedBucketId && nextDecisions.some((item) => item.payload?.bucketId === removedBucketId || item.payload?.referenceBucketId === removedBucketId);
    const nextBuckets = removedBucketId && !bucketStillReferenced ? buckets.filter((bucket) => bucket.id !== removedBucketId) : buckets;
    const nextTasks = tasks.map((item) => item.id === taskId ? {...item, stage: 'product_identity', pendingProductType: null, designReference: null} : item);
    setTasks(nextTasks);
    setDecisions(nextDecisions);
    setBuckets(nextBuckets);
    setSplitTask(null);
    setShowExport(false);
    setLastDecisionNotice({outcome: 'edit_decision', label: 'הבחירה נפתחה מחדש', sourceTitle: decision.payload?.title || taskId, at: Date.now()});
    window.requestAnimationFrame?.(() => window.scrollTo({top: 0, behavior: 'smooth'}));
    persist({tasks: nextTasks, decisions: nextDecisions, buckets: nextBuckets});
  }
  function continueSplitAsProducts(task, clusters) {
    const photoById = new Map(task.photos.map((photo) => [photoId(photo), photo]));
    const productTasks = clusters.map((cluster, index) => ({...task, id: `${task.id}__identity_${index + 1}`, stage: 'product_identity', title: `${task.title} · זהות ${index + 1}`, photos: cluster.photoIds.map((id) => photoById.get(id)).filter(Boolean), existingCandidate: null, existingCandidates: [], splitFrom: task.id}));
    const nextTasks = tasks.flatMap((item) => item.id === task.id ? productTasks : [item]);
    setTasks(nextTasks); setSplitTask(null); persist({tasks: nextTasks});
  }
  function handleAction(action, payload = {}) {
    const task = splitTask || currentTask;
    if (!task) return;
    if (action === 'cluster_same') return updateTask(task, {stage: 'product_identity'});
    if (action === 'cluster_split') return task.photos.length > 2 ? setSplitTask(task) : continueSplitAsProducts(task, task.photos.map((photo) => ({type: 'solo_product_candidate', photoIds: [photoId(photo)]})));
    if (action === 'identity_existing') return updateTask(task, {stage: 'existing_product_selection'});
    if (action === 'identity_new') return updateTask(task, {stage: 'new_identity'});
    if (action === 'back_to_identity') return updateTask(task, {stage: 'product_identity', pendingProductType: null, designReference: null});
    if (action === 'identity_same_design') return updateTask(task, {stage: 'design_reference_selection', designReference: null});

    if (action === 'design_reference_selected') return updateTask(task, {stage: 'same_design', designReference: {...payload, source: payload.unresolvedDesignReference ? 'manual_unresolved' : 'catalog_product'}});
    if (action === 'design_session_bucket_selected') return updateTask(task, {stage: 'same_design', designReference: {...payload, source: 'session_bucket'}});

    if (action === 'product_existing_selected') { const targetProductId = payload.manual ? null : payload.productId; const {bucket, nextBuckets} = createBucket('existing_product', payload.label || payload.productId, task, {targetProductId, catalogSource: payload.catalogSource, imageName: payload.imageName, unresolvedTarget: Boolean(payload.unresolvedTarget || payload.manual), kindLabel: 'מוצר קיים', displayTitle: payload.label || payload.productId}); return completeTask(task, 'existing_product_images', {...payload, productId: targetProductId, targetProductId, bucketId: bucket.id}, nextBuckets); }
    if (action === 'new_identity_type') return updateTask(task, {stage: 'new_design_question', pendingProductType: payload.productType});
    if (action === 'new_product_plain') { const productType = task.pendingProductType || payload.productType || 'מוצר'; const {bucket, nextBuckets} = createBucket('new_product', `${productType} חדשה · ${task.title}`, task, {productType, kindLabel: 'מוצר חדש', displayTitle: `${productType} חדשה`}); return completeTask(task, 'new_product_identity', {productType, bucketId: bucket.id}, nextBuckets); }
    if (action === 'same_design_difference') { const reference = task.designReference || {}; const referenceLabel = reference.label || 'רפרנס עיצוב לא ידוע'; const productType = task.pendingProductType || null; const {bucket, nextBuckets} = createBucket('same_design', `${task.title} · ${payload.visibleDifference}`, task, {visibleDifference: payload.visibleDifference, productType, designReference: reference, referenceProductId: reference.referenceProductId || null, referenceBucketId: reference.referenceBucketId || null, unresolvedDesignReference: Boolean(reference.unresolvedDesignReference), createDesignIfMissing: true, kindLabel: 'מוצר חדש בעיצוב קיים', displayTitle: `${referenceLabel} · ${payload.visibleDifference}`}); return completeTask(task, 'same_design_different_product', {...payload, productType, bucketId: bucket.id, designReference: reference, referenceProductId: reference.referenceProductId || null, referenceBucketId: reference.referenceBucketId || null, referenceLabel, unresolvedDesignReference: Boolean(reference.unresolvedDesignReference), createDesignIfMissing: true}, nextBuckets); }

    if (action === 'attach_to_bucket') return completeTask(task, 'attach_to_session_bucket', {...payload, attachedToSessionBucket: true});
    if (action === 'not_relevant') return completeTask(task, 'not_relevant', {reason: 'reviewer_marked_not_relevant', needsWhatsappFollowup: false});
    if (action === 'duplicate') return completeTask(task, 'duplicate', {reason: 'reviewer_marked_duplicate_or_not_relevant', needsWhatsappFollowup: false});
    if (action === 'split_some_together' || payload.clusters) return continueSplitAsProducts(task, payload.clusters || []);
    if (action === 'unsure') return completeTask(task, 'not_sure', {reason: payload.reason || 'reviewer_unsure'});
  }

  if (showHelp) return <><Header syncStatus={syncStatus} sessionId={sessionConfig.sessionId} started={started} remaining={remaining.length} photos={photoCount} completed={completedCount} total={tasks.length} onReset={reset} onHelp={() => setShowHelp(false)} onExport={() => setShowExport(true)} onSave={manualSave} /><Help onClose={() => setShowHelp(false)} /></>;
  if (showExport) return <><Header syncStatus={syncStatus} sessionId={sessionConfig.sessionId} started={started} remaining={remaining.length} photos={photoCount} completed={completedCount} total={tasks.length} onReset={reset} onHelp={() => setShowHelp(true)} onExport={() => setShowExport(false)} onSave={manualSave} /><ExportPanel decisions={decisions} buckets={buckets} tasks={tasks} sourceAssets={SOURCE_ASSETS} onClose={() => setShowExport(false)} onSave={manualSave} syncStatus={syncStatus} /></>;
  if (!started) return <><Header syncStatus={syncStatus} sessionId={sessionConfig.sessionId} started={started} remaining={remaining.length} photos={photoCount} completed={0} total={tasks.length} onReset={reset} onHelp={() => setShowHelp(true)} onExport={() => setShowExport(true)} onSave={manualSave} /><StartScreen taskCount={tasks.length} photoCount={photoCount} realStats={realStats} demoMode={demoMode} noReviewMode={noReviewMode} onStart={start} onHelp={() => setShowHelp(true)} /></>;
  if (!currentTask) return <><Header syncStatus={syncStatus} sessionId={sessionConfig.sessionId} started={started} remaining={0} photos={photoCount} completed={tasks.length} total={tasks.length} onReset={reset} onHelp={() => setShowHelp(true)} onExport={() => setShowExport(true)} onSave={manualSave} /><Summary decisions={decisions} buckets={buckets} tasks={tasks} sourceAssets={SOURCE_ASSETS} onReset={reset} onEditDecision={editDecision} onSave={manualSave} syncStatus={syncStatus} /></>;
  return <><Header syncStatus={syncStatus} sessionId={sessionConfig.sessionId} started={started} remaining={remaining.length} photos={remaining.reduce((sum, task) => sum + task.photos.length, 0)} completed={completedCount} total={tasks.length} onReset={reset} onHelp={() => setShowHelp(true)} onExport={() => setShowExport(true)} onSave={manualSave} /><main><DecisionNotice notice={lastDecisionNotice} /><CompletedDecisionStrip decisions={decisions} onEdit={editDecision} /><section className="review-context"><strong>פריט {completedCount + 1} מתוך {tasks.length}</strong><span>{remaining.length} נשארו · בוחרים פעולה אחת ומתקדמים</span></section>{splitTask ? <SplitReview task={splitTask} onSave={(result) => handleAction('split_some_together', result)} onBack={() => setSplitTask(null)} /> : <ReviewCard task={currentTask} buckets={buckets} onAction={handleAction} />}</main></>;
}

createRoot(document.getElementById('root')).render(<App />);
