import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('./data.js', import.meta.url), 'utf8');
const sandbox = { window: {} };
vm.runInNewContext(source, sandbox, { filename: 'data.js' });

const groups = sandbox.window.STAV_REAL_GROUPS || [];
const stats = sandbox.window.STAV_REAL_DATASET_STATS || {};
const photos = groups.reduce((sum, group) => sum + group.photos.length, 0);
const ids = new Set(groups.map((group) => group.id));
const rawTitle = groups.find((group) => /pg_[a-f0-9]+/i.test(group.title || ''));
const missingPhotoGroup = groups.find((group) => !Array.isArray(group.photos) || group.photos.length === 0);

if (ids.size !== groups.length) {
  throw new Error('Group ids must be unique.');
}
if (rawTitle) {
  throw new Error(`Raw group id leaked into title for ${rawTitle.id}.`);
}
if (missingPhotoGroup) {
  throw new Error(`Group ${missingPhotoGroup.id} has no photos.`);
}
if (stats.groups_exported !== groups.length) {
  throw new Error(`groups_exported=${stats.groups_exported} but data has ${groups.length} groups.`);
}
if (stats.photos_exported !== photos) {
  throw new Error(`photos_exported=${stats.photos_exported} but data has ${photos} photos.`);
}

console.log(`Verified ${groups.length} groups and ${photos} photos.`);
