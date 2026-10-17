/* Protected-place badge icons, composed on a canvas from Maki glyphs. */

/* Protected-place icons: Maki (Mapbox's CC0 map icon set), drawn as a
   white glyph on an orange badge. web/assets/icons/ holds the SVGs. */
export const PLACE_ICONS = {
  "ff-school": "school",
  "ff-health": "hospital",
  "ff-court": "town-hall",
  "ff-food": "grocery",
  "ff-worship": "place-of-worship",
  "ff-worship-christian": "religious-christian",
  "ff-worship-muslim": "religious-muslim",
  "ff-worship-jewish": "religious-jewish",
  "ff-place": "marker",
};
const BADGE_FILL = "#c2620d";
const BADGE_RING = "rgba(0, 0, 0, .35)";

export async function loadPlaceIcon(map, name, file) {
  const svgText = await (await fetch(`assets/icons/${file}.svg`)).text();
  const white = svgText.replace(
    "<svg ",
    '<svg fill="#ffffff" '
  );
  const glyph = new Image();
  await new Promise((resolve, reject) => {
    glyph.onload = resolve;
    glyph.onerror = reject;
    glyph.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(white);
  });
  const size = 56;
  const c = document.createElement("canvas");
  c.width = size;
  c.height = size;
  const ctx = c.getContext("2d");
  const r = 10; // corner radius: a rounded square reads as a place, not a camera dot
  const m = 2;
  ctx.beginPath();
  ctx.moveTo(m + r, m);
  ctx.arcTo(size - m, m, size - m, size - m, r);
  ctx.arcTo(size - m, size - m, m, size - m, r);
  ctx.arcTo(m, size - m, m, m, r);
  ctx.arcTo(m, m, size - m, m, r);
  ctx.closePath();
  ctx.fillStyle = BADGE_FILL;
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = BADGE_RING;
  ctx.stroke();
  const g = 32; // glyph box
  ctx.drawImage(glyph, (size - g) / 2, (size - g) / 2, g, g);
  map.addImage(name, ctx.getImageData(0, 0, size, size), { pixelRatio: 2 });
}
