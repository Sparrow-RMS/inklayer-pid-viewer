import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { clamp, columnPart, pipingPart, progressFor, tankPart } from '../lib/constructionProgress'
import './ConstructionSite.css'

export interface ConstructionSiteProps {
  /** Characters of the page written so far (0 while Sparrow is still planning). */
  chars: number
  /** Bumped on every streamed event: each one sets something off on site. */
  activity: number
  /** The equipment tag Sparrow mentioned most recently, if any. */
  latestTag: string | null
  fileName: string
}

// The server reports progress every 2000 characters; between reports the
// site keeps growing at the last measured rate, up to the next report.
const REPORT_EVERY = 2000

const CALLS = {
  planning: ['Reading the P&ID…', 'Counting the tanks…', 'Where do the pumps go?', 'Checking line numbers…', 'Surveying the plot…'],
  building: ['Pouring the foundations!', 'Welding the seams…', 'Laying the pipe rack…', 'Bolting on the valves…', 'Tightening flanges…', 'Nearly there!'],
  tagged: ['Lifting {tag} into place…', 'Bolting {tag} down!', 'Where does {tag} go?', 'Piping up {tag}…', '{tag} coming through!'],
}

// Fixed pseudo-random layout, so the stars and windows don't jump on every render.
function seeded(n: number, seed: number) {
  let s = seed
  return Array.from({ length: n }, () => {
    s = (s * 9301 + 49297) % 233280
    return s / 233280
  })
}
const STARS = (() => {
  const r = seeded(150, 7)
  return Array.from({ length: 50 }, (_, i) => [r[i * 3], r[i * 3 + 1], r[i * 3 + 2]])
})()
const SKYLINE = [
  { x: 150, w: 46, h: 70 }, { x: 200, w: 30, h: 110 }, { x: 236, w: 54, h: 58 }, { x: 420, w: 40, h: 90 },
  { x: 466, w: 28, h: 130 }, { x: 500, w: 60, h: 64 }, { x: 690, w: 36, h: 96 }, { x: 732, w: 50, h: 74 }, { x: 790, w: 34, h: 120 },
]
const WINDOWS = SKYLINE.flatMap((b, bi) => {
  const cols = Math.max(1, Math.floor((b.w - 6) / 10))
  const rows = Math.floor((b.h - 10) / 12)
  const r = seeded(cols * rows, bi + 11)
  return Array.from({ length: cols * rows }, (_, i) => ({
    x: b.x + 5 + (i % cols) * 10,
    y: 392 - b.h + 7 + Math.floor(i / cols) * 12,
    lit: r[i] > 0.45,
    delay: -r[i] * 9,
    duration: 4 + r[i] * 8,
  })).filter((w) => w.lit)
})

// Builders are drawn at this size; everything that lines up with their hands is scaled by it too.
const B = 1.35

/** A builder: capsule body, goggle, hard hat and hi-vis vest. Feet at (0, 0), facing right.
 * `delay` staggers its loops against the others; `cheer` makes it jump once. */
function Builder({ walking = false, cheer = false, delay = 0, children }: { walking?: boolean; cheer?: boolean; delay?: number; children?: ReactNode }) {
  return (
    <g className={`cs-builder${walking ? ' cs-walking' : ''}${cheer ? ' cs-cheer' : ''}`} style={{ '--cs-delay': `${delay}s` } as CSSProperties}>
      <g transform={`scale(${B})`}>
        <g transform="translate(-4,-10)">
          <rect className="cs-leg cs-leg-a" x={-2.5} y={0} width={5} height={10} rx={2} />
        </g>
        <g transform="translate(4,-10)">
          <rect className="cs-leg cs-leg-b" x={-2.5} y={0} width={5} height={10} rx={2} />
        </g>
        <g className="cs-breathe">
          <rect className="cs-body" x={-12} y={-42} width={24} height={34} rx={12} />
          <rect className="cs-vest" x={-12} y={-21} width={24} height={11} rx={3} />
          <rect className="cs-vest-stripe" x={-12} y={-17} width={24} height={2} />
          <rect className="cs-strap" x={-12} y={-34} width={24} height={3} />
          <circle className="cs-goggle" cx={3} cy={-32.5} r={6} />
          <circle className="cs-eye" cx={3} cy={-32.5} r={3.4} />
          <circle className="cs-pupil" cx={4.3} cy={-32.5} r={1.6} />
          <path className="cs-mouth" d={cheer ? 'M0 -26 q3 4 6 0 z' : 'M0 -25 q3 2.5 6 0'} />
          <path className="cs-hat" d="M-12 -40 a12 11 0 0 1 24 0 z" />
          <rect className="cs-hat" x={-14} y={-41} width={28} height={3} rx={1.5} />
          <circle className="cs-hat-lamp" cx={0} cy={-46} r={1.8} />
          {children}
        </g>
      </g>
    </g>
  )
}

