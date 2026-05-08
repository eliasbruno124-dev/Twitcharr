# Twitcharr

**Ad-free Twitch live-TV for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr).**

The premium one-click Twitch lineup. Type Twitch logins, click **Sync** — Twitcharr builds the entire lineup, keeps it fresh every 2 minutes, plays streams **ad-free** through the auto-updated `streamlink-ttvlol` plugin, picks the highest quality your bandwidth can sustain without stuttering, and pushes the guide into Emby or Jellyfin automatically.

> **No Twitch login. No Client ID. No Client Secret. No OAuth. No extra container.**

> ⚠️ Educational and personal use only. Twitcharr is a Dispatcharr integration that, at runtime, downloads and uses the third-party [`streamlink-ttvlol`](https://github.com/2bc4/streamlink-ttvlol) project for ad-bypass playback. The author is not affiliated with Twitch, Inc. or with any proxy operator. Using this plugin may violate Twitch's Terms of Service. Use at your own risk.

![Plugin settings overview](docs/01-plugin-settings.png)

---

## Why Twitcharr

| Feature | What it does |
|---|---|
| **Ad-free playback** | The auto-downloaded [`streamlink-ttvlol`](https://github.com/2bc4/streamlink-ttvlol) plugin routes Twitch HLS through public proxies that strip Twitch's pre-roll and mid-roll ads. Playback starts clean. |
| **Smooth channel discovery** | Mix plain logins with `top:de:25` (top 25 German live streams), `game:Just Chatting:10` (top 10 in a category), or `search:gronkh` (channel name search). Or use the dedicated **Trending — countries** field to type ISO codes like `de, en, fr` and have top streamers auto-added every cycle. |
| **Adaptive quality, no stutter** | Built-in 3-probe Cloudflare bandwidth measurement with **minimum-throughput** sampling and a 50 % default safety margin — Twitcharr picks the highest variant your line can *sustain*, not the highest it can briefly hit. Result: best possible quality with zero buffering. |
| **Fastest startup** | `--hls-live-edge 1`, lowered HLS playlist reload, 4 segment threads, no-retry-on-first-segment. Channel switch → first frame in ~2 seconds on a healthy line. |
| **Live preview thumbnails** | The channel icon in your Live TV grid is the streamer's actual current frame, refreshed every 2 minutes (cache-busted). The grid shows what's literally on screen right now, not stale game art. |
| **Live channel grid** | Programme titles always lead with the streamer's name (`🔴 Gronkh • Just Chatting`), so your Live TV grid says *who* is streaming. Description includes stream title, current game, live uptime (`Live seit 2h 34min`), and viewer count. |
| **Always-on placeholder** | When nobody you follow is live, a sentinel "no streams online" channel is kept in the lineup — Emby / Jellyfin Live TV never collapses to an empty section. |
| **Discord go-live alerts** | Drop a Discord webhook URL in the settings and Twitcharr posts a rich embed (with thumbnail, game, viewer count) every time a streamer in your lineup goes live. |
| **Emby / Jellyfin auto-refresh** | After every EPG cycle Twitcharr triggers the media server's "Refresh Guide" task via `/ScheduledTasks/Running/{id}`. Your Live TV section always shows the freshest data. |
| **Self-update from GitHub** | A built-in updater checks GitHub Releases every 6 hours. One click to apply — no zip uploading, no copying files. |
| **Health-check + proxy diagnostics** | A single button gives you a top-to-bottom system status (Twitch API, streamlink, ttv.lol, every proxy URL with latency, Emby/Jellyfin handshake, channel counts). |
| **Direct EPG injection** | Writes both an XMLTV file *and* Dispatcharr's `EPGData` / `ProgramData` rows, and links them to the Channels in the same cycle — guide is visible on the very first sync. |

---

## Install

**UI import**

1. Download / zip the [`twitcharr/`](twitcharr/) folder.
2. Dispatcharr → Plugins → Import → upload the zip.
3. Enable the plugin.

**File copy**

1. Copy `twitcharr/` to `/app/data/plugins/` inside the Dispatcharr container.
2. Dispatcharr → Plugins → refresh → enable the plugin.

After that, all future updates go through the **Apply update** button — no more manual zip handling.

![Plugin actions bar](docs/02-actions.png)

---

## Setup

In the plugin settings, fill **Twitch logins / discovery** — anything goes:

```text
gronkh
papaplatte
knossi
montanablack88

top:de:25
game:Just Chatting:10
search:trymacs
```

URLs work too:

```text
https://www.twitch.tv/gronkh
```

For convenience, you can also fill the dedicated UI fields:

- **Trending — countries (ISO codes):** `de, en, fr`
- **Trending — top streams per country:** `25`
- **Trending — global top streams:** `10`

Click **Sync now**. Twitcharr will:

- download / update `streamlink-ttvlol` (ad bypass);
- create the ad-free, low-latency StreamProfile;
- create a Twitch channel group;
- resolve every login + discovery token + ISO-country trending into a flat unique list;
- create / update one Channel + Stream per result;
- write `<data_dir>/twitch.xmltv` and the matching `EPGData` / `ProgramData` rows;
- link every Channel to its EPG row;
- enable the scheduler (default: every **2 minutes**);
- trigger an Emby / Jellyfin guide refresh if configured;
- post Discord alerts for any streamer that just went live.

![Live channel grid in Emby](docs/03-channel-grid.png)

---

## Discovery tokens

| Token | Resolves to |
|---|---|
| `gronkh` | the single channel `gronkh` |
| `top` | top 10 live streams globally (by viewer count) |
| `top:25` | top 25 live streams globally |
| `top:de:25` | top 25 live streams whose language is German |
| `top:de,en:50` | top 50 streams in German or English |
| `game:Just Chatting` | top 10 live streams in the Just Chatting category |
| `game:Grand Theft Auto V:25` | top 25 GTA V streams |
| `search:gronkh` | first 10 channel-search hits for "gronkh" |
| `search:cooking:5` | first 5 channel-search hits for "cooking" |

Mix them freely. Duplicates are removed automatically.

![EPG detail view with live programme](docs/04-epg-detail.png)

---

## Settings reference

| Setting | Default | Notes |
|---|---|---|
| Channel group name | `Twitch` | |
| Starting channel number | `9000` | First number used for new channels — existing numbers never overwritten. |
| **Stream quality** | `adaptive` | `adaptive` picks the highest variant your bandwidth can sustain. Falls back through a chain. |
| Connection bandwidth (Mbps) | `0` | `0` = use last `Measure bandwidth` result, or a safe mid-tier fallback if never measured. |
| Bandwidth safety margin (%) | `50` | Headroom on top of nominal Twitch bitrate. 50 % is the sweet spot for "no stutter". |
| **Fastest possible startup** | `on` | `--hls-live-edge 1`, lower segment timeouts, 4 segment threads. |
| Low-latency mode | `on` | Twitch's `--twitch-low-latency` + segmented HLS. |
| **Show offline channels** | `on` | When `off`, offline channels are pruned and re-created when live. |
| Offline channel icon | _(empty)_ | URL of an icon used while offline. Empty = **no image**. |
| Always keep "no streams online" channel | `on` | Sentinel so Emby / Jellyfin Live TV never has zero channels. |
| **Live preview thumbnails** | `on` | Channel icon = streamer's current live frame. Refreshed each cycle. |
| Use profile picture for Just Chatting | `on` | Only used when live thumbnails are off. |
| **EPG refresh interval** | `2 min` | |
| Daily ttv.lol update time | `04:30` | |
| ttv.lol proxy servers | EU defaults | Comma-separated. Test with **Test proxies** action. |
| Enable automatic scheduler | `on` | |
| **Trending — countries (ISO codes)** | _(empty)_ | e.g. `de, en, fr`. |
| **Trending — top streams per country** | `0` | 0 disables. Most users want 10–25. |
| **Trending — global top streams** | `0` | 0 disables. |
| **Discord webhook for go-live alerts** | _(empty)_ | Discord webhook URL. Optional. |
| Emby / Jellyfin URL | _(empty)_ | e.g. `http://jellyfin:8096`. |
| Emby / Jellyfin API key | _(empty)_ | |
| Auto-check for plugin updates | `on` | Polls GitHub every 6 hours. Updates are never auto-applied. |
| Data directory | `/app/data/plugins/twitcharr` | XMLTV + ttv.lol cache live here. |

![Settings — quality + bandwidth](docs/05-settings-quality.png)

---

## Actions

| Action | What it does |
|---|---|
| **Sync now** | Full one-click setup. Idempotent. |
| **Refresh EPG** | Re-fetch live status, refresh XMLTV + Dispatcharr EPG rows, trigger Emby/Jellyfin, post Discord alerts. |
| **Sync channels** | Re-create Channels + Streams without touching the guide. |
| **Run full refresh** | ttv.lol → channels → EPG, with Twitch fetched only once. |
| **System status / health-check** | Top-to-bottom diagnostic. |
| **Test ttv.lol proxies** | Probes every proxy in parallel; reports HTTP code + latency. |
| **Send Discord test** | Posts a one-off test embed to the configured webhook. |
| **Measure bandwidth** | Three Cloudflare probes; minimum is persisted and used for adaptive quality. |
| **Refresh media server** | Manual trigger of the Emby / Jellyfin "Refresh Guide" task. |
| **Update ttv.lol** | Force a fresh download of `twitch.py` regardless of ETag. |
| **Check for plugin update** | Look up the latest GitHub release. |
| **Apply plugin update** | Download + overwrite the plugin in-place. |
| **♥ Donate / Support** | GitHub Sponsors link. |
| **Uninstall** | Remove every Channel / Stream / EPGSource / StreamProfile this plugin created. The plugin itself stays. |

![Health-check output](docs/06-health-check.png)

---

## Discord notifications

Drop a Discord webhook URL in the settings and Twitcharr will post an embed every time a streamer goes from offline → live. The embed includes the streamer name, stream title, current game, viewer count, profile picture, and a **live preview thumbnail** of the current stream frame.

![Discord go-live notification](docs/07-discord-notification.png)

To test: configure the webhook → click **Send Discord test**.

---

## Streaming defaults

The generated StreamProfile is tuned for ad-free, fast startup, low latency. With **adaptive quality** + **fast startup** turned on (defaults), the parameters are roughly:

```text
streamlink
  --loglevel warning
  --stdout
  --plugin-dir /app/data/plugins/twitcharr/streamlink_plugins
  --http-timeout 5
  --stream-segment-attempts 1
  --stream-segment-timeout 4
  --stream-timeout 10
  --twitch-disable-ads
  --twitch-proxy-playlist-fallback
  --retry-streams 1
  --retry-max 2
  --hls-playlist-reload-attempts 2
  --hls-playlist-reload-time segment
  --twitch-proxy-playlist https://eu.luminous.dev,https://eu2.luminous.dev,https://lb-eu.cdn-perfprod.com,https://lb-eu2.cdn-perfprod.com
  --twitch-low-latency
  --hls-live-edge 1
  --stream-segment-threads 4
  --hls-segment-stream-data
  {streamUrl}
  720p60,720p,480p,best         # ← chain picked from your measured bandwidth
```

---

## EPG behaviour

The guide follows twitch2tuner's practical model:

- live streams start at Twitch's `createdAt` time and end `now + 24h`;
- offline channels get a 24h offline placeholder (with a custom or empty icon);
- programme title always leads with the streamer name (`🔴 Gronkh • Just Chatting`);
- description includes stream title, current game, live uptime, viewer count;
- channel icon is the live preview thumbnail when live, profile picture when offline;
- XMLTV is written to disk **and** mirrored into Dispatcharr's `EPGData` / `ProgramData`;
- every Channel is linked to its EPGData row in the same cycle, so the guide shows up on the very first sync.

---

## Self-update

Twitcharr checks GitHub Releases for newer versions every 6 hours (toggle via **Auto-check for plugin updates**). When you click **Apply update**:

1. The latest release zip is downloaded.
2. Files are extracted and copied over the live plugin directory.
3. Your `plugin.json` is left untouched (so manifest tweaks survive).
4. Dispatcharr's plugin list reload picks up the new code.

A container restart is the safest way to make sure Python re-imports everything. The plugin will tell you so in the action result.

![Self-update flow](docs/08-self-update.png)

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Channel does not appear | Check the Twitch login spelling (or discovery token), then click **Sync now**. |
| Stream does not start | Click **Update ttv.lol**. Or **Test proxies** to find dead ones. |
| `streamlink` command missing | Use the official Dispatcharr image or install Streamlink in your container. |
| EPG looks stale | Click **Refresh EPG** or wait for the next 2-minute refresh. |
| Stream stutters | Click **Measure bandwidth** — your line slowed down; the new measurement automatically tightens the quality chain. Or raise the safety margin to 70-100. |
| Slow channel switch | Click **Test proxies** — remove dead ones from the list. |
| Emby / Jellyfin Live TV is empty when nobody is live | Make sure **Always keep 'no streams online' channel** is ON. |
| Emby / Jellyfin guide not refreshing | Click **Refresh media server** to test the API key — the action result tells you exactly which task it triggered. |
| Discord notifications missing | Click **Send Discord test**. If that works, the webhook is fine — go-live alerts only fire on offline → live transitions. |

Or just run **System status** for a full report.

---

## Donate / support

If Twitcharr earns you a place on the couch, you can support development at  
**[github.com/sponsors/eliasbruno124](https://github.com/sponsors/eliasbruno124)** ♥

---

## Sources

- [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr)
- [streamlink-ttvlol](https://github.com/2bc4/streamlink-ttvlol)
- [twitch2tuner](https://github.com/micahmo/twitch2tuner)
- [Streamlink plugin sideloading](https://streamlink.github.io/latest/cli/plugin-sideloading.html)
- [Streamlink Twitch low-latency docs](https://streamlink.github.io/latest/cli/plugins/twitch.html)
- [Jellyfin Scheduled Tasks API](https://api.jellyfin.org/#tag/ScheduledTasks)
- [Discord webhook docs](https://discord.com/developers/docs/resources/webhook)
