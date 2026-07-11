# Twitcharr

Twitcharr is a Twitch live-TV plugin for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr).
It turns Twitch channel names, Twitch URLs, and a few discovery tokens into managed
Dispatcharr Channels, Streams, StreamProfile entries, and guide data.

No Twitch login, OAuth token, Client ID, Client Secret, or Twitch API key is used.
Twitcharr reads public Twitch web metadata anonymously. Playback still depends on
Streamlink inside the Dispatcharr container, and Twitch or proxy changes can break
individual streams.

## Features

| Area | Current behavior |
|---|---|
| Channel input | Accepts Twitch login names and Twitch URLs, separated by commas, semicolons, or line breaks. |
| Discovery | Supports `top`, `top:25`, `top:de:25`, `top:de,en:50`, `game:Just Chatting:10`, and `search:gronkh:5`. |
| Channel names | Supports an optional prefix and suffix while retaining the broadcaster name when offline. |
| Channel profiles | Adds managed channels to Dispatcharr channel profiles: globally via the **Channel profiles** setting, per entry via `name(profile1, profile2)`. |
| Dispatcharr objects | Creates and updates Twitcharr-owned Channels, Streams, a Channel Group, an EPG source, one StreamProfile, and an Emby-safe OutputProfile. |
| Guide data | Writes Dispatcharr `EPGData` / `ProgramData` rows and `<data_dir>/twitch.xmltv`. |
| ttv.lol | Checks for and installs the newest stable streamlink-ttvlol release after SHA-256 verification. |
| Offline channels | **Show offline channels** is on by default so Emby/Jellyfin lineups stay stable. Turn it off for a live-only lineup. |
| Images | Uses stable Twitch channel avatars for channel logos by default and category artwork for programme images. Category logos remain selectable. |
| Media servers | Can trigger the Emby/Jellyfin `Refresh Guide` scheduled task when URL and API key are configured. |

## Install

### Import ZIP

1. Download `twitcharr.zip` from the latest GitHub release.
2. Open Dispatcharr.
3. Go to Plugins.
4. Import the ZIP.
5. Enable Twitcharr.


## Quick Setup

Open the Twitcharr plugin settings and fill **Twitch channels and discovery**.

Examples:

```text
gronkh, papaplatte, knossi
https://www.twitch.tv/gronkh
top:de:25
game:Just Chatting:10
search:trymacs:5
```

Then click **Sync now**.

Global prefix and suffix settings apply to every channel. To format individual
channels differently, add one template per line under **Per-channel name templates**:

```text
handofblood = TTV | {name}
tolkin = {name} | TTV
```

`Sync now` creates or updates the StreamProfile, the Emby-safe OutputProfile,
EPG source, guide rows, Channels, Streams, and the background scheduler. If no
Twitch channels are configured, setup still prepares the StreamProfile,
OutputProfile, EPG source, ttv.lol file, and scheduler.

For Emby or Jellyfin, the safest option is the Twitcharr media-server M3U with
the managed OutputProfile and MPEG-TS forced:

```text
http://dispatcharr:9191/output/m3u?tvg_id_source=tvg_id&output_format=mpegts&output_profile=<output_profile_id>
```

The `<output_profile_id>` is returned by **Sync now**, **Sync channels**, and
**Measure bandwidth**. In Emby/Jellyfin, tag the Twitch M3U tuner with `Twitch`
so Twitcharr can keep this URL up to date automatically.

Do not paste OAuth tokens, Client IDs, API keys, or Twitch account credentials into
the channel field. Twitcharr ignores those and reports a settings error for obvious
credential-looking input.

## Discovery Tokens

| Token | Meaning |
|---|---|
| `gronkh` | Adds one channel by login name. |
| `https://www.twitch.tv/gronkh` | Adds one channel from a Twitch URL. |
| `top` | Adds the top 10 live streams globally. |
| `top:25` | Adds the top 25 live streams globally. |
| `top:de:25` | Adds the top 25 German-language live streams. |
| `top:de,en:50` | Adds the top 50 German- or English-language live streams. |
| `game:Just Chatting` | Adds the top 10 live streams in that category. |
| `game:Just Chatting:25` | Adds the top 25 live streams in that category. |
| `search:gronkh` | Adds the first 10 channel-search results. |
| `search:cooking:5` | Adds the first 5 channel-search results. |
| `gronkh(family)` | Adds the channel and puts it into the Dispatcharr channel profile `family`. |
| `top:de:25(Livestreams, TV)` | Adds the top 25 German streams to both profiles. |

