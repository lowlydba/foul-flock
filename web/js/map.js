/* The MapLibre map instance and the PMTiles protocol. Needs the maplibregl
   and pmtiles globals (classic scripts loaded before the modules). */

// Static layers (states, places, clear cameras and their cones) are PMTiles
// archives in web/data, read by HTTP range request through this protocol.
maplibregl.addProtocol("pmtiles", new pmtiles.Protocol().tile);
export const tileSource = (name) => ({
  type: "vector",
  url: `pmtiles://${new URL(`data/${name}`, location.href).href}`,
});

export const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: {
      osm: {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: "© OpenStreetMap contributors",
      },
    },
    layers: [{ id: "osm", type: "raster", source: "osm" }],
  },
  center: [-98, 39],
  zoom: 4,
  attributionControl: {
    compact: false,
    customAttribution:
      '<a href="https://overturemaps.org" target="_blank" rel="noopener">&copy; Overture Maps Foundation</a> | ' +
      'Made by <a href="https://github.com/lowlydba" target="_blank" rel="noopener">lowlydba</a>',
  },
});
const US_BOUNDS = [[-188, 18.5], [-66.5, 72]];
function updateMinZoom() {
  const camera = map.cameraForBounds(US_BOUNDS);
  if (camera) map.setMinZoom(camera.zoom);
}
updateMinZoom();
map.on("resize", updateMinZoom);
map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "bottom-right");
map.addControl(new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }), "bottom-left");
map.addControl(new maplibregl.FullscreenControl(), "bottom-right");
