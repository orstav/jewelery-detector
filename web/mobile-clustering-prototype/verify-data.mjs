import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('./data.js', import.meta.url), 'utf8');
const sandbox = {window: {}};
vm.runInNewContext(source, sandbox, {filename: 'data.js'});

const batches = sandbox.window.STAV_BATCHES || {};
const batchIndex = sandbox.window.STAV_BATCH_INDEX || [];
const products = sandbox.window.STAV_PRODUCT_INDEX || [];
const defaultBatch = sandbox.window.STAV_DEFAULT_BATCH;

if (!batchIndex.length) throw new Error('Batch index must not be empty.');
if (!defaultBatch || !batches[defaultBatch]) throw new Error(`Default batch missing: ${defaultBatch}`);
if (Object.keys(batches).length !== batchIndex.length) throw new Error('Batch index and batch manifest count differ.');

let totalSource = 0;
let totalReviewable = 0;
let totalCards = 0;
const globalPhotoIds = new Set();
for (const summary of batchIndex) {
  const manifest = batches[summary.batch_id];
  if (!manifest) throw new Error(`Missing batch manifest: ${summary.batch_id}`);
  const sourceAssets = manifest.source_assets || [];
  const reviewCards = manifest.review_cards || [];
  const auto = manifest.auto_accounted_assets || [];
  const blocked = manifest.blocked_assets || [];
  const reviewPhotoIds = reviewCards.flatMap((card) => (card.photos || []).map((photo) => photo.id));
  const accounted = new Set([...reviewPhotoIds, ...auto.map((asset) => asset.asset_id), ...blocked.map((asset) => asset.asset_id)]);
  if (sourceAssets.length !== manifest.coverage.expected || sourceAssets.length !== manifest.coverage.seen) {
    throw new Error(`${summary.batch_id}: source coverage mismatch`);
  }
  if (reviewPhotoIds.length !== manifest.coverage.reviewable) throw new Error(`${summary.batch_id}: reviewable count mismatch`);
  if (reviewCards.length !== manifest.coverage.review_cards) throw new Error(`${summary.batch_id}: card count mismatch`);
  if (accounted.size !== sourceAssets.length) throw new Error(`${summary.batch_id}: not every web source asset is reviewable/auto/blocked`);
  if (new Set(reviewCards.map((card) => card.id)).size !== reviewCards.length) throw new Error(`${summary.batch_id}: duplicate card ids`);
  for (const card of reviewCards) {
    if (!(card.photos || []).length) throw new Error(`${card.id}: card has no photos`);
    if ((card.photos || []).length > 1 && card.initialStage !== 'cluster_photos') throw new Error(`${card.id}: multi-photo card must start in clustering`);
    if ((card.photos || []).length === 1 && card.initialStage !== 'product_identity') throw new Error(`${card.id}: singleton must start in identity`);
    for (const photo of card.photos || []) {
      if (globalPhotoIds.has(photo.id)) throw new Error(`Photo appears in multiple batch cards: ${photo.id}`);
      globalPhotoIds.add(photo.id);
      if (!photo.src.startsWith(`/batches/${summary.batch_id}/`)) throw new Error(`${card.id}: wrong batch photo path ${photo.src}`);
      const file = new URL(`./public${photo.src}`, import.meta.url);
      if (!fs.existsSync(file)) throw new Error(`${card.id}: photo file missing ${photo.src}`);
    }
  }
  totalSource += sourceAssets.length;
  totalReviewable += reviewPhotoIds.length;
  totalCards += reviewCards.length;
}

if (!products.length) throw new Error('Catalog product index must not be empty.');
const productsWithoutImages = products.filter((product) => !(product.image && product.image.src));
if (productsWithoutImages.length > Math.ceil(products.length * 0.25)) {
  throw new Error(`Too many catalog products missing safe thumbnails: ${productsWithoutImages.length}/${products.length}`);
}
for (const product of products) {
  const src = product.image?.src;
  if (!src) continue;
  if (!src.startsWith('/real-data/')) throw new Error(`Catalog product ${product.id} has invalid thumbnail src: ${src}`);
  if (!src.includes(`catalog_${product.id}_`)) throw new Error(`Catalog product ${product.id} thumbnail does not match id: ${src}`);
  if (!fs.existsSync(new URL(`./public${src}`, import.meta.url))) throw new Error(`Catalog thumbnail missing: ${src}`);
}

console.log(`Verified ${batchIndex.length} batches, ${totalCards} review cards, ${totalReviewable}/${totalSource} web assets queued for Dalia, and ${products.length} catalog products.`);
