# Wiring relays to the Raspberry Pi

This guide covers the typical setup: a cheap opto‑isolated relay module wired
to the Pi's GPIO header, with the relay contacts "pressing" the buttons of an
adjustable bed's wired remote. Read it once before you touch a wire.

> **Safety first.** Never connect relay contacts to mains voltage for this
> project. You only switch the low‑voltage *signal* wires of the bed's remote –
> the bed's own control box does the heavy lifting and keeps all its limits.

## 1. Check that your bed can be driven this way

Most adjustable beds (Okin/Okimat, Limoss, Richmat‑wired, Leggett & Platt
wired, generic Chinese frames) have a **wired hand control** whose buttons are
simple contacts: pressing *Head up* connects one signal wire to the supply
wire, and the motion stops the moment you let go.

1. Unplug the remote from the control box.
2. With a multimeter, find the two constant wires (**+** and **−**, typically
   5–30 V DC).
3. Open the remote (or buzz the plug pins) and check that each button simply
   connects one signal pin to **+** (or to **−**). If yes – perfect, continue.
4. If the remote only has 3–4 wires and contains a chip (serial protocol), or
   the remote is Bluetooth, relays **cannot** emulate it. Look for a
   Bluetooth/serial integration for your bed brand instead.

## 2. Parts

* Raspberry Pi (Zero W, Zero 2 W, 3, 4, 5 – anything with the 40‑pin header).
* A **2‑ or 4‑channel 5 V relay module with optocouplers** (the common blue
  boards with a `JD‑VCC` jumper) – or a Pi relay HAT.
* A DIN/RJ extension cable matching the bed's remote connector, so you can
  splice in without cutting the original remote.
* Dupont wires, a small screwdriver.

## 3. Wire the relay module to the Pi

Typical relay header pins: `GND`, `IN1 … INn`, `VCC`, `JD‑VCC`.

| Relay module | Raspberry Pi header |
| --- | --- |
| `GND` | any GND (e.g. pin 6, 9, 14) |
| `JD‑VCC` (remove the jumper first) | 5 V (pin 2 or 4) |
| `VCC` | 3.3 V (pin 1 or 17) |
| `IN1`, `IN2`, … | GPIO pins (see below) |

Removing the `JD‑VCC` jumper and feeding `VCC` with 3.3 V is important: it
makes the module's inputs 3.3 V‑logic so a GPIO can switch it *fully* off and
on. With the jumper in place and 5 V logic, a Pi pin often can't turn the relay
completely off.

Most of these boards are **active LOW**: the relay turns on when the `IN` pin
is pulled to 0 V. Choose *Active LOW* in JimboLED for them. Solid‑state relay
boards and transistor boards are usually **active HIGH**.

### Which GPIO pins?

Use the *BCM* numbers (the ones printed on pinout diagrams as GPIO17 etc.),
not the physical position. JimboLED's pin picker marks recommended pins and
"boot‑safe" combinations:

* **Active LOW boards:** GPIO 4, 5, 6 are pulled *up* while the Pi powers on, so
  the relay stays off from the first millisecond. Other free pins are fine too;
  JimboLED writes their safe level into `config.txt` so they are set a few
  seconds after power‑on.
* **Active HIGH boards:** GPIO 17, 27, 22, 23, 24, 25, 12, 13, 16, 26 are pulled
  *down* at power‑on – ideal.
* Avoid GPIO 0/1 (reserved), 2/3 (I²C pull‑ups), 14/15 (serial console) and
  the SPI pins 7–11 if SPI is enabled.

Physical layout of the 40‑pin header (BCM numbers):

```
        3V3  (1) (2)  5V
      GPIO2  (3) (4)  5V
      GPIO3  (5) (6)  GND
      GPIO4  (7) (8)  GPIO14
        GND  (9) (10) GPIO15
     GPIO17 (11) (12) GPIO18
     GPIO27 (13) (14) GND
     GPIO22 (15) (16) GPIO23
        3V3 (17) (18) GPIO24
     GPIO10 (19) (20) GND
      GPIO9 (21) (22) GPIO25
     GPIO11 (23) (24) GPIO8
        GND (25) (26) GPIO7
      GPIO0 (27) (28) GPIO1
      GPIO5 (29) (30) GND
      GPIO6 (31) (32) GPIO12
     GPIO13 (33) (34) GND
     GPIO19 (35) (36) GPIO16
     GPIO26 (37) (38) GPIO20
        GND (39) (40) GPIO21
```

