#!/usr/bin/env python3
"""fanctl - terminal UI to control ThinkPad fan levels (thinkpad_acpi).

Run:  python3 fanctl.py      (asks for sudo itself)

Keys:
  <- / ->  or  h / l      lower / raise fan level
  Up / Down or k / j      same (also +/-)
  0-4                     jump to that level
  a                       auto (firmware control)
  f                       full speed (very loud)
  q / Esc                 quit (fan goes back to auto)

Levels: AUTO, 0 (off), 1-4, MAX. (The hardware also has levels 5-7, but 4-7
behave the same on this laptop, so they are not offered.)

An animated ASCII fan spins at a speed that follows the real RPM, coloured by
level: AUTO green, 0 blue, 1 teal, 2 green, 3 yellow, 4 orange, MAX rainbow.
It needs a terminal about 76 columns wide (small fan) or 88x20 (large fan);
on narrower terminals it is hidden.

Safety:
  * At >= 90 C the fan is forced back to AUTO and manual levels are refused
    until the CPU cools below 85 C.
  * The driver watchdog is fed every 30 s, so if this program dies or hangs
    the firmware reclaims the fan within two minutes.
  * On quit, SIGTERM, SIGHUP or Ctrl-C the fan is returned to AUTO.
"""
import curses
import errno
import glob
import locale
import math
import os
import re
import signal
import sys
import time

FAN = os.environ.get("FAN_PATH", "/proc/acpi/ibm/fan")

_last_level = "auto"   # the last level fanctl successfully applied

MAX_LEVEL = 4          # highest numbered level offered (5-7 act like 4)
OPTIONS = ["auto", "0", "1", "2", "3", "4", "full-speed"]
LABELS = ["AUTO", "0", "1", "2", "3", "4", "MAX"]
COMPACT_LABELS = ["A", "0", "1", "2", "3", "4", "F"]
STAIRS = ["⟳", "▁", "▂", "▃", "▅", "▆", "█"]   # one per OPTIONS entry

DANGER_TEMP = 90.0    # at/above this: force AUTO and refuse manual levels
RESUME_TEMP = 85.0    # manual control is allowed again below this
REASSERT_EVERY = 30.0 # seconds between watchdog feeds
WATCHDOG_SECS = 120   # firmware reclaims the fan this long after we vanish
MSG_TTL = 6.0
CONTENT_W = 54        # width of the dashboard column
FRAME_DELAY_MS = 40   # animation frame time (~25 fps)
POLL_EVERY = 0.5      # seconds between reads of the fan / temperature
MAX_RPM = 5500        # RPM that counts as "full tilt" for the bars + fan art
MAX_VIS_RPS = 2.0     # visual revolutions/sec at full tilt (low enough that
                      # it never aliases at ~25 fps)
MIN_W, MIN_H = 34, 8  # below this we only show "terminal too small"

CPU_SENSORS = ("k10temp", "coretemp", "zenpower")

# ------------------------------------------------------------------- colours
# Fan colour per level 0-4: blue, teal, green, yellow, orange (xterm-256
# numbers), with a plain 8-colour fallback for simple terminals.
LEVEL_COLORS_256 = {"0": 33, "1": 37, "2": 40, "3": 226, "4": 208}
LEVEL_COLORS_8 = {"0": curses.COLOR_BLUE, "1": curses.COLOR_CYAN,
                  "2": curses.COLOR_GREEN, "3": curses.COLOR_YELLOW,
                  "4": curses.COLOR_RED}
# MAX: a rainbow that swirls around the fan.
RAINBOW_256 = [196, 208, 226, 118, 46, 49, 51, 39, 27, 93, 201, 199]
RAINBOW_8 = [curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_GREEN,
             curses.COLOR_CYAN, curses.COLOR_BLUE, curses.COLOR_MAGENTA]
RAINBOW_HZ = 0.35     # full colour rotations per second
LEVEL_PAIR0 = 6       # curses colour-pair numbers (1-5 are the base pairs)
RAINBOW_PAIR0 = 11

BLADES = 5
RAMP = " .:-=+*#%@"
FAN_SIZES = {
    "large": {"w": 27, "h": 13, "R": 6.0},
    "small": {"w": 15, "h": 7, "R": 3.0},
}


# --------------------------------------------------------------------- levels
def canon_level(level):
    """Normalize the different spellings the driver uses."""
    s = (level or "").strip().lower()
    if s in ("full-speed", "fullspeed", "disengaged", "max"):
        return "full-speed"
    if s in ("", "auto"):
        return "auto"
    if s.isdigit():                  # hardware 6 and 7 show as the top level
        return str(min(int(s), MAX_LEVEL))
    return s