Category and search names with commas are ambiguous in a free-text field. Put
those tokens on their own line.

Multi-language `top` tokens can be mixed with other comma-separated entries,
for example `gronkh, top:de,en:25`.

## Settings

| Setting | Default | Actual behavior |
|---|---|---|
| Twitch channels and discovery | empty | Login names, Twitch URLs, or discovery tokens. Append `(profile1, profile2)` to assign channel profiles per entry. |
| Channel group | `Twitch` | Channel group used for Twitcharr-managed Channels. |
| Channel name prefix | empty | Optional text placed before every broadcaster name, for example `TTV \| `. |
| Channel name suffix | empty | Optional text placed after every broadcaster name. |
| Per-channel name templates | empty | Optional `login = template` lines such as `handofblood = TTV \| {name}`. These replace the global prefix/suffix only for that login. |
| Channel profiles | empty | Comma-separated Dispatcharr channel profile names applied to every Twitcharr-managed channel. `*` selects all profiles. |
| Starting channel number | `9000` | First number used for new Twitcharr Channels. Existing Twitcharr channel numbers are kept stable when possible. |
| Stream quality | `adaptive` | Adaptive chooses the best stream variant your bandwidth can sustain, including 60fps HD when Twitch offers no 30fps HD variant. Use **Emby safe (30fps fallback)** to force a fixed 30fps-oriented chain. |
| Connection bandwidth (Mbps) | `0` | `0` uses the last measured bandwidth value, or the plugin's conservative fallback. |
| Bandwidth safety margin (%) | `50` | Extra headroom used by adaptive quality. Values are clamped to the supported range. |
| Fastest possible startup | `true` | Uses shorter Streamlink timeouts and more aggressive HLS startup options. |
| Low-latency mode | `true` | Enables Streamlink's Twitch low-latency options. |
| Show offline channels | `true` | Offline configured channels stay in the lineup with offline guide data. Turn off to prune offline streamers during sync. |
| Live indicator | `XMLTV <live /> tag` | Select the XMLTV tag, red-dot title prefix, both, or neither. |
| Programme description separator | `\\n` | Supports a real newline via `\\n`, arbitrary plain text, legacy `<br />`, or an empty separator. |
| Channel logo | `Twitch channel avatar` | Stable profile avatar by default; current game/category artwork remains optional. Programme artwork still follows the category. |
| EPG refresh interval (minutes) | `2` | Background scheduler interval for Twitch metadata, Dispatcharr guide rows, Channels, Streams, and XMLTV. Minimum is 1 minute. |
| ttv.lol proxy servers | `https://eu.luminous.dev,https://eu2.luminous.dev,https://lb-eu.cdn-perfprod.com,https://lb-eu2.cdn-perfprod.com` | Comma-separated proxy playlist URLs passed to Streamlink. Empty disables proxy playlist use. |
| Emby / Jellyfin URL | empty | Optional media-server base URL. |
| Emby / Jellyfin API key | empty | API key for Emby/Jellyfin guide refresh only. This is not a Twitch key. |
| Data directory | `/app/data/plugins/twitcharr` | Stores XMLTV, scheduler state, Streamlink config, and downloaded streamlink-ttvlol plugin. |

## Actions

