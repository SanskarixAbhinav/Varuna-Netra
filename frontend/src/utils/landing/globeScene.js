import { DETECTIONS, ROUTES } from '../../data/landing/geo';

const SEVERITY_COLORS = {
  probable: { fill: '#c25a49', glow: 'rgba(194, 90, 73, 0.4)' },
  possible: { fill: '#b8862a', glow: 'rgba(184, 134, 42, 0.4)' },
  indeterminate: { fill: '#1f7f93', glow: 'rgba(31, 127, 147, 0.4)' }
};

// Generate Fibonacci surface points for the sphere
function generateFibonacciPoints(count = 1400) {
  const points = [];
  const phi = Math.PI * (3 - Math.sqrt(5)); // golden angle
  for (let i = 0; i < count; i++) {
    const y = 1 - (i / (count - 1)) * 2;
    const radius = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = phi * i;
    points.push({
      x: Math.cos(theta) * radius,
      y,
      z: Math.sin(theta) * radius
    });
  }
  return points;
}

// Convert lat/lon to 3D unit sphere coordinates
function latLonToVector(lat, lon) {
  const phi = (90 - lat) * (Math.PI / 180);
  const theta = (lon + 180) * (Math.PI / 180);
  return {
    x: -Math.sin(phi) * Math.cos(theta),
    y: Math.cos(phi),
    z: Math.sin(phi) * Math.sin(theta)
  };
}