def level_index(info):
    if not info:
        return 0
    lvl = canon_level(info["level"])
    return OPTIONS.index(lvl) if lvl in OPTIONS else 0


# ------------------------------------------------------------------ fan art
def pick_fan_size(w, h):
    if w >= 88 and h >= 20:
        return "large"
    if w >= 76 and h >= 16:
        return "small"
    return None


def visual_rps(rpm):
    """Map real RPM to how fast the drawn fan spins (revolutions/second)."""
    if rpm <= 0:
        return 0.0
    return min(MAX_VIS_RPS, MAX_VIS_RPS * (rpm / MAX_RPM) ** 0.7)


def ring_char(theta):
    a = abs(theta)
    if a < math.pi / 8 or a > 7 * math.pi / 8:
        return "|"
    if 3 * math.pi / 8 <= a <= 5 * math.pi / 8:
        return "-"
    right = a < math.pi / 2
    below = theta > 0
    return "/" if right == below else "\\"


def fan_frame(size, phase, blur):
    """Rows of (char, kind, intensity, hue).

    kind is blade/hub/ring/''. hue (0..1) is a position-based swirl value
    used to colour the fan when it runs at MAX.
    """
    s = FAN_SIZES[size]
    W, H, R = s["w"], s["h"], s["R"]
    cx, cy = (W - 1) / 2.0, (H - 1) / 2.0
    period = 2 * math.pi / BLADES
    width = period * (0.40 + 0.55 * blur)   # blades smear as speed increases
    hub_r = R * 0.22

    rows = []
    for y in range(H):
        row = []
        for x in range(W):
            dx = (x - cx) / 2.0              # cells are ~2x taller than wide
            dy = y - cy
            r = math.hypot(dx, dy)
            theta = math.atan2(dy, dx)
            hue = (theta / (2 * math.pi) + 0.5 + 0.35 * r / R) % 1.0
            if r <= hub_r:
                row.append(("O" if r < 0.3 else "@", "hub", 9, hue))
            elif R <= r < R + 0.9:
                row.append((ring_char(theta), "ring", 0, hue))
            elif r < R - 0.4:
                sweep = 0.9 * (r / R)        # curved blades
                a = (theta - phase - sweep) % period
                if a < width:
                    t = a / width
                    idx = max(0, min(9, round((1 - blur) * 8 + blur * t * 9)))
                    row.append((RAMP[idx], "blade", idx, hue))
                else:
                    row.append((" ", "", 0, hue))
            else:
                row.append((" ", "", 0, hue))
        rows.append(row)
    return rows


# --------------------------------------------------------------- fan access
def read_fan():
    """Parse the fan file. None if it can't be read or holds no fan data."""
    info = {"status": "?", "speed": 0, "level": "?"}
    parsed = False
    try:
        with open(FAN) as f:
            for line in f:
                if ":" not in line:
                    continue
                k, v = (p.strip() for p in line.split(":", 1))
                if k == "speed":
                    m = re.search(r"\d+", v)      # tolerate odd suffixes
                    info["speed"] = int(m.group()) if m else 0
                    parsed = True
                elif k in ("status", "level"):
                    info[k] = v
                    parsed = True
    except OSError:
        return None
    return info if parsed else None


def write_fan(cmd):
    """Returns None on success, or the OSError."""
    try:
        with open(FAN, "w") as f:
            f.write(cmd)
        return None
    except OSError as exc:
        return exc


def set_auto(prev_level):
    """Hand the fan back to firmware control. Returns None on success.

    Some EC firmware (a T14 Gen 1 AMD does this) keeps running the fan at
    full duty after full-speed until a manual level is written, so leave
    disengaged mode with a plain level before asking for auto.
    """
    if canon_level(prev_level) == "full-speed":
        write_fan(f"level {MAX_LEVEL}")
    err = write_fan("level auto")
    write_fan("watchdog 0")
    return err


def explain(exc, info):
    """Turn a write failure into something a human can act on."""
    code = getattr(exc, "errno", None)
    if code in (errno.EACCES, errno.EPERM):
        return "permission denied - run as root"
    if info and info["status"] not in ("enabled", "?"):
        return "fan control off (needs fan_control=1)"
    if code in (errno.ENOENT, errno.ENODEV):
        return "no fan interface - is thinkpad_acpi loaded?"
    if code == errno.EINVAL:
        return "the driver rejected that command"
    return getattr(exc, "strerror", None) or str(exc)


