// Fig: how a council run works, drawn as a sorting bench.
//
//   node tools/anatomy/council-run.mjs [--light]
//
// Builds docs/assets/figures/council-run.html with the iso kit of the "anatomy" figure
// toolkit (set ANATOMY_DIR to its folder; default ~/.claude/skills/anatomy). The page follows
// the reader's colour scheme; --light only changes the default before the script runs.
//
// The object: a turret (the chair) deals one token per plan step onto three rails (members
// working in parallel). Every token passes under a gate bridge (the thought bus) whose stop
// pins can drop and hold it: an overseer holds one thought each cycle, and the reader holds a
// lane by pointing at it (the human intercept). Tokens drop into a ballot box whose dial
// tallies the vote (approve wins ties), and an approved run stacks a sheet in the tray.
import { writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ANATOMY = resolve(process.env.ANATOMY_DIR ?? join(homedir(), ".claude/skills/anatomy"));
const k = await import(pathToFileURL(join(ANATOMY, "kit/iso-kit.mjs")).href);
const { kitScript } = await import(pathToFileURL(join(ANATOMY, "scripts/inline-kit.mjs")).href);

const HERE = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(HERE, "../../docs/assets/figures/council-run.html");
const theme = process.argv.includes("--light") ? "light" : "dark";

const WIDTH = 640;
const HEIGHT = 400;

const PLATE = { x: -170, y: -66, w: 340, d: 132, r: 8 };
const PLATE_H = 12;
const FOOT_H = 5;

const LANES = [-30, 0, 30];
const RAIL_FROM = -104;
const RAIL_TO = 84;
const RAIL_H = 3;
const TOKEN_R = 4.6;
const TOKEN_H = 4;
const GATE = -6;
const HOLD_AT = GATE - 9;
const PIN_UP = 16;
const PIN_DOWN = RAIL_H + 0.4;
const BAR_Z = 32;

const MANIFOLD = { x: -160, y: -44, w: 52, d: 88, r: 4 };
const MANIFOLD_H = 6;
const TURRET = [-134, 0];
const TURRET_R = 19;

const BOX = { x: 86, y: -44, w: 44, d: 88, r: 3 };
const BOX_H = 26;
const DIAL = [108, 0];
const DIAL_R = 14;
const DIAL_Z = BOX_H;
const DIAL_H = 5;
const DIAL_TOP = DIAL_Z + DIAL_H;

const TRAY = { x: 138, y: -22, w: 26, d: 44, r: 2 };

const P = k.fitProjection(
  [...k.boxCorners(PLATE, -PLATE_H - FOOT_H, 0), [GATE, 0, BAR_Z + 10], [TURRET[0], 0, 44]],
  WIDTH,
  HEIGHT,
  { pad: 26, azimuth: 40 },
);

const at = (point) => k.iso(point, P);
const S = (paths, style) => k.solidSvg(paths, style);
const L = (d, style) => k.lineSvg(d, style);
const D = (points, style) => k.dotsSvg(points, style);
const byDepth = (points) => [...points].sort((a, b) => k.depthOf([a[0], a[1], 0], P) - k.depthOf([b[0], b[1], 0], P));

function plate() {
  const out = [`<path class="iso-halo" d="${k.haloOf(PLATE, -PLATE_H - FOOT_H, PLATE_H, P)}"/>`];
  for (const [x, y] of byDepth(k.corners(PLATE, 24))) {
    out.push(S(k.cylinder(x, y, 7, -PLATE_H - FOOT_H, 1.6, P, 28), { tone: "lo" }));
    out.push(S(k.cylinder(x, y, 5.4, -PLATE_H - FOOT_H + 1.6, FOOT_H - 1.6, P, 28), { tone: "mid" }));
  }
  out.push(S(k.slabOf(PLATE, -PLATE_H, PLATE_H, P, 8, 1.6), { tone: "mid" }));
  out.push(L(k.planOutline(k.insetPlan(PLATE, 5), 0, P), { tone: "lo" }));
  const screws = k.corners(PLATE, 10);
  out.push(L(screws.map(([x, y]) => k.ring(x, y, 2.2, 0, P, 16)).join(""), { tone: "lo" }));
  out.push(D(screws.map(([x, y]) => at([x, y, 0])), { size: 0.5 }));
  // a ruler strip along the front edge: run time
  const strip = { x: -100, y: PLATE.y + PLATE.d - 14, w: 186, d: 7, r: 1 };
  out.push(S(k.slabOf(strip, 0, 0.6, P, 3), { tone: "lo", crease: "none" }));
  const ticks = k.topTicks(strip.x + 4, strip.x + strip.w - 4, 4, 5, strip.y + 0.8, 0.6, [2, 3.6], P);
  out.push(L(ticks.minor, { tone: "lo" }), L(ticks.major, { tone: "mid" }));
  out.push(D([at([strip.x + 2, strip.y + 3.5, 0.6]), at([strip.x + strip.w - 2, strip.y + 3.5, 0.6])], { size: 0.45 }));
  // a label plate with three rule lines, front left
  const label = { x: -160, y: PLATE.y + PLATE.d - 18, w: 40, d: 11, r: 1 };
  out.push(S(k.slabOf(label, 0, 0.8, P, 3), { tone: "lo", crease: "none" }));
  for (const [i, y] of [3.4, 5.6, 7.8].entries())
    out.push(`<path class="iso-line" data-tone="${i === 0 ? "mid" : "faint"}" data-free="both" d="${k.lineOnTop([label.x + 4, label.y + y], [label.x + (i === 0 ? 30 : 22), label.y + y], 0.8, P)}"/>`);
  out.push(D([at([label.x + 1.8, label.y + 5.5, 0.8]), at([label.x + label.w - 1.8, label.y + 5.5, 0.8])], { size: 0.4 }));
  return out.join("");
}

function turret() {
  const out = [];
  out.push(S(k.slabOf(MANIFOLD, 0, MANIFOLD_H, P, 4, 0.6), { tone: "mid" }));
  out.push(D(k.corners(MANIFOLD, 4).map(([x, y]) => at([x, y, MANIFOLD_H])), { size: 0.45 }));
  // three chutes from the turret to the rails
  for (const y of LANES) {
    const chute = { x: TURRET[0] + 8, y: y - 3, w: RAIL_FROM - TURRET[0] - 8 + 2, d: 6, r: 1 };
    out.push(S(k.slabOf(chute, MANIFOLD_H, 2, P, 2, 0.3), { tone: "mid" }));
  }
  const [x, y] = TURRET;
  out.push(S(k.cylinder(x, y, TURRET_R + 2, MANIFOLD_H, 3, P, 56, 0.5), { tone: "mid" }));
  out.push(S(k.cylinder(x, y, TURRET_R, MANIFOLD_H + 3, 18, P, 56, 0.6), { tone: "mid" }));
  out.push(L([MANIFOLD_H + 9, MANIFOLD_H + 15].map((z) => k.sideArc(x, y, TURRET_R, z, P, 40)).join(""), { tone: "faint" }));
  // the hopper: where the topic goes in
  out.push(S(k.cylinder(x, y, TURRET_R - 3, MANIFOLD_H + 21, 6, P, 48, 0.5), { tone: "mid" }));
  out.push(L(k.knurl(x, y, TURRET_R - 3, MANIFOLD_H + 21, MANIFOLD_H + 27, 64, P, 0.3), { tone: "lo" }));
  out.push(L(k.ring(x, y, TURRET_R - 6, MANIFOLD_H + 27, P, 40), { tone: "lo" }));
  out.push(L(k.ring(x, y, 4, MANIFOLD_H + 27, P, 20), { tone: "mid" }));
  const ticks = k.radialTicks(x, y, TURRET_R - 7, 12, 3, MANIFOLD_H + 27, [1.2, 2.4], P);
  out.push(L(ticks.minor, { tone: "lo" }), L(ticks.major, { tone: "mid" }));
  return out.join("");
}

const pointerAt = (turn) => {
  const a = turn * Math.PI * 2;
  return k.lineOnTop([TURRET[0], TURRET[1]], [TURRET[0] + Math.cos(a) * 9, TURRET[1] + Math.sin(a) * 9], MANIFOLD_H + 27.2, P);
};

function rail(y) {
  const out = [];
  const r = { x: RAIL_FROM, y: y - 5, w: RAIL_TO - RAIL_FROM, d: 10, r: 1.2 };
  out.push(S(k.slabOf(r, 0, RAIL_H, P, 3, 0.4), { tone: "mid" }));
  out.push(L(k.lineOnTop([RAIL_FROM, y - 2.2], [RAIL_TO, y - 2.2], RAIL_H, P) + k.lineOnTop([RAIL_FROM, y + 2.2], [RAIL_TO, y + 2.2], RAIL_H, P), { tone: "faint" }));
  out.push(D([RAIL_FROM + 5, (RAIL_FROM + RAIL_TO) / 2, RAIL_TO - 5].map((x) => at([x, y + 3.8, RAIL_H])), { size: 0.4 }));
  return out.join("");
}

function token() {
  return [
    S(k.cylinder(0, 0, TOKEN_R, RAIL_H, TOKEN_H, P, 32, 0.4), { tone: "hi", lit: true }),
    L(k.ring(0, 0, TOKEN_R - 1.6, RAIL_H + TOKEN_H, P, 24), { tone: "lo" }),
  ].join("");
}

function pinAt(y, low) {
  return S(k.cylinder(GATE, y, 1.5, low, BAR_Z - low, P, 16), { tone: "hi", crease: "none" });
}

function post(y) {
  const out = [];
  out.push(S(k.cylinder(GATE, y, 5, 0, 2, P, 28), { tone: "mid" }));
  out.push(S(k.cylinder(GATE, y, 3, 2, BAR_Z - 2, P, 24), { tone: "mid", crease: "none" }));
  out.push(L([10, 20].map((z) => k.sideArc(GATE, y, 3, z, P, 14)).join(""), { tone: "faint" }));
  return out.join("");
}

function bridge() {
  const out = [];
  const bar = { x: GATE - 6, y: -54, w: 12, d: 108, r: 2 };
  out.push(S(k.slabOf(bar, BAR_Z, 6, P, 4, 0.6), { tone: "mid" }));
  out.push(L(k.lineOnTop([GATE - 3.4, bar.y + 4], [GATE - 3.4, bar.y + bar.d - 4], BAR_Z + 6, P), { tone: "faint" }));
  for (const y of LANES) {
    out.push(S(k.cylinder(GATE, y, 3.6, BAR_Z + 6, 4, P, 28, 0.4), { tone: "mid" }));
    out.push(L(k.ring(GATE, y, 1.6, BAR_Z + 10, P, 14), { tone: "lo" }));
  }
  out.push(D([bar.y + 4, bar.y + bar.d - 4].map((y) => at([GATE + 3, y, BAR_Z + 6])), { size: 0.45 }));
  return out.join("");
}

function override(lit) {
  // the human's knob at the front end of the bridge
  const out = [];
  const y = 58;
  out.push(S(k.cylinder(GATE, y, 1.4, BAR_Z + 1, 3, P, 14), { tone: "mid", crease: "none" }));
  out.push(`<g data-part="knob" data-lit="${lit ? 1 : 0}">${S(k.cylinder(GATE, y + 3.5, 4.4, BAR_Z - 1, 8, P, 32, 0.4), { tone: "hi", lit })}</g>`);
  out.push(L(k.knurl(GATE, y + 3.5, 4.4, BAR_Z - 1, BAR_Z + 7, 30, P, 0.3), { tone: "lo" }));
  return out.join("");
}

function ballotBox() {
  const out = [];
  out.push(S(k.slabOf(BOX, 0, BOX_H, P, 5, 0.8), { tone: "mid" }));
  out.push(L(k.sideSeam(BOX, BOX_H - 5, P), { tone: "faint" }));
  // three entry slots facing the rails
  for (const y of LANES) out.push(L(k.planOutline({ x: BOX.x - 0.01, y: y - 6, w: 0.02, d: 12, r: 0 }, RAIL_H + 1, P), { tone: "lo" }));
  out.push(D(k.corners(BOX, 4).map(([x, y]) => at([x, y, BOX_H])), { size: 0.45 }));
  return out.join("");
}

function dial() {
  const out = [];
  const [x, y] = DIAL;
  out.push(S(k.cylinder(x, y, DIAL_R, DIAL_Z, DIAL_H, P, 56, 0.6), { tone: "hi", crease: "lo" }));
  out.push(L(k.knurl(x, y, DIAL_R, DIAL_TOP - 2.4, DIAL_TOP, 60, P, 0.25), { tone: "lo" }));
  out.push(L(k.ring(x, y, DIAL_R - 1.8, DIAL_TOP, P, 48), { tone: "lo" }));
  const ticks = k.radialTicks(x, y, DIAL_R - 2.4, 10, 5, DIAL_TOP, [1.1, 2.4], P);
  out.push(L(ticks.minor, { tone: "lo" }), L(ticks.major, { tone: "mid" }));
  out.push(L(k.ring(x, y, 1.3, DIAL_TOP, P, 12), { tone: "mid" }));
  return out.join("");
}

const needleAt = (votes) => {
  // ten divisions round the dial: one per ballot
  const a = (votes / 10) * Math.PI * 2;
  const [x, y] = DIAL;
  return k.lineOnTop([x - Math.sin(a) * 2.6, y + Math.cos(a) * 2.6], [x + Math.sin(a) * (DIAL_R - 3.2), y - Math.cos(a) * (DIAL_R - 3.2)], DIAL_TOP + 0.2, P);
};

function tray() {
  const out = [];
  out.push(S(k.slabOf(TRAY, 0, 1.6, P, 2, 0.3), { tone: "mid" }));
  out.push(S(k.slabOf({ x: TRAY.x + TRAY.w - 2, y: TRAY.y, w: 2, d: TRAY.d, r: 0.6 }, 1.6, 5, P, 2), { tone: "mid", crease: "none" }));
  return out.join("");
}

function sheet(i) {
  const plan = { x: TRAY.x + 2, y: TRAY.y + 3, w: TRAY.w - 5, d: TRAY.d - 6, r: 0.6 };
  const z = 1.6 + i * 1.2;
  return `<g data-part="sheet" data-i="${i}" style="opacity:0">${S(k.slabOf(plan, z, 1, P, 2), { tone: "hi", crease: "none" })}${L(k.lineOnTop([plan.x + 4, plan.y + 5], [plan.x + 4, plan.y + plan.d - 8], z + 1, P), { tone: "lo" })}</g>`;
}

// Painter's order: the plate, then the turret, the back post, the lanes back to front (rail,
// token, pin), the bridge and the front post, then the ballot box, dial, needle and tray.
const lanes = LANES.map((y, i) => [
  rail(y),
  `<g data-part="token" data-lane="${i}" transform="${k.translateAlong(P, [RAIL_FROM + 8, y, 0])}">${token()}</g>`,
  `<path data-part="pin" data-lane="${i}" class="iso-solid" d=""/>`,
].join(""));

const svg = k.figureSvg({
  width: WIDTH,
  height: HEIGHT,
  label:
    "A sorting bench. A turret on the left deals tokens onto three rails; each rail is a council member working in parallel. Halfway along, a gate bridge carries a stop pin over each rail that can drop and hold a token. The rails end in a ballot box whose dial counts the votes, and a tray beside it collects the finished sheets.",
  body: [
    plate(),
    turret(),
    `<path data-part="turret-pointer" class="iso-line" data-tone="lit" data-free="start" d="${pointerAt(0)}" style="stroke-width:1.1"/>`,
    post(-54),
    ...lanes,
    bridge(),
    post(54),
    override(false),
    ballotBox(),
    dial(),
    `<path data-part="needle" class="iso-line" data-tone="lit" data-free="start" d="${needleAt(0)}" style="stroke-width:1.1"/>`,
    tray(),
    sheet(0),
    sheet(1),
    sheet(2),
  ],
});

// Pins are drawn live: their length is the only thing that changes, so the static pieces of a
// solid are re-rendered by the same kit call in the page.
const live = `
const P = ${JSON.stringify(P)};
const LANES = ${JSON.stringify(LANES)};
const RAIL_FROM = ${RAIL_FROM}, RAIL_TO = ${RAIL_TO}, GATE = ${GATE}, HOLD_AT = ${HOLD_AT}, BAR_Z = ${BAR_Z};
const PIN_UP = ${PIN_UP}, PIN_DOWN = ${PIN_DOWN}, TURRET = ${JSON.stringify(TURRET)}, MANIFOLD_H = ${MANIFOLD_H};
const DIAL = ${JSON.stringify(DIAL)}, DIAL_R = ${DIAL_R}, DIAL_TOP = ${DIAL_TOP};
const NAMES = ["researcher", "analyst", "writer"];
const stage = document.querySelector(".iso-stage");
const svg = stage.querySelector("svg");
const tokens = [...svg.querySelectorAll('[data-part="token"]')];
const pins = [...svg.querySelectorAll('[data-part="pin"]')];
const sheets = [...svg.querySelectorAll('[data-part="sheet"]')];
const needle = svg.querySelector('[data-part="needle"]');
const pointer = svg.querySelector('[data-part="turret-pointer"]');
const knob = svg.querySelector('[data-part="knob"] .iso-solid');
const readout = document.querySelector("[data-readout]");
const still = matchMedia("(prefers-reduced-motion: reduce)");
if (matchMedia("(prefers-color-scheme: light)").matches) document.body.dataset.theme = "light";
if (matchMedia("(prefers-color-scheme: dark)").matches) document.body.dataset.theme = "dark";
const pinPath = ${pinAt.toString().replace(/k\./g, "")};
const S = (paths, style) => solidSvg(paths, style);
const pointerAt = ${pointerAt.toString().replace(/k\./g, "")};
const needleAt = ${needleAt.toString().replace(/k\./g, "")};
const TOKEN_R = ${TOKEN_R};

// One cycle of the model, in seconds: plan, work (with one overseer hold), vote, deliver.
const CYCLE = 11, PLAN = 1.4, SPEED = [46, 52, 40], OVERSEER = { lane: 1, from: 3.0, to: 4.6 };
const VOTES = { approve: 4, revise: 1 };
let human = -1, t = 0, last = 0, frame = 0, visible = false, delivered = 0;
const lane = LANES.map(() => ({ x: RAIL_FROM + 8, pin: PIN_UP, done: false, held: 0 }));

function phase() {
  if (t < PLAN) return "plan";
  if (lane.some((l) => !l.done)) return "work";
  if (t < CYCLE - 2.4) return "vote";
  return "deliver";
}

function step(dt) {
  t += dt;
  if (t >= CYCLE && human < 0) {
    t = 0; delivered = (delivered + 1) % 3;
    lane.forEach((l) => { l.x = RAIL_FROM + 8; l.done = false; l.held = 0; });
  }
  lane.forEach((l, i) => {
    const overseer = i === OVERSEER.lane && t > OVERSEER.from && t < OVERSEER.to;
    const holding = human === i || overseer;
    l.pin += ((holding ? PIN_DOWN : PIN_UP) - l.pin) * (1 - Math.exp(-dt / 0.12));
    if (t < PLAN || l.done) return;
    let next = l.x + SPEED[i] * dt;
    if (holding && l.x <= HOLD_AT + 0.01) next = Math.min(next, HOLD_AT);
    l.x = Math.min(next, RAIL_TO + 2);
    if (l.x >= RAIL_TO + 2) l.done = true;
  });
}

function draw() {
  lane.forEach((l, i) => {
    tokens[i].setAttribute("transform", translateAlong(P, [l.x, LANES[i], 0]));
    tokens[i].style.opacity = l.done ? "0" : "1";
  });
  const p = phase();
  const arrived = lane.filter((l) => l.done).length;
  const votes = p === "vote" || p === "deliver" ? Math.min(5, Math.max(0, (t - (CYCLE - 2.4 - 1.6)) / 0.32)) : 0;
  needle.setAttribute("d", needleAt(votes));
  pointer.setAttribute("d", pointerAt(Math.min(t, PLAN) / PLAN * 0.75));
  const shown = Math.min(3, delivered + (p === "deliver" ? 1 : 0));
  sheets.forEach((s, i) => { s.style.opacity = i < shown ? "1" : "0"; });
  let text;
  if (human >= 0) text = "you hold the " + NAMES[human] + ": correct it, then let go";
  else if (p === "plan") text = "chair plans 3 steps";
  else if (p === "work" && t > OVERSEER.from && t < OVERSEER.to) text = "monitor holds the analyst's thought";
  else if (p === "work") text = (3 - arrived) + " of 3 members working";
  else if (p === "vote") text = "vote: " + Math.min(4, Math.floor(votes)) + " approve, " + (votes >= 5 ? 1 : 0) + " revise";
  else text = "approved 4 to 1: deliverable written";
  if (readout.textContent !== text) readout.textContent = text;
  if (knob) { if (human >= 0) knob.setAttribute("data-lit", ""); else knob.removeAttribute("data-lit"); }
}

const drawn = lane.map(() => ({ pin: NaN, lit: null }));
function paintPins() {
  lane.forEach((l, i) => {
    const lit = human === i || (i === OVERSEER.lane && t > OVERSEER.from && t < OVERSEER.to);
    if (Math.abs(drawn[i].pin - l.pin) < 0.02 && drawn[i].lit === lit) return;
    drawn[i] = { pin: l.pin, lit };
    const wrap = document.createElementNS("http://www.w3.org/2000/svg", "g");
    wrap.innerHTML = pinPath(LANES[i], l.pin);
    const g = wrap.firstElementChild;
    g.setAttribute("data-part", "pin");
    if (lit) g.setAttribute("data-lit", "");
    pins[i].replaceWith(g);
    pins[i] = g;
  });
}

function tick(now) {
  const dt = last ? Math.min((now - last) / 1000, 1 / 30) : 1 / 60;
  last = now;
  step(still.matches ? 0 : dt);
  draw();
  paintPins();
  frame = visible && !still.matches ? requestAnimationFrame(tick) : 0;
}
const run = () => { if (!frame && visible) { last = 0; frame = requestAnimationFrame(tick); } };
new IntersectionObserver((e) => { visible = e[e.length - 1].isIntersecting; if (visible) run(); }, { rootMargin: "120px 0px" }).observe(stage);

// Pointing at a lane holds it: find the lane whose rail is nearest the pointer on the floor.
function laneAt(event) {
  const box = svg.getBoundingClientRect();
  const vb = svg.viewBox.baseVal;
  const sx = (event.clientX - box.left) / box.width * vb.width + vb.x;
  const sy = (event.clientY - box.top) / box.height * vb.height + vb.y;
  let best = -1, dist = 18;
  LANES.forEach((y, i) => {
    for (let x = RAIL_FROM; x <= GATE; x += 6) {
      const [px, py] = iso([x, y, 0], P);
      const d = Math.hypot(px - sx, py - sy);
      if (d < dist) { dist = d; best = i; }
    }
  });
  return best;
}
stage.addEventListener("pointermove", (e) => { human = laneAt(e); run(); });
stage.addEventListener("pointerleave", () => { human = -1; run(); });
stage.addEventListener("keydown", (e) => {
  if (["1", "2", "3"].includes(e.key)) { human = Number(e.key) - 1; e.preventDefault(); }
  if (e.key === "Escape" || e.key === "0") { human = -1; e.preventDefault(); }
  run();
});
stage.addEventListener("keyup", (e) => { if (["1", "2", "3"].includes(e.key)) { human = -1; run(); } });
if (still.matches) { t = CYCLE - 1; lane.forEach((l) => { l.done = true; l.x = RAIL_TO + 2; }); }
draw();
paintPins();
`;

const body = k.plateHtml({
  fig: "Fig 2",
  title: "A council run",
  hint: "Point at a rail to hold that member",
  readout: "chair plans 3 steps",
  keys: [
    { mark: "lit", label: "Tokens: thoughts on their way to the vote; a lit pin is a hold" },
    { mark: "raised", label: "Gate bridge: the thought bus every thought passes" },
  ],
  caption:
    "The chair's turret deals one token per step onto three rails, one per member, and they run in parallel. Each token passes under the gate: an overseer can drop a pin and hold a thought until it is corrected, and so can you (point at a rail, or hold the keys 1 to 3). Tokens drop into the ballot box, the dial counts the votes, approve wins ties, and an approved deliverable lands in the tray.",
  body: `<div class="iso-stage" tabindex="0" role="img" aria-label="A council run as a sorting bench: keys 1 to 3 hold a member, Escape releases">${svg}</div>`,
});

const page = k
  .pageHtml({ title: "A council run", theme, body, script: kitScript() + live })
  .replace(".iso-page{width:min(650px,100% - 32px);margin:64px auto 96px}", ".iso-page{width:min(650px,100% - 24px);margin:12px auto 16px}");
writeFileSync(OUT, page);
console.log("wrote", OUT);
