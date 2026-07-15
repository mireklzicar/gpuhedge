"""Build assets/gpuhedge_boxplot.png as an HTML infographic -> Chrome screenshot.

Usage: python scripts/render_boxplot_asset.py, then run the printed Chrome
command (and delete the intermediate .html).

Boxplot without per-round dots; per-row stats aligned as columns so the chart
itself carries the table. Data = the committed 2026-07 MOSS benchmark
(36 evaluation rounds), identical numbers to gpuhedge_ui/src/data/chartData.ts
and benchmarks/2026-07-moss/results.md.
"""
import base64
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"

SERIES = {
    "runpod": [116.48, 5.95, 5.94, 5.95, 104.49, 5.96, 122.15, 5.98, 109.57,
               120.07, 6.74, 6.61, 116.63, 109.75, 89.48, 6.04, 6.28, 5.99,
               8.32, 6.42, 6.11, 5.97, 5.92, 105.01, 100.16, 5.98, 5.99, 5.98,
               94.48, 5.99, 5.78, 6.02, 5.97, 6.0, 5.98, 6.0],
    "modal": [38.82, 39.71, 27.11, 96.75, 39.57, 47.84, 28.53, 30.21, 57.35,
              37.57, 39.27, 41.02, 39.32, 30.34, 28.17, 35.23, 35.98, 30.05,
              30.05, 38.38, 40.17, 82.99, 36.94, 42.18, 27.1, 29.39, 29.79,
              28.76, 26.73, 28.34, 29.24, 61.96, 73.83, 98.03, 37.82, 44.8],
    "cerebrium": [18.82, 18.9, 19.29, 18.74, 19.23, 19.52, 18.81, 18.97,
                  18.31, 19.76, 19.35, 20.2, 19.42, 19.09, 19.45, 19.49,
                  19.64, 19.3, 19.25, 18.76, 19.3, 20.85, 19.29, 18.88, 19.04,
                  19.1, 18.43, 19.91, 17.73, 101.95, 5.34, 19.23, 19.43,
                  19.23, 19.24, 18.42],
    "cutover": [21.32, 5.95, 5.94, 5.95, 21.73, 5.96, 21.31, 5.98, 20.81,
                22.26, 6.74, 6.61, 21.92, 21.59, 21.95, 6.04, 6.28, 5.99,
                8.32, 6.42, 6.11, 5.97, 5.92, 21.38, 21.54, 5.98, 5.99, 5.98,
                20.23, 5.99, 5.78, 6.02, 5.97, 6.0, 5.98, 6.0],
}
STATS = {  # matches gpuhedge replay / README
    "runpod": dict(p50=6.0, p95=116.6, mx=122.2, miss=11),
    "modal": dict(p50=37.8, p95=83.0, mx=98.0, miss=5),
    "cerebrium": dict(p50=19.2, p95=20.2, mx=102.0, miss=1),
    "cutover": dict(p50=6.0, p95=21.9, mx=22.3, miss=0),
}

HERO = "#0f9488"
NEUTRAL = "#6b7fa3"
INK = "#101d1b"
INK2 = "#546360"
MUTED = "#8a9895"
GRID = "#e7ecec"
DEADLINE = "#c2410c"

ROWS = [
    ("runpod", "RunPod", "single provider", NEUTRAL, "logos/runpod-logo-white-bg.jpg"),
    ("modal", "Modal", "single provider", NEUTRAL, "logos/modal-logo-transparent-bg.png"),
    ("cerebrium", "Cerebrium", "single provider", NEUTRAL, "logos/cerebrium-logo-white-bg.jpg"),
    ("cutover", "GPUHedge", "queue cutover @2.5 s", HERO, None),
]


def q(sorted_vals, p):
    pos = (len(sorted_vals) - 1) * p
    lo = int(pos)
    frac = pos - lo
    if lo + 1 < len(sorted_vals):
        return sorted_vals[lo] + frac * (sorted_vals[lo + 1] - sorted_vals[lo])
    return sorted_vals[lo]


def box(vals):
    s = sorted(vals)
    q1, med, q3 = q(s, 0.25), q(s, 0.5), q(s, 0.75)
    iqr = q3 - q1
    lo = min(v for v in s if v >= q1 - 1.5 * iqr)
    hi = max(v for v in s if v <= q3 + 1.5 * iqr)
    return q1, med, q3, lo, hi


def data_uri(path):
    b = (ASSETS / path).read_bytes()
    mime = "image/png" if path.endswith("png") else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(b).decode()}"


