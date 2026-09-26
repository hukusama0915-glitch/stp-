const state = {
  master: { tools: [], conditions: [], machines: [], manufacturer_catalogs: [], manufacturer_cutting_conditions: [] },
  lastResult: null,
  preview: null,
  previewView: { yaw: -0.68, pitch: -0.46, zoom: 1, dragging: false, lastX: 0, lastY: 0, preset: "iso" },
  cadPreview: { mode: "fallback", renderer: null, scene: null, camera: null, group: null, baseRadius: 1, occt: null, displayMode: "shaded", toolpathGroup: null, showPaths: true, originalGroup: null, filledGroup: null, fillBodiesGroup: null, modelView: "original", fillToken: null },
  excluded: new Set(),
  highlightedKey: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

// innerHTMLへ差し込む値は必ずエスケープする（ファイル名・機械名などの利用者入力を含むため）
const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
}

// ワイヤ条件などの入力値をブラウザに保存する（保存できない環境では何もしない）
const PERSIST_PREFIX = "stp-tool:";
function readPersisted(key) {
  try {
    return window.localStorage.getItem(PERSIST_PREFIX + key);
  } catch {
    return null;
  }
}

function writePersisted(key, value) {
  try {
    window.localStorage.setItem(PERSIST_PREFIX + key, value);
  } catch {
    // 保存できなくても計算には影響しない
  }
}

function restorePersistedInputs() {
  $$("input[data-persist]").forEach((input) => {
    const saved = readPersisted(input.dataset.persist);
    if (saved !== null) input.value = saved;
  });
}

function safeUrl(value) {
  const url = String(value || "");
  return /^https?:\/\//i.test(url) ? esc(url) : "#";
}