# ------------------------------------------------------------- temperature
_temp_path = None


def _read_millideg(path):
    try:
        with open(path) as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def _temp_index(path):
    m = re.search(r"temp(\d+)_input$", path)
    return int(m.group(1)) if m else 0


def _find_temp_path():
    """Best CPU temperature file: a known CPU hwmon sensor, then a thermal
    zone named cpu/pkg, and only as a last resort any readable zone."""
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            with open(os.path.join(d, "name")) as f:
                name = f.read().strip()
        except OSError:
            continue
        if name in CPU_SENSORS:
            # numeric sort so temp2 comes before temp10 (temp1 is the main one)
            for t in sorted(glob.glob(os.path.join(d, "temp*_input")),
                            key=_temp_index):
                if _read_millideg(t) is not None:
                    return t
    zones = sorted(glob.glob("/sys/class/thermal/thermal_zone*"))
    for z in zones:
        try:
            with open(os.path.join(z, "type")) as f:
                ztype = f.read().strip().lower()
        except OSError:
            continue
        if "cpu" in ztype or "pkg" in ztype:
            p = os.path.join(z, "temp")
            if _read_millideg(p) is not None:
                return p
    for z in zones:
        p = os.path.join(z, "temp")
        if _read_millideg(p) is not None:
            return p
    return None


def cpu_temp():
    global _temp_path
    if _temp_path is None:
        _temp_path = _find_temp_path()
        if _temp_path is None:
            return None
    v = _read_millideg(_temp_path)
    if v is None:
        _temp_path = None     # rescan next time
    return v


# ------------------------------------------------------------------ drawing
class Palette:
    """Colour pairs, degrading gracefully on simple/colourless terminals."""

    def __init__(self):
        self.enabled = False
        self.rainbow_n = 0
        self.level_pairs = {}
        self.bold_levels = set()

    def setup(self):
        try:
            if not curses.has_colors():
                return
            curses.start_color()
            if curses.COLORS < 6:
                return
            try:
                curses.use_default_colors()
            except curses.error:
                pass
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_RED, -1)
            curses.init_pair(4, curses.COLOR_CYAN, -1)
            curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)
            wide = curses.COLORS >= 256
            levels = LEVEL_COLORS_256 if wide else LEVEL_COLORS_8
            for n, (lv, color) in enumerate(levels.items()):
                curses.init_pair(LEVEL_PAIR0 + n, color, -1)
                self.level_pairs[lv] = LEVEL_PAIR0 + n
            if not wide:
                self.bold_levels = {"0"}       # plain blue is too dark
            rainbow = RAINBOW_256 if wide else RAINBOW_8
            for n, color in enumerate(rainbow):
                curses.init_pair(RAINBOW_PAIR0 + n, color, -1)
            self.rainbow_n = len(rainbow)
            self.enabled = True
        except curses.error:
            self.enabled = False
            self.rainbow_n = 0
            self.level_pairs = {}

    def p(self, n):
        return curses.color_pair(n) if self.enabled else 0

    def level(self, lv):
        """Colour of a numbered level (1-5)."""
        n = self.level_pairs.get(lv)
        if not self.enabled or not n:
            return 0
        attr = curses.color_pair(n)
        return attr | curses.A_BOLD if lv in self.bold_levels else attr

    def rainbow(self, hue):
        """Rainbow colour for a hue in any range (wraps around)."""
        if not self.enabled or not self.rainbow_n:
            return 0
        step = int((hue % 1.0) * self.rainbow_n) % self.rainbow_n
        return curses.color_pair(RAINBOW_PAIR0 + step)


def put(win, y, x, text, attr=0):
    if not text:
        return
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    text = text[: w - x]
    try:
        win.addstr(y, x, text, attr)
    except (curses.error, UnicodeError):
        pass


def temp_pair(t):
    if t is None:
        return 0
    return 1 if t < 60 else 2 if t < 75 else 3


def fan_base_attr(level, pal):
    """Single fan colour for everything except MAX."""
    if level in LEVEL_COLORS_256:
        return pal.level(level)
    if level == "auto":
        return pal.p(1)              # green
    return pal.p(4)                  # unknown level


