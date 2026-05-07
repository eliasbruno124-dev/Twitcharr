# Dispatcharr Twitch EPG

A one-field Dispatcharr plugin for Twitch:

1. Install plugin.
2. Enter Twitch channel logins.
3. Click **Sync now** once.

No Twitch login, no Client ID, no Client Secret, no OAuth, no proxy settings.

The plugin creates Twitch Channels/Streams in Dispatcharr, keeps a twitch2tuner-style XMLTV guide fresh, and updates the local streamlink-ttvlol `twitch.py` automatically once per day.

## What Runs Automatically

| Part | Default |
|---|---|
| ttv.lol plugin update | Daily at 04:30 server time |
| EPG/channel refresh | Every 10 minutes |
| Playback | streamlink + auto-downloaded streamlink-ttvlol |
| Stream tuning | Low-latency, fast startup, EU proxy fallback |

The downloaded file lives at:

```text
/app/data/plugins/dispatcharr_twitch_epg/streamlink_plugins/twitch.py
```

It is loaded with Streamlink's `--plugin-dir`, so the system Streamlink install is never modified.

## Install

**UI import**

1. Zip the `dispatcharr_twitch_epg/` folder.
2. Dispatcharr -> Plugins -> Import -> upload the zip.
3. Enable the plugin.

**File copy**

1. Copy `dispatcharr_twitch_epg/` to `/app/data/plugins/` inside the Dispatcharr container.
2. Dispatcharr -> Plugins -> refresh -> enable the plugin.

## Setup

In the plugin settings, fill only:

```text
gronkh
papaplatte
knossi
montanablack88
```

Full URLs also work:

```text
https://www.twitch.tv/gronkh
```

Click **Sync now**. The plugin will:

- download/update `streamlink-ttvlol` if needed,
- create a low-latency StreamProfile,
- create a Twitch channel group,
- create/update one Channel + Stream per login,
- build `/app/data/plugins/dispatcharr_twitch_epg/twitch.xmltv`,
- write the same guide data directly into Dispatcharr's EPG tables,
- keep the scheduler enabled for future refreshes.

## Streaming Defaults

The generated StreamProfile is optimized for quick Twitch startup:

```text
streamlink
  --loglevel warning
  --stdout
  --plugin-dir /app/data/plugins/dispatcharr_twitch_epg/streamlink_plugins
  --http-timeout 10
  --stream-segment-attempts 2
  --stream-segment-timeout 6
  --stream-timeout 20
  --twitch-disable-ads
  --twitch-proxy-playlist-fallback
  --retry-streams 1
  --retry-max 2
  --twitch-proxy-playlist https://eu.luminous.dev,https://eu2.luminous.dev,https://lb-eu.cdn-perfprod.com,https://lb-eu2.cdn-perfprod.com
  --twitch-low-latency
  --hls-live-edge 2
  --stream-segment-threads 3
  --hls-segment-stream-data
  {streamUrl}
  best
```

This favors fast startup and good live latency while still falling back to direct Twitch if all playlist proxies fail.

## EPG Behavior

The guide follows twitch2tuner's practical model:

- live streams start at Twitch's `createdAt` time and end `now + 24h`,
- offline channels get a 24h offline placeholder,
- program title is the current game/category,
- description is the current stream title,
- "Just Chatting" uses the streamer's profile picture by default,
- XMLTV is written to disk and mirrored into Dispatcharr's `EPGData` / `ProgramData`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Channel does not appear | Check the Twitch login spelling, then click **Sync now**. |
| Stream does not start | Click **Repair ttv.lol now** once; the daily updater normally handles this. |
| `streamlink` command missing | Use the official Dispatcharr image or install Streamlink in your container. |
| EPG looks stale | Click **Refresh EPG** or wait for the next 10-minute refresh. |

## Sources

- [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr)
- [streamlink-ttvlol](https://github.com/2bc4/streamlink-ttvlol)
- [twitch2tuner](https://github.com/micahmo/twitch2tuner)
- [Streamlink plugin sideloading](https://streamlink.github.io/latest/cli/plugin-sideloading.html)
- [Streamlink Twitch low-latency docs](https://streamlink.github.io/latest/cli/plugins/twitch.html)
