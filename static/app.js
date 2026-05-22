const state = {
  master: { tools: [], conditions: [], machines: [], manufacturer_catalogs: [], manufacturer_cutting_conditions: [] },
  lastResult: null,
  preview: null,
  previewView: { yaw: -0.68, pitch: -0.46, zoom: 1, dragging: false, lastX: 0, lastY: 0 },
  cadPreview: { mode: "fallback", renderer: null, scene: null, camera: null, group: null, baseRadius: 1, occt: null },
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function secLabel(value) {
  const total = Math.round(Number(value) || 0);
  const h = String(Math.floor(total / 3600)).padStart(2, "0");
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const s = String(total % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

function toast(message) {
  const box = $("#toast");
  box.textContent = message;
  box.classList.remove("hidden");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => box.classList.add("hidden"), 3200);
}

async function jsonFetch(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "処理に失敗しました。");
  return data;
}

function formJson(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  for (const key of Object.keys(data)) {
    if (data[key] !== "" && !Number.isNaN(Number(data[key]))) data[key] = Number(data[key]);
  }
  return data;
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
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000000);
    camera.up.set(0, 0, 1);

    scene.add(new THREE.HemisphereLight(0xf7f3e7, 0x53655d, 1.55));
    const keyLight = new THREE.DirectionalLight(0xffffff, 1.7);
    keyLight.position.set(1.6, -2.4, 2.8);
    scene.add(keyLight);
    const rimLight = new THREE.DirectionalLight(0xd9eee8, 0.9);
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
    metalness: 0.18,
    roughness: 0.48,
    side: THREE.DoubleSide,
  });
  const mesh = new THREE.Mesh(geometry, material);

  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(geometry, 28),
    new THREE.LineBasicMaterial({ color: 0x26352e, transparent: true, opacity: 0.42 }),
  );
  return { mesh, edges };
}

async function loadDetailedCadPreview(buffer) {
  if (!window.occtimportjs) throw new Error("OpenCascade WASMを読み込めませんでした。");
  initCadPreview();

  if (!state.cadPreview.occt) {
    state.cadPreview.occt = await window.occtimportjs();
  }

  const result = state.cadPreview.occt.ReadStepFile(new Uint8Array(buffer), {
    linearUnit: "millimeter",
    linearDeflectionType: "bounding_box_ratio",
    linearDeflection: 0.0008,
    angularDeflection: 0.35,
  });
  if (!result.success || !result.meshes?.length) {
    throw new Error("OpenCascadeでSTEP形状をメッシュ化できませんでした。");
  }

  const THREE = window.THREE;
  const group = new THREE.Group();
  for (const geometryMesh of result.meshes) {
    const { mesh, edges } = buildCadMesh(geometryMesh);
    group.add(mesh);
    group.add(edges);
  }

  const box = new THREE.Box3().setFromObject(group);
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  group.position.sub(center);

  state.cadPreview.group = group;
  state.cadPreview.baseRadius = Math.max(size.x, size.y, size.z, 1);
  state.cadPreview.scene.add(group);
  setPreviewMode("cad");
  renderCadScene();
}

