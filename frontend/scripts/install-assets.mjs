import { readdir, mkdir, copyFile, rename } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const source = path.join(root, 'dist');
const target = path.resolve(root, '../folder_kb/web');
// Publish index last. Keep previous hashed assets so already open pages still work.
await mkdir(target, { recursive: true });
for (const name of await readdir(source)) {
  if (name !== 'index.html') await copyFile(path.join(source, name), path.join(target, name));
}
await copyFile(path.join(source, 'index.html'), path.join(target, 'index.html.next'));
await rename(path.join(target, 'index.html.next'), path.join(target, 'index.html'));
console.log('Admin assets installed in folder_kb/web');
