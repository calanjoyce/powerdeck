#!/usr/bin/env python3
"""PowerDeck: quick power toggles for the M5 Cardputer Zero.

One screen of switches for the things that drain the battery (screen
brightness, CPU speed, Wi-Fi, Bluetooth, MeshZero, printing, the hidden
desktop), each showing the change in battery draw measured after you flip
it, and a second screen listing the busiest processes.

Changes last until the next restart.

Keys (main):  Fn+F / Fn+X or ; .   move        1-7  jump to a row and toggle
              Enter / Space        toggle      Fn+Z / Fn+C or , /  less / more
              Tab                  processes   Esc, Q  quit
Keys (procs): ; .  move   K twice  stop the selected process   Tab / Esc  back
"""
import json
import os
import pwd
import signal
import struct
import subprocess
import sys
import threading
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import numpy as np  # noqa: E402
import pygame  # noqa: E402

W, H = 320, 170
FB = os.environ.get("POWERDECK_FB", "/dev/fb0")
KBD = os.environ.get(
    "POWERDECK_KBD", "/dev/input/by-path/platform-3f804000.i2c-event")
DRY = os.environ.get("POWERDECK_DRY") == "1"   # tests: don't change anything
SUPPLY = "/sys/class/power_supply/bq27220-0"
BACKLIGHT = "/sys/class/backlight/backlight"
CPUFREQ = "/sys/devices/system/cpu/cpu0/cpufreq"

# Linux input key codes (Fn layer gives the arrows)
K_ESC, K_TAB, K_ENTER, K_SPACE, K_Q, K_K = 1, 15, 28, 57, 16, 37
K_UP, K_DOWN, K_LEFT, K_RIGHT = 103, 108, 105, 106
K_SEMI, K_DOT, K_COMMA, K_SLASH = 39, 52, 51, 53
K_DIGITS = {2 + i: i for i in range(9)}   # "1".."9" -> 0..8

BG = (12, 14, 18)
PANEL = (28, 32, 40)
SEL = (44, 52, 68)
TEXT = (232, 236, 242)
DIM = (140, 148, 162)
GREEN = (52, 211, 120)
AMBER = (245, 177, 52)
RED = (240, 82, 82)
BLUE = (80, 160, 255)
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"

GOVS = ["ondemand", "powersave", "performance"]
DESKTOP = ("wf-panel-pi", "pcmanfm-pi")
# processes the process screen will not stop
PROTECTED = {"M5CardputerZero", "labwc", "pipewire", "pipewire-pulse",
             "wireplumber", "systemd", "dbus-daemon", "sshd", "sshd-session",
             "sd-pam", "(sd-pam)", "meshzerod", "NetworkManager",
             "wpa_supplicant", "kanshi"}
INTERPRETERS = {"python3", "python", "sh", "bash", "perl", "node"}


def font(name, size):
    try:
        return pygame.font.Font(FONT_DIR + name, size)
    except (OSError, FileNotFoundError):
        return pygame.font.Font(None, size + 4)


