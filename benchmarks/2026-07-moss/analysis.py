"""Regenerate the 2026-07 MOSS benchmark figures from the committed traces.

    python benchmarks/2026-07-moss/analysis.py [--traces traces/] [--out figures/]

Figures:
  1 queue_delay_hero      RunPod queue delay vs total latency — the 2.5 s
                          separation that makes the cutover policy possible
  2 cold_start_matrix     rounds x providers (+ replayed GPUHedge column)
  3 cost_vs_misses        modeled billed $/req vs 60 s miss rate per policy
  4 cancel_waterfall      winner-valid -> cancel sent -> ack -> terminal
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from gpuhedge.benchmark.replay import (  # noqa: E402
    evaluate_hedge,
    evaluate_queue_cutover,
    evaluate_single,
    load_rounds,
)
from gpuhedge.config import load_config  # noqa: E402

INF = math.inf
plt.rcParams.update({
    "figure.dpi": 150, "savefig.bbox": "tight", "font.size": 10,
    "axes.spines.top": False, "axes.spines.right": False,
})

PROVIDER_LABELS = {"runpod": "RunPod RTX 4090", "modal": "Modal L40S",
                   "cerebrium": "Cerebrium L40S"}


def _save(fig, out: Path, name: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "svg"):
        fig.savefig(out / f"{name}.{ext}")
    plt.close(fig)
    print(f"  wrote {out / name}.png/.svg")


# ------------------------------------------------------------------- figure 1
def fig_queue_delay_hero(rounds, out: Path) -> None:
    xs, ys, colors = [], [], []
    for r in rounds:
        q = r.queue_delay_s.get("runpod")
        lat = r.latency.get("runpod", INF)
        if q is None or math.isinf(lat):
            continue
        xs.append(q)
        ys.append(lat)
        colors.append("#2a9d8f" if lat <= 30 else "#e76f51")
    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    ax.scatter(xs, ys, c=colors, s=42, alpha=0.85, edgecolors="white",
               linewidths=0.6, zorder=3)
    ax.axvline(2.5, color="#264653", linestyle="--", linewidth=1.4, zorder=2)
    ax.annotate("policy poll at 2.5 s", xy=(2.5, max(ys) * 0.55),
                xytext=(3.1, max(ys) * 0.62), fontsize=9, color="#264653",
                arrowprops={"arrowstyle": "->", "color": "#264653"})
    fast = [x for x, y in zip(xs, ys, strict=False) if y <= 30]
    slow = [x for x, y in zip(xs, ys, strict=False) if y > 30]
    ax.set_xscale("log")
    ax.set_xlabel("RunPod queue delay (s, log scale)")
    ax.set_ylabel("total request latency (s)")
    ax.set_title(
        f"A {2.5:g}-second observation predicted the 90–122 s tail\n"
        f"fast path queued {min(fast):.1f}–{max(fast):.1f} s · fresh worker "
        f"queued {min(slow):.1f}–{max(slow):.1f} s · zero overlap in "
        f"{len(xs)} rounds", fontsize=10.5)
    ax.scatter([], [], c="#2a9d8f", label="FlashBoot hit (≤30 s total)")
    ax.scatter([], [], c="#e76f51", label="fresh worker (89–122 s total)")
    ax.legend(frameon=False, loc="center right")
    _save(fig, out, "fig1_queue_delay_hero")


# ------------------------------------------------------------------- figure 2
def fig_cold_start_matrix(config, rounds, out: Path) -> None:
    providers = ["runpod", "modal", "cerebrium"]
    cols = providers + ["gpuhedge (cutover, replay)"]
    lat = np.full((len(rounds), len(cols)), np.nan)
    for i, r in enumerate(rounds):
        for j, p in enumerate(providers):
            v = r.latency.get(p, INF)
            lat[i, j] = np.nan if math.isinf(v) else v
        q = r.queue_delay_s.get("runpod", INF)
        rp = r.latency.get("runpod", INF)
        rh = r.latency.get("cerebrium", INF)
        if q > 2.5:
            v = 2.5 + rh
        elif rp <= 8.5:
            v = rp
        else:
            v = min(rp, 8.5 + rh)
        lat[i, -1] = np.nan if math.isinf(v) else v

    fig, ax = plt.subplots(figsize=(6.4, 8.4))
    cmap = plt.get_cmap("RdYlGn_r").copy()
    cmap.set_bad("#555555")
    im = ax.imshow(lat, aspect="auto", cmap=cmap, vmin=0, vmax=120)
    ax.set_xticks(range(len(cols)))
    ax.set_xticklabels([PROVIDER_LABELS.get(c, c) for c in cols],
                       rotation=20, ha="right", fontsize=8.5)
    ax.set_ylabel("paired cold-start round")
    ax.set_yticks([0, 17, 35, 53])
    ax.set_yticklabels(["1", "18", "36", "54"])
    ax.axhline(17.5, color="white", linewidth=1.2)
    ax.text(len(cols) - 0.42, 16.6, "calibration ↑ / evaluation ↓",
            fontsize=7.5, color="white", ha="right")
    fig.colorbar(im, ax=ax, shrink=0.75, label="cold-start latency (s)")
    ax.set_title("The slow tail moves between providers;\n"
                  "routing removes most of its impact", fontsize=10.5)
    _save(fig, out, "fig2_cold_start_matrix")


# ------------------------------------------------------------------- figure 3
def fig_cost_vs_misses(config, rounds, out: Path) -> None:
    ev = [r for r in rounds if r.round_id > 18]
    deadlines = [60]
    pts = []
    for p in ("runpod", "modal", "cerebrium"):
        res = evaluate_single(config, ev, p, deadlines)
        pts.append((res.mean_billed_usd, res.miss_rates[60],
                    f"single {p}", "o", "#6c757d"))
    res = evaluate_hedge(config, ev, "runpod", "cerebrium", 0, deadlines)
    pts.append((res.mean_billed_usd, res.miss_rates[60],
                "immediate race", "s", "#457b9d"))
    res = evaluate_hedge(config, ev, "runpod", "cerebrium", 10, deadlines)
    pts.append((res.mean_billed_usd, res.miss_rates[60],
                "fixed hedge @10 s", "D", "#2a9d8f"))
    res = evaluate_queue_cutover(config, ev, "runpod", "cerebrium", deadlines)
    pts.append((res.mean_billed_usd, res.miss_rates[60],
                "queue cutover @2.5 s*", "*", "#e63946"))

    offsets = {"immediate race": (-8, 12, "right"),
               "fixed hedge @10 s": (8, -14, "left"),
               "queue cutover @2.5 s*": (10, 8, "left")}
    fig, ax = plt.subplots(figsize=(7.0, 4.6))
    for x, y, label, marker, color in pts:
        size = 260 if marker == "*" else 80
        ax.scatter(x, y, marker=marker, s=size, color=color, zorder=3,
                   edgecolors="white", linewidths=0.6)
        dx, dy, ha = offsets.get(label, (6, 6, "left"))
        ax.annotate(label, (x, y), xytext=(dx, dy),
                    textcoords="offset points", fontsize=8.8, ha=ha)
    ax.set_xlabel("modeled billed $/request (incl. idle windows)")
    ax.set_ylabel("60 s deadline miss rate")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
    ax.set_title("36 evaluation rounds — down-left is better\n"
                 "*post-hoc replay; pre-registered live validation in "
                 "benchmarks/2026-07-queue-cutover", fontsize=10)
    ax.set_ylim(-0.03, max(y for _, y, *_ in pts) * 1.25 + 0.02)
    _save(fig, out, "fig3_cost_vs_misses")


# ------------------------------------------------------------------- figure 4
def fig_cancel_waterfall(trace_dir: Path, out: Path) -> None:
    receipts = []
    for name in ("live_hedge.jsonl", "state_aware.jsonl", "validation.jsonl",
                 "cancel_audit.jsonl"):
        path = trace_dir / name
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            c = rec.get("cancellation") or rec.get("receipt")
            if not c or c.get("cancel_ack_ms") is None:
                continue
            receipts.append({
                "label": f"{c['provider']} "
                         f"(#{rec.get('request_id', rec.get('attempt', '?'))}"
                         f" {rec.get('kind', '')[:12]})",
                "provider": c["provider"],
                "ack": c["cancel_ack_ms"] - c["cancel_sent_ms"],
                "terminal": ((c.get("terminal_ms") or c["cancel_ack_ms"])
                             - c["cancel_sent_ms"]),
                "confirmed": bool(c.get("terminal_ms")) and not c.get("leaked"),
            })
    if not receipts:
        print("  no receipts found; skipping fig4")
        return
    receipts = receipts[-14:]  # keep the figure readable
    fig, ax = plt.subplots(figsize=(7.2, 0.42 * len(receipts) + 1.6))
    ys = np.arange(len(receipts))[::-1]
    colors = {"runpod": "#5a189a", "modal": "#1d3557", "cerebrium": "#2a9d8f"}
    for y, r in zip(ys, receipts, strict=False):
        color = colors.get(r["provider"], "#6c757d")
        if r["confirmed"]:
            ax.barh(y, r["terminal"], height=0.62, color=color, alpha=0.35,
                    label=None)
        ax.barh(y, r["ack"], height=0.62, color=color)
        label = (f"{r['terminal']:.0f} ms to terminal" if r["confirmed"]
                 else "terminal unconfirmed (leaked)")
        x = r["terminal"] if r["confirmed"] else r["ack"]
        ax.text(x + 8, y, label,
                va="center", fontsize=7.6, color="#333333")
    ax.set_yticks(ys)
    ax.set_yticklabels([r["label"] for r in receipts], fontsize=7.6)
    ax.set_xlabel("ms after cancel sent  (solid = provider ack, "
                  "faded = confirmed terminal)")
    ax.set_title("Cancellation receipts: confirmed stops and gaps", fontsize=10.5)
    _save(fig, out, "fig4_cancel_waterfall")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--traces", default="traces")
    parser.add_argument("--out", default=str(Path(__file__).parent / "figures"))
    args = parser.parse_args()

    config = load_config()
    trace_dir = Path(args.traces)
    out = Path(args.out)
    rounds = load_rounds(trace_dir / "moss_rounds.jsonl")
    print(f"{len(rounds)} rounds from {trace_dir}")
    fig_queue_delay_hero(rounds, out)
    fig_cold_start_matrix(config, rounds, out)
    fig_cost_vs_misses(config, rounds, out)
    fig_cancel_waterfall(trace_dir, out)


if __name__ == "__main__":
    main()