function secLabel(value) {
  const total = Math.round(Number(value) || 0);
  const h = String(Math.floor(total / 3600)).padStart(2, "0");
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const s = String(total % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

function machiningFeatureSummary(features) {
  if (!features) return "-";
  const parts = [];
  const addCount = (label, rows) => {
    const count = (rows || []).reduce((total, row) => total + Number(row.count || 0), 0);
    if (count > 0) parts.push(`${label} ${count}`);
  };
  addCount("穴", features.holes);
  addCount("横穴", features.side_holes);
  addCount("座ぐり", features.counterbores);
  addCount("皿もみ", features.countersinks);
  addCount("スロット", features.slots);
  addCount("深穴", features.deep_holes);
  addCount("面取り", features.chamfers);
  addCount("小R", features.corner_radii);
  if (Number.isFinite(features.roughing_volume_mm3)) {
    parts.push(`荒取り ${Math.round(features.roughing_volume_mm3).toLocaleString()} mm3`);
  }
  return parts.length ? parts.join(" / ") : "-";
}

function toast(message) {
  const box = $("#toast");
  box.textContent = message;
  box.classList.remove("hidden");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => box.classList.add("hidden"), 3200);
}

async function jsonFetch(url, options = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch {
    throw new Error("サーバーに接続できませんでした。ネットワークを確認してください。");
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    // HTMLのエラーページなどJSON以外が返った場合
  }
  if (!response.ok) {
    throw new Error(data?.error || `処理に失敗しました（HTTP ${response.status}）。`);
  }
  if (data === null) throw new Error("サーバーの応答を読み取れませんでした。");
  return data;
}

function formJson(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  for (const key of Object.keys(data)) {
    if (data[key] !== "" && !Number.isNaN(Number(data[key]))) data[key] = Number(data[key]);
  }
  return data;
}

function resetMachineForm() {
  const form = $("#machineForm");
  if (!form) return;
  form.reset();
  form.elements.machine_id.value = "";
  form.elements.axis_count.value = 3;
  $("#machineSubmitButton").textContent = "追加";
  $("#machineResetButton")?.classList.add("hidden");
}

function fillMachineForm(machine) {
  const form = $("#machineForm");
  if (!form || !machine) return;
  form.elements.machine_id.value = machine.machine_id;
  form.elements.machine_name.value = machine.machine_name || "";
  form.elements.axis_count.value = machine.axis_count ?? 3;
  form.elements.rapid_feed_mm_min.value = machine.rapid_feed_mm_min ?? "";
  form.elements.atc_time_sec.value = machine.atc_time_sec ?? "";
  form.elements.max_spindle_rpm.value = machine.max_spindle_rpm ?? "";
  form.elements.max_tool_diameter_mm.value = machine.max_tool_diameter_mm ?? "";
  form.elements.setup_time_min.value = machine.setup_time_min ?? "";
  $("#machineSubmitButton").textContent = "更新";
  $("#machineResetButton")?.classList.remove("hidden");
  form.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function numberLabel(value, digits = 1) {
  if (!Number.isFinite(value)) return "-";
  return value.toFixed(digits);
}

function parseStepPreview(text, file) {
  const entityCount = (text.match(/^#\d+\s*=/gm) || []).length;
  const faceCount = (text.match(/ADVANCED_FACE|FACE_BOUND/gi) || []).length;
  const planeCount = (text.match(/\bPLANE\s*\(/gi) || []).length;
  const pointMatches = Array.from(text.matchAll(
    /#(\d+)\s*=\s*CARTESIAN_POINT\s*\([^,]*,\s*\(\s*([0-9.+\-Ee]+)\s*,\s*([0-9.+\-Ee]+)\s*,\s*([0-9.+\-Ee]+)\s*\)\s*\)/gi,
  ));
  const pointById = new Map();
  const points = pointMatches
    .slice(0, 20000)
    .map((match) => {
      const point = [Number(match[2]), Number(match[3]), Number(match[4])];
      if (point.every(Number.isFinite)) pointById.set(match[1], point);
      return point;
    })
    .filter((point) => point.every(Number.isFinite));

  const vertexPointById = new Map();
  for (const match of text.matchAll(/#(\d+)\s*=\s*VERTEX_POINT\s*\([^,]*,\s*#(\d+)\s*\)/gi)) {
    const point = pointById.get(match[2]);
    if (point) vertexPointById.set(match[1], point);
  }

  const edgeKeys = new Set();
  const edges = [];
  for (const match of text.matchAll(/#(\d+)\s*=\s*EDGE_CURVE\s*\([^,]*,\s*#(\d+)\s*,\s*#(\d+)/gi)) {
    const start = vertexPointById.get(match[2]);
    const end = vertexPointById.get(match[3]);
    if (!start || !end) continue;
    const key = `${start.join(",")}|${end.join(",")}`;
    const reverseKey = `${end.join(",")}|${start.join(",")}`;
    if (edgeKeys.has(key) || edgeKeys.has(reverseKey)) continue;
    edgeKeys.add(key);
    edges.push({ start, end });
  }

  const axisPointById = new Map();
  for (const match of text.matchAll(/#(\d+)\s*=\s*AXIS2_PLACEMENT_3D\s*\([^,]*,\s*#(\d+)/gi)) {
    axisPointById.set(match[1], pointById.get(match[2]) || null);
  }
  const circles = Array.from(text.matchAll(/#(\d+)\s*=\s*CIRCLE\s*\([^,]+,\s*#(\d+),\s*([0-9.+\-Ee]+)/gi))
    .map((match) => ({
      radius: Number(match[3]),
      center: axisPointById.get(match[2]) || null,
    }))
    .filter((item) => Number.isFinite(item.radius) && item.radius > 0);
  const cylinders = Array.from(text.matchAll(/#(\d+)\s*=\s*CYLINDRICAL_SURFACE\s*\([^,]+,\s*#(\d+),\s*([0-9.+\-Ee]+)/gi))
    .map((match) => ({
      radius: Number(match[3]),
      center: axisPointById.get(match[2]) || null,
    }))
    .filter((item) => Number.isFinite(item.radius) && item.radius > 0);

  let bbox = null;
  if (points.length) {
    const xs = points.map((point) => point[0]);
    const ys = points.map((point) => point[1]);
    const zs = points.map((point) => point[2]);
    bbox = {
      minX: Math.min(...xs),
      maxX: Math.max(...xs),
      minY: Math.min(...ys),
      maxY: Math.max(...ys),
      minZ: Math.min(...zs),
      maxZ: Math.max(...zs),
    };
    bbox.x = bbox.maxX - bbox.minX;
    bbox.y = bbox.maxY - bbox.minY;
    bbox.z = bbox.maxZ - bbox.minZ;
  }

  return {
    fileName: file.name,
    fileSize: file.size,
    truncated: text.length < file.size,
    entityCount,
    faceCount,
    planeCount,
    cylinders,
    circles,
    edges,
    points,
    bbox,
  };
}

function rotatePoint(point, model) {
  const x = point[0] - model.cx;
  const y = point[1] - model.cy;
  const z = point[2] - model.cz;
  const yaw = state.previewView.yaw;
  const pitch = state.previewView.pitch;
  const cosY = Math.cos(yaw);
  const sinY = Math.sin(yaw);
  const cosP = Math.cos(pitch);
  const sinP = Math.sin(pitch);
  const rx = x * cosY - y * sinY;
  const ry = x * sinY + y * cosY;
  const rz = z;
  return {
    x: rx,
    y: ry * cosP - rz * sinP,
    z: ry * sinP + rz * cosP,
  };
}

function project3d(point, model, scale, origin) {
  const rotated = rotatePoint(point, model);
  return {
    x: origin.x + rotated.x * scale,
    y: origin.y - rotated.y * scale,
    z: rotated.z,
  };
}

function setPreviewMode(mode) {
  state.cadPreview.mode = mode;
  const fallbackCanvas = $("#stpPreviewCanvas");
  const cadCanvas = $("#cadPreviewCanvas");
  if (!fallbackCanvas || !cadCanvas) return;
  fallbackCanvas.classList.toggle("hidden", mode === "cad");
  cadCanvas.classList.toggle("hidden", mode !== "cad");
}

function renderCurrentPreview() {
  if (state.cadPreview.mode === "cad" && state.cadPreview.renderer) {
    renderCadScene();
  } else if (state.preview) {
    drawStepPreview(state.preview);
  } else {
    drawPreviewPlaceholder();
  }
}

function updateViewerControls() {
  $$("[data-view-preset]").forEach((button) => {
    button.classList.toggle("active", button.dataset.viewPreset === state.previewView.preset);
  });
  $$("[data-view-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.viewMode === state.cadPreview.displayMode);
  });
  const cube = $("#viewCube");
  if (cube) cube.querySelector("strong").textContent = state.previewView.preset.toUpperCase();
}

function applyViewPreset(preset) {
  const presets = {
    iso: { yaw: -0.72, pitch: -0.58 },
    top: { yaw: 0, pitch: -1.5 },
    front: { yaw: 0, pitch: 0 },
    right: { yaw: -Math.PI / 2, pitch: 0 },
  };
  const next = presets[preset] || presets.iso;
  state.previewView.yaw = next.yaw;
  state.previewView.pitch = next.pitch;
  state.previewView.preset = preset in presets ? preset : "iso";
  updateViewerControls();
  renderCurrentPreview();
}

function fitPreview() {
  state.previewView.zoom = 1;
  renderCurrentPreview();
}

function applyCadDisplayMode(mode) {
  state.cadPreview.displayMode = mode;
  const group = state.cadPreview.group;
  if (group) {
    group.traverse((object) => {
      if (object.userData && object.userData.overlay) return;
      if (object.isMesh) {
        object.material.wireframe = mode === "wire";
        object.material.transparent = mode === "transparent";
        object.material.opacity = mode === "transparent" ? 0.48 : 1;
        object.material.depthWrite = mode !== "transparent";
        object.material.needsUpdate = true;
      }
      if (object.isLineSegments) {
        object.visible = mode !== "wire";
        object.material.opacity = mode === "transparent" ? 0.72 : 0.42;
      }
    });
  }
  updateViewerControls();
  renderCurrentPreview();
}

function drawProjectedEllipse(ctx, center, radiusMm, model, scale, origin, options = {}) {
  const c = project3d(center, model, scale, origin);
  const xAxis = project3d([center[0] + radiusMm, center[1], center[2]], model, scale, origin);
  const yAxis = project3d([center[0], center[1] + radiusMm, center[2]], model, scale, origin);
  const rx = Math.hypot(xAxis.x - c.x, xAxis.y - c.y);
  const ry = Math.hypot(yAxis.x - c.x, yAxis.y - c.y);
  const rotation = Math.atan2(xAxis.y - c.y, xAxis.x - c.x);
  ctx.beginPath();
  ctx.ellipse(c.x, c.y, Math.max(2.5, rx), Math.max(2.5, ry), rotation, 0, Math.PI * 2);
  if (options.fillStyle) {
    ctx.fillStyle = options.fillStyle;
    ctx.fill();
  }
  ctx.strokeStyle = options.strokeStyle || "#a3392f";
  ctx.lineWidth = options.lineWidth || 1.2;
  ctx.stroke();
  return c;
}

function initCadPreview() {
  if (!window.THREE) throw new Error("Three.jsを読み込めませんでした。");
  const canvas = $("#cadPreviewCanvas");
  const THREE = window.THREE;

  if (!state.cadPreview.renderer) {
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputEncoding = THREE.sRGBEncoding;
    const scene = new THREE.Scene();
    const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 1000000);
    camera.up.set(0, 0, 1);

    scene.add(new THREE.HemisphereLight(0xf4f4f4, 0x5f635f, 1.45));
    const keyLight = new THREE.DirectionalLight(0xffffff, 1.35);
    keyLight.position.set(1.6, -2.4, 2.8);
    scene.add(keyLight);
    const rimLight = new THREE.DirectionalLight(0xffffff, 0.62);
    rimLight.position.set(-2.8, 2.2, 1.6);
    scene.add(rimLight);

    state.cadPreview.renderer = renderer;
    state.cadPreview.scene = scene;
    state.cadPreview.camera = camera;
  }

  if (state.cadPreview.group) {
    state.cadPreview.scene.remove(state.cadPreview.group);
  }
}

function buildCadMesh(geometryMesh) {
  const THREE = window.THREE;
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(geometryMesh.attributes.position.array, 3));
  if (geometryMesh.attributes.normal) {
    geometry.setAttribute("normal", new THREE.Float32BufferAttribute(geometryMesh.attributes.normal.array, 3));
  } else {
    geometry.computeVertexNormals();
  }
  if (geometryMesh.index?.array) {
    geometry.setIndex(new THREE.BufferAttribute(Uint32Array.from(geometryMesh.index.array), 1));
  }

  const color = geometryMesh.color || [0.78, 0.82, 0.78];
  const material = new THREE.MeshStandardMaterial({
    color: new THREE.Color(color[0], color[1], color[2]),
    metalness: 0.06,
    roughness: 0.36,
    side: THREE.DoubleSide,
  });
  const mesh = new THREE.Mesh(geometry, material);

  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(geometry, 28),
    new THREE.LineBasicMaterial({ color: 0x202020, transparent: true, opacity: 0.42 }),
  );
  return { mesh, edges };
}

function meshGroupFromOcct(result) {
  const group = new window.THREE.Group();
  for (const geometryMesh of result.meshes) {
    const { mesh, edges } = buildCadMesh(geometryMesh);
    group.add(mesh);
    group.add(edges);
  }
  return group;
}

function readStepMeshes(buffer) {
  const result = state.cadPreview.occt.ReadStepFile(new Uint8Array(buffer), {
    linearUnit: "millimeter",
    linearDeflectionType: "bounding_box_ratio",
    linearDeflection: 0.0008,
    angularDeflection: 0.35,
  });
  if (!result.success || !result.meshes?.length) throw new Error("STEP形状をメッシュ化できませんでした。");
  return result;
}

// 埋めたモデルと「埋めた部分」を読み込み、元モデルと同じ座標系で重ねる
async function loadFillModels(token) {
  const cp = state.cadPreview;
  $("#modelViewButtons")?.classList.add("hidden");
  if (cp.filledGroup) cp.group?.remove(cp.filledGroup);
  if (cp.fillBodiesGroup) cp.group?.remove(cp.fillBodiesGroup);
  cp.filledGroup = null;
  cp.fillBodiesGroup = null;
  cp.fillToken = token || null;
  if (!token || !cp.group || !cp.occt || cp.mode !== "cad") {
    setModelView("original");
    return;
  }
  const fetchBuffer = async (kind) => {
    const response = await fetch(`/api/fill-models/${encodeURIComponent(token)}/${kind}`);
    if (!response.ok) throw new Error("埋めたモデルを取得できませんでした（保存期間切れの可能性があります）。");
    return response.arrayBuffer();
  };
  const [filledBuffer, bodiesBuffer] = await Promise.all([fetchBuffer("filled"), fetchBuffer("bodies")]);
  if (cp.fillToken !== token) return; // 読み込み中に別の結果へ切り替わった
  const THREE = window.THREE;
  const filledGroup = meshGroupFromOcct(readStepMeshes(filledBuffer));
  const bodiesGroup = meshGroupFromOcct(readStepMeshes(bodiesBuffer));
  bodiesGroup.traverse((object) => {
    object.userData.overlay = true; // 表示モード（ワイヤ/透過）の切替対象外
    if (object.isMesh) {
      object.material = new THREE.MeshStandardMaterial({
        color: 0xf08a24, roughness: 0.5, metalness: 0.05, transparent: true, opacity: 0.88, side: THREE.DoubleSide,
        // 穴の壁と同一面になるため、手前に描いてちらつきを防ぐ
        polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1,
      });
    }
    if (object.isLineSegments) object.material = new THREE.LineBasicMaterial({ color: 0x9a4a06, transparent: true, opacity: 0.7 });
  });
  cp.group.add(filledGroup);
  cp.group.add(bodiesGroup);
  cp.filledGroup = filledGroup;
  cp.fillBodiesGroup = bodiesGroup;
  applyCadDisplayMode(cp.displayMode);
  $("#modelViewButtons")?.classList.remove("hidden");
  setModelView("compare");
}

function setModelView(view) {
  const cp = state.cadPreview;
  const hasFill = Boolean(cp.filledGroup && cp.fillBodiesGroup);
  const next = hasFill ? view : "original";
  cp.modelView = next;
  if (cp.originalGroup) cp.originalGroup.visible = next !== "filled";
  if (cp.filledGroup) cp.filledGroup.visible = next === "filled";
  if (cp.fillBodiesGroup) cp.fillBodiesGroup.visible = next === "compare";
  $$("[data-model-view]").forEach((button) => button.classList.toggle("active", button.dataset.modelView === next));
  renderCurrentPreview();
}

async function loadDetailedCadPreview(buffer) {
  if (!window.occtimportjs) throw new Error("OpenCascade WASMを読み込めませんでした。");
  initCadPreview();

  if (!state.cadPreview.occt) {
    state.cadPreview.occt = await window.occtimportjs();
  }

  const result = readStepMeshes(buffer);

  const THREE = window.THREE;
  const group = new THREE.Group();
  const originalGroup = meshGroupFromOcct(result);
  group.add(originalGroup);

  const box = new THREE.Box3().setFromObject(group);
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  group.position.sub(center);

  state.cadPreview.group = group;
  state.cadPreview.originalGroup = originalGroup;
  state.cadPreview.filledGroup = null;
  state.cadPreview.fillBodiesGroup = null;
  state.cadPreview.fillToken = null;
  setModelView("original");
  state.cadPreview.baseRadius = Math.max(size.x, size.y, size.z, 1);
  state.cadPreview.scene.add(group);
  applyCadDisplayMode(state.cadPreview.displayMode);
  setPreviewMode("cad");
  renderCadScene();
  if (state.lastResult) renderToolpaths(state.lastResult);
}

function renderFillPanel(result) {
  const panel = $("#fillPanel");
  if (!panel) return;
  const fill = result.fill;
  panel.classList.toggle("hidden", !fill);
  if (!fill) return;
  const rows = fill.items || [];
  const badge = $("#fillBadge");
  const summary = $("#fillSummary");
  if (fill.error || !rows.length) {
    badge.textContent = fill.error ? "埋め不可" : "対象なし";
    badge.className = "fill-badge warn";
    summary.innerHTML = `<p class="fill-message">${esc(fill.error || fill.message || "")}</p>`;
    $("#processCards").innerHTML = "";
    $("#ncHoleSection").classList.add("hidden");
    $("#wireSection").classList.add("hidden");
    return;
  }
  badge.className = "fill-badge";
  badge.textContent = `ドリル穴 ${fill.drill_count}箇所 / ワイヤ形状 ${fill.wire_count}箇所`;
  const original = Number(fill.original_total_sec) || 0;
  const current = Number(result.breakdown.total_sec) || 0;
  const diff = current - original;
  summary.innerHTML = `
    <div><span>元モデル</span><strong>${esc(fill.original_time_label || secLabel(original))}</strong></div>
    <div class="fill-arrow" aria-hidden="true">→</div>
    <div><span>MCのみ（埋めたモデル）</span><strong>${esc(result.time_label || secLabel(current))}</strong></div>
    <div><span>差</span><strong class="${diff <= 0 ? "minus" : "plus"}">${diff <= 0 ? "-" : "+"}${secLabel(Math.abs(diff))}</strong></div>
    <div><span>ワイヤ切断面積 合計</span><strong>${Number(fill.wire_cut_area_mm2 || 0).toLocaleString()} mm²</strong></div>
  `;
  renderProcessPlans(result, fill.processes || {});
}

function processTimeLabel(sec) {
  return Number.isFinite(Number(sec)) && sec !== null ? secLabel(sec) : "未算出";
}

function renderProcessPlans(result, processes) {
  const nc = processes.nc_holes;
  const wire = processes.wire;
  const mcSec = Number(result.breakdown.total_sec) || 0;
  const parts = [{ label: "MC（埋めたモデル）", sec: mcSec, kind: "mc" }];
  if (nc) parts.push({ label: `NC穴加工${nc.machine_name ? `（${nc.machine_name}）` : ""}`, sec: nc.total_sec, kind: "drill" });
  if (wire) parts.push({ label: "ワイヤカット", sec: wire.total_sec, kind: "wire" });
  const computed = parts.filter((p) => p.sec !== null && p.sec !== undefined);
  const total = computed.reduce((sum, p) => sum + Number(p.sec), 0);
  const incomplete = computed.length < parts.length;
  $("#processCards").innerHTML = `
    ${parts.map((p) => `
      <div class="process-card ${p.kind}">
        <span>${esc(p.label)}</span>
        <strong>${processTimeLabel(p.sec)}</strong>
      </div>`).join("")}
    <div class="process-card total">
      <span>工程合計${incomplete ? "（未算出の工程を除く）" : ""}</span>
      <strong>${secLabel(total)}</strong>
    </div>
  `;

  const ncSection = $("#ncHoleSection");
  ncSection.classList.toggle("hidden", !nc);
  if (nc) {
    $("#ncHoleMeta").textContent = nc.total_sec !== null && nc.total_sec !== undefined
      ? ` 切削 ${secLabel(nc.cutting_sec)} / 早送り ${secLabel(nc.rapid_sec)} / 補正 ${secLabel(nc.allowance_sec)} / 工具交換 ${nc.tool_count}本 ${secLabel(nc.tool_change_sec)} / 段取り ${secLabel(nc.setup_sec)}（加工方向 ${(nc.directions || []).join("・")}）`
      : ` ${nc.message || ""}`;
    $("#ncHoleRows").innerHTML = (nc.rows || []).map((row) => `
      <tr class="${row.sec === null ? "row-uncomputed" : ""}">
        <td>${esc(row.kind)}</td>
        <td>${esc(row.dimensions)}</td>
        <td>${esc(row.count)}</td>
        <td>${row.rpm === null ? "-" : Number(row.rpm).toLocaleString()}</td>
        <td>${row.feed_mm_min === null ? "-" : esc(row.feed_mm_min)}</td>
        <td>${processTimeLabel(row.sec)}</td>
        <td class="condition-cell">${esc(row.note)}</td>
      </tr>
    `).join("");
  }

  const wireSection = $("#wireSection");
  wireSection.classList.toggle("hidden", !wire);
  if (wire) {
    const params = wire.params || {};
    $("#wireMeta").textContent = wire.total_sec !== null && wire.total_sec !== undefined
      ? ` 荒 ${params.rough_speed_mm2_min} mm²/分 / 仕上げ ${params.skim_count}回${params.skim_count ? ` ${params.skim_speed_mm_min} mm/分` : ""} / 段取り ${secLabel(wire.setup_sec)}`
      : "";
    const message = $("#wireMessage");
    message.classList.toggle("hidden", !wire.message);
    message.textContent = wire.message || "";
    $("#wireRows").innerHTML = (wire.rows || []).map((row) => `
      <tr>
        <td>${esc(row.kind)}</td>
        <td>${esc(row.dimensions)}</td>
        <td>${esc(row.count)}</td>
        <td>${Number(row.cut_area_mm2).toLocaleString()} mm²</td>
        <td>${processTimeLabel(row.sec)}</td>
      </tr>
    `).join("");
  }
}

function renderCadScene() {
  const { renderer, scene, camera, group, baseRadius } = state.cadPreview;
  if (!renderer || !scene || !camera || !group) return;
  const canvas = $("#cadPreviewCanvas");
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.round(rect.width));
  const height = Math.max(1, Math.round(rect.height));
  renderer.setSize(width, height, false);

  const distance = (baseRadius * 1.85) / Math.max(state.previewView.zoom, 0.2);
  camera.position.set(0, -distance, distance * 0.62);
  camera.near = Math.max(0.01, distance / 1000);
  camera.far = Math.max(1000, distance * 20);
  const viewSize = Math.max(baseRadius * 1.18 / Math.max(state.previewView.zoom, 0.2), 1);
  const aspect = width / height;
  camera.left = -viewSize * aspect;
  camera.right = viewSize * aspect;
  camera.top = viewSize;
  camera.bottom = -viewSize;
  camera.lookAt(0, 0, 0);
  camera.updateProjectionMatrix();

  group.rotation.set(state.previewView.pitch, 0, state.previewView.yaw, "XYZ");
  renderer.render(scene, camera);
}

const TOOLPATH_COLORS = {
  drill: 0x2563eb,
  helix: 0x0e7490,
  slot: 0xd97706,
  vline: 0x7c3aed,
  circle: 0x2563eb,
  hline: 0x2563eb,
  raster: 0x0d9488,
  loops: 0x15803d,
};

function clearToolpaths() {
  const cp = state.cadPreview;
  if (cp.toolpathGroup && cp.group) cp.group.remove(cp.toolpathGroup);
  cp.toolpathGroup = null;
}

function toolpathLine(points, color, dashed) {
  const THREE = window.THREE;
  const geometry = new THREE.BufferGeometry().setFromPoints(points.map((p) => new THREE.Vector3(p[0], p[1], p[2])));
  const material = dashed
    ? new THREE.LineDashedMaterial({ color, dashSize: 1.6, gapSize: 1.0, transparent: true, opacity: 0.95, depthTest: false })
    : new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.9, depthTest: false });
  const line = new THREE.Line(geometry, material);
  if (dashed) line.computeLineDistances();
  line.userData.overlay = true;
  line.renderOrder = 10;
  return line;
}

function circlePoints(x, y, z, r, segments = 28) {
  const pts = [];
  for (let i = 0; i <= segments; i += 1) {
    const a = (i / segments) * Math.PI * 2;
    pts.push([x + Math.cos(a) * r, y + Math.sin(a) * r, z]);
  }
  return pts;
}

function buildOverlayObjects(o, color, dashed, excluded) {
  const THREE = window.THREE;
  const objects = [];
  if (o.kind === "drill" || o.kind === "helix" || o.kind === "vline") {
    if (excluded) {
      // 穴埋め表示: 円柱プラグで埋まっているように見せる
      const radius = Math.max((o.d || 1) / 2, 0.15);
      const geometry = new THREE.CylinderGeometry(radius, radius, o.depth, 20);
      const material = new THREE.MeshStandardMaterial({ color: 0x8a9388, roughness: 0.7, transparent: true, opacity: 0.92 });
      const plug = new THREE.Mesh(geometry, material);
      plug.rotation.x = Math.PI / 2;
      plug.position.set(o.x, o.y, o.z - o.depth / 2);
      plug.userData.overlay = true;
      plug.renderOrder = 9;
      objects.push(plug);
      return objects;
    }
    if (o.kind === "drill") {
      objects.push(toolpathLine([[o.x, o.y, o.z + 3], [o.x, o.y, o.z - o.depth]], color, dashed));
      objects.push(toolpathLine(circlePoints(o.x, o.y, o.z, Math.max(o.d / 2, 0.2), 20), color, dashed));
    } else if (o.kind === "helix") {
      const r = Math.max(o.d * 0.32, 0.2);
      const revs = Math.min(24, Math.max(3, Math.round(o.depth / Math.max(0.2, o.d * 0.15))));
      const steps = revs * 10;
      const pts = [];
      for (let i = 0; i <= steps; i += 1) {
        const a = (i / 10) * Math.PI * 2;
        pts.push([o.x + Math.cos(a) * r, o.y + Math.sin(a) * r, o.z - (i / steps) * o.depth]);
      }
      objects.push(toolpathLine(pts, color, dashed));
    } else {
      objects.push(toolpathLine([[o.x, o.y, o.z + 2], [o.x, o.y, o.z - o.depth]], color, dashed));
    }
    return objects;
  }
  if (o.kind === "circle") {
    const r = Math.max((o.d || 1) / 2, 0.3);
    objects.push(toolpathLine(circlePoints(o.x, o.y, o.z + 0.3, r), color, dashed || excluded));
    if (o.depth > 0.5) objects.push(toolpathLine(circlePoints(o.x, o.y, o.z - o.depth, r * 0.85), color, dashed || excluded));
    return objects;
  }
  if (o.kind === "slot" && Array.isArray(o.seg)) {
    const [x1, y1, x2, y2] = o.seg;
    objects.push(toolpathLine([[x1, y1, o.z + 0.4], [x2, y2, o.z + 0.4]], color, dashed || excluded));
    objects.push(toolpathLine([[x1, y1, o.z - o.depth], [x2, y2, o.z - o.depth]], color, dashed || excluded));
    objects.push(toolpathLine([[x1, y1, o.z + 0.4], [x1, y1, o.z - o.depth]], color, true));
    objects.push(toolpathLine([[x2, y2, o.z + 0.4], [x2, y2, o.z - o.depth]], color, true));
    return objects;
  }
  if (o.kind === "hline" && Array.isArray(o.seg3)) {
    const [x1, y1, z1, x2, y2, z2] = o.seg3;
    objects.push(toolpathLine([[x1, y1, z1], [x2, y2, z2]], color, dashed || excluded));
    return objects;
  }
  if (o.kind === "raster") {
    const lanes = Math.max(2, Math.min(40, Math.floor((o.ymax - o.ymin) / Math.max(o.pitch, 0.5)) + 1));
    const pts = [];
    for (let i = 0; i < lanes; i += 1) {
      const y = o.ymin + ((o.ymax - o.ymin) * i) / (lanes - 1);
      if (i % 2 === 0) {
        pts.push([o.xmin, y, o.z], [o.xmax, y, o.z]);
      } else {
        pts.push([o.xmax, y, o.z], [o.xmin, y, o.z]);
      }
    }
    objects.push(toolpathLine(pts, color, dashed || excluded));
    return objects;
  }
  if (o.kind === "loops") {
    const loops = Math.max(1, Math.min(12, o.loops || 3));
    for (let i = 0; i < loops; i += 1) {
      const z = o.z_top - ((o.z_top - o.z_bottom) * (i + 0.5)) / loops;
      objects.push(
        toolpathLine(
          [
            [o.xmin, o.ymin, z], [o.xmax, o.ymin, z], [o.xmax, o.ymax, z], [o.xmin, o.ymax, z], [o.xmin, o.ymin, z],
          ],
          color,
          dashed || excluded,
        ),
      );
    }
    return objects;
  }
  return objects;
}

function renderToolpaths(result) {
  const cp = state.cadPreview;
  const legend = $("#pathLegend");
  const overlays = result?.analysis?.toolpath_overlays || [];
  if (legend) legend.classList.toggle("hidden", overlays.length === 0);
  if (!window.THREE || !cp.group) return;
  clearToolpaths();
  if (!overlays.length) {
    renderCurrentPreview();
    return;
  }
  const THREE = window.THREE;
  const toolpathGroup = new THREE.Group();
  toolpathGroup.userData.overlay = true;
  const edmKeys = new Set((result.edm_candidates || []).map((c) => c.feature_key).filter(Boolean));
  for (const o of overlays) {
    const excluded = state.excluded.has(o.key);
    const isEdm = edmKeys.has(o.key);
    const color = excluded ? 0x9ca3af : isEdm ? 0xdc2626 : TOOLPATH_COLORS[o.kind] || 0x2563eb;
    for (const object of buildOverlayObjects(o, color, isEdm && !excluded, excluded)) {
      object.userData.featureKey = o.key;
      object.userData.baseOpacity = object.material.opacity;
      object.userData.baseColor = object.material.color.getHex();
      toolpathGroup.add(object);
    }
  }
  cp.toolpathGroup = toolpathGroup;
  toolpathGroup.visible = cp.showPaths;
  cp.group.add(toolpathGroup);
  applyToolpathHighlight();
  renderCurrentPreview();
}

// 選択中フィーチャのパスだけを強調し、他は薄く表示する
function applyToolpathHighlight() {
  const group = state.cadPreview.toolpathGroup;
  const key = state.highlightedKey;
  if (!group) return false;
  let matched = false;
  group.children.forEach((object) => {
    const isTarget = key !== null && object.userData.featureKey === key;
    if (isTarget) matched = true;
    const material = object.material;
    material.opacity = key === null || isTarget ? object.userData.baseOpacity : 0.12;
    material.color.setHex(isTarget ? 0xf59e0b : object.userData.baseColor);
    object.renderOrder = isTarget ? 12 : 10;
    material.needsUpdate = true;
  });
  return matched;
}

function setHighlightedFeature(key) {
  state.highlightedKey = state.highlightedKey === key ? null : key;
  $$("[data-feature-key]").forEach((row) => {
    row.classList.toggle("feature-selected", row.dataset.featureKey === state.highlightedKey);
  });
  const matched = applyToolpathHighlight();
  if (state.highlightedKey !== null) {
    if (!state.cadPreview.toolpathGroup) {
      toast("3Dプレビューが表示されていないため、工具パスを強調できません。");
    } else if (!matched) {
      toast("このフィーチャには表示できる工具パスがありません。");
    } else {
      if (!state.cadPreview.showPaths) {
        state.cadPreview.showPaths = true;
        state.cadPreview.toolpathGroup.visible = true;
        $("[data-view-toggle='paths']")?.classList.add("active");
      }
      const panel = $("#previewPanel");
      const rect = panel?.getBoundingClientRect();
      if (rect && (rect.bottom < 80 || rect.top > window.innerHeight - 80)) {
        panel.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    }
  }
  renderCurrentPreview();
}

function drawPreviewPlaceholder() {
  setPreviewMode("fallback");
  const canvas = $("#stpPreviewCanvas");
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);
  ctx.strokeStyle = "#b9c3bd";
  ctx.lineWidth = 1.5;
  ctx.setLineDash([6, 6]);
  ctx.strokeRect(22, 22, rect.width - 44, rect.height - 44);
  ctx.setLineDash([]);
  ctx.fillStyle = "#68746f";
  ctx.font = "700 14px Yu Gothic, Meiryo, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText("STPファイルをドロップ", rect.width / 2, rect.height / 2 - 6);
  ctx.font = "12px Yu Gothic, Meiryo, sans-serif";
  ctx.fillText("外形・面・円筒面候補を簡易表示", rect.width / 2, rect.height / 2 + 18);
}

function drawStepPreview(preview) {
  setPreviewMode("fallback");
  const canvas = $("#stpPreviewCanvas");
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);

  if (!preview.bbox) {
    drawPreviewPlaceholder();
    return;
  }

  const b = preview.bbox;
  const pad = 46;
  const z = Math.max(b.z, Math.max(b.x, b.y) * 0.08);
  const corners = [
    [b.minX, b.minY, b.minZ],
    [b.maxX, b.minY, b.minZ],
    [b.maxX, b.maxY, b.minZ],
    [b.minX, b.maxY, b.minZ],
    [b.minX, b.minY, b.minZ + z],
    [b.maxX, b.minY, b.minZ + z],
    [b.maxX, b.maxY, b.minZ + z],
    [b.minX, b.maxY, b.minZ + z],
  ];
  const model = {
    cx: b.minX + b.x / 2,
    cy: b.minY + b.y / 2,
    cz: b.minZ + z / 2,
  };
  const projectedUnit = corners.map((point) => project3d(point, model, 1, { x: 0, y: 0 }));
  const minPx = Math.min(...projectedUnit.map((point) => point.x));
  const maxPx = Math.max(...projectedUnit.map((point) => point.x));
  const minPy = Math.min(...projectedUnit.map((point) => point.y));
  const maxPy = Math.max(...projectedUnit.map((point) => point.y));
  const scale = Math.min((rect.width - pad * 2) / Math.max(1, maxPx - minPx), (rect.height - pad * 2) / Math.max(1, maxPy - minPy)) * state.previewView.zoom;
  const origin = { x: rect.width / 2, y: rect.height / 2 + 34 };
  const p = corners.map((point) => project3d(point, model, scale, origin));

  const faces = [
    [0, 1, 2, 3, "rgba(215,120,47,.16)"],
    [4, 5, 6, 7, "rgba(31,92,70,.18)"],
    [1, 2, 6, 5, "rgba(47,72,88,.12)"],
    [2, 3, 7, 6, "rgba(47,72,88,.08)"],
    [0, 3, 7, 4, "rgba(47,72,88,.08)"],
  ];
  ctx.lineJoin = "round";
  const orderedFaces = faces
    .map((face) => ({ face, depth: face.slice(0, 4).reduce((sum, index) => sum + p[index].z, 0) / 4 }))
    .sort((a, b2) => a.depth - b2.depth);
  for (const { face } of orderedFaces) {
    ctx.beginPath();
    ctx.moveTo(p[face[0]].x, p[face[0]].y);
    for (const index of face.slice(1, 4)) ctx.lineTo(p[index].x, p[index].y);
    ctx.closePath();
    ctx.fillStyle = face[4];
    ctx.fill();
    ctx.strokeStyle = "#2f4858";
    ctx.lineWidth = 1.2;
    ctx.stroke();
  }

  const boxEdges = [
    [0, 1], [1, 2], [2, 3], [3, 0],
    [4, 5], [5, 6], [6, 7], [7, 4],
    [0, 4], [1, 5], [2, 6], [3, 7],
  ];
  ctx.strokeStyle = "#17201b";
  ctx.lineWidth = 1.5;
  for (const edge of boxEdges) {
    ctx.beginPath();
    ctx.moveTo(p[edge[0]].x, p[edge[0]].y);
    ctx.lineTo(p[edge[1]].x, p[edge[1]].y);
    ctx.stroke();
  }

  if (preview.edges?.length) {
    const edgeStep = Math.max(1, Math.ceil(preview.edges.length / 3500));
    const modelEdges = [];
    for (let i = 0; i < preview.edges.length; i += edgeStep) {
      const start = project3d(preview.edges[i].start, model, scale, origin);
      const end = project3d(preview.edges[i].end, model, scale, origin);
      modelEdges.push({ start, end, depth: (start.z + end.z) / 2 });
    }
    modelEdges.sort((a, b2) => a.depth - b2.depth);
    ctx.strokeStyle = "rgba(23,32,27,.58)";
    ctx.lineWidth = 1;
    for (const edge of modelEdges) {
      ctx.beginPath();
      ctx.moveTo(edge.start.x, edge.start.y);
      ctx.lineTo(edge.end.x, edge.end.y);
      ctx.stroke();
    }
  }

  const axisLength = Math.max(b.x, b.y, z) * 0.22;
  const axisBase = [b.minX, b.minY, b.minZ];
  const axes = [
    [[axisBase[0] + axisLength, axisBase[1], axisBase[2]], "#a3392f", "X"],
    [[axisBase[0], axisBase[1] + axisLength, axisBase[2]], "#1f5c46", "Y"],
    [[axisBase[0], axisBase[1], axisBase[2] + axisLength], "#2f4858", "Z"],
  ];
  const axisStart = project3d(axisBase, model, scale, origin);
  for (const [axisEndPoint, color, label] of axes) {
    const axisEnd = project3d(axisEndPoint, model, scale, origin);
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(axisStart.x, axisStart.y);
    ctx.lineTo(axisEnd.x, axisEnd.y);
    ctx.stroke();
    ctx.font = "800 11px Yu Gothic, Meiryo, sans-serif";
    ctx.fillText(label, axisEnd.x + 4, axisEnd.y - 4);
  }

  if (preview.points.length > 8) {
    const step = Math.max(1, Math.ceil(preview.points.length / 1800));
    const renderedPoints = [];
    for (let i = 0; i < preview.points.length; i += step) {
      const pp = project3d(preview.points[i], model, scale, origin);
      renderedPoints.push(pp);
    }
    renderedPoints.sort((a, b2) => a.z - b2.z);
    for (const pp of renderedPoints) {
      ctx.beginPath();
      ctx.arc(pp.x, pp.y, 2.4, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(31,92,70,.68)";
      ctx.fill();
    }
  }

  const topCenter = project3d([model.cx, model.cy, b.minZ + z], model, scale, origin);
  const circularFeatures = preview.cylinders.length ? preview.cylinders : (preview.circles || []);
  const holeCount = Math.min(circularFeatures.length, 120);
  if (holeCount) {
    const columns = Math.ceil(Math.sqrt(holeCount));
    const rows = Math.ceil(holeCount / columns);
    for (let i = 0; i < holeCount; i += 1) {
      const col = i % columns;
      const row = Math.floor(i / columns);
      const nx = columns === 1 ? 0.5 : (col + 1) / (columns + 1);
      const ny = rows === 1 ? 0.5 : (row + 1) / (rows + 1);
      const feature = circularFeatures[i];
      const center = feature.center || [b.minX + b.x * nx, b.minY + b.y * ny, b.minZ + z];
      const modelPoint = [center[0], center[1], Math.max(center[2], b.minZ + z)];
      const bottomPoint = [center[0], center[1], b.minZ];
      const topProjected = drawProjectedEllipse(ctx, modelPoint, feature.radius, model, scale, origin, {
        fillStyle: "rgba(163,57,47,.16)",
        strokeStyle: "#a3392f",
        lineWidth: 1.2,
      });
      if (feature.center && preview.cylinders.length) {
        const bottomProjected = project3d(bottomPoint, model, scale, origin);
        ctx.strokeStyle = "rgba(163,57,47,.24)";
        ctx.lineWidth = 0.8;
        ctx.beginPath();
        ctx.moveTo(topProjected.x, topProjected.y);
        ctx.lineTo(bottomProjected.x, bottomProjected.y);
        ctx.stroke();
      }
    }
  } else {
    ctx.fillStyle = "#68746f";
    ctx.font = "12px Yu Gothic, Meiryo, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("円筒面候補なし", topCenter.x, topCenter.y - 12);
  }

  ctx.fillStyle = "#17201b";
  ctx.font = "800 13px Yu Gothic, Meiryo, sans-serif";
  ctx.textAlign = "left";
  ctx.fillText(`${numberLabel(b.x)} x ${numberLabel(b.y)} x ${numberLabel(b.z)} mm`, 18, 26);
  ctx.fillStyle = "#68746f";
  ctx.font = "12px Yu Gothic, Meiryo, sans-serif";
  ctx.fillText(`ドラッグで回転 / ホイールでズーム / 平面 ${preview.planeCount} / エッジ ${preview.edges?.length || 0} / 円筒 ${preview.cylinders.length}`, 18, 46);
}

function renderStepPreview(preview) {
  state.preview = preview;
  const badge = $("#previewBadge");
  const status = $("#previewStatus");
  const stats = $("#previewStats");
  const hasGeometry = Boolean(preview.bbox);

  badge.className = `preview-badge ${hasGeometry ? "ready" : "warn"}`;
  badge.textContent = hasGeometry ? "読込済み" : "点群なし";
  status.textContent = preview.truncated
    ? "先頭部分だけを読み込んで3D概要を表示しています"
    : "座標点・エッジ・円筒面候補から3D概要を表示しています";

  const bboxText = preview.bbox
    ? `${numberLabel(preview.bbox.x)} x ${numberLabel(preview.bbox.y)} x ${numberLabel(preview.bbox.z)} mm`
    : "取得不可";
  stats.innerHTML = `
    <dt>ファイル</dt><dd>${esc(preview.fileName)}</dd>
    <dt>サイズ</dt><dd>${(preview.fileSize / 1024).toFixed(1)} KB</dd>
    <dt>外形</dt><dd>${bboxText}</dd>
    <dt>座標点</dt><dd>${preview.points.length}</dd>
    <dt>エッジ</dt><dd>${preview.edges?.length || 0}</dd>
    <dt>エンティティ</dt><dd>${preview.entityCount}</dd>
    <dt>平面</dt><dd>${preview.planeCount}</dd>
    <dt>面候補</dt><dd>${preview.faceCount}</dd>
    <dt>円/円筒</dt><dd>${preview.circles?.length || 0} / ${preview.cylinders.length}</dd>
  `;
  drawStepPreview(preview);
}

async function previewFile(file) {
  if (!file) {
    drawPreviewPlaceholder();
    return;
  }
  const lowerName = file.name.toLowerCase();
  if (!lowerName.endsWith(".stp") && !lowerName.endsWith(".step")) {
    toast("STPまたはSTEPファイルを選択してください。");
    drawPreviewPlaceholder();
    return;
  }
  $("#previewBadge").className = "preview-badge";
  $("#previewBadge").textContent = "読込中";
  $("#previewStatus").textContent = "ブラウザ内でSTEPテキストを読み込んでいます";
  const maxPreviewBytes = 16 * 1024 * 1024;
  const buffer = await file.arrayBuffer();
  const text = new TextDecoder("utf-8").decode(buffer.slice(0, maxPreviewBytes));
  renderStepPreview(parseStepPreview(text, file));
  try {
    $("#previewStatus").textContent = "OpenCascadeでSTEP形状を詳細メッシュ化しています";
    await loadDetailedCadPreview(buffer);
    $("#previewBadge").className = "preview-badge ready";
    $("#previewBadge").textContent = "詳細表示";
    $("#previewStatus").textContent = "OpenCascadeメッシュをWebGLで表示しています";
  } catch (error) {
    setPreviewMode("fallback");
    $("#previewStatus").textContent = `簡易表示に切替: ${error.message}`;
  }
}

function setTab(name) {
  $$(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === name));
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `tab-${name}`));
  if (name === "history") loadHistories();
}

async function loadMaster() {
  state.master = await jsonFetch("/api/master");
  renderMaster();
}

function renderMaster() {
  $("#machineSelect").innerHTML = state.master.machines
    .map((m) => `<option value="${m.machine_id}">${esc(m.machine_name)}${m.max_tool_diameter_mm ? ` / 最大工具径 ${m.max_tool_diameter_mm}mm` : ""}</option>`)
    .join("");
  const ncSelect = $("#ncMachineSelect");
  if (ncSelect) {
    const saved = readPersisted("nc_machine_id");
    ncSelect.innerHTML = state.master.machines
      .map((m) => `<option value="${m.machine_id}">${esc(m.machine_name)}</option>`)
      .join("");
    if (saved && state.master.machines.some((m) => String(m.machine_id) === saved)) ncSelect.value = saved;
  }
  if ($("#conditionToolSelect")) {
    $("#conditionToolSelect").innerHTML = state.master.tools
      .map((t) => `<option value="${t.tool_id}">${esc(t.tool_name)}</option>`)
      .join("");
  }

  const catalogTools = state.master.manufacturer_catalogs.map((c) => ({
    tool_id: `C-${c.catalog_id}`,
    tool_name: `${c.manufacturer} ${c.product_name}`,
    tool_type: c.tool_type,
    diameter_mm: "-",
    flute_count: c.flute_info || "-",
    max_depth_mm: "-",
    source: "カタログ",
  }));
  if ($("#toolRows")) {
    $("#toolRows").innerHTML = [
      ...catalogTools,
      ...state.master.tools.map((t) => ({
        ...t,
        source: t.memo?.includes("http") ? "メーカーPDF" : "社内",
      })),
    ].map((t) => `
      <tr>
        <td>${esc(t.tool_id)}<br><small>${t.source}</small></td><td>${esc(t.tool_name)}</td><td>${esc(t.tool_type)}</td>
        <td>${esc(t.diameter_mm)}</td><td>${esc(t.flute_count)}</td><td>${esc(t.max_depth_mm)}</td>
        <td>${t.source === "社内" ? `<button class="danger" data-delete-tool="${t.tool_id}">削除</button>` : "-"}</td>
      </tr>
    `).join("");
  }

  $("#machineRows").innerHTML = state.master.machines.map((m) => `
    <tr>
      <td>${m.machine_id}</td><td>${esc(m.machine_name)}</td><td>${esc(m.axis_count)}</td>
      <td>${esc(m.rapid_feed_mm_min)}</td><td>${esc(m.atc_time_sec)}</td><td>${esc(m.max_spindle_rpm)}</td>
      <td>${m.max_tool_diameter_mm ? `${esc(m.max_tool_diameter_mm)} mm` : "制限なし"}</td><td>${esc(m.setup_time_min)}</td>
      <td>
        <button class="secondary-button compact" data-edit-machine="${m.machine_id}">編集</button>
        <button class="danger compact" data-delete-machine="${m.machine_id}">削除</button>
      </td>
    </tr>
  `).join("");

  renderCatalogTypeFilter();
  renderCatalogs();
  renderMakerConditionMaterialFilter();
  renderMakerConditions();
}

function renderCatalogTypeFilter() {
  const select = $("#catalogTypeFilter");
  if (!select) return;
  const current = select.value;
  const types = Array.from(new Set(state.master.manufacturer_catalogs.map((item) => item.tool_type).filter(Boolean))).sort();
  select.innerHTML = `<option value="">全種別</option>${types.map((type) => `<option value="${esc(type)}">${esc(type)}</option>`).join("")}`;
  select.value = types.includes(current) ? current : "";
}

function renderCatalogs() {
  const rowsEl = $("#catalogRows");
  if (!rowsEl) return;
  const search = ($("#catalogSearch")?.value || "").trim().toLowerCase();
  const type = $("#catalogTypeFilter")?.value || "";
  const rows = state.master.manufacturer_catalogs.filter((item) => {
    const text = [
      item.manufacturer,
      item.product_name,
      item.tool_type,
      item.flute_info,
      item.coating,
      item.material_hint,
      item.series_codes,
    ].join(" ").toLowerCase();
    return (!type || item.tool_type === type) && (!search || text.includes(search));
  });
  $("#catalogCountBadge").textContent = `${rows.length}件`;
  rowsEl.innerHTML = rows.map((item) => `
    <tr>
      <td>${item.catalog_id}</td>
      <td>${esc(item.manufacturer)}</td>
      <td>${esc(item.product_name)}<br><small>${esc(item.memo)}</small></td>
      <td>${esc(item.tool_type)}</td>
      <td>${esc(item.flute_info || "-")}</td>
      <td>${esc(item.coating || "-")}</td>
      <td>${esc(item.material_hint || "-")}</td>
      <td>${esc(item.series_codes || "-")}</td>
      <td><a href="${safeUrl(item.catalog_url)}" target="_blank" rel="noopener">PDF</a></td>
    </tr>
  `).join("");
}

function fillSelectOptions(select, values, allLabel) {
  if (!select) return;
  const current = select.value;
  select.innerHTML = `<option value="">${allLabel}</option>${values.map((value) => `<option value="${esc(value)}">${esc(value)}</option>`).join("")}`;
  select.value = values.includes(current) ? current : "";
}

function renderMakerConditionMaterialFilter() {
  const materials = Array.from(new Set([
    ...state.master.manufacturer_cutting_conditions.map((item) => item.work_material).filter(Boolean),
    ...state.master.conditions.map((item) => item.material_type).filter(Boolean),
  ])).sort();
  fillSelectOptions($("#makerConditionMaterialFilter"), materials, "全被削材");
  const makers = Array.from(new Set(
    state.master.manufacturer_cutting_conditions.map((item) => item.manufacturer).filter(Boolean),
  )).sort();
  fillSelectOptions($("#makerConditionMakerFilter"), makers, "全メーカー");
}

// 条件は1,700件以上あるため、描画はこの件数までに抑えて絞り込みを促す
const CONDITION_RENDER_LIMIT = 400;

function readDiameterFilter(selector) {
  const raw = $(selector)?.value;
  if (raw === undefined || raw === "") return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

function renderMakerConditions() {
  const rowsEl = $("#conditionRows");
  if (!rowsEl) return;
  const search = ($("#makerConditionSearch")?.value || "").trim().toLowerCase();
  const material = $("#makerConditionMaterialFilter")?.value || "";
  const maker = $("#makerConditionMakerFilter")?.value || "";
  const diameterMin = readDiameterFilter("#makerConditionDiameterMin");
  const diameterMax = readDiameterFilter("#makerConditionDiameterMax");
  const makerRows = state.master.manufacturer_cutting_conditions.filter((item) => {
    const diameter = Number(item.outside_diameter_mm);
    if (maker && item.manufacturer !== maker) return false;
    if (material && item.work_material !== material) return false;
    if (diameterMin !== null && diameter < diameterMin) return false;
    if (diameterMax !== null && diameter > diameterMax) return false;
    if (!search) return true;
    const text = [
      item.manufacturer,
      item.series_code,
      item.product_name,
      item.model_family,
      item.corner_radius_label,
      item.work_material,
      item.hardness,
      item.material_group,
    ].join(" ").toLowerCase();
    return text.includes(search);
  });
  // 社内条件は外径を持たないため、メーカー・径で絞り込んだときは対象外にする
  const internalRows = maker || diameterMin !== null || diameterMax !== null ? [] : state.master.conditions.filter((item) => {
    if (item.tool_memo?.includes("http")) return false;
    const text = [
      item.tool_name,
      item.material_type,
      item.process_type,
    ].join(" ").toLowerCase();
    return (!material || item.material_type === material) && (!search || text.includes(search));
  });
  const toolCount = $("#toolRows")?.querySelectorAll("tr").length || 0;
  const total = makerRows.length + internalRows.length;
  $("#makerConditionCountBadge").textContent = `工具 ${toolCount} / 条件 ${total}`;
  const shownMakerRows = makerRows.slice(0, CONDITION_RENDER_LIMIT);
  const shownInternalRows = internalRows.slice(0, Math.max(0, CONDITION_RENDER_LIMIT - shownMakerRows.length));
  const shown = shownMakerRows.length + shownInternalRows.length;
  const note = $("#conditionLimitNote");
  if (note) {
    note.classList.toggle("hidden", shown >= total);
    note.textContent = `${total}件中 ${shown}件を表示しています。メーカー・被削材・外径で絞り込んでください。`;
  }
  rowsEl.innerHTML = [
    ...shownMakerRows.map((item) => `
    <tr>
      <td>メーカーPDF<br><small>#${item.condition_id}</small></td>
      <td>${esc(item.manufacturer)}</td>
      <td>${esc(item.series_code)}</td>
      <td>${esc(item.model_family)}</td>
      <td>${Number(item.outside_diameter_mm).toFixed(1)}</td>
      <td>${Number(item.effective_length_mm).toFixed(1)}</td>
      <td>${esc(item.work_material)}<br><small>${esc(item.material_group)}</small></td>
      <td>${esc(item.spindle_rpm)}</td>
      <td>${esc(item.feed_rate_mm_min)}</td>
      <td>${esc(item.axial_depth_mm)}</td>
      <td>${esc(item.radial_depth_mm)}</td>
      <td><a href="${safeUrl(item.source_url)}" target="_blank" rel="noopener">p.${esc(item.source_page || "-")}</a></td>
    </tr>
  `),
    ...shownInternalRows.map((item) => `
    <tr>
      <td>社内<br><small>#${item.condition_id}</small></td>
      <td>${esc(item.tool_name)}</td>
      <td>-</td>
      <td>${esc(item.process_type)}</td>
      <td>-</td>
      <td>-</td>
      <td>${esc(item.material_type)}</td>
      <td>${esc(item.spindle_rpm)}</td>
      <td>${esc(item.feed_rate_mm_min)}</td>
      <td>${esc(item.depth_of_cut_mm)}</td>
      <td>${esc(item.width_of_cut_mm)}</td>
      <td><button class="danger" data-delete-condition="${item.condition_id}">削除</button></td>
    </tr>
  `),
  ].join("") || `<tr><td colspan="12" class="empty-row">条件に一致する切削条件がありません。</td></tr>`;
}

function scorePoints(points) {
  const value = Number(points);
  return `${value > 0 ? "+" : ""}${Number.isInteger(value) ? value : value.toFixed(1)}`;
}

function candidateComparison(candidates) {
  if (!candidates || candidates.length === 0) return "";
  const selected = candidates.find((c) => c.selected) || candidates[0];
  const runnerUp = candidates.find((c) => c !== selected);
  const margin = runnerUp ? `次点との差 ${scorePoints(selected.score - runnerUp.score)}` : "対抗候補なし";
  const rows = candidates.map((c) => `
    <tr class="${c.selected ? "candidate-selected" : ""}">
      <td>${c.rank}</td>
      <td>${esc(c.tool)}${c.selected ? ' <span class="candidate-tag">採用</span>' : ""}<br><small>有効長 ${esc(c.effective_length_mm)} mm / ${esc(c.condition)}</small></td>
      <td class="candidate-score">${scorePoints(c.score)}</td>
      <td><div class="candidate-parts">${c.components.map((p) => `<span class="${p.points < 0 ? "minus" : "plus"}">${esc(p.label)} ${scorePoints(p.points)}</span>`).join("")}</div></td>
    </tr>
  `).join("");
  return `
    <details class="candidate-details">
      <summary>候補比較（${selected.pool_size || candidates.length}条件中の上位${candidates.length}工具 / ${margin}）</summary>
      <table class="candidate-table">
        <thead><tr><th>順位</th><th>工具・条件</th><th>スコア</th><th>内訳</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </details>
  `;
}

function renderResult(result) {
  state.lastResult = result;
  state.highlightedKey = null;
  $("#resultPanel").classList.remove("hidden");
  $("#totalBadge").classList.remove("muted");
  $("#totalBadge").textContent = result.time_label || secLabel(result.breakdown.total_sec);
  $("#setupSec").textContent = secLabel(result.breakdown.setup_sec);
  $("#machiningSec").textContent = secLabel(result.breakdown.machining_sec);
  $("#toolChangeSec").textContent = secLabel(result.breakdown.tool_change_sec);
  $("#rapidSec").textContent = secLabel(result.breakdown.rapid_sec);
  $("#confidenceMeter").value = result.confidence;
  $("#confidenceLabel").textContent = `${Math.round(result.confidence * 100)}%`;
  $("#csvLink").href = `/api/histories/${result.history_id}/csv`;

  const machiningTotal = Number(result.breakdown.machining_sec) || 0;
  $("#featureRows").innerHTML = result.features.map((f) => {
    const share = machiningTotal > 0 ? (Number(f.machining_sec) / machiningTotal) * 100 : 0;
    const keyAttr = f.feature_key ? ` data-feature-key="${esc(f.feature_key)}" title="クリックで3Dプレビューの工具パスを強調表示"` : "";
    return `
    <tr${keyAttr}>
      <td>${esc(f.feature_type)}</td><td>${esc(f.dimensions)}<br><small>${esc(f.note)}</small>${f.reachability ? `<br><small class="reachability-warning">${esc(f.reachability)}</small>` : ""}</td>
      <td>${esc(f.quantity)}</td><td>${esc(f.tool_name)}${f.selection_reason ? `<br><small>${esc(f.selection_reason)}</small>` : ""}${candidateComparison(f.selection_candidates)}</td>
      <td class="condition-cell">${esc(f.cutting_condition || "-")}</td>
      <td class="path-cell">${esc(f.path_plan || "-")}</td>
      <td class="time-cell">${secLabel(f.machining_sec)}<span class="share-bar" aria-hidden="true"><i style="width:${Math.min(100, Math.max(0, share)).toFixed(1)}%"></i></span><small>${share.toFixed(1)}%</small></td>
      <td>${f.feature_key ? `<button type="button" class="link-btn" data-exclude-key="${esc(f.feature_key)}" title="このフィーチャを加工対象外（穴埋め扱い）にして再計算">穴埋め/除外</button>` : ""}</td>
    </tr>
  `;
  }).join("");

  const excludedRows = result.excluded_features || [];
  const excludedPanel = $("#excludedPanel");
  if (excludedPanel) {
    excludedPanel.classList.toggle("hidden", excludedRows.length === 0);
    $("#excludedRows").innerHTML = excludedRows.map((e) => `
      <tr>
        <td>${e.kind === "edm" ? "放電候補" : "切削"}</td>
        <td>${esc(e.label)}</td>
        <td>${esc(e.count)}</td>
        <td><button type="button" class="link-btn" data-restore-key="${esc(e.feature_key)}">加工対象に戻す</button></td>
      </tr>
    `).join("");
  }

  const edmCandidates = result.edm_candidates || [];
  const edmPanel = $("#edmPanel");
  if (edmPanel) {
    edmPanel.classList.toggle("hidden", edmCandidates.length === 0);
    const edmTotal = edmCandidates.reduce((total, c) => total + Number(c.reference_sec || 0), 0);
    $("#edmBadge").textContent = `${edmCandidates.length}件 / 参考 ${secLabel(edmTotal)}`;
    $("#edmRows").innerHTML = edmCandidates.map((c) => `
      <tr${c.feature_key ? ` data-feature-key="${esc(c.feature_key)}" title="クリックで3Dプレビューの位置を強調表示"` : ""}>
        <td><span class="edm-type">${esc(c.edm_type)}</span></td>
        <td>${esc(c.feature_type)}</td>
        <td>${esc(c.dimensions)}</td>
        <td>${esc(c.count)}</td>
        <td class="condition-cell">${esc(c.reason)}</td>
        <td>${secLabel(c.reference_sec)}</td>
        <td>${c.feature_key ? `<button type="button" class="link-btn" data-exclude-key="${esc(c.feature_key)}" title="この形状を加工しない（穴埋め扱い）ことにして候補から外す">除外</button>` : ""}</td>
      </tr>
    `).join("");
  }

  $("#toolUsageRows").innerHTML = result.tool_usage.map((t) => `
    <tr>
      <td>${esc(t.tool_name)}</td><td>${esc(t.usage_count)}</td>
      <td class="condition-cell">${esc(t.cutting_conditions || "-")}</td>
      <td>${secLabel(t.machining_sec)}</td>
    </tr>
  `).join("");

  const bbox = result.analysis.bbox;
  const rawBbox = result.analysis.raw_bbox
    ? `${result.analysis.raw_bbox.x.toFixed(1)} x ${result.analysis.raw_bbox.y.toFixed(1)} x ${result.analysis.raw_bbox.z.toFixed(1)} mm`
    : "-";
  const volumeRows = result.analysis.brep_available ? `
    <dt>実形状寸法</dt><dd>${rawBbox}</dd>
    <dt>部品体積</dt><dd>${Math.round(result.analysis.part_volume_mm3).toLocaleString()} mm3</dd>
    <dt>推定除去体積</dt><dd>${Math.round(result.analysis.removal_volume_mm3).toLocaleString()} mm3</dd>
    <dt>外形ブランク代</dt><dd>${Math.round(result.analysis.outer_allowance_volume_mm3 || 0).toLocaleString()} mm3</dd>
    <dt>内部除去体積</dt><dd>${Math.round(result.analysis.internal_removal_volume_mm3 || 0).toLocaleString()} mm3</dd>
    <dt>加工特徴</dt><dd>${machiningFeatureSummary(result.analysis.machining_features)}</dd>
    <dt>ソリッド/エッジ</dt><dd>${result.analysis.solid_count} / ${result.analysis.edge_count}</dd>
  ` : "";
  const reachabilityIssues = result.analysis.reachability_issues || [];
  const reachabilityRows = reachabilityIssues.length ? `
    <dt>到達性注意</dt><dd>${reachabilityIssues.length}件 / ${esc(reachabilityIssues.slice(0, 3).map((item) => item.feature_type).join("、"))}</dd>
  ` : "";
  const edmPolicy = result.edm_policy || {};
  const edmTaperLabel = Number(edmPolicy.min_taper_deg) > 0 ? ` / テーパ≥${edmPolicy.min_taper_deg}°` : "";
  const edmPolicyRow = edmPolicy.enabled
    ? `<dt>放電置き換え</dt><dd>幅≤${edmPolicy.max_width_mm}mm かつ 深さ≥${edmPolicy.min_depth_mm}mm / 深さ幅比≥${edmPolicy.min_aspect}${edmTaperLabel} / 候補 ${edmCandidates.length}件</dd>`
    : `<dt>放電置き換え</dt><dd>判定オフ</dd>`;
  $("#analysisInfo").innerHTML = `
    <dt>ファイル</dt><dd>${esc(result.file_name || "-")}</dd>
    <dt>解析方式</dt><dd>${esc(result.analysis.parser)}</dd>
    <dt>条件ソース</dt><dd>${esc(result.condition_source || "-")}</dd>
    <dt>見積安全率</dt><dd>${esc(result.estimate_mode_label || "-")}</dd>
    <dt>最大工具径</dt><dd>${result.machine.max_tool_diameter_mm ? `${result.machine.max_tool_diameter_mm} mm` : "制限なし"}</dd>
    <dt>外形寸法</dt><dd>${bbox.x.toFixed(1)} x ${bbox.y.toFixed(1)} x ${bbox.z.toFixed(1)} mm</dd>
    ${volumeRows}
    ${reachabilityRows}
    ${edmPolicyRow}
    <dt>エンティティ</dt><dd>${result.analysis.entity_count}</dd>
    <dt>面候補</dt><dd>${result.analysis.face_count}</dd>
    <dt>円筒面候補</dt><dd>${result.analysis.cylindrical_radii.length}</dd>
  `;

  renderToolpaths(result);
  renderFillPanel(result);
  const fillToken = result.fill?.token || null;
  if (fillToken !== state.cadPreview.fillToken || !state.cadPreview.filledGroup) {
    loadFillModels(fillToken).catch((error) => toast(error.message));
  }
}

async function reanalyzeWithExclusions() {
  const fileInput = $("#analyzeForm [name=stp_file]");
  if (!fileInput || !fileInput.files || !fileInput.files.length) {
    toast("STPファイルを選択し直してから再解析してください。除外指定は保持されます。");
    if (state.lastResult) renderToolpaths(state.lastResult);
    return;
  }
  $("#analyzeForm").requestSubmit();
}

async function openHistory(historyId) {
  const result = await jsonFetch(`/api/histories/${historyId}`);
  // 履歴のファイルは手元に無いため、除外状態だけ復元し3Dは現在の表示のまま
  state.excluded = new Set(result.excluded_keys || []);
  setTab("analyze");
  renderResult(result);
  $("#resultPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  toast(`履歴 #${historyId}（${result.file_name}）の結果を表示しています。再解析するにはファイルを選択してください。`);
}

async function loadHistories() {
  const rows = await jsonFetch("/api/histories");
  if (!rows.length) {
    $("#historyRows").innerHTML = `<tr><td colspan="8" class="empty-row">まだ解析履歴がありません。</td></tr>`;
    return;
  }
  $("#historyRows").innerHTML = rows.map((h) => `
    <tr>
      <td>${h.history_id}</td><td>${esc(h.created_at)}</td><td>${esc(h.file_name)}</td>
      <td>${esc(h.material_type)}</td><td>${esc(h.machine_name)}</td><td>${esc(h.time_label)}</td>
      <td>${Math.round(h.confidence * 100)}%</td>
      <td class="action-cell">
        <button type="button" class="secondary-button compact" data-open-history="${h.history_id}">結果を表示</button>
        <a class="secondary-link compact" href="/api/histories/${h.history_id}/csv">CSV</a>
      </td>
    </tr>
  `).join("");
}

function bindEvents() {
  $$(".tab").forEach((button) => button.addEventListener("click", () => setTab(button.dataset.tab)));

  window.addEventListener("resize", () => {
    renderCurrentPreview();
  });

  const fileInput = $("input[name='stp_file']");
  const dropZone = $(".drop-zone");
  const previewCanvases = [$("#stpPreviewCanvas"), $("#cadPreviewCanvas")].filter(Boolean);

  previewCanvases.forEach((previewCanvas) => {
    previewCanvas.addEventListener("pointerdown", (event) => {
      if (!state.preview) return;
      state.previewView.dragging = true;
      state.previewView.lastX = event.clientX;
      state.previewView.lastY = event.clientY;
      previewCanvas.setPointerCapture(event.pointerId);
    });

    previewCanvas.addEventListener("pointermove", (event) => {
      if (!state.previewView.dragging || !state.preview) return;
      const dx = event.clientX - state.previewView.lastX;
      const dy = event.clientY - state.previewView.lastY;
      state.previewView.lastX = event.clientX;
      state.previewView.lastY = event.clientY;
      state.previewView.yaw += dx * 0.01;
      state.previewView.pitch = Math.max(-1.55, Math.min(1.35, state.previewView.pitch + dy * 0.01));
      state.previewView.preset = "free";
      updateViewerControls();
      renderCurrentPreview();
    });

    previewCanvas.addEventListener("pointerup", () => {
      state.previewView.dragging = false;
    });

    previewCanvas.addEventListener("pointercancel", () => {
      state.previewView.dragging = false;
    });

    previewCanvas.addEventListener("wheel", (event) => {
      if (!state.preview) return;
      event.preventDefault();
      const factor = event.deltaY > 0 ? 0.92 : 1.08;
      state.previewView.zoom = Math.max(0.55, Math.min(3.2, state.previewView.zoom * factor));
      renderCurrentPreview();
    }, { passive: false });

    previewCanvas.addEventListener("dblclick", () => {
      state.previewView.zoom = 1;
      applyViewPreset("iso");
    });
  });

  $$("[data-view-preset]").forEach((button) => {
    button.addEventListener("click", () => applyViewPreset(button.dataset.viewPreset));
  });

  $$("[data-view-mode]").forEach((button) => {
    button.addEventListener("click", () => applyCadDisplayMode(button.dataset.viewMode));
  });

  $("[data-view-fit]")?.addEventListener("click", fitPreview);

  $$("[data-model-view]").forEach((button) => {
    button.addEventListener("click", () => setModelView(button.dataset.modelView));
  });

  $$("[data-persist]").forEach((input) => {
    input.addEventListener("change", () => writePersisted(input.dataset.persist, input.value));
  });

  document.body.addEventListener("click", async (event) => {
    const excludeButton = event.target.closest("[data-exclude-key]");
    const restoreButton = event.target.closest("[data-restore-key]");
    if (!excludeButton && !restoreButton) return;
    if (excludeButton) state.excluded.add(excludeButton.dataset.excludeKey);
    if (restoreButton) state.excluded.delete(restoreButton.dataset.restoreKey);
    await reanalyzeWithExclusions();
  });

  $$("[data-view-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.viewToggle !== "paths") return;
      state.cadPreview.showPaths = !state.cadPreview.showPaths;
      button.classList.toggle("active", state.cadPreview.showPaths);
      if (state.cadPreview.toolpathGroup) {
        state.cadPreview.toolpathGroup.visible = state.cadPreview.showPaths;
      }
      renderCurrentPreview();
    });
  });

  fileInput.addEventListener("change", (event) => {
    state.excluded.clear();
    const file = event.target.files[0];
    $("#fileName").textContent = file ? file.name : "ファイルを選択";
    previewFile(file).catch((error) => toast(error.message));
  });

  ["dragenter", "dragover"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.add("dragging");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.remove("dragging");
    });
  });

  dropZone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files[0];
    if (!file) return;
    const transfer = new DataTransfer();
    transfer.items.add(file);
    fileInput.files = transfer.files;
    $("#fileName").textContent = file.name;
    previewFile(file).catch((error) => toast(error.message));
  });

  $("#analyzeForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    // requestSubmit()による再解析では event.submitter が null になる
    const button = event.submitter || $("#analyzeForm button.primary[type=submit]") || { disabled: false, textContent: "" };
    const fileInput = event.currentTarget.elements.stp_file;
    if (!fileInput?.files?.length) {
      toast("STP / STEP ファイルを選択してください。");
      return;
    }
    button.disabled = true;
    const startedAt = Date.now();
    const showElapsed = () => {
      button.textContent = `解析中… ${Math.floor((Date.now() - startedAt) / 1000)}秒`;
    };
    showElapsed();
    const elapsedTimer = window.setInterval(showElapsed, 1000);
    document.body.classList.add("is-analyzing");
    $("#totalBadge").textContent = "解析中…";
    $("#totalBadge").classList.add("muted");
    try {
      const formData = new FormData(event.currentTarget);
      // チェックボックスは未チェック時に送信されないため、明示的にoffを送る
      if (!formData.has("edm_enabled")) formData.set("edm_enabled", "off");
      if (!formData.has("fill_drill_holes")) formData.set("fill_drill_holes", "off");
      if (!formData.has("fill_wire_shapes")) formData.set("fill_wire_shapes", "off");
      formData.set("excluded_features", JSON.stringify(Array.from(state.excluded)));
      const data = await jsonFetch("/api/analyze", { method: "POST", body: formData });
      renderResult(data);
      toast("解析が完了しました。");
    } catch (error) {
      toast(error.message);
      $("#totalBadge").textContent = state.lastResult ? state.lastResult.time_label || secLabel(state.lastResult.breakdown.total_sec) : "未解析";
      $("#totalBadge").classList.toggle("muted", !state.lastResult);
    } finally {
      window.clearInterval(elapsedTimer);
      document.body.classList.remove("is-analyzing");
      button.disabled = false;
      button.textContent = "解析実行";
    }
  });

  const postMasterForm = (selector, url) => {
    $(selector)?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      try {
        await jsonFetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(formJson(form)),
        });
        form.reset();
        await loadMaster();
        toast("登録しました。");
      } catch (error) {
        toast(error.message);
      }
    });
  };
  postMasterForm("#toolForm", "/api/tools");
  postMasterForm("#conditionForm", "/api/conditions");

  $("#machineForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = formJson(event.currentTarget);
    const machineId = data.machine_id;
    delete data.machine_id;
    try {
      await jsonFetch(machineId ? `/api/machines/${machineId}` : "/api/machines", {
        method: machineId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      resetMachineForm();
      await loadMaster();
      toast(machineId ? "機械マスタを更新しました。" : "機械マスタを追加しました。");
    } catch (error) {
      toast(error.message);
    }
  });

  $("#machineResetButton")?.addEventListener("click", resetMachineForm);

  $("#machineForm").addEventListener("reset", () => {
    window.setTimeout(() => {
      $("#machineSubmitButton").textContent = "追加";
      $("#machineResetButton")?.classList.add("hidden");
    });
  });

  $("#catalogSearch")?.addEventListener("input", renderCatalogs);
  $("#catalogTypeFilter")?.addEventListener("change", renderCatalogs);
  $("#makerConditionSearch")?.addEventListener("input", renderMakerConditions);
  ["#makerConditionMaterialFilter", "#makerConditionMakerFilter"].forEach((selector) => {
    $(selector)?.addEventListener("change", renderMakerConditions);
  });
  ["#makerConditionDiameterMin", "#makerConditionDiameterMax"].forEach((selector) => {
    $(selector)?.addEventListener("input", renderMakerConditions);
  });

  document.body.addEventListener("click", async (event) => {
    const target = event.target.closest("button, a");
    if (!target) return;
    const {
      deleteTool: toolId,
      deleteCondition: conditionId,
      deleteMachine: machineId,
      editMachine: editMachineId,
      openHistory: historyId,
    } = target.dataset;
    try {
      if (historyId) {
        await openHistory(historyId);
        return;
      }
      if (editMachineId) {
        const machine = state.master.machines.find((item) => String(item.machine_id) === String(editMachineId));
        fillMachineForm(machine);
        return;
      }
      if (!toolId && !conditionId && !machineId) return;
      const machine = machineId && state.master.machines.find((item) => String(item.machine_id) === String(machineId));
      const label = machine
        ? `機械「${machine.machine_name}」`
        : conditionId ? `社内条件 #${conditionId}` : `工具 #${toolId}`;
      if (!window.confirm(`${label}を削除します。よろしいですか？`)) return;
      if (toolId) await jsonFetch(`/api/tools/${toolId}`, { method: "DELETE" });
      if (conditionId) await jsonFetch(`/api/conditions/${conditionId}`, { method: "DELETE" });
      if (machineId) {
        await jsonFetch(`/api/machines/${machineId}`, { method: "DELETE" });
        if ($("#machineForm")?.elements.machine_id.value === String(machineId)) resetMachineForm();
      }
      await loadMaster();
      toast(`${label}を削除しました。`);
    } catch (error) {
      toast(error.message);
    }
  });

  // 認識フィーチャ・放電候補の行クリックで3D上の工具パスを強調
  document.body.addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-feature-key]");
    if (!row || event.target.closest("button, a, details, summary")) return;
    setHighlightedFeature(row.dataset.featureKey);
  });
}

restorePersistedInputs();
bindEvents();
drawPreviewPlaceholder();
updateViewerControls();
loadMaster().catch((error) => toast(error.message));
