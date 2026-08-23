/**
 * The colour chain, on the GPU, for a preview that follows the sliders.
 *
 * This is a second implementation of what `services/grading.py` asks ffmpeg to do, and
 * that is a deliberate trade: the final file always comes from ffmpeg, this only has to
 * be close enough to grade by. Every formula below was found by measurement, not from
 * the documentation: a test chart through each filter, values read back, candidates
 * compared. What came out, with the error against ffmpeg on a 256-step ramp:
 *
 *   exposure           out = in * 2^EV, on the encoded values, no linearisation  0.5/255
 *   eq contrast        (v - 0.5) * C + 0.5 on normalised luma                    1.5/255
 *   eq saturation      (u - 128) * S + 128 on the chroma                         1.6/255
 *   colortemperature   one gain per RGB channel, on the encoded values           1.7/255
 *   curves             natural cubic spline through the four points              1.1/255
 *   lutyuv             the linear luma stretch the server resolves for us        exact
 *
 * And the one detail that decides whether the whole thing works: **ffmpeg goes back
 * through 8 bits between filters, so it clips after every stage**. Keeping everything in
 * float instead left the sky 18 levels out; clamping at each stage brought the whole
 * frame to 2 levels of average error, 39 dB PSNR, indistinguishable side by side.
 *
 * The decisions stay on the server. Auto-levels arrives as a resolved [low, gain] pair,
 * because which side is already clipped and whether the stretch is worth doing is
 * reasoning, not arithmetic, and reasoning must not exist twice.
 */

/** Legal range as fractions of full scale, the same numbers the server writes. */
const BLACK_N = 64 / 1023;
const WHITE_N = 940 / 1023;

export interface GradePlan {
  /** [low, gain] from the server, or null when auto-levels does nothing here. */
  levels: [number, number] | null;
  exposure: number;
  shadows: number;
  highlights: number;
  contrast: number;
  saturation: number;
  temperature: number;
}

/**
 * The four-point master curve, as `_curve` in grading.py builds it.
 *
 * Two control points on purpose: enough to open the shadows or pull the highlights
 * back, not enough to build an S-curve that wrecks skies by accident.
 */
export function curvePoints(shadows: number, highlights: number): Array<[number, number]> | null {
  if (Math.abs(shadows) < 1e-3 && Math.abs(highlights) < 1e-3) return null;
  const low = Math.max(0.02, Math.min(0.98, 0.25 + shadows * 0.15));
  const high = Math.max(0.02, Math.min(0.98, 0.75 + highlights * 0.15));
  return [
    [0, 0],
    [0.25, low],
    [0.75, high],
    [1, 1],
  ];
}

/** A natural cubic spline through the points, sampled into a lookup table. */
export function curveTable(points: Array<[number, number]>, size = 1024): Float32Array {
  const n = points.length;
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const h = xs.slice(1).map((x, i) => x - xs[i]);

  // Solve for the second derivatives, tridiagonal, natural ends (zero curvature).
  const a = Array.from({ length: n }, () => new Array(n).fill(0));
  const rhs = new Array(n).fill(0);
  a[0][0] = 1;
  a[n - 1][n - 1] = 1;
  for (let i = 1; i < n - 1; i += 1) {
    a[i][i - 1] = h[i - 1];
    a[i][i] = 2 * (h[i - 1] + h[i]);
    a[i][i + 1] = h[i];
    rhs[i] = 3 * ((ys[i + 1] - ys[i]) / h[i] - (ys[i] - ys[i - 1]) / h[i - 1]);
  }
  // Gaussian elimination: four unknowns, no need for anything cleverer.
  for (let i = 0; i < n; i += 1) {
    const pivot = a[i][i] || 1e-9;
    for (let j = i + 1; j < n; j += 1) {
      const factor = a[j][i] / pivot;
      for (let k = i; k < n; k += 1) a[j][k] -= factor * a[i][k];
      rhs[j] -= factor * rhs[i];
    }
  }
  const c = new Array(n).fill(0);
  for (let i = n - 1; i >= 0; i -= 1) {
    let sum = rhs[i];
    for (let j = i + 1; j < n; j += 1) sum -= a[i][j] * c[j];
    c[i] = sum / (a[i][i] || 1e-9);
  }

  const table = new Float32Array(size);
  for (let k = 0; k < size; k += 1) {
    const x = k / (size - 1);
    let i = 0;
    while (i < n - 2 && xs[i + 1] < x) i += 1;
    const dx = x - xs[i];
    const b = (ys[i + 1] - ys[i]) / h[i] - (h[i] * (2 * c[i] + c[i + 1])) / 3;
    const d = (c[i + 1] - c[i]) / (3 * h[i]);
    table[k] = Math.max(0, Math.min(1, ys[i] + b * dx + c[i] * dx * dx + d * dx * dx * dx));
  }
  return table;
}