# ---- geometry (logical px, rendered @2x) ----
W, H = 1560, 892
PAD = 60
PLOT_X0, PLOT_X1 = 320, 1090            # latency axis area
COLS_X = [1170, 1265, 1360, 1470]        # p50, p95, max, >60s (right-aligned)
ROW0_Y, ROW_H = 128, 112
MAX_S = 126.0
TICKS = [0, 30, 60, 90, 120]
SVG_H = ROW0_Y + len(ROWS) * ROW_H - 36 + 78


def x(v):
    return PLOT_X0 + (min(v, MAX_S) / MAX_S) * (PLOT_X1 - PLOT_X0)


svg = []
plot_top, plot_bot = ROW0_Y - 46, ROW0_Y + len(ROWS) * ROW_H - 36
# gridlines + ticks
for t in TICKS:
    svg.append(f'<line x1="{x(t):.1f}" y1="{plot_top}" x2="{x(t):.1f}" y2="{plot_bot}" stroke="{GRID}" stroke-width="1.5"/>')
    svg.append(f'<text x="{x(t):.1f}" y="{plot_bot + 30}" text-anchor="middle" class="tick">{t}</text>')
svg.append(f'<text x="{(PLOT_X0 + PLOT_X1) / 2:.0f}" y="{plot_bot + 62}" text-anchor="middle" class="axis">end-to-end cold-start latency (seconds)</text>')
# 60s deadline
svg.append(f'<line x1="{x(60):.1f}" y1="{plot_top - 14}" x2="{x(60):.1f}" y2="{plot_bot}" stroke="{DEADLINE}" stroke-width="2" stroke-dasharray="5 6" opacity="0.65"/>')
svg.append(f'<text x="{x(60) + 10:.1f}" y="{plot_top - 2}" class="deadline">60 s deadline</text>')

# column headers
for cx, name in zip(COLS_X, ["p50", "p95", "max", "&gt;60 s"]):
    svg.append(f'<text x="{cx}" y="{plot_top - 2}" text-anchor="end" class="colhead">{name}</text>')

for i, (key, name, sub, col, logo) in enumerate(ROWS):
    cy = ROW0_Y + i * ROW_H
    hero = key == "cutover"
    st = STATS[key]
    q1, med, q3, lo, hi = box(SERIES[key])

    if hero:
        svg.append(f'<rect x="{PAD - 18}" y="{cy - 46}" width="{W - 2 * PAD + 36}" height="94" rx="14" fill="{HERO}" opacity="0.07"/>')

    # label block
    lx = PAD + 66
    if logo:
        svg.append(f'<image href="{data_uri(logo)}" x="{PAD}" y="{cy - 24}" width="48" height="48" preserveAspectRatio="xMidYMid meet"/>')
    else:  # gpuhedge mark
        svg.append(f'<g transform="translate({PAD + 4},{cy - 20})">'
                   f'<rect x="0" y="4" width="16" height="6" rx="3" fill="{NEUTRAL}"/>'
                   f'<rect x="0" y="20" width="28" height="6" rx="3" fill="{HERO}"/>'
                   f'<path d="M24 8l4 4 8-9" fill="none" stroke="{HERO}" stroke-width="4.4" stroke-linecap="round" stroke-linejoin="round"/></g>')
    name_cls = "rowname hero" if hero else "rowname"
    svg.append(f'<text x="{lx}" y="{cy - 1}" class="{name_cls}">{name}</text>')
    svg.append(f'<text x="{lx}" y="{cy + 24}" class="rowsub">{sub}</text>')

    # whiskers + caps
    for a, b in ((lo, q1), (q3, hi)):
        svg.append(f'<line x1="{x(a):.1f}" y1="{cy}" x2="{x(b):.1f}" y2="{cy}" stroke="{col}" stroke-width="2"/>')
    for v in (lo, hi):
        svg.append(f'<line x1="{x(v):.1f}" y1="{cy - 9}" x2="{x(v):.1f}" y2="{cy + 9}" stroke="{col}" stroke-width="2"/>')
    # box
    svg.append(f'<rect x="{x(q1):.1f}" y="{cy - 17}" width="{max(2.5, x(q3) - x(q1)):.1f}" height="34" rx="4" fill="{col}" fill-opacity="0.16" stroke="{col}" stroke-width="2"/>')
    svg.append(f'<line x1="{x(med):.1f}" y1="{cy - 17}" x2="{x(med):.1f}" y2="{cy + 17}" stroke="{col}" stroke-width="3.5"/>')
    # p95 diamond
    px = x(st["p95"])
    svg.append(f'<path d="M{px:.1f} {cy - 9}L{px + 9:.1f} {cy}L{px:.1f} {cy + 9}L{px - 9:.1f} {cy}Z" fill="{col}" stroke="#ffffff" stroke-width="1.6"/>')

    # stat columns
    val_cls = "val hero" if hero else "val"
    for cx, v in zip(COLS_X, [f'{st["p50"]:.1f}&thinsp;s', f'{st["p95"]:.1f}&thinsp;s', f'{st["mx"]:.1f}&thinsp;s', f'{st["miss"]}/36']):
        svg.append(f'<text x="{cx}" y="{cy + 7}" text-anchor="end" class="{val_cls}">{v}</text>')

