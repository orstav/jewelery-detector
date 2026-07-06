const palette = [
  ['#d6b37f', '#8d633f'],
  ['#e8c7a6', '#b87959'],
  ['#d8d4c4', '#8b8a7a'],
  ['#c9a7b8', '#876377'],
  ['#d5bd8c', '#9a7743'],
  ['#b8c7bf', '#60756f'],
  ['#ecd9b7', '#b88c48'],
  ['#d1bfd7', '#7e6290'],
];

const fallbackGroups = [
  {
    id: 'grp-001',
    title: 'נראה מוצר אחד חדש',
    type: 'same_new_product_group',
    confidence: 'high',
    photos: ['1', '2', '3', '4', '5', '6'],
    evidence: '6 תמונות דומות מאוד · אותו סוג תכשיט · אין מוצר קיים חזק',
    recommended: 'אישור מהיר',
    candidates: [],
  },
  {
    id: 'grp-002',
    title: 'כנראה שייך לעבודה שכבר התחילה',
    type: 'link_working_cluster',
    confidence: 'high',
    photos: ['7', '8', '9'],
    evidence: 'דומה לקבוצה #7 · כבר קושרו שם 3 תמונות',
    recommended: 'לחבר לקבוצה #7',
    candidates: [{ id: 'cluster-7', label: 'קבוצה #7', meta: 'כבר קושרו 3 תמונות' }],
  },
  {
    id: 'grp-003',
    title: 'דומה למוצר קיים',
    type: 'link_existing_product',
    confidence: 'medium',
    photos: ['10', '11', '12', '13'],
    evidence: 'התאמה למוצר R123 · צריך אישור שזה באמת אותו תכשיט',
    recommended: 'לפתוח ולאשר',
    candidates: [{ id: 'R123', label: 'R123 · מוצר קיים', meta: '4 תמונות תומכות' }],
  },
  {
    id: 'grp-004',
    title: 'ייתכן שיש כאן שני מוצרים',
    type: 'split_likely',
    confidence: 'low',
    photos: ['14', '15', '16', '17', '18'],
    evidence: 'התמונות דומות, אבל יש הבדל באבן/צורה',
    recommended: 'לפצל',
    candidates: [],
  },
];


const fallbackProductIndex = [
  {
    id: 'R037',
    name: 'טבעת ספיר כחולה',
    aliases: ['sapphire ring', 'טבעת ספיר', 'ספיר כחול'],
    type: 'טבעת',
    family: 'משפחת ספיר',
    meta: 'מוצר קיים בקטלוג · דוגמה ממועמדי הגלאי',
  },
  {
    id: 'NEGEV-NECKLACE',
    name: 'שרשרת נגב',
    aliases: ['Negev Necklace', 'negev', 'נגב', 'תליון נגב'],
    type: 'שרשרת',
    family: 'Negev',
    meta: 'דוגמה לחיפוש לפי שם שההורה זוכר',
  },
  {
    id: 'TULIP-EARRINGS',
    name: 'עגילי טוליפ',
    aliases: ['Tulip earrings', 'טוליפ', 'עגילי פרח'],
    type: 'עגילים',
    family: 'Tulip',
    meta: 'מוצר קיים לדוגמה באינדקס קריאה בלבד',
  },
  {
    id: 'RONI-GREEN',
    name: 'רוני ירוק',
    aliases: ['Roni green', 'רוני', 'ירוק רוני'],
    type: 'טבעת',
    family: 'Roni',
    meta: 'מוצר קיים לדוגמה באינדקס קריאה בלבד',
  },
];

const productIndex = window.STAV_PRODUCT_INDEX || fallbackProductIndex;

function normalizeSearch(value) {
  return String(value || '')
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[\u0591-\u05C7]/g, '')
    .replace(/[^\p{L}\p{N}]+/gu, ' ')
    .trim();
}

function productSearchText(product) {
  return normalizeSearch([product.id, product.name, product.type, product.family, product.meta, ...(product.aliases || [])].join(' '));
}

function autocompleteProducts(query, limit = 4) {
  const normalized = normalizeSearch(query);
  if (normalized.length < 2) return [];
  return searchProducts(query).slice(0, limit);
}

function searchProducts(query) {
  const normalized = normalizeSearch(query);
  if (!normalized) return [];
  const terms = normalized.split(/\s+/).filter(Boolean);
  return productIndex
    .map((product) => {
      const text = productSearchText(product);
      const exact = text.includes(normalized) ? 10 : 0;
      const termHits = terms.reduce((sum, term) => sum + (text.includes(term) ? 1 : 0), 0);
      return { product, score: exact + termHits };
    })
    .filter((item) => item.score >= 10 || item.score >= terms.length)
    .sort((a, b) => b.score - a.score || a.product.name.localeCompare(b.product.name, 'he'))
    .slice(0, 6)
    .map((item) => item.product);
}


