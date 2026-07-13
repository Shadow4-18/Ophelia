/**
 * VRChat FBX / glTF stage — three.js FBXLoader + GLTFLoader.
 *
 * VRChat avatars are authored as Unity humanoid FBX. Loads `.fbx` (primary) or
 * `.glb`/`.gltf` (alternate), then applies the server `vrchat` morph bus
 * (SDK3 visemes + expression aliases) and head look from shared params.
 * Native .vrca AssetBundles are not supported in-browser.
 */
import * as THREE from "three";
import { FBXLoader } from "three/addons/loaders/FBXLoader.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const DEG = Math.PI / 180;

const BONE_CANDIDATES = {
  head: ["Head", "head", "mixamorigHead", "Head_M", "head.x"],
  neck: ["Neck", "neck", "mixamorigNeck", "Neck_M", "neck.x"],
  spine: ["Spine", "spine", "mixamorigSpine", "Spine_M", "spine.x"],
  chest: [
    "Chest",
    "chest",
    "UpperChest",
    "upperchest",
    "mixamorigSpine1",
    "Spine1_M",
    "spine1.x",
  ],
};

function findBone(root, names) {
  const lower = names.map((n) => n.toLowerCase());
  let found = null;
  root.traverse((obj) => {
    if (found) return;
    const n = (obj.name || "").toLowerCase();
    if (lower.includes(n)) found = obj;
  });
  return found;
}

function extensionOf(url) {
  try {
    const path = new URL(url, window.location.href).pathname;
    return path.split(".").pop()?.toLowerCase() || "";
  } catch {
    return (url.split(".").pop() || "").toLowerCase();
  }
}

