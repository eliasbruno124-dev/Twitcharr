# Dispatcharr Twitch EPG

One-field Twitch plugin for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr).

You only enter Twitch channel logins. The plugin automatically:

- downloads and daily-updates the latest [streamlink-ttvlol](https://github.com/2bc4/streamlink-ttvlol) `twitch.py`,
- creates low-latency Streamlink playback optimized for quick startup,
- creates Twitch Channels/Streams in Dispatcharr,
- generates a [twitch2tuner](https://github.com/micahmo/twitch2tuner)-style XMLTV guide,
- mirrors the guide directly into Dispatcharr's EPG tables.

No Twitch login, no Client ID, no Client Secret, no OAuth, no extra container.

The actual plugin lives in [`dispatcharr_twitch_epg/`](dispatcharr_twitch_epg/).
