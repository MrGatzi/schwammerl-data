// Parity harness: runs the reference model (shroomzn habitat/pilz.js, loaded from $SHROOMZN_DIR) on cells
// and 256 x 256 patches the Python test hands over, and writes its results as binary files.
//
//   node parity_harness.mjs <pilz.js> <in.json> <out_dir>
//
// in.json: { planes: [{name, scale, offset}], atOwnHeight, dates: [[y, m, d]], species: [ids],
//            cells: {n, site: base64 (6 x 65536 packed site bytes), wx: base64 (6 x n weather bytes)},
//            latlon: {lat, lon: base64 float64}, wgeo: {n, w, s, e, nx, ny},
//            patches: [{site: base64 (6 x 65536), wx: base64 (6 x 65536)}] }
// out: widx.i32 (pilzWeatherAt's weather cell index for each latlon), score_<date>_<sp>.f64, level_<date>_<sp>.u8 (pilzScore / pilzLevel for each cell),
//      patch<i>_<date>_<sp>.raw.f32 / .shown.f32 / .lv.u8 (pilzRender's raw, scores and levels)
import fs from "fs";
import path from "path";

const [jsPath, inPath, outDir] = process.argv.slice(2);
const src = fs.readFileSync(jsPath, "utf8").split("\n");

// top-level declarations of the reference script (the rest needs a browser and Leaflet)
const NAMES = ["PILZ_GENERA", "PILZ_SPECIES", "PILZ_LEVELS", "PILZ_STEPS", "SHARE_W", "SLOPE_MID", "STREAM_DM",
  "pilzIrradiance", "pilzClamp", "pilzSeason", "pilzDecode", "pilzSite", "pilzSiteRank", "pilzScore", "pilzLut",
  "pilzLevel", "pilzWeatherAt", "pilzCompile", "pilzMemo", "pilzRender"];
function chunk(name) {
  const re = new RegExp(`^(const|let|function|async function) ${name}\\b`);
  const i = src.findIndex(l => re.test(l));
  if (i < 0) throw new Error(`not found in pilz.js: ${name}`);
  const out = [src[i]];
  for (let j = i + 1; j < src.length; j++) {
    const l = src[j];
    if (l.startsWith("}")) { out.push(l); break; }
    if (l === "" || /^\s/.test(l)) { out.push(l); continue; }
    break;
  }
  return out.join("\n");
}
const code = NAMES.map(chunk).join("\n");

const RealDate = Date;
const fixed = { y: 2026, m: 0, d: 1 };
class FakeDate extends RealDate {            // pilzRender asks pilzSeason for "today"
  constructor(...a) { if (a.length) super(...a); else super(fixed.y, fixed.m, fixed.d); }
}
class ImageData { constructor(w, h) { this.data = new Uint8ClampedArray(w * h * 4); this.width = w; this.height = h; } }
const state = { factor: null, pilzMode: "pilz", pilz: null };
const pilz = { wx: null, rendered: new Map() };
let tileData = null;
const pilzTile = () => Promise.resolve(tileData);
const M = new Function("Date", "state", "pilz", "pilzTile", "ImageData",
  code + "\nreturn { PILZ_SPECIES, pilzScore, pilzWeatherAt, pilzSeason, pilzLevel, pilzDecode, pilzRender };")(
  FakeDate, state, pilz, pilzTile, ImageData);

const inp = JSON.parse(fs.readFileSync(inPath, "utf8"));
const b64 = s => new Uint8Array(Buffer.from(s, "base64"));
const P = {};
inp.planes.forEach((p, i) => { P[p.name] = { i, s: p.scale, o: p.offset }; });
// the weather cell as pilzLoad builds it: value = byte / scale - offset, 255 = no data
function wxCell(wx, n, k) {
  const val = name => { const p = P[name]; if (!p) return null; const b = wx[p.i * n + k]; return b === 255 ? null : b / p.s - p.o; };
  const lag = val("rain_lag"), t0 = val("soil_t0");
  if (lag == null || t0 == null) return null;
  return { lag, t0, moist: val("moist"), rain7: val("rain7"), sun7: val("sun7"), air7: val("air7"), atOwnHeight: !!inp.atOwnHeight };
}
const write = (name, arr) => fs.writeFileSync(path.join(outDir, name), Buffer.from(arr.buffer, arr.byteOffset, arr.byteLength));

