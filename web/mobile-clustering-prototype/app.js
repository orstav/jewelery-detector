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

const initialGroups = [
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

const state = {
  screen: 'queue',
  activeGroupId: null,
  groups: JSON.parse(localStorage.getItem('stavGroups') || 'null') || initialGroups,
  decisions: JSON.parse(localStorage.getItem('stavDecisions') || '[]'),
  selectedPhotos: new Set(),
};

function persist() {
  localStorage.setItem('stavGroups', JSON.stringify(state.groups));
  localStorage.setItem('stavDecisions', JSON.stringify(state.decisions));
}

function confidenceHe(value) {
  return value === 'high' ? 'ביטחון גבוה' : value === 'medium' ? 'ביטחון בינוני' : 'דורש בדיקה';
}

function photoTile(photoId, selected = false) {
  const ix = Number.parseInt(photoId, 10) % palette.length;
  const [c1, c2] = palette[ix];
  return `<div class="thumb ${selected ? 'selected' : ''}" data-photo="${photoId}" style="--c1:${c1};--c2:${c2}">${photoId}</div>`;
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

function openGroup(id) {
  state.activeGroupId = id;
  state.screen = 'group';
  state.selectedPhotos = new Set(state.groups.find((group) => group.id === id)?.photos || []);
  render();
}

function startSplit(id) {
  state.activeGroupId = id;
  state.screen = 'split';
  const group = state.groups.find((item) => item.id === id);
  state.selectedPhotos = new Set(group?.photos.slice(0, Math.ceil(group.photos.length / 2)) || []);
  render();
}

function finishSplit(group) {
  const selected = [...state.selectedPhotos];
  const remaining = group.photos.filter((photo) => !state.selectedPhotos.has(photo));
  state.decisions.push({
    id: crypto.randomUUID(),
    groupId: group.id,
    decisionType: 'split_review_group',
    payload: { selected, remaining },
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
  const totalPhotos = initialGroups.reduce((sum, group) => sum + group.photos.length, 0);
  const openPhotos = state.groups.reduce((sum, group) => sum + group.photos.length, 0);
  const done = totalPhotos - openPhotos;
  const pct = Math.round((done / totalPhotos) * 100);
  const cards = state.groups.map((group) => `
    <section class="group-card">
      <div class="group-head">
        <div>
          <div class="group-title">${group.title}</div>
          <div class="meta">${group.evidence}</div>
        </div>
        <span class="badge ${group.confidence}">${confidenceHe(group.confidence)}</span>
      </div>
      <div class="thumbs">${group.photos.slice(0, 8).map((photo) => photoTile(photo)).join('')}</div>
      <div class="meta">${group.photos.length} תמונות · פעולה מומלצת: ${group.recommended}</div>
      <div class="actions three" style="margin-top:10px">
        <button class="btn primary" data-action="quick" data-id="${group.id}">${group.type === 'split_likely' ? 'לפצל' : 'אישור מהיר'}</button>
        <button class="btn" data-action="open" data-id="${group.id}">לפתוח</button>
        <button class="btn warn" data-action="unsure" data-id="${group.id}">לא בטוח</button>
      </div>
    </section>`).join('');
  return `
    <div class="phone">
      <header>
        <h1>קבוצות לבדיקה</h1>
        <div class="progress">נפתרו ${done} מתוך ${totalPhotos} תמונות · נשארו ${state.groups.length} קבוצות</div>
        <div class="progressbar" style="--p:${pct}%"><span></span></div>
      </header>
      <main>
        <div class="notice">המטרה: לא לעבור תמונה-תמונה. מאשרים או מתקנים קבוצות שלמות.</div>
        ${cards || '<div class="empty">סיימנו את כל הקבוצות במדגם 🎉</div>'}
        <section class="panel">
          <strong>יומן החלטות</strong>
          <div class="meta">נשמר מקומית בפרוטוטייפ</div>
          <pre class="log">${JSON.stringify(state.decisions.slice(-5), null, 2)}</pre>
        </section>
      </main>
      <div class="footer">
        <button class="btn ghost" data-action="reset">איפוס</button>
        <button class="btn ghost" data-action="export">ייצוא</button>
        <button class="btn ghost" data-action="noop">עזרה</button>
      </div>
    </div>`;
}

function groupScreen(mode = 'group') {
  const group = state.groups.find((item) => item.id === state.activeGroupId);
  if (!group) return queueScreen();
  const candidates = group.candidates.map((candidate, index) => `
    <div class="candidate">
      ${photoTile(String(index + 21))}
      <div><strong>${candidate.label}</strong><div class="meta">${candidate.meta}</div></div>
    </div>`).join('');
  const thumbsClass = mode === 'split' ? 'thumbs split-grid' : 'thumbs';
  if (mode === 'design-intent') {
    return `
    <div class="phone">
      <header>
        <h1>אותו עיצוב או דגם?</h1>
        <div class="progress">לא צריך להבין Shopify — רק להגיד מה רואים</div>
      </header>
      <main>
        <section class="panel">
          <div class="group-title">${group.title}</div>
          <div class="meta">${group.evidence}</div>
          <div class="thumbs">${group.photos.map((photo) => photoTile(photo)).join('')}</div>
        </section>
        <section class="panel explain">
          <strong>הסבר קצר</strong>
          <div class="meta">אם זה אותו תכשיט בדיוק — מחברים לאותה קבוצה. אם זה אותו סגנון אבל יש הבדל שנמכר אחרת, מסמנים מה ההבדל. אנחנו נחליט אחר כך אם זה מוצר, וריאנט או אותו Design.</div>
        </section>
        <section class="panel">
          <strong>מה ההבדל?</strong>
          <div class="actions" style="margin-top:10px">
            <button class="btn primary" data-action="one-product" data-id="${group.id}">אין הבדל — אותו תכשיט</button>
            <button class="btn" data-action="difference:metal_color" data-id="${group.id}">צבע מתכת אחר</button>
            <button class="btn" data-action="difference:metal_type" data-id="${group.id}">כסף / זהב</button>
            <button class="btn" data-action="difference:stone" data-id="${group.id}">אבן / צבע אבן אחר</button>
            <button class="btn" data-action="difference:size" data-id="${group.id}">גודל אחר</button>
            <button class="btn" data-action="difference:structure" data-id="${group.id}">צורה / פרטים שונים</button>
            <button class="btn warn" data-action="unsure" data-id="${group.id}">לא בטוח</button>
          </div>
        </section>
      </main>
      <div class="footer">
        <button class="btn ghost" data-action="back">חזרה</button>
        <button class="btn ghost" data-action="more-products">עוד מוצרים</button>
        <button class="btn ghost" data-action="more-images">עוד תמונות</button>
      </div>
    </div>`;
  }
  return `
    <div class="phone">
      <header>
        <h1>${mode === 'split' ? 'לפצל קבוצה' : 'בדיקת קבוצה'}</h1>
        <div class="progress">${group.photos.length} תמונות · ${confidenceHe(group.confidence)}</div>
      </header>
      <main>
        <section class="panel">
          <div class="group-title">${group.title}</div>
          <div class="meta">${group.evidence}</div>
          <div class="${thumbsClass}">
            ${group.photos.map((photo) => photoTile(photo, state.selectedPhotos.has(photo))).join('')}
          </div>
          ${mode === 'split' ? '<div class="notice">בחרי רק את התמונות ששייכות יחד. השאר יחזרו לתור כקבוצה נפרדת.</div>' : ''}
        </section>
        ${candidates ? `<section class="panel"><strong>אפשרויות קיימות</strong><div class="candidates">${candidates}</div></section>` : ''}
        <section class="panel">
          <strong>מה הכוונה?</strong>
          <div class="actions" style="margin-top:10px">
            ${mode === 'split'
              ? `<button class="btn primary" data-action="finish-split" data-id="${group.id}">צור קבוצה מהנבחרות</button><button class="btn" data-action="back">ביטול</button>`
              : `<button class="btn primary" data-action="one-product" data-id="${group.id}">כן, זו קבוצה אחת</button>
                 <button class="btn" data-action="link-existing" data-id="${group.id}">לחבר למוצר קיים</button>
                 <button class="btn" data-action="new-product" data-id="${group.id}">זה מוצר חדש</button>
                 <button class="btn" data-action="split" data-id="${group.id}">לפצל</button>
                 <button class="btn" data-action="same-design" data-id="${group.id}">אותו עיצוב, מוצר אחר</button>
                 <button class="btn warn" data-action="unsure" data-id="${group.id}">לא בטוח</button>`}
          </div>
        </section>
      </main>
      <div class="footer">
        <button class="btn ghost" data-action="back">חזרה</button>
        <button class="btn ghost" data-action="more-products">עוד מוצרים</button>
        <button class="btn ghost" data-action="more-images">עוד תמונות</button>
      </div>
    </div>`;
}

function render() {
  const root = document.querySelector('#app');
  root.innerHTML = state.screen === 'queue' ? queueScreen() : groupScreen(state.screen);
}

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
  if (!actionEl) return;
  const action = actionEl.dataset.action;
  const id = actionEl.dataset.id;
  const group = state.groups.find((item) => item.id === id);
  if (action === 'open') openGroup(id);
  if (action === 'quick' && group?.type === 'split_likely') startSplit(id);
  else if (action === 'quick' && group) record(group, 'approve_group_as_one_product', { photoIds: group.photos });
  if (action === 'one-product' && group) record(group, 'approve_group_as_one_product', { photoIds: group.photos });
  if (action === 'link-existing' && group) record(group, 'link_group_to_existing_product', { candidate: group.candidates[0]?.id || null, photoIds: group.photos });
  if (action === 'new-product' && group) record(group, 'create_new_product_cluster', { photoIds: group.photos });
  if (action === 'same-design' && group) { state.screen = 'design-intent'; render(); }
  if (action?.startsWith('difference:') && group) record(group, 'same_design_different_product', { difference: action.replace('difference:', ''), photoIds: group.photos });
  if (action === 'unsure' && group) record(group, 'send_to_or_review', { reason: 'human_not_sure', photoIds: group.photos });
  if (action === 'split') startSplit(id);
  if (action === 'finish-split' && group) finishSplit(group);
  if (action === 'back') { state.screen = 'queue'; state.activeGroupId = null; render(); }
  if (action === 'reset') { localStorage.removeItem('stavGroups'); localStorage.removeItem('stavDecisions'); window.location.reload(); }
  if (action === 'export') {
    const blob = new Blob([JSON.stringify({ decisions: state.decisions }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'stav-clustering-decisions.json';
    a.click();
    URL.revokeObjectURL(url);
  }
});

render();
