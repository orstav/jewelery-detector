import fs from 'node:fs';

const src = fs.readFileSync(new URL('./src/main.jsx', import.meta.url), 'utf8');
const schema = fs.readFileSync(new URL('./src/packetSchema.js', import.meta.url), 'utf8');

const required = [
  'סידור תמונות לפי מוצר',
  'עוברים פריט־פריט',
  'האם כל התמונות כאן הן של אותו תכשיט?',
  'מוצר קיים',
  'מוצר חדש',
  'לא בטוחה',
  'יש לו עיצוב דומה?',
  'new_design_question',
  'new_product_plain',
  'התאמות מהגלאי',
  'איזה מוצר קיים זה?',
  'הגלאי מציע מועמדים לפי דמיון תמונות',
  'שם או מספר מוצר קיים',
  'product_existing_selected',
  "stage: 'existing_product_selection'",
  'התאמות עיצוב מוצעות',
  'מה שונה בין התמונות?',
  'design_reference_selection',
  'reference_airtable_product',
  'האצווה הסתיימה',
  'שמירה לשרת',
  'שמירת עבודה',
  'הורדת גיבוי JSON',
  'השרת מקבל את העבודה',
  'makeDemoTasks',
  "stage: 'cluster_photos'",
  "stage: 'product_identity'",
  'exportPacketBundle',
];

for (const needle of required) {
  if (!src.includes(needle) && !schema.includes(needle)) throw new Error(`Missing production review flow copy/state: ${needle}`);
}

const forbiddenReviewerCopy = [
  'אין עבודה ידנית בכרטיסים האלה',
  'אין כרגע עבודה אנושית',
  'דולגו אוטומטית לשלב התאמת גלאי',
  'מועמד גלאי',
  'Top‑K',
  'בדיקת קיבוץ בלבד',
  'תיקון למוצר קיים',
  'פרטי מוצר לא נכונים',
  'התמונה באתר לא נכונה',
  'fix_existing_product',
  'FIX_OPTIONS',
  'identity_photographer_corrected',
  'corrected_image_selected',
  'תמונות מתוקנות מהצלם',
  'אלו תמונות מתוקנות מהצלם?',
];
for (const needle of forbiddenReviewerCopy) {
  if (src.includes(needle)) throw new Error(`Reviewer-facing banned copy/state still present: ${needle}`);
}

if (!schema.includes('photographer_corrected_image')) throw new Error('Future corrected-image packet support must remain in schema, but not in the current reviewer UI.');

const reviewerMetaCopy = [
  'HAL הכין',
  'HAL מייצא',
  'dry-run בלבד',
  'packet JSON',
];
for (const needle of reviewerMetaCopy) {
  if (src.includes(needle)) throw new Error(`Reviewer-facing meta copy still present: ${needle}`);
}

const productIdentityStart = src.indexOf("{task.stage === 'product_identity' ? <>");
const productIdentityEnd = src.indexOf("{task.stage === 'existing_product_selection'", productIdentityStart);
const productIdentityBlock = productIdentityStart >= 0 && productIdentityEnd > productIdentityStart ? src.slice(productIdentityStart, productIdentityEnd) : '';
for (const bannedInitialChoice of ['אותו עיצוב / מוצר אחר', 'לא רלוונטי / כפול', 'כפול / לא רלוונטי']) {
  if (productIdentityBlock.includes(bannedInitialChoice)) throw new Error(`Initial product decision has too many choices: ${bannedInitialChoice}`);
}
for (const requiredInitialChoice of ['מוצר קיים', 'מוצר חדש', 'לא בטוחה']) {
  if (!productIdentityBlock.includes(requiredInitialChoice)) throw new Error(`Initial product decision missing required simple choice: ${requiredInitialChoice}`);
}

console.log('Verified simplified identity review UI copy, no current corrected-image button, future corrected-image schema support, and no broad fix lane.');
