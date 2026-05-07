# Dispatcharr Twitch EPG

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that turns Twitch into a regular live-TV lineup — with **ad-bypass low-latency playback** (auto-updated [streamlink-ttvlol](https://github.com/2bc4/streamlink-ttvlol)) and a **continuously refreshed XMLTV guide** in the spirit of [twitch2tuner](https://github.com/micahmo/twitch2tuner).

The actual plugin lives in [`dispatcharr_twitch_epg/`](dispatcharr_twitch_epg/) — see its [README](dispatcharr_twitch_epg/README.md) for install and usage details.

## What it does

- Downloads the latest `twitch.py` from `2bc4/streamlink-ttvlol` once per day (configurable cron) and feeds it to streamlink via `--plugin-dirs`. Your system streamlink is never touched.
- Creates a Dispatcharr **Stream Profile** with low-latency flags (`--twitch-low-latency`, `--hls-live-edge 2`, parallel segment threads) so streams start in ~1–2 s.
- Reads a list of Twitch logins from the plugin settings, calls Twitch Helix, and creates one Channel + Stream per login (numbered from 9000 by default).
- Refreshes an XMLTV guide every 15 min (live status, current game, stream title, profile/box-art icons) — written both to disk and directly into Dispatcharr's `EPGData` / `ProgramData` tables.
- Provides UI buttons for one-shot runs and a single *Run everything now* action that mirrors the scheduled run.

No Docker sidecar, no manual M3U/EPG configuration — everything happens inside Dispatcharr.
