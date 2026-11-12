# Simple ThinkPad Fan Control

A single-file terminal UI for controlling the fan on ThinkPad laptops under Linux. It talks to the `thinkpad_acpi` driver through `/proc/acpi/ibm/fan`, so there is no config file and nothing to install besides the script itself.

Written for my ThinkPad T14 Gen 1 (AMD) running Linux Mint for the purpose of setting the fan speed by hand without setting up thinkfan and writing a config for it. It should work on other ThinkPads too, see the compatibility section below.

## Features

- Manual fan levels (0 to 4 plus full speed) or back to firmware auto control, selected with the keyboard
- Live CPU temperature and fan RPM with bar graphs
- An ASCII fan that spins at roughly the real fan speed, turns into RGB at full speed
- Overheat protection: at 90 °C the fan is forced back to auto and manual levels are refused until the CPU cools below 85 °C
- Driver watchdog: while a manual level is set, the script feeds it every 30 seconds, so if the script dies or hangs the firmware takes the fan back within two minutes
- On quit, Ctrl-C, SIGTERM or SIGHUP the fan is returned to auto
- Only the Python standard library, works in a plain terminal, degrades on monochrome or narrow terminals instead of crashing

## Keys

```
left/right or h/l   lower or raise the level
up/down or k/j      same (also + and -)
0-4                 jump to a level
a                   auto (firmware control)
f                   full speed
q or Esc            quit (resets speed to auto)
```

## Requirements

- Linux with the `thinkpad_acpi` module
- Python 3.6 or newer. Only the standard library is used. On Debian, Ubuntu and Mint this works out of the box.
- Root. The script runs sudo on itself, so just start it as your normal user.

Manual fan control is disabled in the driver by default. Enable it once with:

```
echo "options thinkpad_acpi fan_control=1" | sudo tee /etc/modprobe.d/thinkpad_acpi.conf
```

and reboot (or reload the module). If you skip this, the script will start but every write will fail with a "fan control off" message.

## Installation

```
git clone https://github.com/JESUSWiLLSAVE/simple-thinkpad-fanctl.git
cd simple-thinkpad-fanctl
python3 fanctl.py
```

or just download the script on its own:

```
curl -LO https://raw.githubusercontent.com/JESUSWiLLSAVE/simple-thinkpad-fanctl/main/fanctl.py
python3 fanctl.py
```

## Compatibility

Tested on a ThinkPad T14 Gen 1 (AMD) with Linux Mint. On this model fan levels 5 to 7 behave the same as 4, so the UI only offers up to 4. If your model has distinct higher levels, change `MAX_LEVEL` at the top of `fanctl.py` back to 7.

In general it should work on any ThinkPad where thinkpad_acpi fan control works. A few things to know:

- Not every model supports the `full-speed` level. The script will show "the driver rejected that command" and keep working otherwise.
- On models with two fans the level applies to both, but the RPM readout only shows one tachometer.
- This is ThinkPad only. On other laptops the script starts, finds no fan interface, shows an error screen, and changes nothing.
- The temperature sensor is picked from the usual CPU sensors (k10temp, coretemp, zenpower) and falls back to thermal zones. If none is found the script warns you and runs without overheat protection.

## How it works

Everything goes through `/proc/acpi/ibm/fan`. Reading it gives status, speed and level. Writing accepts commands like `level 3` or `watchdog 120`. The script enables the watchdog when you pick a manual level, so if the process disappears the embedded controller reclaims the fan within 120 seconds. Quitting sets `level auto` and clears the watchdog.

If you want a daemon that follows temperature curves on its own, use thinkfan. This is mainly for quick manual control with cool RGB.