/**
 * The per-channel gains `colortemperature` applies, from the same approximation it
 * uses (Tanner Helland). Measured against ffmpeg: 1.7 levels at worst.
 */
export function kelvinGains(kelvin: number): [number, number, number] {
  // The server only adds the filter when the temperature actually moved, and at 6500 K
  // this approximation is not exactly neutral ([1, 0.996, 0.980]), so it would tint
  // every frame by 2% of blue against a chain that does nothing.
  if (Math.abs(kelvin - 6500) <= 1) return [1, 1, 1];
  const t = kelvin / 100;
  let r: number;
  let g: number;
  let b: number;
  if (t <= 66) {
    r = 255;
    g = 99.4708025861 * Math.log(t) - 161.1195681661;
  } else {
    r = 329.698727446 * (t - 60) ** -0.1332047592;
    g = 288.1221695283 * (t - 60) ** -0.0755148492;
  }
  if (t >= 66) b = 255;
  else if (t <= 19) b = 0;
  else b = 138.5177312231 * Math.log(t - 10) - 305.0447927307;

  const clip = (v: number) => Math.max(0, Math.min(255, v)) / 255;
  const rgb: [number, number, number] = [clip(r), clip(g), clip(b)];
  const top = Math.max(...rgb) || 1;
  return [rgb[0] / top, rgb[1] / top, rgb[2] / top];
}

const VERTEX = `#version 300 es
in vec2 a_pos;
out vec2 v_uv;
void main() {
  v_uv = vec2(a_pos.x * 0.5 + 0.5, 0.5 - a_pos.y * 0.5);
  gl_Position = vec4(a_pos, 0.0, 1.0);
}`;

const FRAGMENT = `#version 300 es
precision highp float;
in vec2 v_uv;
out vec4 outColor;

uniform sampler2D u_frame;
uniform sampler2D u_curve;
uniform vec2 u_levels;      // low, gain
uniform float u_hasLevels;
uniform float u_exposure;   // already 2^EV
uniform float u_hasCurve;
uniform float u_contrast;
uniform float u_saturation;
uniform vec3 u_gains;

const float KR = 0.2126;
const float KB = 0.0722;
const float KG = 1.0 - KR - KB;
const float BLACK_N = ${BLACK_N};
const float WHITE_N = ${WHITE_N};

// BT.709, limited range: luma carries the 16..235 offset, chroma is centred on zero.
// Same convention as the numpy model the parity harness checks against.
vec3 toYuv(vec3 c) {
  float y = KR * c.r + KG * c.g + KB * c.b;
  return vec3(16.0 / 255.0 + 219.0 / 255.0 * y,
              (c.b - y) / (2.0 * (1.0 - KB)),
              (c.r - y) / (2.0 * (1.0 - KR)));
}

vec3 toRgb(vec3 yuv) {
  float y = (yuv.x - 16.0 / 255.0) * 255.0 / 219.0;
  float b = y + 2.0 * (1.0 - KB) * yuv.y;
  float r = y + 2.0 * (1.0 - KR) * yuv.z;
  return vec3(r, (y - KR * r - KB * b) / KG, b);
}

void main() {
  vec3 c = texture(u_frame, v_uv).rgb;

  // Every stage lands back in range before the next one sees it, because ffmpeg
  // rounds through 8 bits between filters and the highlights depend on it.
  if (u_hasLevels > 0.5) {
    vec3 yuv = toYuv(c);
    yuv.x = clamp((yuv.x - u_levels.x) * u_levels.y + BLACK_N, BLACK_N, WHITE_N);
    c = clamp(toRgb(yuv), 0.0, 1.0);
  }
  c = clamp(c * u_exposure, 0.0, 1.0);
  if (u_hasCurve > 0.5) {
    c = clamp(vec3(texture(u_curve, vec2(c.r, 0.5)).r,
                   texture(u_curve, vec2(c.g, 0.5)).r,
                   texture(u_curve, vec2(c.b, 0.5)).r), 0.0, 1.0);
  }
  if (u_contrast != 1.0 || u_saturation != 1.0) {
    vec3 yuv = toYuv(c);
    yuv.x = (yuv.x - 0.5) * u_contrast + 0.5;
    yuv.yz *= u_saturation;
    c = clamp(toRgb(yuv), 0.0, 1.0);
  }
  outColor = vec4(clamp(c * u_gains, 0.0, 1.0), 1.0);
}`;

