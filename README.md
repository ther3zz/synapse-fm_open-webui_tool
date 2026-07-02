# SynapseFM Integration Tool for Open WebUI

Stream live AI-generated radio from [SynapseFM](https://synapse-fm.ai) directly in your Open WebUI chat interface.

## Features

- 🎵 **Browse Stations** — List all available SynapseFM radio stations
- 🎶 **Now Playing** — See what's currently playing on any station (artist, title, genre)
- ▶️ **Live Playback** — Persistent floating player with glassmorphism UI
- 🔄 **Station Switching** — Change stations without leaving the player
- 📊 **Live Metadata** — Auto-refreshing track info with artist and style display
- 🔒 **Secure** — HTTPS-only, encrypted credentials, zero PII exposure
- 🔁 **Update-Resilient** — Player automatically re-injects after Open WebUI updates

## Prerequisites

1. **SynapseFM** instance with external streaming enabled
2. **Stream Key** generated from your SynapseFM account (Settings → Stream Keys)
3. **Open WebUI** instance (v0.3.0+ recommended)

## Installation

1. Copy the contents of [`dist/synapsefm_tool.py`](dist/synapsefm_tool.py)
2. In Open WebUI, go to **Workspace → Tools → Add Tool**
3. Paste the code and save
4. Click the gear icon → **Valves** and configure:
   - `synapsefm_url`: Your SynapseFM URL (e.g., `https://synapse-fm.ai`)
   - `stream_key`: Your SynapseFM stream key (starts with `sfm_`)

### Configuration

| Valve | Default | Description |
|-------|---------|-------------|
| `synapsefm_url` | `https://synapse-fm.ai` | SynapseFM instance URL (HTTPS required) |
| `stream_key` | *(empty)* | SynapseFM Stream Key (`sfm_...`) |
| `request_timeout` | `10` | Request timeout in seconds (1-30) |
| `max_stations` | `25` | Maximum stations in LLM responses (1-50) |

## Usage

Once installed and configured, ask the AI:

- *"What stations are available on SynapseFM?"*
- *"What's playing on the Electronic station?"*
- *"Play the Lo-Fi station"*

The AI will use the appropriate tool to respond. For playback, a floating audio player appears at the top of the page and persists across chat navigation.

### Player Controls

- **Play/Pause** — Toggle playback
- **Volume** — Adjustable slider
- **Collapse** — Click the tab to slide the player out of view
- **Close** — Stop playback and dismiss the player

## Stream Key Rotation

If you need to rotate your stream key (e.g., suspected compromise):

1. **Generate** a new key in SynapseFM → Settings → Stream Keys
2. **Update** the `stream_key` Valve in Open WebUI → Tool Settings
3. **Revoke** the old key in SynapseFM → Settings → Stream Keys
4. **Verify** by asking *"What stations are on SynapseFM?"*

> ⚠️ **Important**: Always generate the new key BEFORE revoking the old one to avoid downtime.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Stream key is invalid" | Verify your key in SynapseFM → Settings → Stream Keys |
| "External streaming disabled" | Enable it in SynapseFM Admin → Settings |
| "Could not connect" | Check that Open WebUI can reach your SynapseFM URL |
| "No stations available" | Ensure at least one station is enabled in SynapseFM |
| No audio in player | Check browser console. Try clicking the play button |
| Player disappeared after update | It will auto-reinject on next `play_station` call |

## Development

### Project Structure

```
synapse-fm_open-webui_tool/
├── synapsefm_tool.py          # Source file (modular, for development)
├── bundle.py                  # Build script — produces dist/synapsefm_tool.py
├── dist/
│   └── synapsefm_tool.py      # Bundled single-file (paste into Open WebUI)
├── modules/
│   ├── sanitizer.py           # Output sanitization (LLM + HTML)
│   ├── http_client.py         # HTTP client
│   ├── player_builder.py      # HTML player template + postMessage bridge
│   └── bootloader.py          # Persistent player injection + streaming engine
└── README.md
```

> **Note**: Open WebUI requires tools to be self-contained in a single Python file.
> The `modules/` directory exists for development only.
> Always run `python bundle.py` before deploying.

### Build

```bash
# Generate dist/synapsefm_tool.py
python bundle.py

# Custom output filename
python bundle.py --output my_tool.py
```

The bundler includes pre-flight validation that checks for encoding corruption and syntax errors before producing output.

## License

MIT