/** An arm from the shoulder, rotated by `className`'s animation; `tool` is drawn in its hand. */
function Arm({ className = '', tool }: { className?: string; tool?: ReactNode }) {
  return (
    <g transform="translate(8,-27)">
      <g className={className}>
        <rect className="cs-body" x={0} y={-2} width={12} height={4} rx={2} />
        {tool}
      </g>
    </g>
  )
}

interface Burst {
  id: number
  x: number
  y: number
}

// While Sparrow draws the 3D view: a night-shift construction site that builds
// the shop floor as the page is written. The tank, column and pipe rack grow
// with the page (extrapolated between reports, so they never stall), and every
// streamed event sets something off: a sparkle on the structure, a builder
// cheering, the foreman calling out the tag Sparrow just mentioned.
export default function ConstructionSite({ chars, activity, latestTag, fileName }: ConstructionSiteProps) {
  const planning = chars === 0

  // Smooth progress: ease towards the predicted page length at ~30 fps.
  const report = useRef({ chars: 0, at: 0, rate: 0 })
  useEffect(() => {
    const now = performance.now()
    const last = report.current
    if (chars > last.chars) {
      // chars per ms; the first report has no previous one to measure against.
      const rate = last.chars ? (chars - last.chars) / Math.max(1, now - last.at) : 0.15
      report.current = { chars, at: now, rate }
    }
  }, [chars])
  const [shown, setShown] = useState(0)
  useEffect(() => {
    let frame = 0
    let previous = 0
    const tick = (t: number) => {
      frame = requestAnimationFrame(tick)
      if (t - previous < 33) return
      previous = t
      const { chars: c, at, rate } = report.current
      const predicted = c ? c + Math.min(REPORT_EVERY * 0.95, rate * (t - at)) : 0
      const target = progressFor(predicted)
      setShown((s) => (Math.abs(target - s) < 0.0005 ? s : s + (target - s) * 0.08))
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [])

  const tank = tankPart(shown)
  const column = columnPart(shown)
  const piping = pipingPart(shown)
  const pump = shown > 0.6

  // Reactions to the stream, at most one every 450 ms.
  const [bursts, setBursts] = useState<Burst[]>([])
  const [cheering, setCheering] = useState<number | null>(null)
  const [line, setLine] = useState(CALLS.planning[0])
  const lastReaction = useRef(0)
  const lastLine = useRef(0)
  const nextId = useRef(0)
  const latest = useRef({ tank, column, planning, latestTag })
  useEffect(() => {
    latest.current = { tank, column, planning, latestTag }
  })
  useEffect(() => {
    if (activity === 0) return
    const now = performance.now()
    if (now - lastReaction.current < 450) return
    lastReaction.current = now
    const { tank: t, column: c, planning: p, latestTag: tag } = latest.current
    const r = Math.random()
    // Sparkle where the work is: the tank's rising edge, the column top, or the slab while planning.
    const spot = p
      ? { x: 200 + r * 520, y: 384 }
      : r < 0.55 || c === 0
        ? { x: 262 + r * 130, y: 392 - t * 190 }
        : { x: 626 + r * 40, y: 392 - c * 290 }
    const id = nextId.current++
    setBursts((list) => [...list.slice(-5), { id, ...spot }])
    window.setTimeout(() => setBursts((list) => list.filter((b) => b.id !== id)), 1100)
    if (Math.random() < 0.35) {
      const who = Math.floor(Math.random() * 4)
      setCheering(who)
      window.setTimeout(() => setCheering((w) => (w === who ? null : w)), 900)
    }
    if (now - lastLine.current > 2600) {
      lastLine.current = now
      const pool = tag && !p && Math.random() < 0.7 ? CALLS.tagged.map((s) => s.replace('{tag}', tag)) : p ? CALLS.planning : CALLS.building
      setLine(pool[Math.floor(Math.random() * pool.length)])
    }
  }, [activity])

  const TANK = { x: 250, w: 150, base: 392, h: 190 }
  const tankTop = TANK.base - tank * TANK.h
  const COL = { x: 620, w: 52, base: 392, h: 290 }
  const colTop = COL.base - column * COL.h
  const crateLabel = latestTag && latestTag.length <= 9 ? latestTag : 'P&ID'

  return (
    <div className="cs">
      <svg className="cs-svg" viewBox="0 0 900 500" preserveAspectRatio="xMidYMax meet" role="img" aria-label="Builders constructing the shop floor while Sparrow draws the 3D view">
        <defs>
          {/* In user space: above y = 0 (where a tall box shows more sky) it stays the top colour. */}
          <linearGradient id="cs-sky" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="500">
            <stop offset="0" stopColor="#07090f" />
            <stop offset="1" stopColor="#141b36" />
          </linearGradient>
          <radialGradient id="cs-glow-indigo" cx="1" cy="0" r="0.75">
            <stop offset="0" stopColor="#637bff" stopOpacity="0.5" />
            <stop offset="1" stopColor="#637bff" stopOpacity="0" />
          </radialGradient>
          <radialGradient id="cs-glow-teal" cx="0" cy="1" r="0.7">
            <stop offset="0" stopColor="#5eead4" stopOpacity="0.28" />
            <stop offset="1" stopColor="#5eead4" stopOpacity="0" />
          </radialGradient>
          <linearGradient id="cs-beam" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#fff6d8" stopOpacity="0.5" />
            <stop offset="1" stopColor="#fff6d8" stopOpacity="0" />
          </linearGradient>
          <linearGradient id="cs-steel" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stopColor="#2a3566" />
            <stop offset="0.35" stopColor="#6a7bd0" />
            <stop offset="0.55" stopColor="#4b5aa6" />
            <stop offset="1" stopColor="#222b55" />
          </linearGradient>
          <pattern id="cs-grid" width="32" height="32" patternUnits="userSpaceOnUse">
            <path d="M32 0 H0 V32" fill="none" stroke="#ffffff" strokeOpacity="0.05" />
          </pattern>
          <pattern id="cs-lattice" width="12" height="12" patternUnits="userSpaceOnUse">
            <path d="M0 0 L12 12 M12 0 L0 12" className="cs-lattice-line" />
          </pattern>
          <pattern id="cs-hazard" width="20" height="20" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <rect width="10" height="20" fill="#ffc400" />
            <rect x="10" width="10" height="20" fill="#1f2430" />
          </pattern>
          <filter id="cs-glow" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="3" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
          <clipPath id="cs-tank-clip">
            <rect x={TANK.x - 2} y={tankTop} width={TANK.w + 4} height={TANK.base - tankTop} />
          </clipPath>
        </defs>

        {/* Night sky: the run dialog's navy, glows and grid, with stars and a moon. The
            scene is fitted to its box from the bottom, so the sky runs on above y = 0. */}
        <rect x="-450" y="-600" width="1800" height="1100" fill="url(#cs-sky)" />
        <rect x="0" y="-300" width="1350" height="800" fill="url(#cs-glow-indigo)" />
        <rect x="-450" y="0" width="1350" height="500" fill="url(#cs-glow-teal)" />
        <rect x="-450" y="-600" width="1800" height="1100" fill="url(#cs-grid)" />
        {STARS.map(([x, y, r], i) => (
          <circle key={i} className="cs-star" cx={x * 1300 - 200} cy={y * 530 - 300} r={0.6 + r * 1.2} style={{ animationDelay: `${-r * 6}s`, animationDuration: `${2.5 + r * 4}s` }} />
        ))}
        <circle className="cs-moon" cx="800" cy="74" r="24" filter="url(#cs-glow)" />
        <circle cx="809" cy="68" r="21" fill="#0b1020" opacity="0.9" />
        <g className="cs-birds">
          <path className="cs-bird" d="M0 0 q5 -5 10 0 q5 -5 10 0" />
          <path className="cs-bird cs-bird-2" d="M30 12 q4 -4 8 0 q4 -4 8 0" />
        </g>

        {/* City behind the site, windows lit at random */}
        {SKYLINE.map((b) => (
          <rect key={b.x} className="cs-building" x={b.x} y={392 - b.h} width={b.w} height={b.h} />
        ))}
        {WINDOWS.map((w, i) => (
          <rect key={i} className="cs-window" x={w.x} y={w.y} width="4" height="5" style={{ animationDelay: `${w.delay}s`, animationDuration: `${w.duration}s` }} />
        ))}

        {/* Ground and the slab */}
        <rect className="cs-ground" x="0" y="392" width="900" height="108" />
        <rect className="cs-slab" x="170" y="388" width="600" height="8" rx="2" />
        <line className="cs-slab-edge" x1="170" x2="770" y1="388" y2="388" />

        {/* Floodlights sweeping the site */}
        {[
          { x: 30, flip: 1, delay: 0 },
          { x: 870, flip: -1, delay: -3.5 },
        ].map((f) => (
          <g key={f.x} transform={`translate(${f.x},120)`}>
            <line className="cs-pole" x1="0" x2="0" y1="0" y2="272" />
            <g className="cs-sweep" style={{ animationDelay: `${f.delay}s` }}>
              <path d={`M0 0 L${f.flip * 160} 280 L${f.flip * 360} 260 Z`} fill="url(#cs-beam)" className="cs-beam" />
            </g>
            <rect className="cs-lamp" x="-9" y="-6" width="18" height="10" rx="2" filter="url(#cs-glow)" />
          </g>
        ))}

        {/* Tower crane, swinging a crate across the site */}
        <g className="cs-crane">
          <rect x="82" y="96" width="22" height="296" fill="url(#cs-lattice)" className="cs-crane-steel" />
          <rect x="40" y="82" width="460" height="14" fill="url(#cs-lattice)" className="cs-crane-steel" />
          <rect className="cs-crane-weight" x="40" y="96" width="34" height="24" rx="2" />
          <rect className="cs-crane-cab" x="104" y="96" width="26" height="22" rx="3" />
          <rect className="cs-crane-window" x="114" y="100" width="12" height="9" rx="2" />
          <circle className="cs-beacon" cx="498" cy="80" r="3.5" filter="url(#cs-glow)" />
          <g className="cs-trolley">
            <rect className="cs-crane-cab" x="-8" y="94" width="16" height="8" rx="2" />
            <rect className="cs-cable" x="-1" y="102" width="2" height="40" />
            <g className="cs-hoist">
              <g className="cs-swing">
                <path className="cs-hook" d="M0 142 v8 a5 5 0 1 1 -5 5" />
                <line className="cs-sling" x1="0" y1="152" x2="-26" y2="166" />
                <line className="cs-sling" x1="0" y1="152" x2="26" y2="166" />
                <rect className="cs-crate" x="-34" y="166" width="68" height="26" rx="3" />
                <line className="cs-crate-plank" x1="-34" x2="34" y1="179" y2="179" />
                <text className="cs-crate-label" x="0" y="183">{crateLabel}</text>
                {/* A builder riding the crate, waving */}
                <g transform="translate(20,166) scale(0.5)">
                  <Builder cheer={cheering === 3} delay={-0.4}>
                    <Arm className="cs-wave" />
                  </Builder>
                </g>
              </g>
            </g>
          </g>
        </g>

        {/* Storage tank, rising inside its scaffolding */}
        <rect className={`cs-ghost${planning ? ' cs-surveying' : ''}`} x={TANK.x} y={TANK.base - TANK.h} width={TANK.w} height={TANK.h} rx="4" />
        <g clipPath="url(#cs-tank-clip)">
          <rect className="cs-tank" x={TANK.x} y={TANK.base - TANK.h} width={TANK.w} height={TANK.h} rx="4" fill="url(#cs-steel)" />
          {[1, 2, 3, 4].map((i) => (
            <line key={i} className="cs-seam" x1={TANK.x} x2={TANK.x + TANK.w} y1={TANK.base - (i * TANK.h) / 5} y2={TANK.base - (i * TANK.h) / 5} />
          ))}
        </g>
        {!planning && tank < 1 && <line className="cs-hot-edge" x1={TANK.x} x2={TANK.x + TANK.w} y1={tankTop} y2={tankTop} filter="url(#cs-glow)" />}
        {tank >= 1 && <ellipse className="cs-tank cs-pop" cx={TANK.x + TANK.w / 2} cy={TANK.base - TANK.h} rx={TANK.w / 2} ry="12" fill="url(#cs-steel)" />}
        <g className="cs-scaffold">
          {[TANK.x - 14, TANK.x + TANK.w + 14].map((x) => (
            <line key={x} x1={x} x2={x} y1={TANK.base} y2={TANK.base - TANK.h - 10} />
          ))}
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <line key={i} className="cs-plank" x1={TANK.x - 20} x2={TANK.x + TANK.w + 20} y1={TANK.base - i * 40} y2={TANK.base - i * 40} />
          ))}
        </g>
        {/* The welder rides the scaffold up with the tank's rising edge */}
        {!planning && tank < 1 && (
          <g transform={`translate(${TANK.x + TANK.w + 6},${Math.max(tankTop + 6, TANK.base - TANK.h + 20)})`}>
            <g transform="scale(-0.7,0.7)">
              <Builder delay={-0.9}>
                <Arm className="cs-weld" tool={<rect className="cs-tool" x={10} y={-1.5} width={9} height={3} rx={1} />} />
              </Builder>
            </g>
            {[0, 1, 2, 3, 4].map((i) => (
              <circle key={i} className={`cs-spark cs-spark-${i}`} cx={-18.5} cy={-25.5} r={1.8} />
            ))}
          </g>
        )}

        {/* Distillation column, after the tank has a start */}
        <rect className={`cs-ghost${planning ? ' cs-surveying' : ''}`} x={COL.x} y={COL.base - COL.h} width={COL.w} height={COL.h} rx="20" />
        {column > 0 && <rect className="cs-column" x={COL.x} y={colTop} width={COL.w} height={COL.base - colTop} rx="8" fill="url(#cs-steel)" />}
        {column > 0 && column < 1 && <line className="cs-hot-edge" x1={COL.x} x2={COL.x + COL.w} y1={colTop} y2={colTop} filter="url(#cs-glow)" />}
        {column > 0.3 && <line className="cs-ladder" x1={COL.x + COL.w + 6} x2={COL.x + COL.w + 6} y1={COL.base} y2={colTop + 10} />}
        {[0.35, 0.65, 0.95].filter((t) => column >= t).map((t) => (
          <rect key={t} className="cs-platform cs-pop" x={COL.x - 10} y={COL.base - t * COL.h} width={COL.w + 26} height="4" />
        ))}
        {column >= 1 && <circle className="cs-beacon" cx={COL.x + COL.w / 2} cy={COL.base - COL.h - 6} r="3" filter="url(#cs-glow)" />}

        {/* Pipe rack between them, laid as the page is written */}
        <g className="cs-rack">
          {[450, 510, 570].map((x) => (
            <line key={x} x1={x} x2={x} y1={392} y2={312} />
          ))}
          <line x1="440" x2="580" y1="312" y2="312" />
        </g>
        <path className="cs-pipe cs-pipe-a" pathLength={1} strokeDasharray="1" strokeDashoffset={1 - piping} filter="url(#cs-glow)" d="M400 330 H430 V306 H600 V300 H620" />
        <path className="cs-pipe cs-pipe-b" pathLength={1} strokeDasharray="1" strokeDashoffset={1 - clamp(piping * 1.3 - 0.3)} filter="url(#cs-glow)" d="M400 370 H480 V378" />
        {pump && (
          <g transform="translate(480,392)">
            {/* The pop-in's CSS transform would replace a transform attribute on the same element. */}
            <g className="cs-pop">
              <rect className="cs-pump" x="-18" y="-14" width="36" height="14" rx="3" />
              <circle className="cs-pump" cx="0" cy="-16" r="9" />
            </g>
          </g>
        )}

        {/* Sparkles from the stream */}
        {bursts.map((b) => (
          <g key={b.id} transform={`translate(${b.x},${b.y})`}>
            <circle className="cs-ring" r="6" />
            <path className="cs-sparkle" d="M0 -11 L2.5 -2.5 L11 0 L2.5 2.5 L0 11 L-2.5 2.5 L-11 0 L-2.5 -2.5 Z" filter="url(#cs-glow)" />
          </g>
        ))}

        {/* The hammerer at the column's base */}
        <g transform="translate(704,392)">
          <g transform="scale(-1,1)">
            <Builder cheer={cheering === 0} delay={-0.2}>
              <Arm className="cs-hammer" tool={<><rect className="cs-handle" x={10} y={-12} width={2.5} height={14} /><rect className="cs-tool" x={6} y={-15} width={11} height={5} rx={1} /></>} />
            </Builder>
          </g>
        </g>

        {/* The foreman with the blueprint, calling out what's happening */}
        <g transform="translate(150,452)">
          <g className="cs-bob">
            <Builder cheer={cheering === 1} delay={-0.6}>
              <Arm className="cs-point" tool={<rect className="cs-blueprint" x={8} y={-10} width={18} height={13} rx={1} />} />
            </Builder>
          </g>
          <g className="cs-bubble" key={line}>
            <rect x="-10" y="-106" width={line.length * 7.2 + 24} height="30" rx="10" />
            <path d="M2 -76 l6 10 l6 -10 z" />
            <text x="2" y="-86">{line}</text>
          </g>
        </g>

        {/* Two builders carrying a pipe across the site */}
        <g transform="translate(0,468)">
          <g className="cs-walk-across">
            <rect className="cs-pipe-load" x={-52} y={-34 * B} width={100} height={9} rx={4.5} />
            <Builder walking cheer={cheering === 2} />
            <g transform="translate(-40,0)">
              <Builder walking delay={-0.25} />
            </g>
          </g>
        </g>

        {/* A builder with a plank on the hard hat, walking the other way, slower */}
        <g transform="translate(0,446)">
          <g className="cs-walk-back">
            <g transform="scale(-1,1)">
              <rect className="cs-plank-load" x={-30} y={-62} width={60} height={6} rx={2} />
              <Builder walking delay={-0.1} />
            </g>
          </g>
        </g>

        {/* The wheelbarrow run, kicking up dust */}
        <g transform="translate(0,482)">
          <g className="cs-barrow-run">
            <Builder walking delay={-0.35} />
            <g transform="translate(20,-4) scale(1.25)">
              <path className="cs-barrow" d="M0 -14 H30 L24 -2 H6 Z" />
              <circle className="cs-wheel" cx="27" cy="0" r="5" />
              <path className="cs-dirt" d="M3 -14 q12 -10 24 0 z" />
            </g>
            {[0, 1, 2].map((i) => (
              <circle key={i} className={`cs-dust cs-dust-${i}`} cx={-14} cy={-3} r={4} />
            ))}
          </g>
        </g>

        {/* Fence, cones and the site board */}
        <rect x="0" y="488" width="900" height="12" fill="url(#cs-hazard)" />
        {[60, 330, 860].map((x, i) => (
          <g key={x} transform={`translate(${x},488)`}>
            <path className="cs-cone" d="M-9 0 L-3 -22 H3 L9 0 Z" />
            <rect className="cs-cone-band" x="-5" y="-14" width="10" height="4" />
            <circle className="cs-cone-light" cx="0" cy="-25" r="2.4" filter="url(#cs-glow)" style={{ animationDelay: `${-i * 0.37}s` }} />
          </g>
        ))}
        <g transform="translate(730,404)">
          <line className="cs-board-post" x1="18" x2="18" y1="44" y2="84" />
          <line className="cs-board-post" x1="138" x2="138" y1="44" y2="84" />
          <rect className="cs-board" x="0" y="0" width="156" height="46" rx="4" />
          <text className="cs-board-title" x="78" y="19">UNDER CONSTRUCTION</text>
          <text className="cs-board-text" x="78" y="35">{fileName.length > 22 ? `${fileName.slice(0, 21)}…` : fileName}</text>
        </g>
      </svg>
    </div>
  )
}
