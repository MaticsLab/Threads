/* 3D stitch viewer: every stitch is an instanced capsule of thread lying on
   a woven fabric plane, lit for a realistic sheen. Loaded as an ES module;
   "three" resolves via the import map to the vendored build. */
import * as THREE from 'three';
import { OrbitControls } from '/static/vendor/three/OrbitControls.js';

const PX_PER_MM = 96 / 25.4;

let renderer = null, scene = null, camera = null, controls = null,
    raf = 0, meshes = [], host = null, resizeFn = null;

function fabricTexture() {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const g = c.getContext('2d');
  g.fillStyle = '#efe9dc';
  g.fillRect(0, 0, 64, 64);
  g.strokeStyle = 'rgba(120,110,90,0.25)';
  g.lineWidth = 1;
  for (let i = 0; i < 64; i += 4) {
    g.beginPath(); g.moveTo(i + 0.5, 0); g.lineTo(i + 0.5, 64); g.stroke();
    g.beginPath(); g.moveTo(0, i + 0.5); g.lineTo(64, i + 0.5); g.stroke();
  }
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  return t;
}

export function mount(container, blocks, colorOf) {
  dispose();
  host = container;

  // bounds in mm (stitch JSON is in CSS px)
  let minx = 1e18, miny = 1e18, maxx = -1e18, maxy = -1e18, total = 0;
  blocks.forEach(b => b.runs.forEach(r => r.forEach(p => {
    if (p[0] < minx) minx = p[0]; if (p[0] > maxx) maxx = p[0];
    if (p[1] < miny) miny = p[1]; if (p[1] > maxy) maxy = p[1];
  })));
  blocks.forEach(b => b.runs.forEach(r => { total += Math.max(0, r.length - 1); }));
  const cx = (minx + maxx) / 2 / PX_PER_MM, cz = (miny + maxy) / 2 / PX_PER_MM;
  const w = (maxx - minx) / PX_PER_MM || 10, h = (maxy - miny) / PX_PER_MM || 10;

  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x171a21);

  camera = new THREE.PerspectiveCamera(38, container.clientWidth / container.clientHeight, 0.5, 4000);
  const dist = Math.max(w, h) * 1.15;
  camera.position.set(cx, dist * 0.85, cz + dist * 0.75);

  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.domElement.style.cssText = 'width:100%;height:100%;display:block;border-radius:inherit';
  container.appendChild(renderer.domElement);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(cx, 0, cz);
  controls.enableDamping = true;
  controls.maxPolarAngle = Math.PI / 2.05;
  controls.minDistance = 5;
  controls.maxDistance = dist * 4;

  scene.add(new THREE.HemisphereLight(0xf4f6ff, 0x3a3428, 0.95));
  const sun = new THREE.DirectionalLight(0xfff2dd, 1.6);
  sun.position.set(cx - w, Math.max(w, h), cz - h * 0.6);
  scene.add(sun);

  // fabric
  const tex = fabricTexture();
  tex.repeat.set(w / 3, h / 3);
  const fabric = new THREE.Mesh(
    new THREE.PlaneGeometry(w * 1.5, h * 1.5),
    new THREE.MeshStandardMaterial({ map: tex, roughness: 0.95, metalness: 0 }));
  fabric.rotation.x = -Math.PI / 2;
  fabric.position.set(cx, 0, cz);
  scene.add(fabric);

  // stitches: one InstancedMesh per colour block so recolouring is cheap
  const geo = new THREE.CapsuleGeometry(0.2, 1, 3, 10);
  const up = new THREE.Vector3(0, 1, 0);
  const m4 = new THREE.Matrix4(), q = new THREE.Quaternion(),
        pos = new THREE.Vector3(), dir = new THREE.Vector3(), scl = new THREE.Vector3();
  meshes = [];
  blocks.forEach((b, bi) => {
    let n = 0;
    b.runs.forEach(r => { n += Math.max(0, r.length - 1); });
    if (!n) return;
    const mat = new THREE.MeshStandardMaterial({ roughness: 0.32, metalness: 0.12 });
    const mesh = new THREE.InstancedMesh(geo, mat, n);
    const col = new THREE.Color(colorOf(bi));
    let i = 0;
    b.runs.forEach(r => {
      for (let k = 1; k < r.length; k++) {
        const x1 = r[k - 1][0] / PX_PER_MM, z1 = r[k - 1][1] / PX_PER_MM;
        const x2 = r[k][0] / PX_PER_MM, z2 = r[k][1] / PX_PER_MM;
        const L = Math.max(0.25, Math.hypot(x2 - x1, z2 - z1));
        pos.set((x1 + x2) / 2, 0.22 + (i % 7) * 0.012, (z1 + z2) / 2);
        dir.set(x2 - x1, 0.06, z2 - z1).normalize();
        q.setFromUnitVectors(up, dir);
        scl.set(1, L, 1);
        m4.compose(pos, q, scl);
        mesh.setMatrixAt(i, m4);
        mesh.setColorAt(i, col);
        i++;
      }
    });
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    scene.add(mesh);
    meshes.push({ mesh, block: bi, count: n });
  });

  resizeFn = () => {
    if (!renderer || !host) return;
    camera.aspect = host.clientWidth / host.clientHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(host.clientWidth, host.clientHeight);
  };
  window.addEventListener('resize', resizeFn);

  const loop = () => {
    raf = requestAnimationFrame(loop);
    controls.update();
    renderer.render(scene, camera);
  };
  loop();
  return total;
}

export function setColors(colorOf) {
  const c = new THREE.Color();
  for (const { mesh, block, count } of meshes) {
    c.set(colorOf(block));
    for (let i = 0; i < count; i++) mesh.setColorAt(i, c);
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  }
}

export function dispose() {
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
  if (resizeFn) window.removeEventListener('resize', resizeFn);
  resizeFn = null;
  if (renderer) {
    renderer.domElement.remove();
    renderer.dispose();
  }
  if (scene) scene.traverse(o => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) {
      if (o.material.map) o.material.map.dispose();
      o.material.dispose();
    }
  });
  renderer = scene = camera = controls = host = null;
  meshes = [];
}

window.Stitch3D = { mount, setColors, dispose };
