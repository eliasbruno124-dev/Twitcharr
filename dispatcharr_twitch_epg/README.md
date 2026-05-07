# Dispatcharr Twitch EPG

A Dispatcharr plugin that brings Twitch into your live-TV lineup like a regular IPTV provider — with **ad-bypass low-latency streamlink playback** and a **continuously refreshed XMLTV guide**.

It combines three things:

| Component | What it does |
|---|---|
| [streamlink-ttvlol](https://github.com/2bc4/streamlink-ttvlol) | The plugin downloads the **latest** `twitch.py` directly from the upstream GitHub release every 24h and feeds it to streamlink via `--plugin-dirs`. Your system streamlink is never touched, and playback never breaks because the file went stale. |
| [twitch2tuner](https://github.com/micahmo/twitch2tuner)–style EPG | An XMLTV guide is rendered (`twitch.xmltv`) and **also** written directly into Dispatcharr's `EPGData` / `ProgramData` so the channel guide just works — no manual EPG-source configuration. |
| Native Dispatcharr integration | Channels, Streams, a StreamProfile, an EPG source and a ChannelGroup are created/updated automatically. No M3U or external service required. |

---

## Requirements

- Dispatcharr (recent enough to support the new `plugin.json` manifest format)
- `streamlink` available inside the Dispatcharr container (already present in the official image)
- A Twitch developer app — only **Client ID** + **Client Secret** are needed (Client-Credentials flow, no user OAuth, no redirect)

Create the app at <https://dev.twitch.tv/console/apps> with redirect URL `http://localhost` and category *Application Integration*. Then click *New Secret*.

---

## Install

**Option A — UI import**

1. Zip the `dispatcharr_twitch_epg/` folder.
2. In Dispatcharr → *Plugins* → *Import* → upload the zip.
3. Toggle the plugin on.

**Option B — File copy**

1. Copy the `dispatcharr_twitch_epg/` folder to `/app/data/plugins/` inside the container.
2. *Plugins* → click the refresh icon → enable the plugin.

---

## First-time setup

1. Open the plugin's settings card and fill in:
   - **Twitch Client ID / Secret**
   - **Twitch logins** — comma- or newline-separated (just the part after `twitch.tv/`).  
     Pasting full URLs works too (`https://twitch.tv/gronkh`).
   - Leave the rest at the defaults unless you have a reason.
2. Click **Run setup** — this downloads `twitch.py`, creates the StreamProfile and the EPG source.
3. Click **Sync channels** — creates one Channel + Stream per login, numbered from 9000 upward.
4. Click **Refresh EPG now** — fetches live status / current game / titles and renders the guide.
5. Click **Enable scheduled refresh** — registers two Celery-beat jobs:
   - **EPG refresh** every *N* minutes (default 15)
   - **ttv.lol auto-update** every day (default 04:30 server time)

Your channels now show up in the Dispatcharr lineup with live program info, and playback uses streamlink + ttv.lol with low-latency flags.

---

## Settings reference

| Field | Default | Notes |
|---|---|---|
| Twitch Client ID / Secret | – | Required. Stored as password fields. |
| Twitch logins | empty | Comma/newline list. Order = channel-number order. |
| Channel group | `Twitch` | Created if missing. |
| Starting channel number | `9000` | Plugin avoids collisions automatically. |
| ttv.lol proxy servers | `https://lb-eu.cdn-perfprod.com,https://eu.luminous.dev` | Passed verbatim to `--twitch-proxy-playlist`. Add more comma-separated entries if upstream goes down. |
| Low-latency / fast start | on | Adds `--twitch-low-latency`, `--hls-live-edge 2`, `--stream-segment-threads 3`, `--hls-segment-stream-data`. ~1–2 s faster start, slightly more CPU. |
| Stream quality | `best` | streamlink quality selector — `1080p60,1080p,best` style fallbacks supported. |
| EPG refresh interval | 15 min | Live status / current game changes propagate this fast. |
| ttv.lol update cron | `30 4 * * *` | 5-part cron, system timezone. |
| Plugin data directory | `/app/data/plugins/dispatcharr_twitch_epg` | Holds `twitch.xmltv` and `streamlink_plugins/twitch.py`. |
| Include offline channels in EPG | on | Mirrors twitch2tuner's behaviour. Off = offline channels disappear. |
| Use profile pic for "Just Chatting" | on | Otherwise the Just Chatting category art is used. |

---

## Actions

| Action | What it does |
|---|---|
| **Initial setup** | Idempotent — downloads ttv.lol, creates the StreamProfile + EPG source. |
| **Sync channels** | Upserts Channel/Stream rows from the configured logins. |
| **Refresh EPG now** | One-shot Helix call → XMLTV file + DB EPG rows. |
| **Update streamlink-ttvlol now** | Forces a download regardless of ETag. |
| **Run everything now** | Same task as the scheduled run (ttv.lol + sync + EPG). |
| **Enable / Disable scheduled refresh** | Toggles the two Celery-beat jobs. |
| **Uninstall managed objects** | Deletes the StreamProfile, EPG source, and every Channel/Stream this plugin created. The plugin and its settings stay in place. |

---

## How playback works

The plugin creates a Dispatcharr **Stream Profile** named *"Twitch (ttv.lol low-latency)"* with:

```text
streamlink \
    --stdout \
    --plugin-dirs /app/data/plugins/dispatcharr_twitch_epg/streamlink_plugins \
    --twitch-disable-ads \
    --twitch-proxy-playlist-fallback \
    --http-header User-Agent={userAgent} \
    --retry-streams 1 --retry-max 3 \
    --twitch-proxy-playlist <your proxies> \
    --twitch-low-latency --hls-live-edge 2 --stream-segment-threads 3 --hls-segment-stream-data \
    {streamUrl} <quality>
```

`{streamUrl}` is rendered as `https://twitch.tv/<login>`. `--plugin-dirs` makes streamlink prefer the auto-updated ttv.lol `twitch.py` over its built-in one, so the system streamlink install is never modified.

If a proxy server is down the `--twitch-proxy-playlist-fallback` flag falls back to direct Twitch (with ads) instead of erroring.

---

## EPG details

The XMLTV produced is intentionally close to what [twitch2tuner](https://github.com/micahmo/twitch2tuner) emits:

- One `<channel>` per Twitch login, with `id="twitch.<login>"`, both display name and login as `<display-name>`, profile/game-art icon and a `<url>https://twitch.tv/<login></url>`.
- One live `<programme>` from `started_at` (or *now* fallback) to `now + 24h` while live, otherwise an "offline" placeholder of 24h.
- Title prefixed with `•` to make it easy to spot live channels (`• Just Chatting`, `• Counter-Strike 2`, …).
- Description = the streamer's current stream title; for offline channels = the channel description.
- Box art rendered at 272x380 (the de-facto standard).

The same data is written straight into Dispatcharr's EPG tables so the plugin works without a separate EPG parsing pass. The XMLTV file is still produced for users who want to re-share it externally.

---

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| Plugin action returns `Twitch Client ID and Client Secret must be set` | Fill them on the plugin settings card. |
| `Downloaded file does not look like a streamlink Twitch plugin` | GitHub returned an error page (rate-limit / outage). Retry later or click *Update streamlink-ttvlol now*. |
| `command not found: streamlink` in stream errors | streamlink isn't installed in your container. Use the official Dispatcharr image, or `pip install streamlink` into your custom build. |
| Streams play but show ads | The proxy server you configured might be filtering on country / IP. Add another from the [streamlink-ttvlol README](https://github.com/2bc4/streamlink-ttvlol#proxy-servers). |
| Channels appear without a guide | Run *Refresh EPG now* once. Subsequent runs happen via the periodic task. |
| Periodic task doesn't fire | Make sure the plugin is **enabled** before clicking *Enable scheduled refresh* — Celery worker only knows about the tasks once the plugin module has been imported. |

---

## File layout

```text
dispatcharr_twitch_epg/
├── plugin.json              # manifest + UI fields/actions
├── plugin.py                # Plugin class, action routing, Celery beat tasks
├── twitch_api.py            # Helix client (Client-Credentials flow)
├── ttvlol.py                # Auto-updater for streamlink-ttvlol/twitch.py
├── epg.py                   # XMLTV writer + direct EPGData/ProgramData upserts
├── streamlink_setup.py      # StreamProfile / Channel / Stream / Logo upserts
└── README.md                # this file
```

At runtime the plugin owns:

```text
<data_dir>/
├── streamlink_plugins/
│   ├── twitch.py            # auto-updated streamlink-ttvlol plugin
│   └── .state.json          # ETag / last-check / release tag
└── twitch.xmltv             # rendered guide (also mirrored into the DB)
```

---

## License

Dispatcharr Twitch EPG plugin — under the same license as the parent repo (see `LICENSE`). The streamlink-ttvlol `twitch.py` is downloaded at runtime and remains under its upstream license.