const datasetVersion = window.STAV_DATASET_VERSION || 'mock-v1';
if (localStorage.getItem('stavDatasetVersion') !== datasetVersion) {
  localStorage.removeItem('stavGroups');
  localStorage.removeItem('stavDecisions');
  localStorage.setItem('stavDatasetVersion', datasetVersion);
}

function photoId(photo) {
  return typeof photo === 'string' ? photo : photo.id;
}

function photoSrc(photo) {
  return typeof photo === 'string' ? null : photo.src;
}

const state = {
  screen: 'queue',
  activeGroupId: null,
  groups: JSON.parse(localStorage.getItem('stavGroups') || 'null') || (window.STAV_REAL_GROUPS || fallbackGroups),
  decisions: JSON.parse(localStorage.getItem('stavDecisions') || '[]'),
  selectedPhotos: new Set(),
  knownProductQuery: '',
  previewPhotoId: null,
  backScreen: 'queue',
};

function datasetGroups() {
  return window.STAV_REAL_GROUPS || fallbackGroups;
}

const productDecisionTypes = new Set([
  'link_group_to_existing_product',
  'human_named_existing_product_not_in_suggestions',
  'create_new_product_cluster',
  'create_new_product_existing_design',
  'same_design_different_product',
  'same_product_variant',
  'metal_type_difference',
  'send_to_or_review_product_stage',
]);

function latestDecision(groupId, types = null) {
  return [...state.decisions].reverse().find((decision) => decision.groupId === groupId && (!types || types.has(decision.decisionType)));
}

function isPhotoGroupApproved(groupId) {
  return Boolean(latestDecision(groupId, new Set(['approve_photos_same_jewelry'])));
}

function hasProductDecision(groupId) {
  return Boolean(latestDecision(groupId, productDecisionTypes));
}

function productStageGroups() {
  return datasetGroups().filter((group) => isPhotoGroupApproved(group.id));
}

function pendingProductStageGroups() {
  return productStageGroups().filter((group) => !hasProductDecision(group.id));
}

function rejectedOrReviewGroups() {
  return state.decisions.filter((decision) => ['reject_photos_same_jewelry', 'send_to_or_review', 'split_review_group'].includes(decision.decisionType));
}

function findGroupAny(id) {
  return state.groups.find((group) => group.id === id) || datasetGroups().find((group) => group.id === id);
}

function setScreen(screen, groupId = state.activeGroupId, backScreen = state.screen) {
  state.screen = screen;
  state.activeGroupId = groupId;
  state.backScreen = backScreen || 'queue';
  render();
}

function goBack() {
  const target = state.backScreen || (pendingProductStageGroups().length ? 'product-stage' : 'queue');
  state.screen = target;
  state.activeGroupId = null;
  state.backScreen = 'queue';
  state.knownProductQuery = '';
  render();
}

function backButton(label = 'חזרה') {
  return `<button class="header-back" data-action="back" aria-label="${label}">‹ ${label}</button>`;
}

function persist() {
  localStorage.setItem('stavGroups', JSON.stringify(state.groups));
  localStorage.setItem('stavDecisions', JSON.stringify(state.decisions));
}

function confidenceHe(value) {
  return value === 'high' ? 'ביטחון גבוה' : value === 'medium' ? 'ביטחון בינוני' : 'דורש בדיקה';
}

function recommendedText(group) {
  if (group.type === 'split_likely') return 'יש חשד לערבוב — צריך לסמן מה שייך יחד';
  if ((group.recommended || '').includes('לפתוח')) return 'בדיקה קצרה של הקבוצה';
  return group.recommended || 'בדיקה קצרה';
}

const typeLabels = {
  ring: 'טבעת',
  necklace: 'שרשרת',
  earrings: 'עגילים',
  bracelet: 'צמיד',
};

const stoneLabels = {
  sapphire: 'ספיר',
  emerald: 'אמרלד',
  diamond: 'יהלום',
  ruby: 'רובי',
  pearl: 'פנינה',
  moonstone: 'מונסטון',
};

const metalLabels = {
  gold: 'זהב',
  silver: 'כסף',
  mixed: 'מתכות שונות',
  white_gold: 'זהב לבן',
  yellow_gold: 'זהב צהוב',
  rose_gold: 'זהב אדום',
};

function labelValue(value, labels) {
  return labels[value] || value;
}