function renderCadScene() {
  const { renderer, scene, camera, group, baseRadius } = state.cadPreview;
  if (!renderer || !scene || !camera || !group) return;
  const canvas = $("#cadPreviewCanvas");
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.round(rect.width));
  const height = Math.max(1, Math.round(rect.height));
  renderer.setSize(width, height, false);
  camera.aspect = width / height;

  const distance = (baseRadius * 1.85) / Math.max(state.previewView.zoom, 0.2);
  camera.position.set(0, -distance, distance * 0.62);
  camera.near = Math.max(0.01, distance / 1000);
  camera.far = Math.max(1000, distance * 20);
  camera.lookAt(0, 0, 0);
  camera.updateProjectionMatrix();

  group.rotation.set(state.previewView.pitch * 0.55, 0, state.previewView.yaw, "XYZ");
  renderer.render(scene, camera);
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
    <dt>ファイル</dt><dd>${preview.fileName}</dd>
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
    .map((m) => `<option value="${m.machine_id}">${m.machine_name}</option>`)
    .join("");
  if ($("#conditionToolSelect")) {
    $("#conditionToolSelect").innerHTML = state.master.tools
      .map((t) => `<option value="${t.tool_id}">${t.tool_name}</option>`)
      .join("");
  }

  const makerTools = Array.from(new Map(state.master.manufacturer_cutting_conditions.map((c) => {
    const key = `${c.manufacturer}|${c.series_code}|${c.model_family}|${c.outside_diameter_mm}|${c.effective_length_mm}`;
    return [key, {
      tool_id: `M-${c.series_code}-${c.model_family}-${c.effective_length_mm}`,
      tool_name: `${c.manufacturer} ${c.series_code} ${c.model_family} φ${Number(c.outside_diameter_mm).toFixed(1)}`,
      tool_type: c.tool_type,
      diameter_mm: c.outside_diameter_mm,
      flute_count: "-",
      max_depth_mm: c.effective_length_mm,
      source: "メーカーPDF",
    }];
  })).values());
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
      ...state.master.tools.map((t) => ({ ...t, source: "社内" })),
      ...catalogTools,
      ...makerTools,
    ].map((t) => `
      <tr>
        <td>${t.tool_id}<br><small>${t.source}</small></td><td>${t.tool_name}</td><td>${t.tool_type}</td>
        <td>${t.diameter_mm}</td><td>${t.flute_count}</td><td>${t.max_depth_mm}</td>
        <td>${t.source === "社内" ? `<button class="danger" data-delete-tool="${t.tool_id}">削除</button>` : "-"}</td>
      </tr>
    `).join("");
  }

  $("#machineRows").innerHTML = state.master.machines.map((m) => `
    <tr>
      <td>${m.machine_id}</td><td>${m.machine_name}</td><td>${m.axis_count}</td>
      <td>${m.rapid_feed_mm_min}</td><td>${m.atc_time_sec}</td><td>${m.max_spindle_rpm}</td><td>${m.setup_time_min}</td>
      <td><button class="danger" data-delete-machine="${m.machine_id}">削除</button></td>
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
  select.innerHTML = `<option value="">全種別</option>${types.map((type) => `<option value="${type}">${type}</option>`).join("")}`;
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
      <td>${item.manufacturer}</td>
      <td>${item.product_name}<br><small>${item.memo || ""}</small></td>
      <td>${item.tool_type}</td>
      <td>${item.flute_info || "-"}</td>
      <td>${item.coating || "-"}</td>
      <td>${item.material_hint || "-"}</td>
      <td>${item.series_codes || "-"}</td>
      <td><a href="${item.catalog_url}" target="_blank" rel="noopener">PDF</a></td>
    </tr>
  `).join("");
}

function renderMakerConditionMaterialFilter() {
  const select = $("#makerConditionMaterialFilter");
  if (!select) return;
  const current = select.value;
  const materials = Array.from(new Set([
    ...state.master.manufacturer_cutting_conditions.map((item) => item.work_material).filter(Boolean),
    ...state.master.conditions.map((item) => item.material_type).filter(Boolean),
  ])).sort();
  select.innerHTML = `<option value="">全被削材</option>${materials.map((material) => `<option value="${material}">${material}</option>`).join("")}`;
  select.value = materials.includes(current) ? current : "";
}

function renderMakerConditions() {
  const rowsEl = $("#conditionRows");
  if (!rowsEl) return;
  const search = ($("#makerConditionSearch")?.value || "").trim().toLowerCase();
  const material = $("#makerConditionMaterialFilter")?.value || "";
  const makerRows = state.master.manufacturer_cutting_conditions.filter((item) => {
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
    return (!material || item.work_material === material) && (!search || text.includes(search));
  });
  const internalRows = state.master.conditions.filter((item) => {
    if (item.tool_memo?.includes("メーカーPDF条件から自動生成")) return false;
    const text = [
      item.tool_name,
      item.material_type,
      item.process_type,
    ].join(" ").toLowerCase();
    return (!material || item.material_type === material) && (!search || text.includes(search));
  });
  $("#makerConditionCountBadge").textContent = `${makerRows.length + internalRows.length}件`;
  rowsEl.innerHTML = [
    ...makerRows.map((item) => `
    <tr>
      <td>メーカーPDF<br><small>#${item.condition_id}</small></td>
      <td>${item.manufacturer}</td>
      <td>${item.series_code}</td>
      <td>${item.model_family}</td>
      <td>${Number(item.outside_diameter_mm).toFixed(1)}</td>
      <td>${Number(item.effective_length_mm).toFixed(1)}</td>
      <td>${item.work_material}<br><small>${item.material_group || ""}</small></td>
      <td>${item.spindle_rpm}</td>
      <td>${item.feed_rate_mm_min}</td>
      <td>${item.axial_depth_mm}</td>
      <td>${item.radial_depth_mm}</td>
      <td><a href="${item.source_url}" target="_blank" rel="noopener">p.${item.source_page || "-"}</a></td>
    </tr>
  `),
    ...internalRows.map((item) => `
    <tr>
      <td>社内<br><small>#${item.condition_id}</small></td>
      <td>${item.tool_name}</td>
      <td>-</td>
      <td>${item.process_type}</td>
      <td>-</td>
      <td>-</td>
      <td>${item.material_type}</td>
      <td>${item.spindle_rpm}</td>
      <td>${item.feed_rate_mm_min}</td>
      <td>${item.depth_of_cut_mm}</td>
      <td>${item.width_of_cut_mm}</td>
      <td><button class="danger" data-delete-condition="${item.condition_id}">削除</button></td>
    </tr>
  `),
  ].join("");
}

