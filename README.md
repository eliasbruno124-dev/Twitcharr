# Twitcharr

Twitcharr is a Twitch live-TV plugin for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr). It creates Dispatcharr Channels, Streams and XMLTV guide data from Twitch channel names or discovery tokens.

No Twitch account login, OAuth token, API key, Client ID or Client Secret is required. Twitch metadata is fetched anonymously through Twitch's public web GraphQL endpoint.

Important: Twitcharr can download and use the third-party [`streamlink-ttvlol`](https://github.com/2bc4/streamlink-ttvlol) Streamlink plugin. The author is not affiliated with Twitch, Dispatcharr, streamlink-ttvlol or any proxy operator. Use at your own risk and follow the rules that apply to your setup.

## What Works

| Feature | Status |
|---|---|
| Twitch channel input | Channel names can be separated by commas or line breaks. Twitch URLs also work. |
| Discovery tokens | Supports `top`, `top:de:25`, `game:Just Chatting:10` and `search:gronkh`. |
| Dispatcharr objects | Creates and updates managed Channels, Streams, StreamProfile and EPG rows. |
| XMLTV | Writes `<data_dir>/twitch.xmltv`. |
| Instant guide link | New or changed Channels are linked to freshly written Twitcharr EPG rows in the same sync cycle. |
| Plugin logo | Includes `logo.png` so Twitcharr has its own icon on the Dispatcharr plugin card. |
| Offline handling | Can keep offline channels or remove offline channels while they are offline. |
| Offline artwork | Uses Twitch profile images for offline streamers and shows offline guide tiles as `⚫ Offline`. |
| DB-safe artwork URLs | Keeps Dispatcharr Logo and EPG icon URLs below the 500-character database limit. |
| Adaptive quality | Measures bandwidth and updates the Streamlink quality fallback chain. |
| Short StreamProfile parameters | Stores long Streamlink options in `<data_dir>/twitcharr.streamlinkrc` so Dispatcharr's 500-character parameter field is not exceeded. |
| ttv.lol update | Downloads the latest `twitch.py` for Streamlink when requested or scheduled. |
| Emby / Jellyfin | Triggers the server's Refresh Guide task after scheduled EPG refreshes if URL and media-server token are configured. |
| Diagnostics | Includes proxy and bandwidth checks. |

Built-in plugin artwork:

![Twitcharr plugin logo](twitcharr/logo.png)

Plugin settings screenshot:

![Plugin settings overview](docs/01-plugin-settings.png)

## Install

### Import ZIP

1. Zip the `twitcharr/` folder, or use `twitcharr.zip` from this repository.
2. Open Dispatcharr.
3. Go to Plugins.
4. Import the ZIP.
5. Enable Twitcharr.

### Copy Folder

1. Copy `twitcharr/` into `/app/data/plugins/` inside the Dispatcharr container.
2. Refresh the Dispatcharr plugin list.
3. Enable Twitcharr.

Dispatcharr reads `twitcharr/logo.png` automatically and shows it on the plugin card after the plugin list is refreshed or the imported ZIP is installed.

Actions screenshot:

![Plugin actions](docs/02-actions.png)

## Quick Setup

Open the Twitcharr plugin settings and fill **Twitch channels / discovery**.

Comma-separated channel names:

```text
gronkh, papaplatte, knossi
```

Line-separated channel names:

```text
gronkh
papaplatte
knossi
```

Twitch URLs:

```text
https://www.twitch.tv/gronkh
```

Discovery tokens:

```text
top:de:25
game:Just Chatting:10
search:trymacs
```

Then click **Sync now**.

Twitcharr will refresh Twitch metadata, write XMLTV and EPG rows, sync Channels and Streams, update the StreamProfile and start the scheduler if enabled.

Do not paste OAuth tokens, API keys or Twitch account credentials into the channel field. They are not used.

Live TV grid screenshot:

![Live channel grid](docs/03-channel-grid.png)

## Discovery Tokens

| Token | Meaning |
|---|---|
| `gronkh` | Adds one channel by channel name. |
| `top` | Adds the top 10 live streams globally. |
| `top:25` | Adds the top 25 live streams globally. |
| `top:de:25` | Adds the top 25 German-language live streams. |
| `top:de,en:50` | Adds the top 50 streams in German or English. |
| `game:Just Chatting` | Adds the top 10 streams in that category. |
| `game:Just Chatting:25` | Adds the top 25 streams in that category. |
| `search:gronkh` | Adds the first 10 channel-search results. |
| `search:cooking:5` | Adds the first 5 channel-search results. |

For category or search names that contain commas, put the token on its own line.

Guide detail screenshot:

![EPG detail](docs/04-epg-detail.png)

## Important Settings

| Setting | Default | What it does |
|---|---|---|
| Twitch channels / discovery | empty | Channel names, URLs or discovery tokens. Commas and line breaks are accepted. |
| Stream quality | `adaptive` | Builds a Streamlink quality fallback chain from measured or manual bandwidth. |
| Connection bandwidth (Mbps) | `0` | `0` uses the last measured value or a conservative fallback. |
| Bandwidth safety margin (%) | `50` | Extra headroom used by adaptive quality. |
| Show offline channels | on | Keeps offline streamers in the lineup. Turn off to prune them while offline. |
| EPG refresh interval | `2` minutes | Scheduler interval for Twitch status and guide refresh. |
| ttv.lol proxy servers | EU defaults | Comma-separated proxy playlist URLs. Clear to disable. |
| Emby / Jellyfin URL | empty | Optional media-server base URL. |
| Emby / Jellyfin access token | empty | Optional media-server token for Emby/Jellyfin only. This is not a Twitch key. |
| Auto-check for plugin updates | on | Checks GitHub Releases every 6 hours. |
| Auto-apply plugin updates | on | Applies newer GitHub Releases automatically; reload/restart Dispatcharr afterwards. |
| Data directory | `/app/data/plugins/twitcharr` | Stores XMLTV, scheduler state, Streamlink config and the downloaded Streamlink plugin. |

Quality settings screenshot:

![Quality and bandwidth settings](docs/05-settings-quality.png)

## Actions

| Action | Use it for |
|---|---|
| Sync now | Full setup and refresh. |
| Refresh guide | Refresh Twitch status, XMLTV and EPG rows. |
| Sync channels | Writes current guide rows first, creates or updates Channels and Streams, links EPG immediately and refreshes Emby/Jellyfin if configured. |
| Full refresh | ttv.lol check, channel sync and guide refresh. |
| Test proxies | Proxy HTTP status and latency. |
| Measure bandwidth | Runs quick download probes, saves Mbps and updates adaptive quality. |
| Refresh media server | Triggers Emby/Jellyfin Refresh Guide. |
| Update ttv.lol | Forces a fresh `twitch.py` download. |
| Update plugin | Checks GitHub Releases and applies the latest release. |
| Uninstall | Removes managed Dispatcharr objects and triggers one Emby/Jellyfin guide refresh if configured. Plugin files and settings remain. |

Health-check screenshot:

![Health-check output](docs/06-health-check.png)

## Offline Behavior

`Show offline channels` controls real streamer channels:

- ON: offline streamers stay visible with offline guide data.
- OFF: offline streamers are removed during sync and recreated when they go live.

When no configured channel is included in the current lineup, Twitcharr removes the managed channels instead of creating a placeholder channel.

## EPG And Images

Twitcharr does not burn guide data into the video stream. That would require video overlays or transcoding and would make the stream worse.

Instead it writes guide data where TV clients expect it:

- Dispatcharr `EPGData` and `ProgramData` rows
- `<data_dir>/twitch.xmltv`
- channel icons, using current game/category artwork for live streams
- programme icons in XMLTV, using category/game artwork when an icon is available
- rich programme titles with streamer, category and current Twitch viewer count
- Twitch profile images for offline streamers, with offline guide tiles named `⚫ Offline`

For Emby and Jellyfin, the plugin triggers the server's Refresh Guide task after scheduled EPG refreshes. That is still required because those servers cache Live TV guide data.

Channel syncs also link freshly-created Dispatcharr Channels to the current Twitcharr EPG rows immediately, then trigger the media-server guide refresh and warm Live TV image cache entries.

Twitcharr keeps artwork URLs stored in Dispatcharr database fields short enough for Dispatcharr's 500-character URL columns. Offline streamers use their Twitch profile image because Emby/Jellyfin render those thumbnails more reliably in Live TV tiles; the guide title carries the `⚫ Offline` state.

## Troubleshooting

| Problem | What to try |
|---|---|
| No channels appear | Check channel names, then run **Sync now**. |
| OAuth/API-key confusion | Remove Twitch OAuth/API-key text from the channel field. Twitcharr does not need Twitch credentials. |
| Offline channels do not disappear | Turn **Show offline channels** off, then run **Sync channels**. |
| Streams do not start | Run **Update ttv.lol** and **Test proxies**. Twitcharr disables Streamlink's browser flow and relies on ttv.lol playlist proxies for restricted streams. |
| `streamlink` is missing | Install Streamlink in the Dispatcharr container. |
| Guide looks stale | Run **Refresh guide**. |
| Offline image is broken in Emby/Jellyfin | Run **Sync channels** and **Refresh media server** so the Twitch profile-image URLs are regenerated. |
| Emby/Jellyfin does not update | Set both server URL and media-server token, then run **Refresh media server**. |
| Stuttering | Run **Measure bandwidth** or increase the safety margin. |

Self-update screenshot:

![Self-update flow](docs/08-self-update.png)

## Donate

[![Donate with PayPal](https://www.paypalobjects.com/en_US/i/btn/btn_donate_LG.gif)](https://paypal.me/eliasbruno124)

PayPal: [paypal.me/eliasbruno124](https://paypal.me/eliasbruno124)

## Sources

- [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr)
- [streamlink-ttvlol](https://github.com/2bc4/streamlink-ttvlol)
- [twitch2tuner](https://github.com/micahmo/twitch2tuner)
- [Streamlink plugin sideloading](https://streamlink.github.io/latest/cli/plugin-sideloading.html)
- [Streamlink Twitch plugin docs](https://streamlink.github.io/latest/cli/plugins/twitch.html)
- [Jellyfin Scheduled Tasks API](https://api.jellyfin.org/#tag/ScheduledTasks)
