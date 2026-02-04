# BingeWatcher

Automated binge-watching helper for **s.to** and **aniworld.to** with progress
 tracking, end-screen skipping, and a modern sidebar UI for quick navigation.

> **Strict disclaimer**: I do **not** support, endorse, or encourage the use of
> this script. It is published **for educational review only**. Do **not** use
> it to access or automate any streaming platform. If you choose to inspect the
> code, do so responsibly and in compliance with all applicable laws and terms
> of service. By continuing, you acknowledge that you will **not** use this
> script operationally.

## Features

- **Multi-provider support**: s.to and aniworld.to with automatic provider
  detection.
- **Progress tracking**: resume by series/season/episode with saved timestamps.
- **Optional intro skip**: fingerprint-configured per season.
- **End screen skip**: jump past credits/outro if configured.
- **Auto fullscreen**: multiple fallback strategies for stubborn players.
- **Sidebar UI**: search, sort, quick actions, and settings panel.
- **Tor proxy support**: optional SOCKS proxy routing.

## Requirements

- Python **3.8+**
- Firefox
- GeckoDriver (included as `geckodriver.exe` in this repo)


## Quick-Start (Windows)

1. Run `start_watching.bat` from the repo root.
2. The script will:
   - check your Python install,
   - install missing Python modules (`selenium`, `configparser`),
   - optionally start Tor if `settings.json` has `useTorProxy: true`,
   - launch `s.toBot.py`.

> Note: This helper does **not** run `pip install -r requirements.txt`.
> If you want the full dependency set or you are on macOS/Linux, use the
> installation steps below.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python s.toBot.py
```

The script will:
1. Launch Firefox with a dedicated profile in `user.BingeWatcher/`.
2. Inject the sidebar into supported pages.
3. Track and resume episodes automatically.

## Configuration

### Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `BW_HEADLESS` | `false` | Run Firefox headless (`true/false`). |
| `BW_START_URL` | `https://s.to/` | Start URL (provider homepage). |
| `BW_MAX_RETRIES` | `3` | Navigation retry count. |
| `BW_WAIT_TIMEOUT` | `25` | Page load wait timeout. |
| `BW_PROGRESS_INTERVAL` | `5` | Progress save interval (seconds). |
| `BW_TOR_PORT` | `9050` | Tor SOCKS port (if enabled). |
| `BW_KIOSK` | `false` | Try to start in fullscreen window mode. |
| `BW_POPOUT_IFRAME` | `false` | Attempt iframe popout for fullscreen. |

### Settings file

`settings.json` is created automatically and can be edited while the app runs.
Important keys:

- `useTorProxy` (boolean)
- `autoFullscreen` (boolean)
- `autoSkipEndScreen` (boolean)
- `autoNext` (boolean)
- `playbackRate` (number)
- `volume` (number, `0.0`–`1.0`)

### Intro fingerprints (optional)

To enable intro skipping per season, create `intro_fingerprints.json` with keys
like `<series>_s<season>`, for example `one_piece_s07`:

```json
{
  "one_piece_s07": {
    "fingerprint": "A_LONG_FP_STRING",
    "fingerprintDuration": 10,
    "fullIntroDurationSeconds": 145
  }
}
```

If `fingerprint` is omitted but `fullIntroDurationSeconds` is present, the
player will skip the first N seconds at the start of the episode. If a
fingerprint is present, an external matcher can signal a match by writing the
matched key into `localStorage` as `bw_intro_fp_match`. You can also edit these
values per series/season from the in-app “Skip Settings” panel.

## Data Files

- `progress.json`: persisted progress by series.
- `intro_fingerprints.json`: optional intro fingerprint configuration.
- `settings.json`: app settings.

## Sidebar Highlights

- **Series list** with last watched time.
- **Provider tabs** to filter s.to vs. aniworld.to.
- **Per-series controls** for intro duration/fingerprint and end skip windows.
- **Quick actions**: skip episode, open settings, quit.

## Troubleshooting

- **GeckoDriver not found**: Ensure `geckodriver.exe` sits next to `s.toBot.py`.
- **Video not playing**: Refresh the page or press Space to play.
- **Sidebar missing**: Reload; some pages block injection until fully loaded.

## Project Structure

```
SerienJunkie/
├── s.toBot.py              # Main script
├── requirements.txt        # Dependencies
├── README.md               # This file
├── geckodriver.exe         # Firefox WebDriver
├── progress.json           # Progress database (auto-created)
├── intro_fingerprints.json # Optional intro fingerprint data
└── user.BingeWatcher/      # Firefox profile (auto-created)
```

## Feature Requests

Feature requests and bug reports are welcome. Please open an issue with as much
context as possible (environment, steps to reproduce, and expected behavior).

## License

This repository uses **CC BY-NC 4.0** (non-commercial) and includes a custom
**Educational Use Only** notice. Both are required.

### Educational Use Only Notice

This project is provided strictly for **educational review**. You may read and
study the code, but you may **not** use it operationally, deploy it, or use it
to access or automate any streaming platform.