def draw_fan(win, fx, fy, size, phase, blur, rpm, level, hue, pal):
    frame = fan_frame(size, phase, blur)
    rgb = level == "full-speed" and pal.rainbow_n > 0
    base = fan_base_attr(level, pal)

    def attr_of(kind, idx, h):
        col = pal.rainbow(h + hue) if rgb else base
        if kind == "ring":
            return col if rgb else pal.p(4) | curses.A_DIM
        if kind == "hub":
            return col | curses.A_BOLD
        return col | (curses.A_BOLD if idx >= 7 else 0)

    for ry, row in enumerate(frame):
        y = fy + ry
        n = len(row)
        x = 0
        while x < n:
            ch, kind, idx, h = row[x]
            if ch == " ":
                x += 1
                continue
            attr = attr_of(kind, idx, h)
            start = x
            text = [ch]
            x += 1
            while x < n:                     # group same-attribute runs
                ch2, kind2, idx2, h2 = row[x]
                if ch2 == " " or attr_of(kind2, idx2, h2) != attr:
                    break
                text.append(ch2)
                x += 1
            put(win, y, fx + start, "".join(text), attr)
    if size == "large":
        if rpm:
            label = f"{rpm} RPM"
        elif level == "full-speed":
            label = "full speed"
        else:
            label = "stopped"
        put(win, fy + FAN_SIZES[size]["h"],
            fx + (FAN_SIZES[size]["w"] - len(label)) // 2, label, curses.A_DIM)


def draw(win, info, temp, sel, disp_msg, hot, fan, pal):
    win.erase()
    h, w = win.getmaxyx()
    if w < MIN_W or h < MIN_H:
        put(win, 0, 0, "Terminal too small", curses.A_BOLD)
        put(win, 1, 0, "Enlarge the window")
        win.refresh()
        return

    size = pick_fan_size(w, h)
    bw = max(12, min(w - 2, CONTENT_W))
    fan_w = FAN_SIZES[size]["w"] if size else 0
    total = bw + (3 + fan_w if size else 0)
    x0 = max(0, (w - total) // 2)

    y = 1
    put(win, y, x0, "╭" + "─" * (bw - 2) + "╮", pal.p(4))
    title = "  ThinkPad  FAN  CONTROL  "
    put(win, y + 1, x0, "│", pal.p(4))
    put(win, y + 1, x0 + max(1, (bw - len(title)) // 2), title,
        pal.p(4) | curses.A_BOLD)
    put(win, y + 1, x0 + bw - 1, "│", pal.p(4))
    put(win, y + 2, x0, "╰" + "─" * (bw - 2) + "╯", pal.p(4))
    y += 4

    if info is None:
        put(win, y, x0, f"Cannot read {FAN}"[:bw], pal.p(3) | curses.A_BOLD)
        put(win, y + 1, x0, "Is thinkpad_acpi loaded with fan_control=1?"[:bw],
            pal.p(2))
        y += 3
    else:
        rpm = info["speed"]
        tach_ok = rpm > 0 or canon_level(info["level"]) != "full-speed"
        bar_w = max(4, min(30, bw - 20))
        t_txt = f"{temp:5.1f} °C" if temp is not None else "  n/a"
        put(win, y, x0, "CPU temp  ", curses.A_DIM)
        put(win, y, x0 + 10, t_txt, pal.p(temp_pair(temp)) | curses.A_BOLD)
        if temp is not None:
            filled = max(0, min(bar_w, int((temp - 30) / 70 * bar_w)))
            put(win, y, x0 + 20, "█" * filled, pal.p(temp_pair(temp)))
            put(win, y, x0 + 20 + filled, "░" * (bar_w - filled), curses.A_DIM)
        y += 1
        put(win, y, x0, "Fan speed ", curses.A_DIM)
        put(win, y, x0 + 10, f"{rpm:5d} RPM" if tach_ok else " n/a RPM",
            pal.p(4) | curses.A_BOLD)
        filled = max(0, min(bar_w, int(rpm / MAX_RPM * bar_w)))
        put(win, y, x0 + 20, "█" * filled, pal.p(4))
        put(win, y, x0 + 20 + filled, "░" * (bar_w - filled), curses.A_DIM)
        y += 1
        extra = "" if info["status"] in ("enabled", "?") \
            else f"  [{info['status']}]"
        put(win, y, x0, "Firmware  ", curses.A_DIM)
        put(win, y, x0 + 10, f"level: {info['level']}{extra}", curses.A_BOLD)
        y += 2

    put(win, y, x0, "Fan level", curses.A_BOLD)
    y += 1
    if bw >= 48:
        cell, labels = 6, LABELS
    elif bw >= 40:
        cell, labels = 5, LABELS
    else:
        cell, labels = 3, COMPACT_LABELS
    sx = x0 + max(0, (bw - cell * len(labels)) // 2)
    sel_attr = (pal.p(5) if pal.enabled else curses.A_REVERSE) | curses.A_BOLD
    for i, lab in enumerate(labels):
        put(win, y, sx + i * cell, lab.center(cell),
            sel_attr if i == sel else curses.A_DIM)
    y += 1
    # staircase under the selector, coloured like the fan at each level
    for i in range(len(labels)):
        ch = STAIRS[i]
        if cell >= 5:
            ch *= 3
        cx = sx + i * cell + max(0, (cell - len(ch)) // 2)
        lv = OPTIONS[i]
        if i > sel and i != 0:
            put(win, y, cx, ch, curses.A_DIM)
        elif lv == "full-speed":
            for j, c1 in enumerate(ch):          # RGB bar for MAX
                put(win, y, cx + j, c1,
                    pal.rainbow(fan["hue"] + 0.15 * j))
        elif lv == "auto":
            put(win, y, cx, ch, pal.p(1))
        else:
            put(win, y, cx, ch, pal.level(lv))
    y += 1

    if disp_msg:
        put(win, y, x0, disp_msg[:bw], pal.p(3) if hot else pal.p(2))
    y += 2
    put(win, y, x0, "←/→ level   0-4 jump   a auto   f max   q quit",
        curses.A_DIM)
    put(win, y + 1, x0, "Quitting restores automatic fan control.",
        curses.A_DIM)

    if size:
        blur = min(1.0, fan["vis_rps"] / MAX_VIS_RPS) ** 1.5
        fy = 4 if size == "large" else 5
        draw_fan(win, x0 + bw + 3, fy, size, fan["phase"], blur,
                 fan["rpm"], fan["level"], fan["hue"], pal)

    win.refresh()


# --------------------------------------------------------------------- main
def run(stdscr):
    pal = Palette()
    pal.setup()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.timeout(FRAME_DELAY_MS)
    try:
        stdscr.keypad(True)
    except curses.error:
        pass

    info = read_fan()
    temp = cpu_temp()
    sel = level_index(info)

    msg = ""
    msg_until = 0.0
    hot = False
    phase = 0.0
    vis_rps = 0.0
    fails = 0
    tach_note_shown = False
    now0 = time.time()
    last_frame = now0
    last_poll = 0.0
    last_keepalive = now0
    last_apply = 0.0
    dirty = True
    last_size = (0, 0)
    last_msg_disp = None
    prev_sample = None

    def set_msg(text, ttl=MSG_TTL):
        nonlocal msg, msg_until, dirty
        msg = text
        msg_until = time.time() + ttl
        dirty = True

    def apply(i):
        """Set level i. Returns True on success."""
        nonlocal last_apply, msg, msg_until, dirty
        global _last_level
        if OPTIONS[i] != "auto":
            write_fan(f"watchdog {WATCHDOG_SECS}")
            err = write_fan("level " + OPTIONS[i])
        else:
            err = set_auto(_last_level)
        last_apply = time.time()
        if err:
            set_msg("Error: " + explain(err, info), 8)
            return False
        _last_level = OPTIONS[i]
        msg, msg_until = "", 0.0
        dirty = True
        return True

    if sel != 0:                       # fan was already in manual mode
        write_fan(f"watchdog {WATCHDOG_SECS}")
    if temp is None:
        set_msg("No temp sensor found: overheat protection OFF", 10)

    while True:
        now = time.time()

        if now - last_poll >= POLL_EVERY:
            last_poll = now
            new_info = read_fan()
            if new_info is not None:
                info, fails = new_info, 0
            else:
                fails += 1             # one bad read must not blank the UI
                if fails >= 3:
                    info = None
            temp = cpu_temp()
            sample = (
                info["speed"] if info else None,
                canon_level(info["level"]) if info else None,
                info["status"] if info else None,
                None if temp is None else round(temp, 1),
            )
            if sample != prev_sample:
                prev_sample = sample
                dirty = True

        # safety: too hot -> firmware control, and stay there until cool
        if temp is not None and temp >= DANGER_TEMP:
            if not hot:
                hot = True
                dirty = True
                if sel != 0 or (info and canon_level(info["level"]) != "auto"):
                    sel = 0
                    apply(0)
        elif hot and temp is not None and temp <= RESUME_TEMP:
            hot = False
            set_msg(f"Cooled to {temp:.0f} °C - manual control available.", 4)

        # Feed the watchdog. Only re-send the level if the firmware really
        # changed it (re-sending it every time made the fan pause briefly).
        if sel != 0 and now - last_keepalive >= REASSERT_EVERY:
            last_keepalive = now
            write_fan(f"watchdog {WATCHDOG_SECS}")
            if info is not None and not hot:
                cur = canon_level(info["level"])
                if cur in OPTIONS and cur != OPTIONS[sel] \
                        and now - last_apply > 2.0:
                    apply(sel)

        # animation: the drawn fan eases toward the real fan's speed
        dt = min(max(now - last_frame, 0.0), 0.1)
        last_frame = now
        rpm = info["speed"] if info else 0
        fan_level = canon_level(info["level"]) if info else "auto"
        # some ECs freeze the tachometer at 0 in full-speed; the fan is
        # actually running, so keep the animation going
        anim_rpm = rpm if rpm > 0 else MAX_RPM if fan_level == "full-speed" else 0
        vis_rps += (visual_rps(anim_rpm) - vis_rps) * min(1.0, dt * 2.5)
        if anim_rpm == 0 and vis_rps < 0.01:
            vis_rps = 0.0
        if vis_rps > 0.0:
            phase = (phase + vis_rps * 2 * math.pi * dt) % (2 * math.pi)

        rainbow_on = fan_level == "full-speed" or OPTIONS[sel] == "full-speed"
        hue = (now * RAINBOW_HZ) % 1.0

        if fan_level == "full-speed" and rpm == 0 and not tach_note_shown:
            tach_note_shown = True
            set_msg("Full-speed: this EC stops reporting RPM, the fan is running.", 8)

        if msg and now < msg_until:      # a fresh message beats the banner
            disp = msg
        elif hot:
            disp = (f"{temp:.0f} °C - too hot: fan forced to AUTO"
                    if temp is not None
                    else "CPU sensor unavailable: fan forced to AUTO")
        else:
            disp = ""
        if disp != last_msg_disp:
            last_msg_disp = disp
            dirty = True

        size_now = stdscr.getmaxyx()
        if size_now != last_size:
            last_size = size_now
            dirty = True

        # redraw only when something changed or something is animating
        if dirty or vis_rps > 0.02 or rainbow_on:
            draw(stdscr, info, temp, sel, disp, hot,
                 {"phase": phase, "vis_rps": vis_rps, "rpm": rpm,
                  "level": fan_level, "hue": hue}, pal)
            dirty = False

        k = stdscr.getch()
        if k == -1:
            continue
        if k in (ord("q"), 27):
            break
        new = sel
        if k in (curses.KEY_RIGHT, curses.KEY_UP, ord("l"), ord("k"), ord("+")):
            new = min(sel + 1, len(OPTIONS) - 1)
        elif k in (curses.KEY_LEFT, curses.KEY_DOWN, ord("h"), ord("j"),
                   ord("-")):
            new = max(sel - 1, 0)
        elif k == ord("a"):
            new = 0
        elif k == ord("f"):
            new = len(OPTIONS) - 1
        elif ord("0") <= k <= ord(str(MAX_LEVEL)):
            new = 1 + (k - ord("0"))
        if new != sel:
            if hot and new != 0:
                set_msg("CPU is too hot - manual levels are disabled.", 3)
            elif apply(new):
                sel = new
                last_poll = 0.0        # refresh readings right away
                dirty = True


def main():
    if "FAN_PATH" not in os.environ and os.geteuid() != 0:
        try:
            os.execvp("sudo", ["sudo", "--", sys.executable,
                               os.path.abspath(sys.argv[0])] + sys.argv[1:])
        except OSError as exc:
            print(f"fanctl: cannot run sudo: {exc}", file=sys.stderr)
            sys.exit(1)

    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    os.environ.setdefault("ESCDELAY", "25")   # make Esc quit responsive

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, lambda *_: sys.exit(0))
        except (ValueError, OSError):
            pass

    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass
    except curses.error as exc:
        print(f"fanctl: terminal error: {exc}", file=sys.stderr)
    finally:
        err = set_auto(_last_level)
        if err:
            print(f"fanctl: could not restore auto fan control: "
                  f"{explain(err, None)}", file=sys.stderr)
        else:
            print("Fan control restored to auto.")


if __name__ == "__main__":
    main()
