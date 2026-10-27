/**
 * Council: the Argus mark built as a room. Eight chairs ring a round table,
 * the iris of the mark; the almond of the eye lies on the floor around them,
 * and the pupil is a dot on the table top. The chair under the pointer rises
 * and slides back as its member stands to speak, its neighbours stir after it,
 * staggered by distance, and the pupil turns to look at the speaker.
 * The read-out names the speaker's role. The slider is how far the speaker rises.
 *
 * Pattern: discrete items on a ring. Tweens, a stagger by ring distance, and a
 * hit test on the ground plane by angle, so a rising chair never moves the target.
 */
const {
  Cam, circ, clamp, disposer, facing, fit, mk, open, place, pointer, poly, prism, proj,
  put, rad, register, rrect, solid, tdone, tset, tval, tween, unproj,
} = HL;

const N = 8, R = 26, TH = 14, RS = 44, OFF = 22.5;
const SEAT = [-5, -6, 5, 6], BACK = [5, -6, 7.6, 6], SEAT_H = 6, BACK_H = 18;
const REST_OUT = [1.5, 0, 2.5, 0.5, 0, 2, 1, 3]; // chairs are never tucked in evenly
const ROLES = ["chair", "researcher", "analyst", "critic", "writer", "monitor", "researcher", "security"];
const FALL = [1, 0.32, 0.08, 0, 0], SLIDE = 6, STEP = 45, LOOK = 7;
const EL = 80, EH = 58; // the almond on the floor: half-length along the screen, half-height across

/** A local rounded rectangle (u radial, v tangential) turned to angle th and moved out to radius rr. */
function turned(box, r, th, rr) {
  const c = Math.cos(th), s = Math.sin(th);
  return rrect(box[0], box[1], box[2], box[3], r, 4).map((q) => ({
    u: (q.u + rr) * c - q.v * s, v: (q.u + rr) * s + q.v * c, nu: q.nu * c - q.nv * s, nv: q.nu * s + q.nv * c,
  }));
}

/** The almond lid, lying on the floor: screen-horizontal axis a = (1,-1)/√2, depth axis b = (1,1)/√2. */
function almond(P) {
  const pts = [], h = Math.SQRT1_2;
  for (const side of [1, -1])
    for (let i = 0; i <= 40; i++) {
      const t = side > 0 ? -1 + i / 20 : 1 - i / 20, A = t * EL, B = side * EH * (1 - t * t);
      pts.push(P((A + B) * h, (-A + B) * h, 0));
    }
  return poly(pts);
}

function mount({ stage, svg, read }, value) {
  const bag = disposer();
  let rise = value;

  const C = Cam(45, 0.5, 1.98);
  const h = Math.SQRT1_2;
  fit(C, [[EL * h, -EL * h, 0], [-EL * h, EL * h, 0], [EH * h, EH * h, 0], [-EH * h, -EH * h, 0],
    [-(RS + 8) * h, -(RS + 8) * h, 40], [-R * h, -R * h, TH]], 200, 170);
  const P = proj(C), front = facing(C);

  const g = mk("g", {}, svg);
  mk("path", { d: almond(P), class: "nf lo" }, g);

  // chairs and the table, painted back to front by depth (x + y)
  const angle = (i) => rad(OFF + i * 45);
  const chairs = [];
  for (let i = 0; i < N; i++) chairs.push({ i, th: angle(i), z: tween(0), out: tween(REST_OUT[i]) });
  const depth = (th) => Math.cos(th) + Math.sin(th);
  const order = [...chairs].sort((a, b) => depth(a.th) - depth(b.th));
  const table = { tableAt: true };
  const items = [...order.filter((c) => depth(c.th) < 0), table, ...order.filter((c) => depth(c.th) >= 0)];

  let pupil, look = { x: tween(Math.cos(angle(0)) * 2), y: tween(Math.sin(angle(0)) * 2) };
  for (const it of items) {
    if (it === table) {
      const t = solid(g);
      put(t, prism(P, front, circ(R, 64), circ(R - 1.6, 64), 0, TH));
      t.sil.classList.add("sil");
      pupil = mk("circle", { r: 3.4, class: "dot" }, g);
      continue;
    }
    it.g = mk("g", {}, g);
    // the back stands on the outer edge: nearer the viewer than the seat when the chair faces away
    const backFirst = depth(it.th) < 0;
    const a = solid(it.g), b = solid(it.g);
    it.seat = backFirst ? b : a;
    it.back = backFirst ? a : b;
  }

  function drawChair(c, lift, out) {
    const rr = RS + out;
    put(c.seat, prism(P, front, turned(SEAT, 2.4, c.th, rr), turned([-4, -5, 4, 5], 1.6, c.th, rr), lift, lift + SEAT_H));
    put(c.back, prism(P, front, turned(BACK, 1, c.th, rr), null, lift, lift + BACK_H));
  }

  const B = register(stage, (_dt, now) => {
    let moving = false;
    for (const c of chairs) {
      drawChair(c, tval(c.z, now), tval(c.out, now));
      if (!tdone(c.z, now) || !tdone(c.out, now)) moving = true;
    }
    place(pupil, P(tval(look.x, now), tval(look.y, now), TH));
    if (!tdone(look.x, now) || !tdone(look.y, now)) moving = true;
    return moving;
  });
  bag.add(B.unregister);

  /** The seat under a screen point: by angle on the plane of the seats, which never moves. */
  function hit([sx, sy]) {
    const [x, y] = unproj(C, sx, sy, SEAT_H);
    const r = Math.hypot(x, y);
    if (r < R + 4 || r > RS + 16) return -1;
    const k = Math.round((Math.atan2(y, x) / rad(1) - OFF) / 45);
    return ((k % N) + N) % N;
  }

  let act = -1;
  function setActive(a) {
    if (a === act) return;
    const now = performance.now(), from = a >= 0 ? a : act;
    act = a;
    for (const c of chairs) {
      const d = from < 0 ? 0 : Math.min(Math.abs(c.i - from), N - Math.abs(c.i - from));
      const near = a < 0 ? 0 : Math.min(Math.abs(c.i - a), N - Math.abs(c.i - a));
      const delay = d * STEP;
      tset(c.z, a < 0 ? 0 : rise * FALL[clamp(near, 0, 4)], now, delay);
      tset(c.out, REST_OUT[c.i] + (a === c.i ? SLIDE : 0), now, delay);
      c.seat.sil.classList.toggle("hi", c.i === a);
      c.back.sil.classList.toggle("hi", c.i === a);
    }
    const th = angle(a < 0 ? 0 : a), reach = a < 0 ? 2 : LOOK;
    tset(look.x, Math.cos(th) * reach, now, 0);
    tset(look.y, Math.sin(th) * reach, now, 0);
    pupil.setAttribute("class", a < 0 ? "dot" : "dot m");
    read.textContent = a < 0 ? "rest" : ROLES[a];
    B.wake();
  }

  for (const c of chairs) for (const s of [c.seat, c.back]) s.sil.classList.add("sil");
  bag.add(pointer(stage, { move: (p) => setActive(hit(p)), leave: () => setActive(-1) }));
  bag.add(() => svg.replaceChildren());
  read.textContent = "rest";

  return {
    set: (v) => { rise = v; },
    destroy: bag.dispose,
  };
}

hairline({
  name: "council",
  means: "The Argus mark as a room: chairs ring a table inside an eye. The chair you point at stands to speak and the pupil turns to it.",
  rules: [1, 2, 5, 10],
  range: [6, 12, 18],
  tour: [[236, 233], [116, 168], [236, 142], null],
  mount,
});