export class VrchatStage {
  constructor(canvas) {
    this.canvas = canvas;
    this.ready = false;
    this.loading = false;
    this.error = null;
    this.modelUrl = null;
    this.root = null;
    this._morphs = [];
    this._bones = {};
    this._clock = new THREE.Clock();
    this._pointer = { x: 0, y: 0 };
    this._state = null;
    this._raf = 0;
    this._running = false;
    this._mixer = null;

    this.renderer = new THREE.WebGLRenderer({
      canvas,
      alpha: true,
      antialias: true,
      powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(30, 1, 0.1, 40);
    this.camera.position.set(0, 1.4, 2.4);

    this.scene.add(new THREE.HemisphereLight(0xfff0ff, 0x1a1020, 1.15));
    const key = new THREE.DirectionalLight(0xffffff, 1.4);
    key.position.set(1.2, 1.8, 1.5);
    this.scene.add(key);
    const fill = new THREE.DirectionalLight(0xb48cff, 0.4);
    fill.position.set(-1.4, 1.0, -0.6);
    this.scene.add(fill);

    canvas.addEventListener("pointermove", (e) => {
      const r = canvas.getBoundingClientRect();
      this._pointer.x = ((e.clientX - r.left) / r.width) * 2 - 1;
      this._pointer.y = ((e.clientY - r.top) / r.height) * 2 - 1;
    });
    canvas.addEventListener("pointerleave", () => {
      this._pointer.x = 0;
      this._pointer.y = 0;
    });

    this.resize();
  }

  resize() {
    const parent = this.canvas.parentElement;
    const w = Math.max(1, Math.floor(parent?.clientWidth || this.canvas.clientWidth || 640));
    const h = Math.max(1, Math.floor(parent?.clientHeight || this.canvas.clientHeight || 720));
    this.canvas.width = w;
    this.canvas.height = h;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  start() {
    if (this._running) return;
    this._running = true;
    const loop = () => {
      this._raf = requestAnimationFrame(loop);
      this._frame();
    };
    this._raf = requestAnimationFrame(loop);
  }

  stop() {
    this._running = false;
    cancelAnimationFrame(this._raf);
  }

  dispose() {
    this.stop();
    this._clearRoot();
    this.renderer.dispose();
    this.ready = false;
  }

  _clearRoot() {
    if (!this.root) return;
    this.scene.remove(this.root);
    this.root.traverse((obj) => {
      if (obj.geometry) obj.geometry.dispose?.();
      if (obj.material) {
        const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
        mats.forEach((m) => m.dispose?.());
      }
    });
    this.root = null;
    this._mixer = null;
    this._morphs = [];
    this._bones = {};
  }

  apply(state) {
    this._state = state;
    if (state?.model_url && state.model_url !== this.modelUrl) {
      this.load(state.model_url);
    }
  }

  async load(url) {
    if (this.loading || (url === this.modelUrl && this.ready)) return;
    this.loading = true;
    this.error = null;
    this.modelUrl = url;
    try {
      this._clearRoot();
      const ext = extensionOf(url);
      let root;
      if (ext === "fbx") {
        const loader = new FBXLoader();
        root = await loader.loadAsync(url);
        // FBX from Unity is often huge (cm). Normalize toward meters.
        if (root.scale.x === 1 && root.scale.y === 1) {
          const box = new THREE.Box3().setFromObject(root);
          const size = box.getSize(new THREE.Vector3());
          if (size.y > 10) {
            const s = 1.7 / Math.max(size.y, 0.001);
            root.scale.setScalar(s);
          }
        }
      } else {
        const loader = new GLTFLoader();
        const gltf = await loader.loadAsync(url);
        root = gltf.scene;
      }

      root.traverse((obj) => {
        if (obj.isMesh) {
          obj.castShadow = false;
          obj.receiveShadow = false;
          if (obj.morphTargetDictionary && obj.morphTargetInfluences) {
            for (const [name, index] of Object.entries(obj.morphTargetDictionary)) {
              this._morphs.push({
                mesh: obj,
                index,
                name,
                nameLower: name.toLowerCase(),
              });
            }
          }
        }
      });
      for (const [key, names] of Object.entries(BONE_CANDIDATES)) {
        this._bones[key] = findBone(root, names);
      }

      this.scene.add(root);
      this.root = root;
      this._frameModel();
      this.ready = true;
      console.info(
        "VRChat model loaded:",
        url,
        `format=${ext || "?"}`,
        `morphs=${this._morphs.length}`,
        `bones=${Object.keys(this._bones).filter((k) => this._bones[k]).join(",") || "none"}`
      );
    } catch (err) {
      this.error = String(err?.message || err);
      this.ready = false;
      console.warn("VRChat model load failed:", err);
    } finally {
      this.loading = false;
    }
  }

  _setMorph(name, value) {
    if (!name) return;
    const target = Math.max(0, Math.min(1, value));
    const lower = name.toLowerCase();
    for (const m of this._morphs) {
      if (m.name === name || m.nameLower === lower) {
        const cur = m.mesh.morphTargetInfluences[m.index] || 0;
        m.mesh.morphTargetInfluences[m.index] = cur * 0.55 + target * 0.45;
      }
    }
  }

  _applyMorphWeights(weights) {
    if (!weights) return;
    for (const [name, value] of Object.entries(weights)) {
      this._setMorph(name, value);
    }
  }

  _applyPose(params) {
    const g = this._state?.gesture || {};
    const ax = (params?.ParamAngleX || 0) + this._pointer.x * 10;
    const ay = (params?.ParamAngleY || 0) + this._pointer.y * -8 + (g.nod || 0) * 10;
    const az = params?.ParamAngleZ || 0;
    const bodyX = (params?.ParamBodyAngleX || 0) + (g.lean_in || 0) * 6;
    const breath = params?.ParamBreath ?? 0.5;

    const head = this._bones.head;
    const neck = this._bones.neck;
    const spine = this._bones.spine;
    const chest = this._bones.chest;

    if (head) {
      head.rotation.x = ay * DEG * 0.55;
      head.rotation.y = -ax * DEG * 0.5;
      head.rotation.z = -az * DEG * 0.35;
    }
    if (neck) {
      neck.rotation.x = ay * DEG * 0.22;
      neck.rotation.y = -ax * DEG * 0.18;
    }
    if (spine) {
      spine.rotation.y = -bodyX * DEG * 0.12;
      spine.rotation.x = (breath - 0.5) * 0.04;
    }
    if (chest) {
      chest.rotation.y = -bodyX * DEG * 0.08;
      chest.rotation.x = (breath - 0.5) * 0.03;
    }
  }

  _frameModel() {
    if (!this.root) return;
    const box = new THREE.Box3().setFromObject(this.root);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const focusY = center.y + size.y * 0.18;
    const dist = Math.max(1.7, Math.max(size.x, size.y) * 1.25);
    this.camera.position.set(0, focusY, dist);
    this.camera.lookAt(0, focusY - 0.05, 0);
  }

  _frame() {
    const dt = this._clock.getDelta();
    if (this._mixer) this._mixer.update(dt);
    const state = this._state;
    if (this.root) {
      if (state?.vrchat) this._applyMorphWeights(state.vrchat);
      this._applyPose(state?.params || {});
    }
    this.renderer.render(this.scene, this.camera);
  }
}

export async function createVrchatStage(canvas) {
  return new VrchatStage(canvas);
}
