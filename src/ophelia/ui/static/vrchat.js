/**
 * VRChat glTF/GLB stage — three.js GLTFLoader + morph-target / bone driving.
 *
 * Loads humanoid exports (Blender/Unity → .glb/.gltf) and applies the server
 * `vrchat` morph bus (SDK3 visemes + expression aliases) plus head look from
 * Live2D-style params. Native .vrca AssetBundles are not supported in-browser.
 */
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const DEG = Math.PI / 180;

const BONE_CANDIDATES = {
  head: ["Head", "head", "mixamorigHead", "Head_M"],
  neck: ["Neck", "neck", "mixamorigNeck", "Neck_M"],
  spine: ["Spine", "spine", "mixamorigSpine", "Spine_M"],
  chest: ["Chest", "chest", "UpperChest", "mixamorigSpine1", "Spine1_M"],
};

function findBone(root, names) {
  let found = null;
  root.traverse((obj) => {
    if (found || !obj.isBone && !obj.isObject3D) return;
    if (names.includes(obj.name)) found = obj;
  });
  return found;
}

export class VrchatStage {
  constructor(canvas) {
    this.canvas = canvas;
    this.ready = false;
    this.loading = false;
    this.error = null;
    this.modelUrl = null;
    this.root = null;
    this._morphs = []; // { mesh, index, name, nameLower }
    this._bones = {};
    this._clock = new THREE.Clock();
    this._pointer = { x: 0, y: 0 };
    this._state = null;
    this._raf = 0;
    this._running = false;
    this._breath = 0;

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
    if (this.root) {
      this.scene.remove(this.root);
      this.root.traverse((obj) => {
        if (obj.geometry) obj.geometry.dispose?.();
        if (obj.material) {
          const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
          mats.forEach((m) => m.dispose?.());
        }
      });
      this.root = null;
    }
    this.renderer.dispose();
    this.ready = false;
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
      if (this.root) {
        this.scene.remove(this.root);
        this.root = null;
      }
      this._morphs = [];
      this._bones = {};

      const loader = new GLTFLoader();
      const gltf = await loader.loadAsync(url);
      const root = gltf.scene;
      root.traverse((obj) => {
        if (obj.isMesh && obj.morphTargetDictionary && obj.morphTargetInfluences) {
          for (const [name, index] of Object.entries(obj.morphTargetDictionary)) {
            this._morphs.push({
              mesh: obj,
              index,
              name,
              nameLower: name.toLowerCase(),
            });
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
        "VRChat glTF loaded:",
        url,
        `morphs=${this._morphs.length}`,
        `bones=${Object.keys(this._bones).filter((k) => this._bones[k]).join(",") || "none"}`
      );
    } catch (err) {
      this.error = String(err?.message || err);
      this.ready = false;
      console.warn("VRChat glTF load failed:", err);
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
        m.mesh.morphTargetInfluences[m.index] = target;
      }
    }
  }

  _applyMorphWeights(weights) {
    if (!weights) return;
    // Reset known influences gently by writing provided keys; leave others alone.
    for (const [name, value] of Object.entries(weights)) {
      this._setMorph(name, value);
    }
  }

  _applyPose(params) {
    const ax = (params?.ParamAngleX || 0) + this._pointer.x * 10;
    const ay = (params?.ParamAngleY || 0) + this._pointer.y * -8;
    const az = params?.ParamAngleZ || 0;
    const bodyX = params?.ParamBodyAngleX || 0;
    const breath = params?.ParamBreath ?? 0.5;
    this._breath = breath;

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
    // Prefer upper body / face framing
    const focusY = center.y + size.y * 0.18;
    const dist = Math.max(1.7, Math.max(size.x, size.y) * 1.25);
    this.camera.position.set(0, focusY, dist);
    this.camera.lookAt(0, focusY - 0.05, 0);
  }

  _frame() {
    this._clock.getDelta();
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