export function createGlobeScene(container, options = {}) {
  const canvas = document.createElement('canvas');
  canvas.style.width = '100%';
  canvas.style.height = '100%';
  canvas.style.display = 'block';
  container.appendChild(canvas);

  const ctx = canvas.getContext('2d');
  let width = 0;
  let height = 0;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);

  const resize = () => {
    width = container.clientWidth || 300;
    height = container.clientHeight || 300;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };
  resize();

  const observer = new ResizeObserver(resize);
  observer.observe(container);

  const surfacePoints = generateFibonacciPoints(1400);

  // Pre-calculate detection vectors
  const detectionPoints = DETECTIONS.map((d, i) => ({
    ...d,
    vec: latLonToVector(d.lat, d.lon),
    offset: i * 0.4
  }));

  // Pre-calculate route sample points with parabolic height
  const routeCurves = ROUTES.map(([from, to]) => {
    const vFrom = latLonToVector(from.lat, from.lon);
    const vTo = latLonToVector(to.lat, to.lon);
    const numSteps = 40;
    const pts = [];
    for (let i = 0; i <= numSteps; i++) {
      const t = i / numSteps;
      const x = vFrom.x + (vTo.x - vFrom.x) * t;
      const y = vFrom.y + (vTo.y - vFrom.y) * t;
      const z = vFrom.z + (vTo.z - vFrom.z) * t;
      const len = Math.sqrt(x * x + y * y + z * z) || 1;
      const arcLift = 1.0 + Math.sin(t * Math.PI) * 0.18;
      pts.push({
        x: (x / len) * arcLift,
        y: (y / len) * arcLift,
        z: (z / len) * arcLift
      });
    }
    return pts;
  });

  let rotY = -1.1;
  const tiltX = 0.22;
  const pointer = { x: 0, y: 0 };
  const eased = { x: 0, y: 0 };

  let animationFrameId = null;
  let lastTime = performance.now();
  let elapsed = 0;

  const spinSpeed = options.spinSpeed || 0.18;
  const reducedMotion = Boolean(options.reducedMotion);

  function project(p, radius, cx, cy, cosY, sinY, cosTilt, sinTilt) {
    // 1. Rotate around Y (longitude rotation)
    const x1 = p.x * cosY + p.z * sinY;
    const y1 = p.y;
    const z1 = -p.x * sinY + p.z * cosY;

    // 2. Rotate around X (tilt / pitch)
    const x2 = x1;
    const y2 = y1 * cosTilt - z1 * sinTilt;
    const z2 = y1 * sinTilt + z1 * cosTilt;

    return {
      x: cx + x2 * radius,
      y: cy - y2 * radius,
      z: z2,
      visible: z2 > 0
    };
  }

  function render(now) {
    const delta = Math.min((now - lastTime) / 1000, 0.1);
    lastTime = now;
    elapsed += delta;

    if (!reducedMotion) {
      rotY += delta * spinSpeed;
    }

    eased.x += (pointer.x - eased.x) * 0.05;
    eased.y += (pointer.y - eased.y) * 0.05;

    const curTiltX = tiltX + eased.y * 0.2;
    const curRotY = rotY + eased.x * 0.35;

    ctx.clearRect(0, 0, width, height);

    const cx = width / 2;
    const cy = height / 2;
    const radius = Math.min(width, height) * 0.38;

    const cosY = Math.cos(curRotY);
    const sinY = Math.sin(curRotY);
    const cosTilt = Math.cos(curTiltX);
    const sinTilt = Math.sin(curTiltX);

    // 1. Atmosphere halo
    const haloGrad = ctx.createRadialGradient(cx, cy, radius * 0.8, cx, cy, radius * 1.25);
    haloGrad.addColorStop(0, 'rgba(143, 196, 209, 0.12)');
    haloGrad.addColorStop(0.6, 'rgba(47, 147, 168, 0.06)');
    haloGrad.addColorStop(1, 'rgba(0, 0, 0, 0)');
    ctx.fillStyle = haloGrad;
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 1.25, 0, Math.PI * 2);
    ctx.fill();

    // 2. Globe core sphere
    const sphereGrad = ctx.createRadialGradient(cx - radius * 0.28, cy - radius * 0.32, radius * 0.1, cx, cy, radius);
    sphereGrad.addColorStop(0, '#f2f6f8');
    sphereGrad.addColorStop(0.6, '#e0e8eb');
    sphereGrad.addColorStop(1, '#c4d4d9');
    ctx.fillStyle = sphereGrad;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.fill();

    // Subtle edge shadow
    const edgeGrad = ctx.createRadialGradient(cx, cy, radius * 0.85, cx, cy, radius);
    edgeGrad.addColorStop(0, 'rgba(31, 127, 147, 0)');
    edgeGrad.addColorStop(1, 'rgba(31, 127, 147, 0.25)');
    ctx.fillStyle = edgeGrad;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.fill();

    // Clip to globe circle for surface features
    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.clip();

    // 3. Graticule (Latitude & Longitude grid lines)
    ctx.strokeStyle = 'rgba(31, 127, 147, 0.16)';
    ctx.lineWidth = 1;

    // Latitude rings
    for (let lat = -60; lat <= 60; lat += 30) {
      ctx.beginPath();
      let first = true;
      for (let lon = -180; lon <= 180; lon += 6) {
        const v = latLonToVector(lat, lon);
        const p = project(v, radius, cx, cy, cosY, sinY, cosTilt, sinTilt);
        if (p.visible) {
          if (first) { ctx.moveTo(p.x, p.y); first = false; }
          else ctx.lineTo(p.x, p.y);
        } else {
          first = true;
        }
      }
      ctx.stroke();
    }

    // Longitude meridians
    for (let lon = -180; lon < 180; lon += 30) {
      ctx.beginPath();
      let first = true;
      for (let lat = -85; lat <= 85; lat += 5) {
        const v = latLonToVector(lat, lon);
        const p = project(v, radius, cx, cy, cosY, sinY, cosTilt, sinTilt);
        if (p.visible) {
          if (first) { ctx.moveTo(p.x, p.y); first = false; }
          else ctx.lineTo(p.x, p.y);
        } else {
          first = true;
        }
      }
      ctx.stroke();
    }

    // 4. Surface Fibonacci Points (dot matrix globe)
    for (let i = 0; i < surfacePoints.length; i++) {
      const p = project(surfacePoints[i], radius * 0.995, cx, cy, cosY, sinY, cosTilt, sinTilt);
      if (p.visible) {
        const alpha = Math.max(0.08, p.z * 0.35);
        ctx.fillStyle = `rgba(31, 127, 147, ${alpha})`;
        ctx.beginPath();
        ctx.arc(p.x, p.y, 1.1, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // 5. Routes (Arcs) & moving tracers
    routeCurves.forEach((pts, rIdx) => {
      ctx.beginPath();
      let hasVisible = false;
      let first = true;
      for (let i = 0; i < pts.length; i++) {
        const p = project(pts[i], radius, cx, cy, cosY, sinY, cosTilt, sinTilt);
        if (p.visible) {
          if (first) { ctx.moveTo(p.x, p.y); first = false; }
          else ctx.lineTo(p.x, p.y);
          hasVisible = true;
        } else {
          first = true;
        }
      }
      if (hasVisible) {
        ctx.strokeStyle = 'rgba(31, 127, 147, 0.28)';
        ctx.lineWidth = 1.4;
        ctx.stroke();
      }

      // Moving tracer packet
      if (!reducedMotion && pts.length > 0) {
        const tracerT = ((elapsed * (0.35 + (rIdx % 3) * 0.12) + rIdx * 0.25) % 1);
        const idx = Math.min(Math.floor(tracerT * (pts.length - 1)), pts.length - 1);
        const tp = project(pts[idx], radius, cx, cy, cosY, sinY, cosTilt, sinTilt);
        if (tp.visible) {
          ctx.fillStyle = 'rgba(20, 96, 111, 0.9)';
          ctx.beginPath();
          ctx.arc(tp.x, tp.y, 2.4, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    });

    // 6. Detection Markers (dots & pulsating radar rings)
    detectionPoints.forEach((d) => {
      const p = project(d.vec, radius * 1.01, cx, cy, cosY, sinY, cosTilt, sinTilt);
      if (p.visible) {
        const colors = SEVERITY_COLORS[d.severity] || SEVERITY_COLORS.indeterminate;
        const pulse = reducedMotion ? 0.35 : ((elapsed * 0.65 + d.offset) % 1);
        const ringRadius = 3 + pulse * 14;
        const ringAlpha = Math.max(0, (1 - pulse) * 0.75);

        // Expanding ring
        ctx.strokeStyle = colors.fill;
        ctx.globalAlpha = ringAlpha;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.arc(p.x, p.y, ringRadius, 0, Math.PI * 2);
        ctx.stroke();

        // Core marker dot
        ctx.globalAlpha = 1;
        ctx.fillStyle = colors.fill;
        ctx.beginPath();
        ctx.arc(p.x, p.y, 3.2, 0, Math.PI * 2);
        ctx.fill();
      }
    });

    ctx.restore();

    animationFrameId = requestAnimationFrame(render);
  }

  animationFrameId = requestAnimationFrame(render);

  return {
    setPointer(x, y) {
      pointer.x = x;
      pointer.y = y;
    },
    dispose() {
      if (animationFrameId) {
        cancelAnimationFrame(animationFrameId);
      }
      observer.disconnect();
      if (canvas.parentNode === container) {
        container.removeChild(canvas);
      }
    }
  };
}