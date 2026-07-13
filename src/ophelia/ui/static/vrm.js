/**
 * VRoid / VRM stage — three.js + @pixiv/three-vrm (loaded from CDN via import map).
 *
 * Applies the server avatar bus: expression presets, lip sync (aa/oh), look, head pose.
 */
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";

const DEG = Math.PI / 180;

export class VrmStage {
  constructor(canvas) {
    this.canvas = canvas;
    this.ready = false;
    this.loading = false;
    this.error = null;
    this.modelUrl = null;
    this.vrm = null;
    this._clock = new THREE.Clock();
    this._pointer = { x: 0, y: 0 };
    this._state = null;
    this._raf = 0;
    this._running = false;

    this.renderer = new THREE.WebGLRenderer({
      canvas,
      alpha: true,
      antialias: true,
      powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(30, 1, 0.1, 20);
    this.camera.position.set(0, 1.35, 2.2);

    const hemi = new THREE.HemisphereLight(0xfff0ff, 0x1a1020, 1.1);
    this.scene.add(hemi);
    const key = new THREE.DirectionalLight(0xffffff, 1.35);
    key.position.set(1.2, 1.8, 1.5);
    this.scene.add(key);
    const fill = new THREE.DirectionalLight(0xb48cff, 0.45);
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
    if (this.vrm) {
      this.scene.remove(this.vrm.scene);
      VRMUtils.deepDispose?.(this.vrm.scene);
      this.vrm = null;
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
    if (this.loading || url === this.modelUrl && this.ready) return;
    this.loading = true;
    this.error = null;
    this.modelUrl = url;
    try {
      if (this.vrm) {
        this.scene.remove(this.vrm.scene);
        VRMUtils.deepDispose?.(this.vrm.scene);
        this.vrm = null;
      }
      const loader = new GLTFLoader();
      loader.register((parser) => new VRMLoaderPlugin(parser));
      const gltf = await loader.loadAsync(url);
      const vrm = gltf.userData.vrm;
      if (!vrm) throw new Error("No VRM data in file");
      VRMUtils.removeUnnecessaryVertices?.(gltf.scene);
      VRMUtils.combineSkeletons?.(gltf.scene);
      // VRM 0.x faces -Z; rotate so model looks at camera
      if (vrm.meta?.metaVersion === "0" || vrm.meta?.metaVersion === 0) {
        vrm.scene.rotation.y = Math.PI;
      }
      this.scene.add(vrm.scene);
      this.vrm = vrm;
      this._frameModel();
      this.ready = true;
      console.info("VRoid/VRM loaded:", url);
    } catch (err) {
      this.error = String(err?.message || err);
      this.ready = false;
      console.warn("VRM load failed:", err);
    } finally {
      this.loading = false;
    }
  }

  _setExpression(name, value) {
    const em = this.vrm?.expressionManager;
    if (!em) return;
    try {
      const cur = typeof em.getValue === "function" ? em.getValue(name) : 0;
      const next = cur * 0.55 + Math.max(0, Math.min(1, value)) * 0.45;
      em.setValue(name, next);
    } catch {
      /* preset missing on some models */
    }
  }

  _applyVrmWeights(weights) {
    if (!weights) return;
    for (const [name, value] of Object.entries(weights)) {
      this._setExpression(name, value);
    }
  }

  _applyPose(params) {
    if (!this.vrm?.humanoid) return;
    const g = this._state?.gesture || {};
    const ax = (params?.ParamAngleX || 0) + this._pointer.x * 10;
    const ay = (params?.ParamAngleY || 0) + this._pointer.y * -8 + (g.nod || 0) * 10;
    const az = params?.ParamAngleZ || 0;
    const bodyX = (params?.ParamBodyAngleX || 0) + (g.lean_in || 0) * 6;
    const breath = params?.ParamBreath ?? 0.5;

    const head = this.vrm.humanoid.getNormalizedBoneNode("head");
    const neck = this.vrm.humanoid.getNormalizedBoneNode("neck");
    const spine = this.vrm.humanoid.getNormalizedBoneNode("spine");
    const chest = this.vrm.humanoid.getNormalizedBoneNode("chest");

    if (head) {
      head.rotation.x = ay * DEG * 0.6;
      head.rotation.y = -ax * DEG * 0.55;
      head.rotation.z = -az * DEG * 0.4;
    }
    if (neck) {
      neck.rotation.x = ay * DEG * 0.25;
      neck.rotation.y = -ax * DEG * 0.2;
    }
    if (spine) {
      spine.rotation.y = -bodyX * DEG * 0.15;
      spine.rotation.x = (breath - 0.5) * 0.04;
    }
    if (chest) {
      chest.rotation.y = -bodyX * DEG * 0.1;
      chest.rotation.x = (breath - 0.5) * 0.03;
    }

    // Soft look-at via eye bones when present
    const look = this.vrm.lookAt;
    if (look?.target) {
      look.target.position.set(this._pointer.x * 0.6, 1.4 + this._pointer.y * 0.25, 1.5);
    } else if (look?.applier) {
      try {
        look.yaw = -this._pointer.x * 12 * DEG;
        look.pitch = this._pointer.y * 8 * DEG;
      } catch {
        /* older three-vrm */
      }
    }
  }

  _frameModel() {
    if (!this.vrm) return;
    // Frame upper body
    const box = new THREE.Box3().setFromObject(this.vrm.scene);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const headY = center.y + size.y * 0.22;
    this.camera.position.set(0, headY, Math.max(1.6, size.y * 1.15));
    this.camera.lookAt(0, headY - 0.05, 0);
  }

  _frame() {
    const dt = this._clock.getDelta();
    const state = this._state;
    if (this.vrm) {
      if (state?.vrm) this._applyVrmWeights(state.vrm);
      this._applyPose(state?.params || {});
      this.vrm.update(dt);
    }
    this.renderer.render(this.scene, this.camera);
  }
}

export async function createVrmStage(canvas) {
  return new VrmStage(canvas);
}