def rd(path, default=None):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def run(cmd, timeout=15):
    """Run a command; return (ok, stdout). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode == 0, p.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False, ""


def sudo(*cmd):
    if DRY:
        return True, ""
    return run(["sudo", "-n", *cmd])


def battery_ua():
    """Signed battery current in µA (negative = discharging)."""
    v = rd(f"{SUPPLY}/current_now")
    try:
        ua = abs(int(v))
    except (TypeError, ValueError):
        return None
    return -ua if rd(f"{SUPPLY}/status") == "Discharging" else ua


# ---------- launcher settings (it restores its own brightness on wake) ----

LAUNCHER_CFG = os.path.expanduser("~/.config/cardputerzero/config.json")


def launcher_brightness():
    try:
        with open(LAUNCHER_CFG) as fh:
            return int(json.load(fh)["brightness"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_launcher_brightness(pct):
    try:
        with open(LAUNCHER_CFG) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return
    cfg["brightness"] = int(pct)
    tmp = LAUNCHER_CFG + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, LAUNCHER_CFG)
    except OSError:
        pass


# ---------- desktop extras (panel + desktop file manager) ----------
#
# labwc's autostart runs  lwrespawn /usr/bin/wf-panel-pi  and
# lwrespawn /usr/bin/pcmanfm-pi  (a wrapper that runs pcmanfm --desktop).
# Nothing shows on the Cardputer's screen, so they only use memory and CPU.
# Matching is exact (argv / comm) so nothing else is ever touched.

SESSION_ENV = ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DISPLAY",
               "DBUS_SESSION_BUS_ADDRESS", "WLR_XWAYLAND",
               "QT_WAYLAND_DISABLE_WINDOWDECORATION")


def argv_of(pid):
    raw = rd(f"/proc/{pid}/cmdline", "") or ""
    return [a for a in raw.split("\0") if a]


def desktop_pids():
    """(respawn loops, wrapper + programs) belonging to the desktop extras."""
    loops, progs = [], []
    me, uid = os.getpid(), os.getuid()
    for p in os.listdir("/proc"):
        if not p.isdigit() or int(p) == me:
            continue
        pid = int(p)
        try:
            if os.stat(f"/proc/{p}").st_uid != uid:
                continue
        except OSError:
            continue
        argv, comm = argv_of(pid), rd(f"/proc/{p}/comm", "")
        if comm == "lwrespawn" and argv[-1:] and \
                argv[-1] in [f"/usr/bin/{d}" for d in DESKTOP]:
            loops.append(pid)
        elif comm == "wf-panel-pi" or \
                (comm == "pcmanfm" and "--desktop" in argv) or \
                argv[:2] == ["/bin/bash", "/usr/bin/pcmanfm-pi"]:
            progs.append(pid)
    return loops, progs


def desktop_running():
    loops, progs = desktop_pids()
    return bool(loops or progs)


def session_env():
    """Display settings of the desktop session, taken from kanshi (labwc
    itself doesn't carry WAYLAND_DISPLAY)."""
    env = dict(os.environ)
    for name in ("kanshi", "labwc"):
        pids = run(["pidof", name])[1].split()
        if not pids:
            continue
        try:
            raw = open(f"/proc/{pids[0]}/environ", "rb").read()
        except OSError:
            continue
        found = {}
        for kv in raw.split(b"\0"):
            k, _, v = kv.decode(errors="replace").partition("=")
            if k in SESSION_ENV:
                found[k] = v
        if "WAYLAND_DISPLAY" in found:
            env.update(found)
            return env
    return None


def desktop_set(on):
    if DRY:
        return True
    loops, progs = desktop_pids()
    if not on:
        for pid in loops + progs:      # loops first so nothing respawns
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        return True
    env = session_env()
    if env is None:
        return False
    for pid in loops + progs:          # clear half-running leftovers
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(0.3)
    for prog in DESKTOP:
        subprocess.Popen(["/usr/bin/lwrespawn", f"/usr/bin/{prog}"], env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    return True


# ---------- toggles ----------

class Item:
    kind = "switch"          # or "level"
    confirm = None           # text shown before turning OFF

    def __init__(self):
        self.state = None    # last read state
        self.busy = False
        self.mark = None     # (t, baseline_ua) after a change
        self.delta = None    # measured change in mA

    def read(self):
        raise NotImplementedError

    def label(self):
        return "ON" if self.state else "OFF" if self.state is False else "--"


class Brightness(Item):
    name, hint, kind = "Brightness", "screen backlight", "level"

    def read(self):
        try:
            cur = int(rd(f"{BACKLIGHT}/brightness"))
            mx = int(rd(f"{BACKLIGHT}/max_brightness"))
        except (TypeError, ValueError):
            return launcher_brightness()
        # 0 means the launcher has put the screen to sleep
        return round(cur * 100 / mx) if cur else launcher_brightness()

    def label(self):
        return f"{self.state}%" if self.state is not None else "--"

    def step(self, d):
        cur = self.state if self.state is not None else 50
        new =(cur + 10 * d) // 10 * 10 if d > 0 else -(-(cur - 10) // 10) * 10
        self.set(max(5, min(100, new)))

    def toggle(self):
        presets = [20, 50, 100]
        cur = self.state if self.state is not None else 50
        self.set(next((p for p in presets if p > cur), presets[0]))

    def set(self, pct):
        mx = int(rd(f"{BACKLIGHT}/max_brightness", "100"))
        if not DRY:
            save_launcher_brightness(pct)
            try:
                with open(f"{BACKLIGHT}/brightness", "w") as fh:
                    fh.write(str(round(pct * mx / 100)))
            except OSError:
                sudo("sh", "-c", f"echo {round(pct * mx / 100)} > "
                     f"{BACKLIGHT}/brightness")
        self.state = pct


class Cpu(Item):
    name, hint, kind = "CPU speed", "governor", "level"

    def read(self):
        return rd(f"{CPUFREQ}/scaling_governor")

    def label(self):
        mhz = rd(f"{CPUFREQ}/scaling_cur_freq")
        f = f" {int(mhz) // 1000}MHz" if mhz and mhz.isdigit() else ""
        return f"{self.state or '--'}{f}"

    def step(self, d):
        i = GOVS.index(self.state) if self.state in GOVS else 0
        self.set(GOVS[(i + d) % len(GOVS)])

    def toggle(self):
        self.step(1)

    def set(self, gov):
        sudo("sh", "-c", "for f in /sys/devices/system/cpu/cpufreq/policy*/"
             f"scaling_governor; do echo {gov} > $f; done")
        self.state = gov if DRY else self.read()


class Service(Item):
    def __init__(self, name, hint, units, confirm=None):
        super().__init__()
        self.name, self.hint, self.units = name, hint, units
        self.confirm = confirm

    def read(self):
        ok, out = run(["systemctl", "is-active", self.units[0]], 5)
        return out == "active"

    def toggle(self):
        verb = "stop" if self.state else "start"
        sudo("systemctl", verb, *self.units)
        self.state = (not self.state) if DRY else self.read()


class Wifi(Item):
    name, hint = "Wi-Fi", "network (SSH needs it)"
    confirm = "Wi-Fi off: SSH and network drop"

    def read(self):
        ok, out = run(["nmcli", "radio", "wifi"], 5)
        return out == "enabled" if ok else None

    def toggle(self):
        if not DRY:
            ok, _ = run(["nmcli", "radio", "wifi",
                         "off" if self.state else "on"])
            if not ok:
                sudo("nmcli", "radio", "wifi", "off" if self.state else "on")
        self.state = (not self.state) if DRY else self.read()


class Bluetooth(Item):
    name, hint = "Bluetooth", "radio power"

    def read(self):
        ok, out = run(["bluetoothctl", "show"], 5)
        if not ok or "Powered:" not in out:
            return None
        return "Powered: yes" in out

    def toggle(self):
        arg = "off" if self.state else "on"
        if not DRY:
            ok, out = run(["bluetoothctl", "power", arg], 10)
            if not ok or "succeeded" not in out:
                sudo("bluetoothctl", "power", arg)
        self.state = (not self.state) if DRY else self.read()


class Desktop(Item):
    name, hint = "Desktop extras", "hidden panel + file manager"

    def read(self):
        return desktop_running()

    def toggle(self):
        desktop_set(not self.state)
        time.sleep(1.5)
        self.state = (not self.state) if DRY else self.read()


def make_items():
    return [
        Brightness(),
        Cpu(),
        Wifi(),
        Bluetooth(),
        Service("MeshZero", "LoRa radio + mesh API", ["meshzero"],
                "MeshZero off: no mesh messages"),
        Service("Printing", "CUPS print server",
                ["cups", "cups.socket", "cups.path", "cups-browsed"]),
        Desktop(),
    ]


# ---------- processes ----------

CLK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def proc_times():
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        st = rd(f"/proc/{pid}/stat")
        if not st:
            continue
        r = st.rfind(")")
        comm = st[st.find("(") + 1:r]
        if comm in INTERPRETERS:      # show the script, e.g. zsynth.py
            argv = (rd(f"/proc/{pid}/cmdline", "") or "").split("\0")
            script = next((a for a in argv[1:] if a and not
                           a.startswith("-")), "")
            if script:
                comm = os.path.basename(script)
        f = st[r + 2:].split()
        try:
            ticks = int(f[11]) + int(f[12])
            rss = int(f[21]) * PAGE
        except (IndexError, ValueError):
            continue
        out[int(pid)] = (comm, ticks, rss)
    return out


def top_procs(prev, cur, dt):
    rows = []
    me = os.getpid()
    for pid, (comm, ticks, rss) in cur.items():
        if rss == 0:          # kernel threads
            continue
        p = prev.get(pid)
        cpu = (ticks - p[1]) / CLK / dt * 100 if p and dt > 0 else 0.0
        try:
            uid = os.stat(f"/proc/{pid}").st_uid
            user = pwd.getpwuid(uid).pw_name
        except (OSError, KeyError):
            uid, user = -1, "?"
        rows.append({"pid": pid, "comm": comm, "cpu": cpu,
                     "mb": rss / 1048576, "uid": uid, "user": user,
                     "self": pid == me})
    rows.sort(key=lambda r: (-r["cpu"], -r["mb"]))
    return rows


def can_stop(r):
    return (r["uid"] == os.getuid() and not r["self"]
            and r["comm"] not in PROTECTED)


# ---------- app ----------

class App:
    def __init__(self):
        pygame.font.init()
        self.s = pygame.Surface((W, H))
        self.f_mid = font("DejaVuSans-Bold.ttf", 12)
        self.f_sm = font("DejaVuSans.ttf", 11)
        self.f_lbl = font("DejaVuSansCondensed.ttf", 10)
        self.items = make_items()
        self.sel = 0
        self.screen = "main"
        self.psel = 0
        self.procs = []
        self.pending = None      # (row index, deadline) awaiting 2nd press
        self.pkill = None        # (pid, deadline)
        self.msg = ("", 0)
        self.samples = []        # (t, ua)
        self.lock = threading.Lock()
        self.fb = os.open(FB, os.O_RDWR)
        try:
            self.kbd = os.open(KBD, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            print(f"powerdeck: no keyboard ({e})", file=sys.stderr)
            self.kbd = None
        self.running = True
        ua = battery_ua()
        if ua is not None:
            self.samples.append((time.time(), ua))
        for it in self.items:
            it.state = it.read()
        threading.Thread(target=self.poller, daemon=True).start()

    # ---------- background ----------
    def poller(self):
        prev, tprev = proc_times(), time.time()
        n = 0
        while self.running:
            time.sleep(1)
            ua = battery_ua()
            now = time.time()
            if ua is not None:
                with self.lock:
                    self.samples.append((now, ua))
                    self.samples = [s for s in self.samples
                                    if s[0] > now - 120]
            for it in self.items:
                self.update_delta(it, now)
            n += 1
            if n % 2 == 0:
                cur = time.time()
                pt = proc_times()
                self.procs = top_procs(prev, pt, cur - tprev)
                prev, tprev = pt, cur
            if n % 4 == 0:
                for it in self.items:
                    if not it.busy and it.kind == "switch":
                        it.state = it.read()

    def avg_ma(self, t0, t1):
        with self.lock:
            v = [ua for t, ua in self.samples if t0 <= t <= t1]
        return sum(v) / len(v) / 1000 if v else None

    def update_delta(self, it, now):
        if not it.mark:
            return
        t0, base = it.mark
        if base is None:           # changed right after start-up
            base = self.avg_ma(t0 - 15, t0)
            it.mark = (t0, base)
        if now - t0 < 6:
            return
        after = self.avg_ma(t0 + 5, now)
        if after is not None and base is not None:
            it.delta = after - base
        if now - t0 > 40:      # settled; stop updating
            it.mark = None

    def act(self, it, fn):
        if it.busy:
            return
        now = time.time()
        base = self.avg_ma(now - 15, now)

        def work():
            it.busy = True
            try:
                fn()
            finally:
                it.busy = False
                it.delta = None
                it.mark = (time.time(), base)
        if isinstance(it, Brightness):     # instant: keep up with key repeat
            work()
        else:
            threading.Thread(target=work, daemon=True).start()

    # ---------- input ----------
    def poll_keys(self):
        if self.kbd is None:
            return
        while True:
            try:
                data = os.read(self.kbd, 24 * 16)
            except (BlockingIOError, OSError):
                return
            if not data:
                return
            for i in range(0, len(data) - 23, 24):
                _, _, typ, code, val = struct.unpack_from("llHHi", data, i)
                if typ == 1 and val in (1, 2):
                    self.on_key(code, val == 2)

    def say(self, text, secs=3):
        self.msg = (text, time.time() + secs)

    def on_key(self, code, repeat=False):
        if self.screen == "procs":
            return self.on_key_procs(code, repeat)
        if repeat and code not in (K_LEFT, K_RIGHT, K_COMMA, K_SLASH,
                                   K_UP, K_DOWN, K_SEMI, K_DOT):
            return
        n = len(self.items)
        if code in (K_ESC, K_Q):
            self.running = False
        elif code == K_TAB:
            self.screen, self.psel = "procs", 0
        elif code in (K_UP, K_SEMI):
            self.sel = (self.sel - 1) % n
        elif code in (K_DOWN, K_DOT):
            self.sel = (self.sel + 1) % n
        elif code in (K_LEFT, K_COMMA, K_RIGHT, K_SLASH):
            it = self.items[self.sel]
            d = 1 if code in (K_RIGHT, K_SLASH) else -1
            if it.kind == "level":
                self.act(it, lambda: it.step(d))
            elif (d > 0) != bool(it.state):
                self.toggle(self.sel)
        elif code in (K_ENTER, K_SPACE):
            self.toggle(self.sel)
        elif code in K_DIGITS and K_DIGITS[code] < n:
            self.sel = K_DIGITS[code]
            self.toggle(self.sel)

    def toggle(self, i):
        it = self.items[i]
        if it.kind == "switch" and it.state and it.confirm:
            if not (self.pending and self.pending[0] == i
                    and time.time() < self.pending[1]):
                self.pending = (i, time.time() + 3)
                self.say(f"{it.confirm} - press again")
                return
        self.pending = None
        self.say("")
        self.act(it, it.toggle)

    def on_key_procs(self, code, repeat):
        rows = self.procs[:8]
        if code in (K_ESC, K_TAB, K_Q) and not repeat:
            self.screen = "main"
        elif code in (K_UP, K_SEMI):
            self.psel = max(0, self.psel - 1)
        elif code in (K_DOWN, K_DOT):
            self.psel = min(max(0, len(rows) - 1), self.psel + 1)
        elif code == K_K and not repeat and rows:
            r = rows[min(self.psel, len(rows) - 1)]
            if not can_stop(r):
                self.say(f"{r['comm']} is a system process - not stopping it")
            elif self.pkill and self.pkill[0] == r["pid"] and \
                    time.time() < self.pkill[1]:
                self.pkill = None
                try:
                    if not DRY:
                        os.kill(r["pid"], signal.SIGTERM)
                    self.say(f"stopped {r['comm']} ({r['pid']})")
                except OSError as e:
                    self.say(f"couldn't stop it: {e.strerror}")
            else:
                self.pkill = (r["pid"], time.time() + 3)
                self.say(f"press K again to stop {r['comm']}")

    # ---------- drawing ----------
    def text(self, f, s, color, pos, anchor="topleft"):
        img = f.render(s, True, color)
        r = img.get_rect(**{anchor: pos})
        self.s.blit(img, r)
        return r

    def draw_header(self, title):
        now = time.time()
        pct = rd(f"{SUPPLY}/capacity", "--")
        st = rd(f"{SUPPLY}/status", "")
        ma = self.avg_ma(now - 5, now)
        uv = rd(f"{SUPPLY}/voltage_now")
        pygame.draw.rect(self.s, PANEL, (0, 0, W, 20))
        self.text(self.f_mid, title, TEXT, (6, 3))
        parts = [f"{pct}%"]
        if ma is not None:
            parts.append(f"{ma:+.0f} mA")
            if uv and uv.isdigit():
                parts.append(f"{int(uv) / 1e6 * ma / 1000:+.2f} W")
        col = GREEN if st == "Charging" else AMBER if ma and ma < -500 \
            else TEXT
        self.text(self.f_sm, "  ".join(parts), col, (W - 6, 4), "topright")

    def draw_main(self):
        self.draw_header("PowerDeck")
        y0, rh = 23, 18
        for i, it in enumerate(self.items):
            y = y0 + i * rh
            if i == self.sel:
                pygame.draw.rect(self.s, SEL, (2, y, W - 4, rh - 1),
                                 border_radius=3)
            self.text(self.f_lbl, str(i + 1), DIM, (8, y + 3))
            self.text(self.f_mid, it.name, TEXT, (20, y + 2))
            # value
            if it.busy:
                val, col = "...", AMBER
            elif it.kind == "level":
                val, col = it.label(), BLUE
            else:
                val = it.label()
                col = GREEN if it.state else DIM if it.state is False \
                    else AMBER
            vx = 262
            self.val_x = vx - self.f_mid.size(val)[0]
            if isinstance(it, Brightness) and it.state is not None:
                bx, bw = 130, 80
                pygame.draw.rect(self.s, PANEL, (bx, y + 5, bw, 7),
                                 border_radius=3)
                pygame.draw.rect(self.s, BLUE, (bx, y + 5,
                                                max(3, bw * it.state // 100),
                                                7), border_radius=3)
            self.text(self.f_mid, val, col, (vx, y + 2), "topright")
            if i == self.sel and not isinstance(it, Brightness):
                self.draw_hint(it.hint, 20 + self.f_mid.size(it.name)[0]
                               + 8, self.val_x - 8, y + 3)
            # measured change
            if it.mark and it.delta is None:
                self.text(self.f_lbl, "meas…", DIM, (W - 6, y + 3),
                          "topright")
            elif it.delta is not None:
                d = it.delta
                dcol = GREEN if d > 5 else RED if d < -5 else DIM
                # current is negative when discharging: +d = less drain
                self.text(self.f_lbl, f"{-d:+.0f}mA", dcol, (W - 6, y + 3),
                          "topright")
        self.draw_footer("1-7 toggle  ;. move  ,/ adjust  Tab procs  Esc")

    def draw_procs(self):
        self.draw_header("Processes")
        y0, rh = 34, 14
        self.text(self.f_lbl, "NAME", DIM, (8, 22))
        self.text(self.f_lbl, "CPU", DIM, (200, 22), "topright")
        self.text(self.f_lbl, "MEM", DIM, (248, 22), "topright")
        self.text(self.f_lbl, "USER", DIM, (256, 22))
        rows = self.procs[:8]
        self.psel = min(self.psel, max(0, len(rows) - 1))
        for i, r in enumerate(rows):
            y = y0 + i * rh
            if i == self.psel:
                pygame.draw.rect(self.s, SEL, (2, y, W - 4, rh),
                                 border_radius=3)
            col = TEXT if can_stop(r) else DIM
            name = r["comm"][:24] + (" (this)" if r["self"] else "")
            self.text(self.f_sm, name, col, (8, y + 1))
            ccol = RED if r["cpu"] > 25 else AMBER if r["cpu"] > 5 else col
            self.text(self.f_sm, f"{r['cpu']:.0f}%", ccol, (200, y + 1),
                      "topright")
            self.text(self.f_sm, f"{r['mb']:.0f}M", col, (248, y + 1),
                      "topright")
            self.text(self.f_lbl, r["user"][:9], DIM, (256, y + 2))
        if not rows:
            self.text(self.f_sm, "measuring…", DIM, (W // 2, 90), "center")
        self.draw_footer(";. move   K K stop   Tab back")

    def draw_hint(self, s, x0, x1, y):
        """Row description, shortened to fit between name and value."""
        while s and self.f_lbl.size(s)[0] > x1 - x0:
            s = s[:-2] + "…" if len(s) > 2 else ""
        if s:
            self.text(self.f_lbl, s, DIM, (x0, y))

    def draw_footer(self, hint):
        text, until = self.msg
        if text and time.time() < until:
            pygame.draw.rect(self.s, PANEL, (0, H - 15, W, 15))
            self.text(self.f_lbl, text, AMBER, (W // 2, H - 2), "midbottom")
        else:
            self.text(self.f_lbl, hint, DIM, (W // 2, H - 2), "midbottom")

    def render(self):
        self.s.fill(BG)
        if self.screen == "main":
            self.draw_main()
        else:
            self.draw_procs()
        self.flush()

    def flush(self):
        a = pygame.surfarray.pixels3d(self.s)
        r = a[:, :, 0].astype(np.uint16)
        g = a[:, :, 1].astype(np.uint16)
        b = a[:, :, 2].astype(np.uint16)
        del a
        px = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        os.pwrite(self.fb, px.T.astype("<u2").tobytes(), 0)

    def run(self):
        next_draw = 0
        while self.running:
            self.poll_keys()
            if time.time() >= next_draw:
                self.render()
                next_draw = time.time() + 0.5
            time.sleep(0.04)
        self.s.fill((0, 0, 0))
        self.flush()


def main():
    app = App()

    def stop(*_):
        app.running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    app.run()


if __name__ == "__main__":
    main()
