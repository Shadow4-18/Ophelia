/**
 * Ophelia avatar stage — procedural presence + Live2D hook + VRoid/VRM.
 *
 * Consumes the same parameter bus the server emits (ParamAngleX, ParamMouthOpenY, …)
 * plus VRM expression weights. Drop a Cubism model or VRoid .vrm under ~/.ophelia/avatar.
 */
(function (global) {
  const DEFAULT_PARAMS = {
    ParamAngleX: 0,
    ParamAngleY: 0,
    ParamAngleZ: 0,
    ParamEyeLOpen: 1,
    ParamEyeROpen: 1,
    ParamEyeBallX: 0,
    ParamEyeBallY: 0,
    ParamBrowLY: 0,
    ParamBrowRY: 0,
    ParamMouthOpenY: 0,
    ParamMouthForm: 0,
    ParamBodyAngleX: 0,
    ParamBreath: 0.5,
  };

  class AvatarStage {
    constructor(canvas, vrmCanvas) {
      this.canvas = canvas;
      this.vrmCanvas = vrmCanvas || null;
      this.ctx = canvas.getContext("2d");
      this.params = { ...DEFAULT_PARAMS };
      this.expression = "neutral";
      this.speaking = false;
      this.backend = "procedural";
      this.modelUrl = null;
      this.pointer = { x: 0, y: 0 };
      this._blinkUntil = 0;
      this._nextBlink = performance.now() + 2800;
      this._raf = 0;
      this._live2d = null;
      this._vrm = null;
      this._vrmLoading = false;
      this._useVrm = false;
      this._running = false;

      canvas.addEventListener("pointermove", (e) => {
        const r = canvas.getBoundingClientRect();
        this.pointer.x = ((e.clientX - r.left) / r.width) * 2 - 1;
        this.pointer.y = ((e.clientY - r.top) / r.height) * 2 - 1;
      });
      canvas.addEventListener("pointerleave", () => {
        this.pointer.x = 0;
        this.pointer.y = 0;
      });
    }

    start() {
      if (this._running) return;
      this._running = true;
      const loop = (t) => {
        this._raf = requestAnimationFrame(loop);
        this._tickBlink(t);
        if (!this._useVrm) this.draw(t);
      };
      this._raf = requestAnimationFrame(loop);
    }

    stop() {
      this._running = false;
      cancelAnimationFrame(this._raf);
      this._vrm?.stop?.();
    }

    apply(state) {
      if (!state || state.enabled === false) return;
      this.expression = state.expression || "neutral";
      this.speaking = !!state.speaking;
      this.backend = state.backend || "procedural";
      this.modelUrl = state.model_url || null;
      if (state.params) {
        this.params = { ...DEFAULT_PARAMS, ...state.params };
      }
      if (typeof state.mouth_open === "number") {
        this.params.ParamMouthOpenY = state.mouth_open;
      }
      if (this.backend === "vroid" && this.modelUrl) {
        this._ensureVrm(state);
      } else {
        this._showProcedural();
        if (this.backend === "live2d" && this.modelUrl && !this._live2d) {
          this._tryLoadLive2D(this.modelUrl);
        }
      }
    }

    _showProcedural() {
      this._useVrm = false;
      this.canvas.hidden = false;
      if (this.vrmCanvas) this.vrmCanvas.hidden = true;
      this._vrm?.stop?.();
    }

    _showVrm() {
      this._useVrm = true;
      this.canvas.hidden = true;
      if (this.vrmCanvas) this.vrmCanvas.hidden = false;
      this._vrm?.start?.();
      this._vrm?.resize?.();
    }

    async _ensureVrm(state) {
      if (!this.vrmCanvas) {
        this._showProcedural();
        return;
      }
      if (this._vrm) {
        this._showVrm();
        this._vrm.apply(state);
        return;
      }
      if (this._vrmLoading) return;
      this._vrmLoading = true;
      try {
        const mod = await import("/static/vrm.js");
        this._vrm = await mod.createVrmStage(this.vrmCanvas);
        this._showVrm();
        this._vrm.apply(state);
        this._vrm.start();
      } catch (err) {
        console.warn("VRoid/VRM stage failed; falling back to procedural", err);
        this.backend = "procedural";
        this._showProcedural();
      } finally {
        this._vrmLoading = false;
      }
    }

    async _tryLoadLive2D(url) {
      if (!global.Live2DCubismCore && !global.PIXI) {
        return;
      }
      try {
        this._live2d = { url, ready: false };
        console.info("Live2D runtime detected; model URL ready:", url);
      } catch (err) {
        console.warn("Live2D load failed; staying procedural", err);
        this._live2d = null;
      }
    }

    resize() {
      if (this._useVrm && this._vrm) this._vrm.resize();
    }

    _tickBlink(now) {
      if (now >= this._nextBlink) {
        this._blinkUntil = now + 140;
        this._nextBlink = now + 2400 + Math.random() * 3200;
      }
    }

    _p(name) {
      return this.params[name] ?? DEFAULT_PARAMS[name] ?? 0;
    }

    draw(now) {
      const ctx = this.ctx;
      const w = this.canvas.width;
      const h = this.canvas.height;
      ctx.clearRect(0, 0, w, h);

      const g = ctx.createRadialGradient(w * 0.5, h * 0.42, 40, w * 0.5, h * 0.5, w * 0.55);
      g.addColorStop(0, "rgba(140, 70, 190, 0.22)");
      g.addColorStop(0.55, "rgba(40, 24, 70, 0.18)");
      g.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);

      const breath = this._p("ParamBreath");
      const ax = this._p("ParamAngleX") + this.pointer.x * 8;
      const ay = this._p("ParamAngleY") + this.pointer.y * -6;
      const az = (this._p("ParamAngleZ") * Math.PI) / 180;
      const bodyX = this._p("ParamBodyAngleX");
      const cx = w * 0.5 + bodyX * 2.2;
      const cy = h * 0.58 + (breath - 0.5) * 10;
      const scale = Math.min(w / 720, h / 900) * 1.15;

      ctx.save();
      ctx.translate(cx, cy);
      ctx.scale(scale, scale);
      ctx.rotate(az * 0.35);
      ctx.translate(ax * 1.4, ay * 1.1);

      this._drawBody(ctx, breath);
      this._drawHead(ctx, now);
      ctx.restore();
    }

    _drawBody(ctx, breath) {
      ctx.save();
      ctx.translate(0, 40 + (breath - 0.5) * 6);
      const body = ctx.createLinearGradient(0, -40, 0, 220);
      body.addColorStop(0, "#2a1a3a");
      body.addColorStop(0.45, "#1a1228");
      body.addColorStop(1, "#0c0a14");
      ctx.fillStyle = body;
      ctx.beginPath();
      ctx.moveTo(-54, -20);
      ctx.quadraticCurveTo(-78, 50, -70, 180);
      ctx.lineTo(70, 180);
      ctx.quadraticCurveTo(78, 50, 54, -20);
      ctx.quadraticCurveTo(0, 10, -54, -20);
      ctx.fill();

      ctx.strokeStyle = "rgba(196, 77, 255, 0.55)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(-36, -8);
      ctx.quadraticCurveTo(0, 18, 36, -8);
      ctx.stroke();
      ctx.restore();
    }

    _drawHead(ctx, now) {
      const eyeL = this._eyeOpen("ParamEyeLOpen", now);
      const eyeR = this._eyeOpen("ParamEyeROpen", now);
      const browL = this._p("ParamBrowLY");
      const browR = this._p("ParamBrowRY");
      const mouthOpen = Math.max(0, Math.min(1, this._p("ParamMouthOpenY")));
      const mouthForm = Math.max(-1, Math.min(1, this._p("ParamMouthForm")));
      const ballX = this._p("ParamEyeBallX") + this.pointer.x * 0.25;
      const ballY = this._p("ParamEyeBallY") + this.pointer.y * 0.2;

      ctx.fillStyle = "#1a0f28";
      ctx.beginPath();
      ctx.ellipse(0, -118, 92, 108, 0, 0, Math.PI * 2);
      ctx.fill();

      const skin = ctx.createLinearGradient(-40, -160, 40, -40);
      skin.addColorStop(0, "#f3d7c8");
      skin.addColorStop(1, "#e2b7a8");
      ctx.fillStyle = skin;
      ctx.beginPath();
      ctx.ellipse(0, -108, 68, 78, 0, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = "#241433";
      ctx.beginPath();
      ctx.moveTo(-70, -150);
      ctx.quadraticCurveTo(-40, -180, 0, -168);
      ctx.quadraticCurveTo(40, -180, 70, -150);
      ctx.quadraticCurveTo(50, -120, 20, -128);
      ctx.quadraticCurveTo(0, -145, -20, -128);
      ctx.quadraticCurveTo(-50, -120, -70, -150);
      ctx.fill();

      ctx.beginPath();
      ctx.moveTo(-72, -120);
      ctx.quadraticCurveTo(-95, -40, -78, 30);
      ctx.quadraticCurveTo(-60, -20, -58, -90);
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(72, -120);
      ctx.quadraticCurveTo(95, -40, 78, 30);
      ctx.quadraticCurveTo(60, -20, 58, -90);
      ctx.fill();

      if (this.expression === "shy" || this.expression === "happy" || mouthForm > 0.35) {
        ctx.fillStyle = "rgba(230, 110, 140, 0.28)";
        ctx.beginPath();
        ctx.ellipse(-38, -88, 14, 8, 0, 0, Math.PI * 2);
        ctx.ellipse(38, -88, 14, 8, 0, 0, Math.PI * 2);
        ctx.fill();
      }

      this._drawEye(ctx, -26, -112, eyeL, ballX, ballY, browL);
      this._drawEye(ctx, 26, -112, eyeR, ballX, ballY, browR);
      this._drawMouth(ctx, 0, -72, mouthOpen, mouthForm);
    }

    _eyeOpen(param, now) {
      let open = Math.max(0.05, Math.min(1, this._p(param)));
      if (now < this._blinkUntil) open = Math.min(open, 0.08);
      return open;
    }

    _drawEye(ctx, x, y, open, ballX, ballY, brow) {
      ctx.save();
      ctx.translate(x, y);
      ctx.strokeStyle = "#2a1838";
      ctx.lineWidth = 3;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(-14, -18 - brow * 10);
      ctx.quadraticCurveTo(0, -22 - brow * 14, 14, -16 - brow * 8);
      ctx.stroke();

      ctx.beginPath();
      ctx.ellipse(0, 0, 15, 11 * open, 0, 0, Math.PI * 2);
      ctx.clip();
      ctx.fillStyle = "#faf8ff";
      ctx.fillRect(-16, -12, 32, 24);
      ctx.fillStyle = "#3a1f55";
      ctx.beginPath();
      ctx.arc(ballX * 6, ballY * 4, 7.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#0a0610";
      ctx.beginPath();
      ctx.arc(ballX * 6, ballY * 4, 3.2, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.85)";
      ctx.beginPath();
      ctx.arc(ballX * 6 - 2.5, ballY * 4 - 2.5, 1.6, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();

      ctx.strokeStyle = "#1c1028";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.ellipse(x, y, 15, 11 * Math.max(open, 0.15), 0, Math.PI * 1.05, Math.PI * 1.95);
      ctx.stroke();
    }

    _drawMouth(ctx, x, y, open, form) {
      ctx.save();
      ctx.translate(x, y);
      const smile = form * 6;
      const h = 3 + open * 16;
      ctx.fillStyle = "#5a2038";
      ctx.beginPath();
      if (open < 0.08) {
        ctx.moveTo(-12, smile * 0.15);
        ctx.quadraticCurveTo(0, smile, 12, smile * 0.15);
        ctx.strokeStyle = "#5a2038";
        ctx.lineWidth = 2.2;
        ctx.lineCap = "round";
        ctx.stroke();
      } else {
        ctx.ellipse(0, 2 + smile * 0.2, 10 + open * 4, h * 0.55, 0, 0, Math.PI * 2);
        ctx.fill();
        if (open > 0.35) {
          ctx.fillStyle = "#f2b8c4";
          ctx.beginPath();
          ctx.ellipse(0, 4 + smile * 0.1, 6, h * 0.22, 0, 0, Math.PI);
          ctx.fill();
        }
      }
      ctx.restore();
    }
  }

  global.OpheliaAvatarStage = AvatarStage;
})(window);
