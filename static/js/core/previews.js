/** Locate a frame in Jellyfin's tiled preview sheets. Times are in seconds. */
export function previewFrame(preview, seconds) {
  if (!preview || !preview.count || preview.interval <= 0 || !preview.columns || !preview.rows) return null;
  const frame = Math.min(preview.count - 1, Math.max(0, Math.floor(seconds * 1000 / preview.interval)));
  const perSheet = preview.columns * preview.rows;
  const sheet = Math.floor(frame / perSheet);
  const position = frame % perSheet;
  const scale = Math.min(1, 200 / preview.width);
  return {
    url: preview.url.replace('{index}', String(sheet)),
    width: preview.width * scale,
    height: preview.height * scale,
    size: `${preview.width * preview.columns * scale}px ${preview.height * preview.rows * scale}px`,
    position: `${-(position % preview.columns) * preview.width * scale}px ${-Math.floor(position / preview.columns) * preview.height * scale}px`,
  };
}