# annotation over the RunPod tail
tail_mid = (x(89) + x(122)) / 2
svg.append(f'<path d="M{x(89):.0f} {ROW0_Y - 30} v-8 H{x(122):.0f} v8" fill="none" stroke="{MUTED}" stroke-width="1.5"/>')
svg.append(f'<text x="{tail_mid:.0f}" y="{ROW0_Y - 48}" text-anchor="middle" class="anno">the cold-start tail — 11/36 rounds</text>')

SVG = f'<svg viewBox="0 0 {W} {SVG_H}" width="{W}" height="{SVG_H}" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" style="margin-top:26px">{"".join(svg)}</svg>'

HTML = f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;750&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  * {{ margin:0; box-sizing:border-box; }}
  body {{ width:{W}px; height:{H}px; background:#ffffff; color:{INK};
         font-family:Inter,system-ui,sans-serif; -webkit-font-smoothing:antialiased; }}
  .page {{ padding:{PAD - 8}px {PAD}px 0; }}
  .head {{ display:flex; justify-content:space-between; align-items:baseline; }}
  h1 {{ font-size:40px; font-weight:750; letter-spacing:-0.025em; }}
  h1 .accent {{ color:{HERO}; }}
  .sub {{ margin-top:10px; font-size:19px; color:{INK2}; }}
  .meta {{ font-size:15px; color:{MUTED}; font-weight:500; white-space:nowrap; }}
  .tick {{ font:600 14px 'JetBrains Mono',monospace; fill:{MUTED}; }}
  .axis {{ font:500 15px Inter,sans-serif; fill:{MUTED}; }}
  .deadline {{ font:600 14px Inter,sans-serif; fill:{DEADLINE}; opacity:0.85; }}
  .colhead {{ font:600 13px 'JetBrains Mono',monospace; fill:{MUTED}; letter-spacing:0.06em; }}
  .rowname {{ font:600 21px Inter,sans-serif; fill:{INK}; }}
  .rowname.hero {{ fill:{HERO}; font-weight:750; }}
  .rowsub {{ font:500 14.5px Inter,sans-serif; fill:{MUTED}; }}
  .val {{ font:500 17px 'JetBrains Mono',monospace; fill:{INK2}; }}
  .val.hero {{ fill:{INK}; font-weight:600; }}
  .anno {{ font:500 14.5px Inter,sans-serif; fill:{INK2}; }}
  .foot {{ display:flex; justify-content:space-between; margin-top:8px;
           font-size:14px; color:{MUTED}; }}
  .foot code {{ font-family:'JetBrains Mono',monospace; font-size:13px; color:{INK2}; }}
</style></head><body>
<div class="page">
  <div class="head">
    <div>
      <h1>Fast-path speed, <span class="accent">without the tail.</span></h1>
      <p class="sub">GPUHedge keeps RunPod&rsquo;s 6.0&thinsp;s median and cuts p95 cold-start latency 116.6&thinsp;s&nbsp;&rarr;&nbsp;21.9&thinsp;s.</p>
    </div>
    <div class="meta">36 paired rounds &middot; 17&thinsp;GB TTS model &middot; three real providers &middot; 2026-07</div>
  </div>
  {SVG}
  <div class="foot">
    <span>box = Q1&ndash;Q3 &middot; line = median &middot; whiskers = 1.5&times;IQR &middot; &#9670; = p95 &middot; same 36 requests replayed through every policy</span>
    <span>reproduce: <code>gpuhedge replay traces/moss_rounds.jsonl</code></span>
  </div>
</div>
</body></html>"""

out = ASSETS / "gpuhedge_boxplot.html"
out.write_text(HTML)
print("wrote", out)
print("render:  google-chrome --headless --hide-scrollbars "
      f"--window-size={W},{H} --force-device-scale-factor=2 "
      f"--screenshot={ASSETS / 'gpuhedge_boxplot.png'} {out}")