| Action | Actual behavior |
|---|---|
| Sync now | Updates ttv.lol if needed, creates StreamProfile, OutputProfile and EPG source, resolves Twitch inputs, writes guide data, syncs Channels/Streams, starts scheduler, and refreshes Emby/Jellyfin if configured. |
| Refresh guide | Resolves Twitch inputs, writes XMLTV plus Dispatcharr EPG rows, opportunistically checks ttv.lol freshness, and refreshes Emby/Jellyfin if configured. |
| Sync channels | Writes guide data, creates or updates Channels/Streams, links Channels to fresh EPG rows, prunes stale Twitcharr-owned Channels/Streams, refreshes Emby/Jellyfin if configured, then repeats the EPG link/programme repair pass after refresh tasks settle. |
| Full refresh | Runs ttv.lol update check, resolves Twitch inputs once, syncs Channels/Streams, writes guide data, and refreshes Emby/Jellyfin if configured. |
| Measure bandwidth | Downloads a small Cloudflare speed-test payload, saves the measured Mbps value, recalculates adaptive quality, and updates the StreamProfile and media-server OutputProfile. |
| Test proxies | Tests configured ttv.lol proxy URLs and reports reachability, HTTP status, and latency. |
| Refresh Emby / Jellyfin | Triggers the configured server's `Refresh Guide` task. |
| Update ttv.lol | Checks GitHub and installs or verifies the newest stable streamlink-ttvlol release, rejecting any SHA-256 mismatch. |
| Uninstall | Deletes Twitcharr-managed Channels, Streams, StreamProfile, OutputProfile, and EPG source rows, then refreshes Emby/Jellyfin if configured. Plugin files and settings remain. |

## Offline Behavior

`Show offline channels` controls configured streamer channels and is on by default:

- On: offline streamers stay in the Dispatcharr lineup with offline guide data.
- Off: offline streamers are removed during sync and recreated when they are live again.

If `Show offline channels` is off and nobody in the configured lineup is live,
Twitcharr prunes its managed Channels/Streams instead of creating a placeholder
channel.

## Guide And Images

Twitcharr writes guide data where Dispatcharr and TV clients expect it:

- Dispatcharr `EPGData` and `ProgramData` rows
- `<data_dir>/twitch.xmltv`
- channel icons from stable Twitch profile images by default, with category artwork available as an option
- programme icons from Twitch category artwork when available
- programme titles with streamer, category, and viewer count for live streams
- standards-based XMLTV `<live />` markers by default
- configurable plain-text description separators without requiring inline HTML
- offline programme title `⚫ Offline` with Twitch profile artwork for offline streamers

## Troubleshooting

| Problem | What to check |
|---|---|
| No channels appear | Add valid channel names or discovery tokens, then run **Sync now**. |
| OAuth/API-key confusion | Remove Twitch credentials from the channel field. Twitcharr does not use Twitch credentials. |
| Offline channels do not disappear | Turn **Show offline channels** off, then run **Sync channels**. |
| Streams do not start | Run **Update ttv.lol** and **Test proxies**. For Emby/Jellyfin, use the Twitcharr M3U with `output_format=mpegts&output_profile=<output_profile_id>`. |
| Proxy playback is unreliable | Remove dead proxies, reorder the list, or clear the proxy field to stop passing proxy playlist URLs to Streamlink. |
| Guide looks stale | Run **Refresh guide** or **Sync channels**. For Emby/Jellyfin, also run **Refresh Emby / Jellyfin**. |
| Emby/Jellyfin does not update | Set both media-server URL and API key, then run **Refresh Emby / Jellyfin**. |
| Adaptive quality is too high or too low | Run **Measure bandwidth**, set a manual bandwidth value, or adjust the safety margin. |

## Sources

- [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr)
- [streamlink-ttvlol](https://github.com/2bc4/streamlink-ttvlol)
- [Streamlink plugin sideloading](https://streamlink.github.io/latest/cli/plugin-sideloading.html)
- [Streamlink Twitch plugin docs](https://streamlink.github.io/latest/cli/plugins/twitch.html)
- [Jellyfin Scheduled Tasks API](https://api.jellyfin.org/#tag/ScheduledTasks)

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

## Support the Developer

This plugin is free and open-source, maintained with a lot of love in my spare time. If it brings value to your Emby setup, a small donation would mean the world to me.

<p align="center">
  <a href="https://paypal.me/eliasbruno124">
    <img src="https://img.shields.io/badge/Donate%20with-PayPal-0070BA?style=for-the-badge&logo=paypal&logoColor=white" alt="Donate with PayPal">
  </a>
</p>