## 4. Wire the relays to the bed remote

Use the extension cable: plug its male end into the bed, its female end into
the original remote, and splice the relays into the cable in between. The
original remote keeps working as a fallback.

For each direction you want to control:

* relay `COM` → that button's **signal wire**
* relay `NO` (normally open) → the **+** wire (or **−**, whichever the remote's
  buttons connect to)

When the relay closes, the control box sees exactly what it sees when you
press the button. When it opens, motion stops – just like letting go.

Example for a generic 6‑wire DIN remote (yours may differ – measure!):

| Pin | Function |
| --- | --- |
| 1 | Feet up |
| 2 | Head up |
| 3 | + supply |
| 4 | Feet down |
| 5 | Head down |
| 6 | − supply |

## 5. Set it up in JimboLED

1. Settings → Switches → **Bed template**. Enter the GPIO pins for *up* and
   *down* and whether the board is active LOW.
2. Use **Test this pin** on each switch – you should hear the relay click once.
   If a relay is on when it should be off, flip *Relay trigger*.
3. Try the *Hold* buttons on the dashboard. The bed moves only while you hold.
4. Optional: add *pulse* switches for preset buttons on the remote (Flat,
   Zero‑G…), and a plain *on/off* switch for an under‑bed light.

The template creates the switches interlocked (up and down can never be on
together) with a 60 s limit. A full head or foot travel takes 25–35 s, so 60 s
is a comfortable ceiling; lower it in the switch settings if you like.

## 6. A physical emergency stop (recommended)

The on-screen **E-stop** works from any phone on the network. A wired button
works when the phone is asleep, across the room, or flat — and it is the one
you reach for without looking. JimboLED reads it on a GPIO **input**.

### Parts

A **normally-closed** (NC) emergency-stop button: the red mushroom kind with a
twist-to-release head, marked `NC` or `1 NC`. A plain NC pushbutton works too.
Two wires, no resistors — the Pi's internal pull-up does that job.

### Wiring

| Button | Raspberry Pi header |
| --- | --- |
| one terminal | your chosen GPIO (e.g. GPIO26, physical pin 37) |
| other terminal | any GND (e.g. pin 39) |

That is the whole circuit. While the button is out, its closed contact holds
the pin at 0 V. Pressing it opens the contact, the Pi's pull-up lets the pin
rise to 3.3 V, and JimboLED latches the stop within about a fifth of a second.

**Why normally closed?** Because a broken wire, a pulled connector or a
corroded terminal looks exactly like a press. A normally-*open* button would
fail the other way: the fault would sit there silently and you would only find
out when you needed the button. Use NC unless you have a reason not to.

### Set it up

1. Settings → **Emergency stop** → *Physical buttons* → **Add a button**.
2. Pick the GPIO pin, leave *Normally closed* and *Pull-up* as they are, and
   choose what it stops — **All relays**, or one zone such as *Bed*.
3. Press the button. The dashboard should turn red within a second and the
   relays it covers should refuse to run.
4. Twist the button back out, then press **Reset** in the dashboard. JimboLED
   refuses to reset while the button is still held, so it cannot be cleared
   from a phone while somebody is holding it down.

A pin used by an emergency-stop button cannot also be used by a relay — driving
it as an output would fight the pull-up and quietly disarm the stop — so
JimboLED rejects that combination.

### Zones

One stop for everything is the default. If more than one thing moves, give
each its own stop (Settings → Emergency stop → **Add a stop**) so stopping the
awning does not also lock out the bed. A zone can cover an interlock group
(both bed directions at once) or individual switches.

## 7. Good to know

* JimboLED releases every relay when it starts, stops, restarts or crashes,
  when a held button loses contact with the phone for 1.5 s, and when you press
  **All off**.
* **All off** and an **emergency stop** are different things. All off releases
  everything and anything can be switched straight back on. An emergency stop
  latches: what it covers stays locked out — from every phone, every scene, the
  API — until somebody resets it, including across a restart or a power cut.
* A short click from the relays during a Pi reboot is harmless with a control
  box (it ignores blips shorter than a real press), but it is avoided anyway by
  the boot‑safe pin choice and the `config.txt` entries JimboLED maintains.
* Powering many relay coils from the Pi's 5 V pin can brown‑out a Pi Zero. Two
  relays are fine; for four or more, feed `JD‑VCC` from a separate 5 V supply
  (sharing GND with the Pi).
