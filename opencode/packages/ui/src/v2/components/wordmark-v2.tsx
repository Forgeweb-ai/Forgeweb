import { createUniqueId, type ComponentProps } from "solid-js"

// Pixel-art wordmark for "FORGE UI"
// Grid: 4 cols × 5 rows per character, each cell = 18.4615 × 18.4287 units
// 8 character slots (slot 5 = space), slot width = 92.3125
export function WordmarkV2(props: Pick<ComponentProps<"svg">, "class">) {
  const filter = createUniqueId()
  const mask = createUniqueId()
  const maskGradient = createUniqueId()

  // Each letter is composed of filled rectangles in the pixel grid.
  // Block rect helper: Mx1 y1 Hx2 Vy2 Hx1 Z
  //
  // Slot offsets: F=0  O=92.3125  R=184.625  G=276.9375  E=369.25
  //               [space]=461.5625  U=553.875  I=646.1875
  //
  // y rows: y0=18.4297  y1=36.8583  y2=55.2868  y3=73.7154  y4=92.144  y5=110.573

  const d = [
    // ── F (slot 0) ──────────────────────────────────────
    "M0 18.4297H73.8462V36.8583H0Z",            // top bar
    "M0 36.8583H18.4615V110.573H0Z",            // left col
    "M18.4615 55.2868H55.3846V73.7154H18.4615Z",// mid bar

    // ── O (slot 1, offset 92.3125) ───────────────────────
    "M92.3125 18.4297H166.159V36.8583H92.3125Z", // top
    "M92.3125 92.144H166.159V110.573H92.3125Z",  // bottom
    "M92.3125 36.8583H110.774V92.144H92.3125Z",  // left col
    "M147.697 36.8583H166.159V92.144H147.697Z",  // right col

    // ── R (slot 2, offset 184.625) ───────────────────────
    "M184.625 18.4297H203.086V110.573H184.625Z", // left col
    "M203.086 18.4297H258.467V36.8583H203.086Z", // top bar
    "M240.008 36.8583H258.467V73.7154H240.008Z", // right col (upper bowl)
    "M203.086 55.2868H240.008V73.7154H203.086Z", // mid bar
    "M221.547 73.7154H258.467V110.573H221.547Z", // right leg

    // ── G (slot 3, offset 276.9375) ──────────────────────
    "M276.9375 18.4297H350.778V36.8583H276.9375Z",// top bar
    "M276.9375 36.8583H295.398V110.573H276.9375Z",// left col
    "M295.398 92.144H350.778V110.573H295.398Z",   // bottom bar
    "M313.859 55.2868H350.778V73.7154H313.859Z",  // mid-right bar
    "M332.32 73.7154H350.778V92.144H332.32Z",     // right col (lower)

    // ── E (slot 4, offset 369.25) ────────────────────────
    "M369.25 18.4297H387.711V110.573H369.25Z",    // left col
    "M387.711 18.4297H443.092V36.8583H387.711Z",  // top bar
    "M387.711 55.2868H424.633V73.7154H387.711Z",  // mid bar (3/4 width)
    "M387.711 92.144H443.092V110.573H387.711Z",   // bottom bar

    // ── [space] (slot 5, offset 461.5625) — intentionally empty ──

    // ── U (slot 6, offset 553.875) ───────────────────────
    "M553.875 18.4297H572.336V92.144H553.875Z",   // left col
    "M572.336 92.144H627.717V110.573H572.336Z",   // bottom bar
    "M609.258 18.4297H627.717V92.144H609.258Z",   // right col

    // ── I (slot 7, offset 646.1875) ──────────────────────
    "M646.1875 18.4297H720.034V36.8583H646.1875Z",// top bar
    "M664.649 36.8583H701.572V92.144H664.649Z",   // center col
    "M646.1875 92.144H720.034V110.573H646.1875Z", // bottom bar
  ].join("")

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 720.002 129.001"
      fill="none"
      preserveAspectRatio="none"
      classList={{ [props.class ?? ""]: !!props.class }}
    >
      <g opacity="0.16" filter={`url(#${filter})`} mask={`url(#${mask})`}>
        <path opacity="0.7" d={d} fill="currentColor" />
      </g>
      <defs>
        <mask id={mask} maskUnits="userSpaceOnUse" x="0" y="0" width="720" height="129">
          <rect width="720" height="129" fill={`url(#${maskGradient})`} />
        </mask>
        <linearGradient id={maskGradient} x1="360" y1="0" x2="360" y2="112" gradientUnits="userSpaceOnUse">
          <stop stop-color="white" stop-opacity="0.7" />
          <stop offset="1" stop-color="white" stop-opacity="0" />
        </linearGradient>
        <filter
          id={filter}
          x="0"
          y="0"
          width="720.002"
          height="130.001"
          filterUnits="userSpaceOnUse"
          color-interpolation-filters="sRGB"
        >
          <feFlood flood-opacity="0" result="BackgroundImageFix" />
          <feBlend mode="normal" in="SourceGraphic" in2="BackgroundImageFix" result="shape" />
          <feColorMatrix
            in="SourceAlpha"
            type="matrix"
            values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 127 0"
            result="hardAlpha"
          />
          <feOffset dy="1" />
          <feGaussianBlur stdDeviation="1" />
          <feComposite in2="hardAlpha" operator="arithmetic" k2="-1" k3="1" />
          <feColorMatrix type="matrix" values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0" />
          <feBlend mode="normal" in2="shape" result="effect1_innerShadow_4938_16028" />
        </filter>
      </defs>
    </svg>
  )
}
