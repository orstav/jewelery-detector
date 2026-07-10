import fs from 'node:fs';

const app = fs.readFileSync('src/main.jsx', 'utf8');
const shared = fs.readFileSync('src/sharedSession.js', 'utf8');
const api = fs.readFileSync('api/identity-session.js', 'utf8');
const tool = fs.readFileSync('tools/hal_export_identity_session.mjs', 'utf8');

const required = [
  [shared, 'loadRemoteState'],
  [shared, 'saveRemoteState'],
  [shared, 'deleteRemoteState'],
  [api, 'VERCEL_KV_REST_API_URL'],
  [api, 'STAV_IDENTITY_SESSION_DIR'],
  [api, 'no_live_writes: true'],
  [tool, 'exportPacketBundle'],
  [app, 'syncStatusLabel'],
  [app, 'backend-sync'],
  [app, 'sourceAssets: SOURCE_ASSETS'],
];
const missing = required.filter(([text, needle]) => !text.includes(needle)).map(([, needle]) => needle);
if (missing.length) {
  console.error(`Missing backend contract markers: ${missing.join(', ')}`);
  process.exit(1);
}
console.log('Verified shared backend/session contract and HAL export path.');
