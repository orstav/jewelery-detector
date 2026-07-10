export const SYNC_DISABLED = 'disabled';

function queryParams() {
  try { return new URLSearchParams(window.location.search); } catch { return new URLSearchParams(); }
}

function sessionSlug(value) {
  return String(value || 'stav-session').replace(/[^a-zA-Z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80) || 'stav-session';
}

export function sharedSessionConfig(batchId) {
  const params = queryParams();
  const backendParam = params.get('backend');
  const endpoint = backendParam && backendParam !== 'off' ? backendParam : (window.STAV_IDENTITY_BACKEND || '/api/identity-session');
  return {
    enabled: backendParam !== 'off',
    endpoint,
    sessionId: sessionSlug(params.get('session') || window.STAV_IDENTITY_SESSION_ID || `stav-${batchId}`),
  };
}

function sessionUrl(config, batchId) {
  const url = new URL(config.endpoint, window.location.origin);
  url.searchParams.set('session_id', config.sessionId);
  url.searchParams.set('batch_id', batchId);
  return url.toString();
}

export function localState(storageKey) {
  try {
    const parsed = JSON.parse(localStorage.getItem(storageKey) || 'null');
    if (parsed && Array.isArray(parsed.tasks) && Array.isArray(parsed.decisions) && Array.isArray(parsed.buckets)) return parsed;
  } catch {}
  return null;
}

export function saveLocalState(storageKey, state) {
  localStorage.setItem(storageKey, JSON.stringify(state, null, 2));
}

export async function loadRemoteState(config, batchId) {
  if (!config.enabled) return {status: SYNC_DISABLED, remoteAvailable: false};
  try {
    const response = await fetch(sessionUrl(config, batchId), {headers: {'accept': 'application/json'}});
    if (response.status === 404) return {status: 'ready_empty', remoteAvailable: true};
    if (response.status === 501) return {status: 'not_configured', remoteAvailable: false, message: 'backend_not_configured'};
    if (!response.ok) return {status: 'error', remoteAvailable: false, message: `load_failed_${response.status}`};
    const payload = await response.json();
    return {status: payload.state ? 'loaded' : 'ready_empty', remoteAvailable: true, state: payload.state || null, updatedAt: payload.updatedAt};
  } catch (error) {
    return {status: 'offline', remoteAvailable: false, message: error?.message || 'network_error'};
  }
}

export async function saveRemoteState(config, batchId, state) {
  if (!config.enabled) return {status: SYNC_DISABLED};
  try {
    const response = await fetch(sessionUrl(config, batchId), {
      method: 'PUT',
      headers: {'content-type': 'application/json', 'accept': 'application/json'},
      body: JSON.stringify({state}),
    });
    if (response.status === 409) {
      const payload = await response.json().catch(() => ({}));
      return {status: 'conflict', message: 'stale_revision', currentRevision: payload.currentRevision};
    }
    if (response.status === 501) return {status: 'not_configured', message: 'backend_not_configured'};
    if (!response.ok) return {status: 'error', message: `save_failed_${response.status}`};
    const payload = await response.json();
    return {status: 'saved', updatedAt: payload.updatedAt, revision: payload.revision};
  } catch (error) {
    return {status: 'offline', message: error?.message || 'network_error'};
  }
}

export async function deleteRemoteState(config, batchId) {
  if (!config.enabled) return {status: SYNC_DISABLED};
  try {
    const response = await fetch(sessionUrl(config, batchId), {method: 'DELETE', headers: {'accept': 'application/json'}});
    if (response.status === 501) return {status: 'not_configured', message: 'backend_not_configured'};
    if (!response.ok && response.status !== 404) return {status: 'error', message: `delete_failed_${response.status}`};
    return {status: 'deleted'};
  } catch (error) {
    return {status: 'offline', message: error?.message || 'network_error'};
  }
}

export function syncStatusLabel(status) {
  return {
    checking: 'בודק סנכרון',
    saving: 'שומר לשרת',
    loaded: 'מסונכרן בין מכשירים',
    saved: 'נשמר לענן',
    ready_empty: 'סשן ציבורי מוכן',
    local: 'שמירה מקומית בלבד',
    not_configured: 'צריך Backend ציבורי',
    offline: 'אין חיבור ל־Backend',
    error: 'שגיאת סנכרון',
    conflict: 'יש שינוי חדש יותר — צריך לרענן',
    disabled: 'סנכרון כבוי',
  }[status] || 'שמירה מקומית בלבד';
}