// ---- single cells: pilzScore + pilzLevel
const cn = inp.cells.n, csite = b64(inp.cells.site), cwx = b64(inp.cells.wx);
for (const [y, m, d] of inp.dates) {
  const tag = `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
  for (const id of inp.species) {
    const sp = M.PILZ_SPECIES[id], s = M.pilzSeason(sp, new RealDate(y, m - 1, d));
    const score = new Float64Array(cn), lvl = new Uint8Array(cn);
    for (let k = 0; k < cn; k++) {
      const r = M.pilzScore(sp, M.pilzDecode(csite, k), wxCell(cwx, cn, k), s);
      score[k] = r.score; lvl[k] = M.pilzLevel(r.score);
    }
    write(`score_${tag}_${id}.f64`, score);
    write(`level_${tag}_${id}.u8`, lvl);
  }
}

// ---- weather cell of a lat/lon: pilzWeatherAt with the reference's own n / dlat / dlon
{
  const g = inp.wgeo;
  pilz.wx = { n: g.n, w: g.w, nx: g.nx, ny: g.ny, dlon: (g.e - g.w) / g.nx, dlat: (g.n - g.s) / g.ny, cell: k => k };
  const f64 = s => { const u = b64(s); return new Float64Array(u.buffer, u.byteOffset, u.byteLength / 8); };
  const lat = f64(inp.latlon.lat), lon = f64(inp.latlon.lon), idx = new Int32Array(lat.length);
  for (let k = 0; k < lat.length; k++) { const r = M.pilzWeatherAt(lat[k], lon[k]); idx[k] = r == null ? -1 : r; }
  write("widx.i32", idx);
}

// ---- patches: pilzRender with a weather grid so fine that every pixel has its own weather cell
const Z = 10, X = 550, Y = 350, N = 2 ** Z, D = 1e-5;
const wcol = new Int32Array(256), wrow = new Int32Array(256);
const w = { n: 90, w: -180, dlat: D, dlon: D, nx: Math.round(360 / D), ny: Math.round(180 / D), doc: { generated: "" } };
for (let i = 0; i < 256; i++) {                      // the same expressions as pilzRender
  const lon = (X + (i + 0.5) / 256) / N * 360 - 180;
  const lat = Math.atan(Math.sinh(Math.PI * (1 - 2 * (Y + (i + 0.5) / 256) / N))) * 180 / Math.PI;
  wcol[i] = Math.floor((lon - w.w) / w.dlon);
  wrow[i] = Math.floor((w.n - lat) / w.dlat);
}
const pixelOf = new Map();
for (let r = 0; r < 256; r++) for (let c = 0; c < 256; c++) pixelOf.set(wrow[r] * w.nx + wcol[c], r * 256 + c);
if (pixelOf.size !== 65536) throw new Error("weather cells not one per pixel");
pilz.wx = w;
for (const [pi, patch] of inp.patches.entries()) {
  tileData = b64(patch.site);
  const pwx = b64(patch.wx);
  w.cell = wk => wxCell(pwx, 65536, pixelOf.get(wk));
  for (const [y, m, d] of inp.dates) {
    fixed.y = y; fixed.m = m - 1; fixed.d = d;
    const tag = `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    for (const id of inp.species) {
      state.pilz = id; pilz.rendered.clear();
      const img = await M.pilzRender(Z, X, Y);
      write(`patch${pi}_${tag}_${id}.raw.f32`, img.raw);
      write(`patch${pi}_${tag}_${id}.shown.f32`, img.scores);
      write(`patch${pi}_${tag}_${id}.lv.u8`, img.levels);
    }
  }
}
console.log("ok");
