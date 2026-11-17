#!/usr/bin/env python3
"""Headless check: drive PowerDeck with fake key presses and dump frames.

Run on the device:  python3 test_ui.py /tmp/pd   (writes /tmp/pd-*.raw)
Runs in dry-run mode, so nothing is actually switched, and draws into a file
instead of the LCD.
"""
import os
import sys
import time

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/pd"
os.environ["POWERDECK_FB"] = out + "-fb.raw"
os.environ["POWERDECK_KBD"] = "/nonexistent"
os.environ["POWERDECK_DRY"] = "1"
open(os.environ["POWERDECK_FB"], "wb").write(b"\0" * 320 * 170 * 2)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import powerdeck as pd  # noqa: E402

app = pd.App()
results = []


def check(name, ok):
    results.append((name, bool(ok)))


def snap(name):
    app.render()
    with open(os.environ["POWERDECK_FB"], "rb") as f, \
            open(f"{out}-{name}.raw", "wb") as g:
        g.write(f.read())


def wait_idle(it, secs=3):
    t = time.time() + secs
    while it.busy and time.time() < t:
        time.sleep(0.05)
    time.sleep(0.05)


names = [it.name for it in app.items]
check(f"rows {names}", len(names) == 7)
states = {it.name: it.state for it in app.items}
print("initial:", states)
check("every row has a state", all(v is not None for v in states.values()))

bri = app.items[0]
b0 = bri.state
app.on_key(pd.K_SLASH)
wait_idle(bri)
check(f"brightness up {b0} -> {bri.state}", bri.state == min(100, (b0 + 10)
                                                               // 10 * 10))
app.on_key(pd.K_COMMA)
app.on_key(pd.K_COMMA)
wait_idle(bri)
check(f"brightness down -> {bri.state}", bri.state < b0)

cpu = app.items[1]
app.on_key(pd.K_DOT)
check("; . moves the selection", app.sel == 1)
g0 = cpu.state
app.on_key(pd.K_ENTER)
wait_idle(cpu)
check(f"CPU cycles {g0} -> {cpu.state}", cpu.state != g0 and
      cpu.state in pd.GOVS)

wifi = app.items[2]
app.on_key(3 + 1)                   # key "3"
check("Wi-Fi off asks first", app.pending and app.pending[0] == 2
      and wifi.state is True)
snap("confirm")
app.on_key(3 + 1)
wait_idle(wifi)
check("second press turns Wi-Fi off", wifi.state is False)

mesh = app.items[4]
app.on_key(5 + 1)                   # key "5": MeshZero needs confirming
app.on_key(5 + 1)
wait_idle(mesh)
check("MeshZero toggles after confirm", mesh.state is False)

app.on_key(7 + 1)                   # key "7": desktop extras, no confirm
wait_idle(app.items[6], 3)
check("desktop extras toggles at once", app.items[6].state is False)

time.sleep(8)
check("measured change shows after a toggle",
      any(it.delta is not None for it in app.items))
snap("main")

app.on_key(pd.K_TAB)
time.sleep(2.5)
check("process screen lists processes", app.screen == "procs" and
      len(app.procs) > 5)
sys_row = next(i for i, r in enumerate(app.procs[:8])
               if not pd.can_stop(r))
app.psel = sys_row
app.on_key(pd.K_K)
check("won't stop system processes", "system process" in app.msg[0])
snap("procs")
app.on_key(pd.K_TAB)
check("Tab returns", app.screen == "main")

app.running = False
for name, ok in results:
    print("PASS" if ok else "FAIL", name)
print(f"{sum(ok for _, ok in results)}/{len(results)} passed")
