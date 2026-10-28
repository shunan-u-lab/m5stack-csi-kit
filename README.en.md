English | [日本語](README.md)

# M5Stack CSI Kit

[![Code: MIT](https://img.shields.io/badge/code-MIT-blue.svg)](LICENSE-CODE)
[![Docs: CC BY 4.0](https://img.shields.io/badge/docs-CC_BY_4.0-lightgrey.svg)](LICENSE-DOCS)

**A kit for recording Wi-Fi CSI (Channel State Information) with two M5Stack Basic boards and a PC.**
The goal: with two M5Stack Basic boards, a PC and two USB cables — no router and no other boards — a student researching Wi-Fi CSI can go from *flash → check on a live monitor → record* without getting stuck. Almost all PC-side tools are installed with `uv`.

- The transmitter (T1) sends packets at 100 Hz; the receiver (R1) streams their CSI to the PC (USB, 921600 bps). The receiver can also record to its SD card without a PC
- No router is needed. The two boards talk directly on a fixed channel (ch11 by default)
- Each recording gets a draft metadata file for the conditions (placement, people, segments)

> **New to microcontrollers, CSI or programming?** Start with the [tutorial](docs/tutorial/README.md) (Japanese), which begins with smartphone analogies.

> **Disclaimer:** a research tool published in a personal capacity by Nobufumi Yoshida, first cohort (2026), Professional Graduate School of Information Science, Shunan University. It is not an official product of the university or any laboratory, and it has not been reviewed by experts. **No support is provided.**
> Only one configuration has been tested (see "Test status" below). Please read the [disclaimer](DISCLAIMER.en.md) first.

## What you need

| Item | Qty | Notes |
|---|---|---|
| M5Stack Basic | 2 | Transmitter (T1) and receiver (R1). **In Japan, use units with the Giteki (technical conformity) mark** (see "Radio law") |
| USB-C cable that carries data | 2 | Charge-only cables are invisible to the PC |
| PC | 1 | Linux, Windows, macOS or a Chromebook (Linux development environment). Windows and macOS not tested on hardware |
| microSD card (FAT32) | optional | Only for recording without a PC |

- **Not needed:** a Wi-Fi router, other boards, antennas or wiring, an IDE such as Arduino IDE (PlatformIO only if you want to change the firmware)
- **Installed outside `uv`:** a USB driver on Windows or macOS (only if needed; see step 1)

## Getting started

### 1. Install the tools

Install [uv](https://docs.astral.sh/uv/), then:

```sh
git clone https://github.com/shunan-u-lab/m5stack-csi-kit.git
cd m5stack-csi-kit
uv sync                        # Python and libraries
uv tool install esptool        # flashing tool
```

On Windows and macOS you may need a USB driver (the USB-serial chip of the M5Stack Basic is a CH9102 on newer units and a CP2104 on older ones).
On Linux, join the group that may use serial ports (`dialout` on Debian/Ubuntu) and log in again.
On Debian/Ubuntu, also install the GUI toolkit for the monitor window: `sudo apt install python3-tk` (without it, the `csikit monitor` window does not open).
On a **Chromebook**, turn on the Linux development environment and share the M5Stack with Linux first ([tutorial appendix](docs/tutorial/chromebook.md), Japanese; tested on an ARM Chromebook).
`doctor` (next step) tells you what is missing.

### 2. Get the firmware

Download `csi-tx-m5stack-basic.bin` and `csi-rx-m5stack-basic.bin` from
[Releases](https://github.com/shunan-u-lab/m5stack-csi-kit/releases) into `out/firmware/` (check them against `SHA256SUMS`).

To build it yourself (if you want to change the firmware):

```sh
uv tool install platformio
uv run python scripts/make_dist.py --build    # writes the .bin files to out/firmware/
```

### 3. Check your setup

```sh
uv run csikit doctor
```

It checks the tools, firmware files, USB ports, board replies and pairing, and prints how to fix each problem.

### 4. Flash and pair

Boards without this kit's firmware cannot say which one is which. Connect them **one at a time**, run
`uv run csikit doctor`, note the port name (`/dev/ttyACM0` on Linux, `COM3` on Windows, ...), then:

```sh
uv run csikit flash --tx-port <port for T1> --rx-port <port for R1>
```

- **Flashing erases what was on the board (e.g. the factory demo).** To keep it, dump it first with `scripts/backup_flash.py`
- **Re-flashing also erases the settings stored on the board** (including when you update the firmware). `flash` restores the pairing,
  but the Wi-Fi network the receiver uses to get the time (name and password) and the screen brightness stay erased. If you want timestamps, set it again after flashing:
  `uv run python scripts/sd_fetch.py --port <port for R1> wifi --ssid <Wi-Fi name>` (you are asked for the password; it is not shown)
- After flashing, the two boards are paired: each gets the other's MAC address and a key newly generated for this pair. The receiver may take up to about 40 s to restart
- From then on, ports are found automatically with the `WHO` command. To pair again without flashing: `uv run csikit flash --pair-only`

When it works, the status bar on the screen shows a green `SENDING` on the transmitter and a green `RX from T1` on the receiver.

### 5. Check on the live monitor

```sh
uv run csikit monitor
```

Shows an amplitude heatmap (last 10 s) on top, RSSI below, and the receive rate (about 100 Hz), loss and drops in the title.
Wave your hand and the pattern should change. The receiver is not reset. A full-featured viewer is `scripts/live_csi.py`.

### 6. Record

```sh
uv run csikit record --duration 60 --label empty
```

Without resetting the receiver (an SD session keeps running), writes `data/raw/<datetime>-<label>.csv` and a draft `data/meta/<same name>.toml`.
Fill in the `TODO`s (placement, people, segments) while you still remember (format: [docs/decisions/0003](docs/decisions/0003-recording-metadata-format.md) (Japanese),
examples: [examples/meta/](examples/meta/)).

To record without a PC, put an SD card in the receiver: button A starts, B stops, C adds a mark.
Import with `uv run python scripts/sd_fetch.py` (see "SD card" in the [receiver README](firmware/csi-rx/README.md), Japanese).

### 7. Change the channel or power, clear the pairing

```sh
uv run csikit channel 12      # change the channel of both boards (1-13; back to 11 after a restart)
uv run csikit txpower 8       # change the transmitter's power [dBm] (2-20; rounded to a step; default after a restart)
uv run csikit unpair          # clear the pairing (peer MAC and key) of the connected boards (before handing them over)
```

The receiver passes signed channel and power changes to the transmitter over the air (the transmitter needs no USB).
Changing the channel may reduce packet loss (see "Packet loss" under "Test status"). Do not change them during a recording.

## Data format and loading

```python
from csikit import io

cap = io.load_rx("data/raw/20260101-120000-empty.csv")   # .csv.zst works too
```

The line format and columns are described under "Output" in the [receiver README](firmware/csi-rx/README.md).
Long recordings shrink to about 1/5 with `uv run python scripts/raw_store.py compress` ([docs/decisions/0004](docs/decisions/0004-raw-storage-zstd-parquet.md)).

## Ethics and safety

- **CSI reveals presence and movement, even through walls.** Do not measure people who do not know they are being measured.
  For experiments with people, follow your institution's research-ethics procedures (e.g. ethics review) and get informed consent
- Recordings can be **personal information** (who was in the room, daily routines). Think about what can be inferred before you publish or share them
- The beacon survey tool (`scripts/beacon_scan.py`) shows the names (SSID) and identifiers (BSSID) of nearby access points.
  Use `--mask` when sharing the screen or the results
- The transmitter keeps transmitting. Switch it off when you are not recording (the effect on nearby Wi-Fi has not been measured)
- Do not use this kit for medical, security, monitoring or any other safety-related purpose

### Radio law (when used in Japan)

- Use **M5Stack Basic units that carry the Giteki mark, unmodified** (e.g. no antenna changes).
  Using uncertified or overseas units in Japan may violate the Radio Act
- The firmware sets the country to Japan (`JP`, channels 1–13). Outside Japan, check your local regulations yourself

### Signed control

Commands from the receiver to the transmitter (channel and power changes) are signed with a per-pair key ([docs/decisions/0005](docs/decisions/0005-control-auth-and-tx-power.md), Japanese).
The key is stored on the boards (NVS, unencrypted), so clear the pairing before giving a board to someone else (connect both and run `uv run csikit unpair`).
What is protected, what is not (passive sensing from the radio cannot be prevented) and what you can do are explained in [tutorial chapter 8](docs/tutorial/08_security.md) (Japanese).

## Test status

| Item | Status |
|---|---|
| Firmware (two M5Stack Basic boards, Linux, in the original research) | About 100 Hz, no drops inside the receiver |
| `csikit doctor` | On Linux hardware: tells the transmitter from the receiver, detects old firmware and missing pairing, and reports no problems once paired (2026-10-28) |
| `csikit channel / txpower / unpair` | On two boards with Linux: ch6 → ch11, power 8 dBm requested (6.25 dBm actual), clearing both pairings → `doctor` reports unpaired → `flash --pair-only` restores (2026-10-28) |
| Pairing stored in NVS (`PAIRSET`), `csikit flash` | On two boards with Linux: flashing, pairing, re-pairing (`--pair-only`), `PAIRCLEAR`, and signed channel changes (rejected with a different key) (2026-10-28) |
| Chromebook (ARM, Linux development environment, Debian 13) | All `doctor`, `flash`, `monitor` and `record` checks above were done in this environment (2026-10-28). Intel/AMD Chromebooks not tested |
| Windows | **Not tested on hardware** (tests and firmware build pass in CI) |
| macOS | **Not tested on hardware** (tests and firmware build pass in CI) |
| Packet loss (over the air) | Boards on either side of the PC, 1 minute per channel (2026-10-28): 1.06 % overall, 0.32 % on the default ch11, 0.08–2.81 % depending on the channel. No drops inside the receiver. Depends on how busy nearby Wi-Fi is ([log](docs/test-log/2026-10-28-packet-loss-60s-per-channel.md), Japanese). On ch11, lowering the transmit power from 19.5 to 0.75 dBm did not increase the loss (0.38–1.11 %, [log](docs/test-log/2026-10-28-tx-power-vs-loss-ch11.md), Japanese) |
| SD card recording (receiver) | With the original research firmware, the CSI lines on the SD card matched the serial output in count, content and order ([ADR 0006](docs/decisions/0006-sd-card-logging.md), Japanese). **Not yet tested** with this kit's firmware |
| `csikit monitor / record` | On Linux hardware: shows and records at about 100 Hz without resetting the receiver (2026-10-28; with this kit's firmware, 3 missing sequence numbers in a 20 s recording) |
| CI (Linux, Windows, macOS) | Tests and firmware builds pass (macOS on an Apple Silicon runner). Release tags run all three automatically; otherwise CI is started manually (Linux only or all three) |

## Repository layout

```
firmware/csi-tx/     transmitter firmware (command list in its README)
firmware/csi-rx/     receiver firmware (output format and SD card in its README)
firmware/shared/     parts shared by both (signing, pairing, display, watchdog)
src/csikit/          PC side (csikit commands, loading recordings, metadata)
scripts/             recording, import, compression and other tools
examples/meta/       metadata template and examples
docs/decisions/      design decisions and their reasons (ADRs)
```

Most documents other than this README are in Japanese.

## License

- Code (`src/`, `tests/`, `scripts/`, `firmware/`, `examples/`): **MIT** — [LICENSE-CODE](LICENSE-CODE)
- Documents (Markdown): **CC BY 4.0** — [LICENSE-DOCS](LICENSE-DOCS)
- Overview: [LICENSE](LICENSE)