function compactGroupHint(group) {
  const firstPhoto = group.photos.find((photo) => typeof photo !== 'string') || {};
  const parts = [
    labelValue(firstPhoto.jewelryType, typeLabels),
    labelValue(firstPhoto.stoneType, stoneLabels),
    labelValue(firstPhoto.metalColor, metalLabels),
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : 'קבוצת תמונות חדשה';
}

function activePhotos() {
  if (!state.activeGroupId) return datasetGroups().flatMap((group) => group.photos);
  return findGroupAny(state.activeGroupId)?.photos || [];
}

function findPhotoById(id) {
  return datasetGroups().flatMap((group) => group.photos).find((photo) => photoId(photo) === id) || id;
}

function previewOverlay() {
  if (!state.previewPhotoId) return '';
  const photos = activePhotos();
  const index = Math.max(0, photos.findIndex((photo) => photoId(photo) === state.previewPhotoId));
  const photo = findPhotoById(state.previewPhotoId);
  const src = photoSrc(photo);
  const label = photoId(photo);
  return `
    <div class="lightbox" role="dialog" aria-modal="true" aria-label="תצוגת תמונה גדולה">
      <div class="lightbox-top">
        <button class="icon-btn" data-action="close-preview">סגור</button>
        <div class="lightbox-title">תמונה ${index + 1} מתוך ${photos.length}</div>
      </div>
      <div class="lightbox-stage">${src ? `<img src="${src}" alt="${label}">` : `<div class="lightbox-placeholder">${label}</div>`}</div>
      <div class="lightbox-actions">
        <button class="btn ghost" data-action="prev-preview">הקודמת</button>
        <button class="btn ghost" data-action="next-preview">הבאה</button>
      </div>
      <div class="lightbox-hint">אפשר לעבור בין התמונות כדי לבדוק אבן, שיניים, צבע מתכת ומבנה.</div>
    </div>`;
}

function photoTile(photo, selected = false) {
  const id = photoId(photo);
  const src = photoSrc(photo);
  const numeric = [...id].reduce((sum, ch) => sum + ch.charCodeAt(0), 0);
  const ix = numeric % palette.length;
  const [c1, c2] = palette[ix];
  const content = src ? `<img src="${src}" alt="${id}">` : id;
  return `<div class="thumb ${selected ? 'selected' : ''}" data-photo="${id}" style="--c1:${c1};--c2:${c2}">${content}</div>`;
}

function record(group, decisionType, payload = {}) {
  state.decisions.push({
    id: crypto.randomUUID(),
    groupId: group.id,
    decisionType,
    payload,
    at: new Date().toISOString(),
  });
  state.groups = state.groups.filter((item) => item.id !== group.id);
  state.screen = 'queue';
  state.activeGroupId = null;
  state.selectedPhotos = new Set();
  persist();
  render();
}

function recordProductDecision(group, decisionType, payload = {}) {
  state.decisions.push({
    id: crypto.randomUUID(),
    groupId: group.id,
    decisionType,
    payload: { ...payload, stage: 'product_design_linking' },
    at: new Date().toISOString(),
  });
  state.screen = 'product-stage';
  state.activeGroupId = null;
  state.selectedPhotos = new Set();
  state.knownProductQuery = '';
  persist();
  render();
}

function openGroup(id) {
  state.activeGroupId = id;
  state.screen = 'group';
  state.selectedPhotos = new Set((state.groups.find((group) => group.id === id)?.photos || []).map(photoId));
  render();
}

function startSplit(id) {
  state.activeGroupId = id;
  state.screen = 'split';
  const group = findGroupAny(id);
  state.selectedPhotos = new Set((group?.photos.slice(0, Math.ceil(group.photos.length / 2)) || []).map(photoId));
  render();
}

function finishSplit(group) {
  const selected = group.photos.filter((photo) => state.selectedPhotos.has(photoId(photo)));
  const remaining = group.photos.filter((photo) => !state.selectedPhotos.has(photoId(photo)));
  state.decisions.push({
    id: crypto.randomUUID(),
    groupId: group.id,
    decisionType: 'split_review_group',
    payload: { selected: selected.map(photoId), remaining: remaining.map(photoId) },
    at: new Date().toISOString(),
  });
  state.groups = state.groups.filter((item) => item.id !== group.id);
  if (selected.length) {
    state.groups.push({
      ...group,
      id: `${group.id}-a`,
      title: 'קבוצה מפוצלת לאישור',
      confidence: 'medium',
      photos: selected,
      evidence: 'נוצר מפיצול ידני',
      recommended: 'אישור מהיר',
      type: 'same_new_product_group',
    });
  }
  if (remaining.length) {
    state.groups.push({
      ...group,
      id: `${group.id}-b`,
      title: 'שאר התמונות מהפיצול',
      confidence: 'low',
      photos: remaining,
      evidence: 'נשאר לבדיקה אחרי פיצול',
      recommended: 'לפתוח',
      type: remaining.length === 1 ? 'singleton' : 'split_likely',
    });
  }
  state.screen = 'queue';
  state.activeGroupId = null;
  state.selectedPhotos = new Set();
  persist();
  render();
}

function queueScreen() {
  const datasetGroups = window.STAV_REAL_GROUPS || fallbackGroups;
  const totalPhotos = datasetGroups.reduce((sum, group) => sum + group.photos.length, 0);
  const openPhotos = state.groups.reduce((sum, group) => sum + group.photos.length, 0);
  const done = totalPhotos - openPhotos;
  const pct = totalPhotos ? Math.round((done / totalPhotos) * 100) : 0;
  const pendingStage = pendingProductStageGroups();
  const approvedCount = productStageGroups().length;
  const productDone = approvedCount - pendingStage.length;
  const reviewCount = rejectedOrReviewGroups().length;
  const emptyState = pendingStage.length ? `
    <section class="empty panel">
      <div class="empty-title">שלב הקבוצות הסתיים</div>
      <div class="meta">אישרת ${approvedCount} קבוצות תמונות. עכשיו צריך להחליט לכל קבוצה: מוצר קיים, מוצר חדש, אותו עיצוב עם הבדל, או לא בטוח.</div>
      <button class="btn primary" data-action="product-stage" style="margin-top:14px">המשך לשיוך מוצרים ועיצובים</button>
    </section>` : `
    <section class="empty panel">
      <div class="empty-title">המדגם הזה הסתיים 🎉</div>
      <div class="meta">קבוצות שאושרו: ${approvedCount}. החלטות מוצר/עיצוב: ${productDone}. לבדיקה/פיצול: ${reviewCount}. אפשר להתחיל את אותו מדגם מחדש כדי לבדוק את הזרימה שוב.</div>
      <button class="btn primary" data-action="reset" style="margin-top:14px">התחל מדגם מחדש</button>
    </section>`;
  const cards = state.groups.map((group, index) => `
    <section class="group-card">
      <div class="group-head">
        <div>
          <div class="group-title">קבוצה ${index + 1}: האם אלה תמונות של אותו תכשיט?</div>
          <div class="meta">${group.photos.length} תמונות · ${compactGroupHint(group)}</div>
        </div>
        <span class="badge ${group.confidence}">${confidenceHe(group.confidence)}</span>
      </div>
      <div class="thumbs review-thumbs">${group.photos.slice(0, 8).map((photo) => photoTile(photo)).join('')}</div>
      <div class="meta decision-note">במסך הזה אין שיוך למוצר קיים. רק כן / לא / לא בטוח לגבי התמונות שבקבוצה.</div>
      <div class="actions three" style="margin-top:10px">
        <button class="btn primary" data-action="quick" data-id="${group.id}">${group.type === 'split_likely' ? 'סמן מי שייך יחד' : 'כן — אותו תכשיט'}</button>
        <button class="btn" data-action="not-same" data-id="${group.id}">לא — לא אותו תכשיט</button>
        <button class="btn warn" data-action="unsure" data-id="${group.id}">לא בטוח</button>
      </div>
    </section>`).join('');
  return `
    <div class="phone">
      <header>
        <h1>בדיקת קבוצות</h1>
        <div class="progress">${state.groups.length} קבוצות · ${openPhotos} תמונות נשארו</div>
        <div class="progressbar" style="--p:${pct}%"><span></span></div>
      </header>
      <main>
        <div class="notice">במסך הזה מאשרים רק דבר אחד: התמונות בקבוצה הן אותו תכשיט.</div>
        ${cards || emptyState}
      </main>
      <div class="footer">
        <button class="btn ghost" data-action="reset">התחל מחדש</button>
        <button class="btn ghost" data-action="help">עזרה</button>
      </div>
    </div>`;
}

function productStageScreen() {
  const pending = pendingProductStageGroups();
  const done = productStageGroups().length - pending.length;
  const cards = pending.map((group, index) => `
    <section class="group-card">
      <div class="group-head">
        <div>
          <div class="group-title">קבוצה ${index + 1}: מה זה בקטלוג?</div>
          <div class="meta">${group.photos.length} תמונות · ${compactGroupHint(group)}</div>
        </div>
        <span class="badge ${group.confidence}">אחרי אישור תמונות</span>
      </div>
      <div class="thumbs review-thumbs">${group.photos.slice(0, 8).map((photo) => photoTile(photo)).join('')}</div>
      <div class="meta decision-note">המערכת מתחילה בשבילכם: אם זה נראה כמו תמונות חדשות למוצר שכבר קיים — קודם מציגה מועמדים. אם לא, עוברים רק בסוף להחלטת עיצוב.</div>
      <div class="actions" style="margin-top:10px">
        <button class="btn primary" data-action="classify-existing" data-id="${group.id}">נראה מוצר קיים — הצג מועמדים</button>
        <button class="btn" data-action="classify-design" data-id="${group.id}">לא נראה מוצר קיים — החלטת עיצוב</button>
        <button class="btn warn" data-action="product-unsure" data-id="${group.id}">לא בטוח</button>
        <button class="btn ghost wide" data-action="classify-known" data-id="${group.id}">לא מצאת? חיפוש לפי שם</button>
      </div>
    </section>`).join('');
  return `
    <div class="phone">
      <header>
        ${backButton()}
        <h1>שיוך מוצרים ועיצובים</h1>
        <div class="progress">${pending.length} קבוצות לשיוך · ${done} כבר טופלו</div>
      </header>
      <main>
        <div class="notice">שלב 2: קודם בודקים האם אלה תמונות חדשות למוצר קיים. רק אם לא — מגיעים להחלטת עיצוב.</div>
        ${cards || '<section class="empty panel"><div class="empty-title">כל הקבוצות קיבלו החלטת מוצר/עיצוב 🎉</div><div class="meta">אין עוד קבוצות שמחכות לשיוך במדגם הזה.</div></section>'}
      </main>
      <div class="footer">
        <button class="btn ghost" data-action="back">חזרה</button>
        <button class="btn ghost" data-action="reset">התחל מחדש</button>
      </div>
    </div>`;
}

function helpScreen() {
  return `
    <div class="phone">
      <header>
        ${backButton()}
        <h1>עזרה קצרה</h1>
        <div class="progress">מה עושים במסך בדיקת הקבוצות</div>
      </header>
      <main>
        <section class="panel explain">
          <strong>שלב 1 — רק קבוצת תמונות</strong>
          <div class="meta">בודקים אם התמונות שמוצגות באותה קבוצה הן של אותו תכשיט. אין כאן חיבור למוצר קיים ואין יצירת מוצר.</div>
        </section>
        <section class="panel explain">
          <strong>אם זה לא אותו תכשיט</strong>
          <div class="meta">לוחצים “לא — לא אותו תכשיט”. המערכת תחזיר את הקבוצה לריקלסטר / בדיקת פיצול, ולא תיצור מוצר אוטומטית.</div>
        </section>
        <section class="panel explain">
          <strong>שיוך למוצר קיים</strong>
          <div class="meta">שיוך מגיע רק אחרי שהקבוצה עצמה נקייה. שם אפשר לבחור מועמד, או להשתמש ב“אני יודע/ת מה זה” ולחפש בכל המוצרים.</div>
        </section>
        <section class="panel explain">
          <strong>בדיקת פרטים עדינים</strong>
          <div class="meta">לוחצים על תמונה כדי לפתוח הגדלה למסך מלא ולבדוק אבן, צבע מתכת, שיניים ומבנה.</div>
        </section>
      </main>
      <div class="footer">
        <button class="btn ghost" data-action="back">חזרה</button>
      </div>
    </div>`;
}

function groupScreen(mode = 'group') {
  const group = findGroupAny(state.activeGroupId);
  if (!group) return queueScreen();
  const candidates = group.candidates.map((candidate, index) => `
    <div class="candidate">
      ${photoTile(String(index + 21))}
      <div><strong>${candidate.label}</strong><div class="meta">${candidate.meta}</div></div>
    </div>`).join('');
  const thumbsClass = mode === 'split' ? 'thumbs split-grid' : 'thumbs';

  if (mode === 'link-existing') {
    const candidateCards = group.candidates.length ? group.candidates.map((candidate, index) => `
      <button class="candidate choice" data-action="link-candidate" data-id="${group.id}" data-candidate="${candidate.id}">
        ${photoTile(String(index + 21))}
        <span><strong>${candidate.label}</strong><small>${candidate.meta}</small></span>
      </button>`).join('') : `<div class="notice">אין כרגע מועמד חזק מספיק. עדיף לשלוח לבדיקה במקום לנחש.</div>`;
    return `
    <div class="phone">
      <header>
        ${backButton()}
        <h1>האם זה מוצר שכבר קיים?</h1>
        <div class="progress">המועמדים הם קיצור דרך. אם אף אחד לא נכון — ממשיכים להחלטת עיצוב.</div>
      </header>
      <main>
        <section class="panel">
          <div class="group-title">התמונות החדשות</div>
          <div class="meta">אם זה אותו תכשיט שכבר קיים באתר/בקטלוג — בוחרים את המוצר הקיים. אם זה רק דומה, לא מחברים.</div>
          <div class="thumbs review-thumbs">${group.photos.map((photo) => photoTile(photo)).join('')}</div>
        </section>
        <section class="panel">
          <strong>מועמדים קיימים</strong>
          <div class="meta">אם אחד המועמדים נכון — בוחרים אותו. אם לא, לא כותבים ידנית עדיין; ממשיכים להחלטת עיצוב.</div>
          <div class="candidates">${candidateCards}</div>
          <div class="actions" style="margin-top:10px">
            <button class="btn primary" data-action="same-design" data-id="${group.id}">אף מועמד לא מתאים — החלטת עיצוב</button>
            <button class="btn warn" data-action="unsure" data-id="${group.id}">לא בטוח</button>
            <button class="btn ghost wide" data-action="known-product" data-id="${group.id}">לא מצאת? חיפוש לפי שם</button>
          </div>
        </section>
      </main>
      <div class="footer">
        <button class="btn ghost" data-action="back">חזרה</button>
      </div>
    </div>`;
  }
  if (mode === 'known-product') {
    return `
    <div class="phone">
      <header>
        ${backButton()}
        <h1>חיפוש לפי שם</h1>
        <div class="progress">רק אם המועמדים לא עוזרים — כותבים רמז או שם מוכר</div>
      </header>
      <main>
        <section class="panel">
          <div class="group-title">התמונות החדשות</div>
          <div class="meta">המידע הזה נשמר עם התמונות, כדי שלא נשאל שוב מאפס.</div>
          <div class="thumbs review-thumbs">${group.photos.map((photo) => photoTile(photo)).join('')}</div>
        </section>
        <section class="panel">
          <strong>שם / רמז לזיהוי</strong>
          <div class="meta">זה שלב אחרון כשבחירה מהמועמדים לא מספיקה. מתחילים להקליד ורואים השלמות — עדיין אפשר לשמור רמז אם אין התאמה.</div>
          <input class="text-input" id="knownProductName" dir="auto" placeholder="שם מוצר, עיצוב או רמז" value="${state.knownProductQuery || ''}" autocomplete="off">
          <div class="autocomplete">${autocompleteProducts(state.knownProductQuery).map((product) => `
            <button class="autocomplete-item" data-action="link-search-product" data-id="${group.id}" data-product="${product.id}">
              <strong>${product.name}</strong><small>${product.id} · ${product.type || ''} · ${product.family || ''}</small>
            </button>`).join('')}</div>
          <div class="actions" style="margin-top:10px">
            <button class="btn primary" data-action="search-known-product" data-id="${group.id}">הצג תוצאות</button>
            <button class="btn" data-action="link-existing" data-id="${group.id}">חזרה למועמדים</button>
            <button class="btn warn" data-action="unsure" data-id="${group.id}">לא בטוח</button>
          </div>
        </section>
        ${state.knownProductQuery ? `
        <section class="panel">
          <strong>תוצאות חיפוש</strong>
          <div class="meta">בחרו מוצר אם אחד מהם נכון. אם לא — שומרים את השם ל־HAL.</div>
          <div class="candidates">${searchProducts(state.knownProductQuery).map((product, index) => `
            <button class="candidate choice" data-action="link-search-product" data-id="${group.id}" data-product="${product.id}">
              ${photoTile(String(index + 51))}
              <span><strong>${product.name}</strong><small>${product.id} · ${product.type || ''} · ${product.family || ''}<br>${product.meta || ''}</small></span>
            </button>`).join('') || '<div class="notice">לא נמצאה התאמה טובה באינדקס. אפשר לשמור את השם כדי ש־HAL יחבר ידנית.</div>'}</div>
          <button class="btn" data-action="save-known-product" data-id="${group.id}">לא מצאתי — שמור את השם ל־HAL</button>
        </section>` : ''}
        <section class="panel explain">
          <strong>מה קורה אם המערכת עדיין לא מוצאת?</strong>
          <div class="meta">זה לא הופך ל“מוצר חדש”. זה נשמר כ: מוצר מוכר לפי ההורה, לא נמצא אוטומטית. HAL יחבר ידנית או יביא מועמדים לפי השם.</div>
        </section>
      </main>
      <div class="footer">
        <button class="btn ghost" data-action="back">חזרה</button>
      </div>
    </div>`;
  }
  if (mode === 'design-intent') {
    return `
    <div class="phone">
      <header>
        ${backButton()}
        <h1>החלטת עיצוב</h1>
        <div class="progress">רק אחרי שלא מצאנו מוצר קיים מתאים</div>
      </header>
      <main>
        <section class="panel">
          <div class="group-title">${group.title}</div>
          <div class="meta">${group.evidence}</div>
          <div class="thumbs review-thumbs">${group.photos.map((photo) => photoTile(photo)).join('')}</div>
          <div class="meta zoom-hint">לחיצה על תמונה פותחת הגדלה למסך מלא.</div>
        </section>
        <section class="panel explain">
          <strong>שאלה אחרונה</strong>
          <div class="meta">לא צריך לבחור שדות קטלוג. רק האם זה מרגיש אותו עיצוב שכבר קיים, או עיצוב חדש. מוצר חדש יכול עדיין להיות באותו עיצוב.</div>
        </section>
        <section class="panel">
          <strong>מה זה מבחינת עיצוב?</strong>
          <div class="actions" style="margin-top:10px">
            <button class="btn primary" data-action="new-product-existing-design" data-id="${group.id}">אותו עיצוב — מוצר חדש</button>
            <button class="btn" data-action="new-product" data-id="${group.id}">עיצוב חדש</button>
            <button class="btn" data-action="difference:stone" data-id="${group.id}">אותו עיצוב — אבן / צבע / גודל שונה</button>
            <button class="btn" data-action="variant:gold_color" data-id="${group.id}">אותו מוצר — רק צבע זהב שונה</button>
            <button class="btn warn" data-action="unsure" data-id="${group.id}">לא בטוח</button>
          </div>
        </section>
      </main>
      <div class="footer">
        <button class="btn ghost" data-action="back">חזרה</button>
      </div>
    </div>`;
  }
  return `
    <div class="phone">
      <header>
        ${backButton()}
        <h1>${mode === 'split' ? 'סימון תמונות ששייכות יחד' : 'בדיקת שיוך הקבוצה'}</h1>
        <div class="progress">${group.photos.length} תמונות · ${confidenceHe(group.confidence)}</div>
      </header>
      <main>
        <section class="panel">
          <div class="group-title">${group.title}</div>
          <div class="meta">${group.evidence}</div>
          <div class="${mode === 'split' ? 'thumbs split-grid' : 'thumbs review-thumbs'}">
            ${group.photos.map((photo) => photoTile(photo, state.selectedPhotos.has(photoId(photo)))).join('')}
          </div>
          <div class="meta zoom-hint">${mode === 'split' ? 'במסך הזה לחיצה מסמנת שייכות. הגדלה זמינה במסכי הבדיקה.' : 'לחיצה על תמונה פותחת הגדלה למסך מלא.'}</div>
          ${mode === 'split' ? '<div class="notice">לא צריך לחשוב על “פיצול”. פשוט מסמנים את התמונות שהן אותו תכשיט. התמונות שלא מסומנות יחזרו לבדיקה כקבוצה נפרדת.</div>' : ''}
        </section>
        ${candidates ? `<section class="panel"><strong>אפשרויות קיימות</strong><div class="candidates">${candidates}</div></section>` : ''}
        <section class="panel">
          <strong>מה לעשות עם הקבוצה?</strong>
          <div class="actions" style="margin-top:10px">
            ${mode === 'split'
              ? `<button class="btn primary" data-action="finish-split" data-id="${group.id}">אשר את התמונות המסומנות</button><button class="btn" data-action="back">ביטול</button>`
              : `<button class="btn primary" data-action="one-product" data-id="${group.id}">כן — התמונות אותו תכשיט</button>
                 <button class="btn" data-action="link-existing" data-id="${group.id}">בחר מוצר קיים לחיבור</button>
                 <button class="btn" data-action="known-product" data-id="${group.id}">אני יודע/ת מה זה</button>
                 <button class="btn" data-action="new-product" data-id="${group.id}">זה מוצר חדש</button>
                 <button class="btn" data-action="split" data-id="${group.id}">יש תמונות שלא שייכות</button>
                 <button class="btn" data-action="same-design" data-id="${group.id}">אותו עיצוב, מוצר אחר</button>
                 <button class="btn warn" data-action="unsure" data-id="${group.id}">לא בטוח</button>`}
          </div>
        </section>
      </main>
      <div class="footer">
        <button class="btn ghost" data-action="back">חזרה</button>
      </div>
    </div>`;
}

function render() {
  const root = document.querySelector('#app');
  root.innerHTML = (state.screen === 'queue' ? queueScreen() : state.screen === 'help' ? helpScreen() : state.screen === 'product-stage' ? productStageScreen() : groupScreen(state.screen)) + previewOverlay();
}

document.addEventListener('input', (event) => {
  if (event.target?.id !== 'knownProductName') return;
  state.knownProductQuery = event.target.value.trim();
  render();
  requestAnimationFrame(() => {
    const input = document.querySelector('#knownProductName');
    if (!input) return;
    input.focus();
    const pos = input.value.length;
    input.setSelectionRange(pos, pos);
  });
});

document.addEventListener('click', (event) => {
  const actionEl = event.target.closest('[data-action]');
  const photoEl = event.target.closest('[data-photo]');
  if (photoEl && state.screen === 'split') {
    const photo = photoEl.dataset.photo;
    if (state.selectedPhotos.has(photo)) state.selectedPhotos.delete(photo);
    else state.selectedPhotos.add(photo);
    render();
    return;
  }
  if (photoEl && !actionEl) {
    state.previewPhotoId = photoEl.dataset.photo;
    render();
    return;
  }
  if (!actionEl) return;
  const action = actionEl.dataset.action;
  const id = actionEl.dataset.id;
  const group = findGroupAny(id);
  if (action === 'close-preview') { state.previewPhotoId = null; render(); return; }
  if (action === 'prev-preview' || action === 'next-preview') {
    const photos = activePhotos();
    const currentIndex = Math.max(0, photos.findIndex((photo) => photoId(photo) === state.previewPhotoId));
    const delta = action === 'next-preview' ? 1 : -1;
    const nextIndex = (currentIndex + delta + photos.length) % photos.length;
    state.previewPhotoId = photoId(photos[nextIndex]);
    render();
    return;
  }
  if (action === 'product-stage') { state.screen = 'product-stage'; state.activeGroupId = null; render(); }
  if (action === 'classify-existing' && group) setScreen('link-existing', id, 'product-stage');
  if (action === 'classify-known' && group) { state.knownProductQuery = ''; setScreen('known-product', id, 'product-stage'); }
  if (action === 'classify-new-existing-design' && group) recordProductDecision(group, 'create_new_product_existing_design', { photoIds: group.photos.map(photoId), source: 'product_stage', designRelationship: 'existing_design_new_product' });
  if (action === 'classify-new' && group) recordProductDecision(group, 'create_new_product_cluster', { photoIds: group.photos.map(photoId), source: 'product_stage', designRelationship: 'new_design_or_unknown' });
  if (action === 'classify-design' && group) setScreen('design-intent', id, 'product-stage');
  if (action === 'product-unsure' && group) recordProductDecision(group, 'send_to_or_review_product_stage', { reason: 'human_not_sure_product_or_design', photoIds: group.photos.map(photoId) });
  if (action === 'open') openGroup(id);
  if (action === 'quick' && group?.type === 'split_likely') startSplit(id);
  else if (action === 'quick' && group) record(group, 'approve_photos_same_jewelry', { photoIds: group.photos.map(photoId), scope: 'photo_group_only_not_product_link' });
  if (action === 'not-same' && group) record(group, 'reject_photos_same_jewelry', { photoIds: group.photos.map(photoId), scope: 'photo_group_only_not_product_link', nextStep: 'system_recluster_or_manual_split' });
  if (action === 'one-product' && group) record(group, 'approve_photos_same_jewelry', { photoIds: group.photos.map(photoId), scope: 'photo_group_only_not_product_link' });
  if (action === 'link-existing' && group) setScreen('link-existing', id, state.screen === 'known-product' ? 'product-stage' : state.screen);
  if (action === 'known-product' && group) { state.knownProductQuery = ''; setScreen('known-product', id, state.screen === 'link-existing' ? 'link-existing' : state.screen); }
  if (action === 'search-known-product' && group) {
    state.knownProductQuery = (document.querySelector('#knownProductName')?.value || '').trim();
    render();
  }
  if (action === 'save-known-product' && group) {
    const typedName = (document.querySelector('#knownProductName')?.value || state.knownProductQuery || '').trim();
    recordProductDecision(group, 'human_named_existing_product_not_in_suggestions', { typedName, photoIds: group.photos.map(photoId), nextStep: 'search_catalog_by_name_alias_and_visual_candidates' });
  }
  if (action === 'link-search-product' && group) recordProductDecision(group, 'link_group_to_existing_product', { candidate: actionEl.dataset.product, source: 'all_product_search', typedName: state.knownProductQuery, photoIds: group.photos.map(photoId) });
  if (action === 'link-candidate' && group) recordProductDecision(group, 'link_group_to_existing_product', { candidate: actionEl.dataset.candidate, source: 'detector_candidate', photoIds: group.photos.map(photoId) });
  if (action === 'new-product-existing-design' && group) recordProductDecision(group, 'create_new_product_existing_design', { photoIds: group.photos.map(photoId), source: 'candidate_screen', designRelationship: 'existing_design_new_product' });
  if (action === 'new-product' && group) recordProductDecision(group, 'create_new_product_cluster', { photoIds: group.photos.map(photoId), source: 'product_stage', designRelationship: 'new_design_or_unknown' });
  if (action === 'same-design' && group) setScreen('design-intent', id, state.screen);
  if (action?.startsWith('metal-type:') && group) recordProductDecision(group, 'metal_type_difference', { difference: action.replace('metal-type:', ''), photoIds: group.photos.map(photoId), note: 'silver_or_gold_material_type; silver_photo_may_cover_white_gold' });
  if (action?.startsWith('variant:') && group) recordProductDecision(group, 'same_product_variant', { difference: action.replace('variant:', ''), photoIds: group.photos.map(photoId) });
  if (action?.startsWith('difference:') && group) recordProductDecision(group, 'same_design_different_product', { difference: action.replace('difference:', ''), photoIds: group.photos.map(photoId) });
  if (action === 'unsure' && group) record(group, 'send_to_or_review', { reason: 'human_not_sure', photoIds: group.photos.map(photoId) });
  if (action === 'split') startSplit(id);
  if (action === 'finish-split' && group) finishSplit(group);
  if (action === 'back') goBack();
  if (action === 'help') setScreen('help', null, 'queue');
  if (action === 'reset') { localStorage.removeItem('stavGroups'); localStorage.removeItem('stavDecisions'); window.location.reload(); }
});

render();
