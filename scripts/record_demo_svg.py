"""Regenerate assets/gpuhedge_demo.svg from a REAL terminal session.

Runs the actual CLI commands (`gpuhedge login-check`, `gpuhedge demo`) in a
pty and records their genuine output as an asciicast v2 file. Only the shell
prompt + keystroke typing is staged; every output byte comes from the real
CLI. Inter-chunk gaps are compressed (speed factor + idle cap) so viewers
don't wait on network calls or the simulated race.

Usage:
    python scripts/record_demo_svg.py assets/gpuhedge_demo.cast
    npx svg-term-cli --in assets/gpuhedge_demo.cast \
        --out assets/gpuhedge_demo.svg --window --width 88 --height 18 --padding 14

Requires all three providers to be authenticated (the login-check output is
recorded live) and `gpuhedge` on PATH (or set GPUHEDGE_BIN).
"""
import fcntl
import json
import os
import random
import select
import shutil
import struct
import subprocess
import sys
import termios
import time

COLS, ROWS = 88, 18
OUT = sys.argv[1] if len(sys.argv) > 1 else "assets/gpuhedge_demo.cast"
GH = os.environ.get("GPUHEDGE_BIN") or shutil.which("gpuhedge") or "gpuhedge"

PROMPT = "\x1b[1;32m$\x1b[0m "

events = []  # (t, data)
clock = [0.0]


def emit(dt, data):
    clock[0] += dt
    events.append((clock[0], data))


def type_command(cmd):
    emit(0.0, PROMPT)
    for ch in cmd:
        emit(random.uniform(0.03, 0.09), ch)
    emit(random.uniform(0.25, 0.4), "\r\n")


def run_real(cmd, speed=1.0, idle_cap=1.0):
    """Run cmd in a pty sized COLSxROWS; append its real output events."""
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor",
               COLUMNS=str(COLS), LINES=str(ROWS))
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave,
                            close_fds=True, env=env)
    os.close(slave)
    last = time.monotonic()
    while True:
        r, _, _ = select.select([master], [], [], 0.25)
        if r:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            now = time.monotonic()
            gap = min((now - last) * speed, idle_cap)
            last = now
            emit(gap, data.decode("utf-8", "replace"))
        elif proc.poll() is not None:
            while True:
                r2, _, _ = select.select([master], [], [], 0.05)
                if not r2:
                    break
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                emit(0.02, data.decode("utf-8", "replace"))
            break
    os.close(master)
    proc.wait()


type_command("gpuhedge login-check")
run_real([GH, "login-check"], speed=0.45, idle_cap=0.8)
emit(0.9, "\r\n")

type_command("gpuhedge demo --requests 4")
run_real([GH, "demo", "--requests", "4"], speed=0.55, idle_cap=1.1)
emit(0.5, "\x1b[?25l")  # hide cursor for the hold
emit(2.8, "\x1b[?25h")  # hold the final frame before the loop restarts

cast = {"version": 2, "width": COLS, "height": ROWS, "title": "gpuhedge demo"}
with open(OUT, "w") as f:
    f.write(json.dumps(cast) + "\n")
    for t, data in events:
        f.write(json.dumps([round(t, 4), "o", data]) + "\n")
print(f"wrote {OUT}: {len(events)} events, {events[-1][0]:.1f}s duration")
