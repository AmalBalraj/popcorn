import test from 'node:test';
import assert from 'node:assert/strict';
import { previewFrame } from '../static/js/core/previews.js';
const preview = { width: 320, height: 180, columns: 10, rows: 10, count: 220, interval: 10000, url: '/sheet/{index}.jpg' };
test('preview sheets advance on frame 100 and clamp at the final frame', () => {
  assert.equal(previewFrame(preview, 999).url, '/sheet/0.jpg');
  assert.equal(previewFrame(preview, 1000).url, '/sheet/1.jpg');
  const end = previewFrame(preview, 5000);
  assert.equal(end.url, '/sheet/2.jpg');
  assert.equal(end.position, '-1800px -112.5px');
  assert.equal(previewFrame(preview, -10).position, '0px 0px');
});
test('missing or incomplete preview metadata leaves the time tooltip usable', () => {
  assert.equal(previewFrame(null, 30), null);
  assert.equal(previewFrame({ ...preview, count: 0 }, 30), null);
  assert.equal(previewFrame({ ...preview, interval: 0 }, 30), null);
});