function compile(gl: WebGL2RenderingContext, kind: number, source: string) {
  const shader = gl.createShader(kind)!;
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    throw new Error(gl.getShaderInfoLog(shader) ?? "shader failed");
  }
  return shader;
}

export interface Renderer {
  /** Paint one frame of the source with the plan applied. */
  draw: (source: TexImageSource, plan: GradePlan) => void;
  destroy: () => void;
}

/** Set up the pipeline on a canvas. Throws when WebGL2 is not available. */
export function createRenderer(canvas: HTMLCanvasElement): Renderer {
  // preserveDrawingBuffer, because the canvas has to stay readable after it has been
  // composited: the histogram reads it back, and so does the parity harness that
  // compares this against ffmpeg. The extra copy per frame is not measurable at 1080p.
  const gl = canvas.getContext("webgl2", {
    premultipliedAlpha: false,
    preserveDrawingBuffer: true,
  });
  if (!gl) throw new Error("WebGL2 unavailable");

  const program = gl.createProgram()!;
  gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERTEX));
  gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAGMENT));
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    throw new Error(gl.getProgramInfoLog(program) ?? "link failed");
  }
  gl.useProgram(program);

  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const pos = gl.getAttribLocation(program, "a_pos");
  gl.enableVertexAttribArray(pos);
  gl.vertexAttribPointer(pos, 2, gl.FLOAT, false, 0, 0);

  const frameTex = gl.createTexture();
  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, frameTex);
  for (const axis of [gl.TEXTURE_WRAP_S, gl.TEXTURE_WRAP_T]) {
    gl.texParameteri(gl.TEXTURE_2D, axis, gl.CLAMP_TO_EDGE);
  }
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);

  const curveTex = gl.createTexture();
  gl.activeTexture(gl.TEXTURE1);
  gl.bindTexture(gl.TEXTURE_2D, curveTex);
  for (const axis of [gl.TEXTURE_WRAP_S, gl.TEXTURE_WRAP_T]) {
    gl.texParameteri(gl.TEXTURE_2D, axis, gl.CLAMP_TO_EDGE);
  }
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);

  const at = (name: string) => gl.getUniformLocation(program, name);
  gl.uniform1i(at("u_frame"), 0);
  gl.uniform1i(at("u_curve"), 1);

  let curveKey = "";

  return {
    draw(source, plan) {
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, frameTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);

      const points = curvePoints(plan.shadows, plan.highlights);
      const key = points ? points.map((p) => p.join(":")).join(",") : "";
      if (key !== curveKey) {
        curveKey = key;
        // Only rebuilt when the two sliders move, not on every frame.
        const table = points ? curveTable(points) : new Float32Array([0, 1]);
        // Bytes, not floats: sampling an R32F texture with LINEAR filtering needs an
        // extension, and an incomplete texture reads as zero, which blacked out the
        // whole frame. A byte per entry is the precision of the output anyway.
        const bytes = new Uint8Array(table.length);
        for (let i = 0; i < table.length; i += 1) bytes[i] = Math.round(table[i] * 255);
        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, curveTex);
        gl.texImage2D(
          gl.TEXTURE_2D, 0, gl.R8, bytes.length, 1, 0, gl.RED, gl.UNSIGNED_BYTE, bytes,
        );
      }

      gl.uniform1f(at("u_hasLevels"), plan.levels ? 1 : 0);
      gl.uniform2f(at("u_levels"), plan.levels?.[0] ?? 0, plan.levels?.[1] ?? 1);
      gl.uniform1f(at("u_exposure"), 2 ** plan.exposure);
      gl.uniform1f(at("u_hasCurve"), points ? 1 : 0);
      gl.uniform1f(at("u_contrast"), plan.contrast);
      gl.uniform1f(at("u_saturation"), plan.saturation);
      gl.uniform3fv(at("u_gains"), kelvinGains(plan.temperature));

      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    },
    destroy() {
      gl.deleteTexture(frameTex);
      gl.deleteTexture(curveTex);
      gl.deleteBuffer(buffer);
      gl.deleteProgram(program);
    },
  };
}
