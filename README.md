# 🎣 NTE_FishingBot

**Author:** Lolka12321  
**License:** see [LICENSE](LICENSE) — personal use allowed, redistribution prohibited.

---

## What the bot can do

- **Automatic fishing** — the bot presses `F` to cast the rod, waits for the mini-game to appear and starts keeping the float inside the zone automatically.
- **Color-based bar detection** — recognizes the cyan progress bar via screen capture. The top and bottom halves of the bar are scanned with separate color profiles (`#20b5a1` and `#36e6bf`) for accurate detection without false positives on sky or background.
- **Yellow marker tracking** — finds the position of the yellow indicator inside the bar and controls it with `A` / `D` keys.
- **Smart input control** — uses a pulse algorithm: the further the marker is from center, the longer the key is held. A ±1.5px dead zone prevents jitter.
- **GDI overlay** — draws a transparent overlay on top of the game showing the scan zone, bar bounds, and marker position in real time.
- **Watchdog** — if the bot gets stuck or loses the bar for more than 60 seconds, it automatically clicks the required screen point and restarts the loop.
- **Auto-shutdown timer** — set a countdown (hours / minutes / seconds), after which the PC will shut down automatically.
- **Game audio mute** — mutes the game process audio without affecting system sound.
- **Any resolution support** — automatically calculates the capture zone and click point for any screen resolution (480p, 720p, 1080p, 1440p, 4K, 8K and anything else).

---

## System requirements

- **OS:** Windows 10 / 11 (Windows only)
- **Python:** 3.14.4 or newer
- **Privileges:** Administrator (the bot will request them automatically on launch)

---

## Installation

**1. Install Python**  
Download from [python.org](https://www.python.org/downloads/) and check **Add Python to PATH** during installation.

**2. Install dependencies**  
Open the bot folder in a terminal and run:
```
pip install -r requirements.txt
```

---

## Running

```
python NTE_FishingBot.py
```

On first launch Windows will ask for administrator permission — click **Yes**.

---

## Interface

| Element | Description |
|---|---|
| **START** button | Start the bot |
| **STOP** button | Stop the bot |
| **H / M / S** fields | Auto-shutdown timer input |
| ▶ timer button | Start the timer |
| ✕ timer button | Cancel the timer |
| **Disable overlay** | Hide / show the GDI overlay on top of the game |
| **Mute sound** | Mute the game audio |
| **WD: Xs** | Time remaining before the watchdog fires |

---

## Keys (controlled by the bot)

| Key | Action |
|---|---|
| `F` | Cast / confirm |
| `A` / `D` | Move marker left / right |
| `ESC` | Exit mini-game when bar is lost |

---

## License

Personal use is allowed. Redistribution on any platform is **prohibited**.  
Full terms in the [LICENSE](LICENSE) file.  
© 2026 Lolka12321