function renderResult(result) {
  state.lastResult = result;
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

  $("#featureRows").innerHTML = result.features.map((f) => `
    <tr>
      <td>${f.feature_type}</td><td>${f.dimensions}<br><small>${f.note}</small></td>
      <td>${f.quantity}</td><td>${f.tool_name}</td><td>${secLabel(f.machining_sec)}</td>
    </tr>
  `).join("");

  $("#toolUsageRows").innerHTML = result.tool_usage.map((t) => `
    <tr><td>${t.tool_name}</td><td>${t.usage_count}</td><td>${secLabel(t.machining_sec)}</td></tr>
  `).join("");

  const bbox = result.analysis.bbox;
  $("#analysisInfo").innerHTML = `
    <dt>解析方式</dt><dd>${result.analysis.parser}</dd>
    <dt>条件ソース</dt><dd>${result.condition_source || "-"}</dd>
    <dt>外形寸法</dt><dd>${bbox.x.toFixed(1)} x ${bbox.y.toFixed(1)} x ${bbox.z.toFixed(1)} mm</dd>
    <dt>エンティティ</dt><dd>${result.analysis.entity_count}</dd>
    <dt>面候補</dt><dd>${result.analysis.face_count}</dd>
    <dt>円筒面候補</dt><dd>${result.analysis.cylindrical_radii.length}</dd>
  `;
}

async function loadHistories() {
  const rows = await jsonFetch("/api/histories");
  $("#historyRows").innerHTML = rows.map((h) => `
    <tr>
      <td>${h.history_id}</td><td>${h.created_at}</td><td>${h.file_name}</td>
      <td>${h.material_type}</td><td>${h.machine_name}</td><td>${h.time_label}</td>
      <td>${Math.round(h.confidence * 100)}%</td>
      <td><a class="secondary-link compact" href="/api/histories/${h.history_id}/csv">CSV</a></td>
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
      state.previewView.pitch = Math.max(-1.35, Math.min(1.15, state.previewView.pitch + dy * 0.01));
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
      state.previewView.yaw = -0.68;
      state.previewView.pitch = -0.46;
      state.previewView.zoom = 1;
      renderCurrentPreview();
    });
  });

  fileInput.addEventListener("change", (event) => {
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
    const button = event.submitter;
    button.disabled = true;
    button.textContent = "解析中";
    try {
      const data = await jsonFetch("/api/analyze", { method: "POST", body: new FormData(event.currentTarget) });
      renderResult(data);
      toast("解析が完了しました。");
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
      button.textContent = "解析実行";
    }
  });

  $("#toolForm")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await jsonFetch("/api/tools", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formJson(event.currentTarget)),
    });
    event.currentTarget.reset();
    await loadMaster();
  });

  $("#conditionForm")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    await jsonFetch("/api/conditions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formJson(event.currentTarget)),
    });
    event.currentTarget.reset();
    await loadMaster();
  });

  $("#machineForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await jsonFetch("/api/machines", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formJson(event.currentTarget)),
    });
    event.currentTarget.reset();
    await loadMaster();
  });

  $("#catalogSearch")?.addEventListener("input", renderCatalogs);
  $("#catalogTypeFilter")?.addEventListener("change", renderCatalogs);
  $("#makerConditionSearch")?.addEventListener("input", renderMakerConditions);
  $("#makerConditionMaterialFilter")?.addEventListener("change", renderMakerConditions);

  document.body.addEventListener("click", async (event) => {
    const toolId = event.target.dataset.deleteTool;
    const conditionId = event.target.dataset.deleteCondition;
    const machineId = event.target.dataset.deleteMachine;
    if (toolId) await jsonFetch(`/api/tools/${toolId}`, { method: "DELETE" });
    if (conditionId) await jsonFetch(`/api/conditions/${conditionId}`, { method: "DELETE" });
    if (machineId) await jsonFetch(`/api/machines/${machineId}`, { method: "DELETE" });
    if (toolId || conditionId || machineId) await loadMaster();
  });
}

bindEvents();
drawPreviewPlaceholder();
loadMaster().catch((error) => toast(error.message));
