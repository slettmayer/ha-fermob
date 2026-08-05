# The Light Command

> `DEVICE_DATA_SET` (`0x41`) — the one command that changes the light, its two family-dependent bodies, and
> the warm/cold mixing that expresses colour temperature.

**Scope.** Outbound light control only. The inbound record that reports the same fields back is in
[PROTOCOL-INBOUND-STATE.md](PROTOCOL-INBOUND-STATE.md). Which family a lamp is, and how that is decided, is
in [DEVICES.md](DEVICES.md). Why the lamp may emit less than you asked for is in
[DEAD-ENDS.md](DEAD-ENDS.md#brightness-that-feels-capped-is-the-lamps-own-power-management).

## The two bodies

Both families use `DEVICE_DATA_SET` (`0x41`) as a `MSG_FIRE` frame under `ENCRYPT_PRIVATE`, addressed with
the short address, and both use `led_mode = LEDS_MODE_COLOR = 1`, so the on-byte is identical:

```
on_byte = (1 if on else 0) | (led_mode << 4)     # 0x11 on, 0x10 off
```

They differ **only** in the body:

```
Dimmable white (Hoopik L1200):  [6, 0x41, dev, on_byte, level,             fade_lo, fade_hi]
Tunable white  (every MOOON!):  [7, 0x41, dev, on_byte, cold_white, warm_white, fade_lo, fade_hi]
```

The leading byte is the body length, `dev` is the device index (always `0` for a single lamp), and `fade` is
`FADE = 50` ms little-endian (the app's `fade_timing_10.color_transition`).

**Sending the 6-byte dimmable-white body to a tunable-white lamp does nothing** — that was the entire MOOON!
bug. Because it is a no-ACK FIRE write, there is no error to observe; the lamp simply ignores it. (Upstream
issue #1 reported a `GATT Protocol Error: Unlikely Error` on the write instead, which does not match the
silent-drop explanation given in the PR. Both accounts come from the same author and we cannot reproduce
either, so treat the precise failure mode as unsettled — only the fix is confirmed.)

## Tunable-white mixing

Colour temperature is expressed as a ratio between two intensity channels whose **sum is the total output**:

```
warm_white = round(brightness% × warm_ratio)
cold_white = brightness% − warm_white
warm_white + cold_white == brightness%
```

`warm_ratio` spans the 3000 K – 6000 K envelope: `1.0` is 3000 K (all warm), `0.0` is 6000 K (all cold).
`protocol.kelvin_to_warm_ratio` and `warm_ratio_to_kelvin` are exact inverses at every integer Kelvin in the
envelope, and both clamp outside it.

**The mapping is linear in mired, not in Kelvin** — mired being 10⁶/K. Two fixed-CCT emitters mixed at some
ratio land at the ratio's position in *reciprocal* colour temperature, so an even mix of a 3000 K and a
6000 K channel is **4000 K**, not the arithmetic mean 4500 K:

| Kelvin | 3000 | 3750 | 4000 | 4500 | 5000 | 6000 |
|---|---|---|---|---|---|---|
| `warm_ratio` | 1.0 | 0.6 | 0.5 | ⅓ | 0.2 | 0.0 |

Up to and including 0.5.0 this interpolated Kelvin directly, which overstated the temperature everywhere
strictly between the endpoints — worst at a 4727 K slider, where the lamp actually emitted about 4212 K, a
515 K error. `test_mix_is_linear_in_mired` pins round mired fractions specifically so a revert to
Kelvin-linear interpolation fails rather than merely looking slightly off.

One caveat on the physics: mired-linearity assumes the two channels put out **equal luminous flux at equal
drive percent**. Fermob publishes no per-channel flux figures, so if the warm and cold LEDs differ in
efficacy the true midpoint shifts toward the brighter channel. Mired is the correct model absent that data,
and it is a large improvement on Kelvin-linear either way, but it is not calibrated against a meter.

### Rounding ties are unspecified

At very low brightness the split quantises hard, and **which way it skews alternates**, because `warm` is
computed with Python's `round()` — which is half-to-even, not half-up. At mid colour temperature
(`warm_ratio = 0.5`): `level = 1` gives `cold = 1, warm = 0` (skews **cold**, since `round(0.5) == 0`), while
`level = 3` gives `cold = 1, warm = 2` (skews warm). That is inherent to expressing temperature as two
integer percentages, not a bug in the conversion.

Exact splits *are* pinned in a few places — `test_tw_payload_layout` fixes `cold = 50, warm = 50` at 100 % /
4000 K, and `test_tw_extremes_are_single_channel` fixes both endpoints — so a gross rounding change (a switch
to `floor`, or an off-by-one) fails CI immediately. What is **not** covered is the **tie-breaking rule**: none
of the pinned cases lands on a `.5` boundary, so swapping half-to-even for half-up would keep the suite green
while changing behaviour at low brightness. Verify ties by hand if you touch this.

A worked example of why the ties are so slippery, from the mired change itself: `kelvin_to_warm_ratio(4000)`
returns `0.5000000000000001`, not `0.5`, because 4000 K is not exactly representable as a ratio of the two
mired endpoints. That hair of float error is enough to *escape* the tie, and it flips the low-brightness skew
relative to a literal `0.5` — at `level = 1`, `DEFAULT_KELVIN` gives `warm = 1, cold = 0` while an exact
`warm_ratio = 0.5` gives `cold = 1, warm = 0`. Both are defensible at one percent of output; the point is that
the tie-break here is decided by floating-point representation rather than by any rule, so do not treat either
skew as specified behaviour.
