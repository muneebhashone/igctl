# igctl

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Agent Skill](https://img.shields.io/badge/agent%20skill-included-8A2BE2.svg)](skills/igctl/SKILL.md)

`igctl` is a command-line toolkit to **publish, schedule, analyze and automate Instagram**:

- **Publishing:** photos, videos, reels (with cover and feed toggle), carousels and stories, now or at a set time.
- **Scheduling:** a scheduling queue with CSV/YAML content-calendar import, plus a runner for cron, Task Scheduler or systemd.
- **Analytics:** engagement rate, best posting days and hours, top posts and hashtags, follower growth and churn, account insights and audience demographics. Reports export to JSON or CSV.
- **Engagement:** likes, comments, follows, comment moderation and bulk actions.
- **Direct messages:** inbox, sending, replies and templated broadcasts.
- **Downloads:** posts, whole profiles and stories.
- **Automation:** rules in a YAML file (comment auto-reply, DM auto-reply, welcome DMs, hashtag engagement, unfollowing non-followers, follow-from-audience). Every rule runs behind per-account safety limits.

```text
igctl [--account NAME] [--backend graph|private] [--dry-run] [--json] COMMAND ...
```

Every command accepts `--help`. `--dry-run` shows what would happen and changes nothing. `--json` prints machine-readable output; progress and status messages go to stderr.

---

## Two backends and their trade-offs

| | `graph`: official Instagram Graph API | `private`: Instagram's mobile API via [instagrapi](https://github.com/subzeroid/instagrapi) |
|---|---|---|
| Account type | Business or Creator only | Any account |
| Login | Access token from a Meta app | Username + password (+2FA), session saved locally |
| Publish photo/video/reel/carousel/story | ✅ (media fetched from public URLs, see [media_host](#media_host-graph-publishing-of-local-files)) | ✅ (direct upload) |
| Edit caption | ❌ (the API cannot) | ✅ |
| Insights, demographics | ✅ (full) | Partial (business/creator accounts only) |
| Comments: list/reply/delete/hide | ✅ | ✅ (no hide) |
| Other accounts' profile and posts | ✅ via Business Discovery (business/creator targets only) | ✅ |
| Hashtag search | ✅ (30 unique hashtags per 7 days) | ✅ |
| Likes, follows, followers lists, DMs, user search, downloads | ❌ | ✅ |
| **Terms of Service risk** | **None.** This is the supported API. | **Real.** Automating the private API violates Instagram's Terms. Accounts can be challenged, rate-limited, action-blocked or disabled. |

Use `graph` for anything it supports, especially publishing and analytics on your brand account. Use `private` only where you need it, ideally on a secondary account, from a stable IP (or a fixed `--proxy`), with low limits.

An account has a default backend. You can override it for a single command with `igctl --backend private ...`. If a command needs something the current backend can't do, igctl stops with a hint to switch. Automation rules choose a suitable backend per trigger.

**Defaults are deliberately conservative.** Every automated write goes through a guard that:
- waits a random 8–25 s between actions,
- enforces rolling 24-hour caps (likes 150, comments 40, follows 60, unfollows 80, DMs 30, publishes 25),
- logs every action, and
- respects `--dry-run`.

Instagram's real thresholds are not published, change over time and are lower for new accounts. Do not raise the caps casually.

---

## Install

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install git+https://github.com/muneebhashone/igctl          # puts `igctl` on your PATH
uv tool install "igctl[s3] @ git+https://github.com/muneebhashone/igctl"   # ...with S3 support for media_host
igctl --help
```

Or from a clone: `git clone https://github.com/muneebhashone/igctl && cd igctl && uv tool install .`

To develop: `uv sync`, then `uv run igctl ...`, `uv run pytest`, and `uv run ruff check .`.

Video uploads on the private backend use MoviePy (installed automatically) together with the ffmpeg binary that ships with `imageio-ffmpeg`. You don't need a system ffmpeg.

---

## Setup

### Graph API backend (recommended)

1. **Make the Instagram account a Business or Creator account.** You can switch in the Instagram app's settings.
2. **Create a Meta app** at <https://developers.facebook.com/apps> (type *Business*). Add the product that matches how you want to log in:
   - **Instagram API with Facebook Login** (host `graph.facebook.com`). The Instagram account must be linked to a Facebook Page. Request these permissions:
     - `instagram_basic`
     - `instagram_content_publish`
     - `instagram_manage_comments`
     - `instagram_manage_insights`
     - `pages_show_list`
     - `pages_read_engagement`
     - `business_management` (needed when the Page belongs to a Business Manager)
   - **Instagram API with Instagram Login** (host `graph.instagram.com`). No Facebook Page is needed. Request these permissions:
     - `instagram_business_basic`
     - `instagram_business_content_publish`
     - `instagram_business_manage_comments`
     - `instagram_business_manage_insights`
3. **Generate a long-lived access token**, which lasts about 60 days. Use the Graph API Explorer, or the app dashboard's "Generate token" for Instagram Login. Your own account works in development mode, so you don't need App Review to use it yourself.
4. **Log in:**

   ```bash
   # Facebook Login token (the Instagram user id is discovered through your Pages)
   igctl auth login brand --backend graph --token EAAG... --username mybrand
   # Instagram Login token
   igctl auth login brand --backend graph --host graph.instagram.com --token IGAA...
   igctl auth whoami
   ```

5. **Refresh the token before it expires.** Schedule this monthly:

   ```bash
   igctl auth token-refresh brand                                    # Instagram Login tokens
   igctl auth token-refresh brand --app-id 123 --app-secret ...      # Facebook tokens (or IGCTL_APP_SECRET)
   ```

#### `media_host`: Graph publishing of local files

Instagram downloads Graph API media from a **public URL**. You have two choices:
- Pass `--url https://...` instead of a file.
- Set `media_host` in `config.yaml` so igctl uploads local files for you.

```yaml
media_host: s3://my-bucket/igctl/                   # boto3 (s3 extra) + AWS credentials; presigned URL valid 1h
# media_host: imgbb:IMGBB_API_KEY                   # imgbb.com, API key read from that env var (images only)
# media_host: "cmd:rclone copyto {path} r2:pub/ && echo https://cdn.example.com/$(basename {path})"
```

A `cmd:` host runs any shell command. `{path}` is replaced with the quoted file path, and the last line the command prints must be the public URL.

On `graph.facebook.com`, videos and reels can be uploaded directly (resumable upload) without a media host.

### Private API backend

```bash
igctl auth login alt --backend private --username myalt          # prompts for the password
igctl auth login alt --backend private --username myalt --2fa 123456
igctl auth login alt --backend private --username myalt --proxy http://user:pass@host:port
```

- **Session:** after login the session is saved to `~/.igctl/sessions/<alias>.json` and reused. igctl keeps the same device identity across re-logins, which looks far less suspicious.
- **Password:** stored in the OS keyring so igctl can log in again automatically.
- **Challenges:** if Instagram asks for a challenge code, igctl prompts for it in the terminal. Unattended runs fail with an auth error instead. When that happens, run `igctl auth whoami` interactively once.

### Managing accounts

```bash
igctl auth list                       # all accounts, default marked
igctl auth use brand                  # change the default
igctl -a alt engage like <url>        # one-off: use another account
igctl auth set-limit alt follow 30    # rolling-24h cap for an action
igctl auth set-limit alt --min-delay 20 --max-delay 60
igctl auth logout alt [--purge]       # forget secrets/session (and the account)
```

---

## Command reference

`MEDIA` arguments accept a media id, a shortcode (`C1a2B3c4D5e`) or a post/reel URL. On the Graph backend, shortcodes and URLs are resolved against your own recent 200 posts. `USER` arguments accept `name`, `@name` or a profile URL.

### `post`: publish now or later

```bash
igctl post photo pic.jpg -c "Launch day ☀️ #newrelease" --location "Lahore" -t friend1 -t friend2
igctl post reel clip.mp4 --cover cover.jpg --caption-file caption.txt
igctl post reel clip.mp4 --no-feed                 # Reels tab only
igctl post carousel a.jpg b.jpg c.mp4 -c "Swipe →"
igctl post story story.jpg
igctl post photo --url https://cdn.example.com/pic.jpg       # graph without media_host
igctl post photo pic.jpg --at "tomorrow 9am"                 # queue instead of posting
igctl post list [USER] -n 20
igctl post edit MEDIA -c "new caption"             # private backend
igctl post delete MEDIA
```

Before anything is uploaded, igctl checks the request:
- **Files:** they must exist and be jpg/jpeg/png/webp images or mp4/mov videos.
- **Feed aspect ratio:** feed images must be between 4:5 and 1.91:1. You get a warning when a story or reel cover isn't 9:16.
- **Carousels:** 2–10 items.
- **Captions and tags:** at most 2,200 characters, at most 30 hashtags, at most 20 user tags.

### `schedule`: queue and publish later

```bash
igctl schedule add pic.jpg --at "fri 18:00" -c "Weekend!"
igctl schedule add a.jpg b.jpg --at "+2h"            # 2+ files -> carousel
igctl schedule add clip.mp4 --type reel --at "2026-12-01 10:00" --tz Europe/London
igctl schedule import calendar.csv --tz Asia/Karachi
igctl schedule list [--status pending|running|done|failed|cancelled]
igctl schedule show 12
igctl schedule cancel 12
igctl schedule retry 12
igctl schedule run --once          # publish what's due, exit (cron / Task Scheduler)
igctl schedule run --loop -i 60    # or run as a daemon
```

`--at` accepts these formats:
- relative times: `now`, `+30m`, `+1h30m`, `in 2h`
- `today 18:00` and `tomorrow 9am`
- weekdays: `fri 18:00`
- a bare `18:00`, meaning the next occurrence
- ISO-8601, with or without an offset

Times without a zone use `--tz`, or the system time zone if `--tz` isn't given.

**How the runner handles failures:**
- **Transient errors** (rate limits, timeouts) retry with backoff of 5, 10 and 20 minutes, for up to 3 attempts.
- **Permanent errors** (auth, unsupported, a missing file) fail the post immediately.
- **Daily publish cap reached:** the post moves forward an hour without using up an attempt.

`schedule run --once` exits with code 1 if any post failed.

**Content calendar.** In CSV, `when` and `files` are required. `files` can hold several entries separated by `;`, and relative paths are resolved next to the calendar file:

```csv
when,type,files,caption,account,tz,cover,location
2026-11-01 10:00,,launch.jpg,"Launch day!",,,,
2026-11-02 18:00,carousel,a.jpg;b.jpg;c.jpg,"Behind the scenes",brand,Europe/London,,
+3d,reel,clip.mp4,"Teaser",,,cover.jpg,
```

The same calendar in YAML:

```yaml
- when: 2026-11-01 10:00
  files: [launch.jpg]
  caption: Launch day!
- when: fri 18:00
  type: reel
  files: clip.mp4
  tz: "+05:00"
```

The import is all or nothing: if any row is invalid, every error is listed and nothing is queued.

#### Running the scheduler as a service

The queue is shared, so posts queued with `post --at` or `schedule add` are published by whichever runner is active. Use the full path to `igctl`, which `where igctl` or `which igctl` prints.

**Windows Task Scheduler.** Run every 5 minutes:

```powershell
schtasks /Create /TN igctl-scheduler /SC MINUTE /MO 5 /TR "C:\Users\me\.local\bin\igctl.exe schedule run --once"
```

**cron:**

```cron
*/5 * * * * /home/me/.local/bin/igctl schedule run --once >> ~/.igctl/scheduler.log 2>&1
```

**systemd user service** (`~/.config/systemd/user/igctl-scheduler.service`):

```ini
[Unit]
Description=igctl scheduler

[Service]
ExecStart=%h/.local/bin/igctl schedule run --loop --interval 60
Restart=on-failure

[Install]
WantedBy=default.target
```

Then run `systemctl --user enable --now igctl-scheduler`, and also `loginctl enable-linger $USER` so it keeps running after logout. The same patterns work for `igctl automate run rules.yaml --once` or `--loop`.

### `analyze`: analytics and reports

```bash
igctl analyze profile                       # your account; snapshots for growth tracking
igctl analyze profile natgeo -n 50 --tz Asia/Karachi --top 10 -o natgeo.json
igctl analyze compare brand rival1 rival2 -o compare.csv
igctl analyze followers                     # snapshot + diff: new/lost, not-following-back, fans
igctl analyze followers --save              # snapshot only (e.g. daily via cron)
igctl analyze post MEDIA                    # counts + insights (reach, views, saves, shares...)
igctl analyze hashtag streetphotography --recent
igctl analyze insights --days 90 --metrics reach,views,accounts_engaged,follower_count
igctl analyze audience --metric follower_demographics   # age/gender/city/country (graph)
```

**What a profile report contains:**
- engagement rate, average likes, comments and views, and posts per week
- the best weekdays and hours to post, in your time zone
- performance by media type
- top and bottom posts
- hashtags ranked by use and by engagement
- how caption length relates to engagement
- growth since the last snapshot

Pass `-o file.json` or `-o file.csv` to export any report.

### `engage`: likes, comments, follows, moderation

```bash
igctl engage like MEDIA [MEDIA...]           # private backend
igctl engage comment MEDIA "Great shot!"     [--reply-to COMMENT_ID]
igctl engage comments MEDIA -n 100
igctl engage hide-comment COMMENT_ID [--unhide]   # graph
igctl engage delete-comment MEDIA COMMENT_ID
igctl engage purge-comments MEDIA -m "(?i)free followers|promo" [--hide] [-y]
igctl engage follow USER [USER...]
igctl engage unfollow USER [USER...]
igctl engage followers [USER] -n 500 --json > followers.json
igctl engage following [USER]
igctl engage search "coffee lahore"
igctl engage hashtag latteart --recent -n 30
igctl engage bulk-like -t latteart --max 20 [--recent]
igctl engage bulk-follow -f prospects.txt --max 15
igctl engage bulk-unfollow --non-followers --max 25 -w whitelist.txt
```

Bulk commands show progress and stop cleanly when they hit a daily cap or Instagram rate-limits them. Each prints a summary at the end.

### `dm`: direct messages (private backend)

```bash
igctl dm inbox [--unread] -n 20
igctl dm read THREAD_ID -n 30
igctl dm send alice bob -t "Hi!"             # one DM each
igctl dm send alice bob -t "Hi both" --group # one group thread
igctl dm reply THREAD_ID "Thanks!"
igctl dm broadcast -f customers.txt -t "Hi {first_name}, our sale starts today!" --max 25
```

`broadcast` remembers who already received a given template. Running it again only messages new users.

### `download`

```bash
igctl download post https://www.instagram.com/p/C1a2B3c4D5e/ -d out/
igctl download profile natgeo -n 100            # -> ./natgeo/
igctl download stories natgeo                   # -> ./natgeo/stories/
```

Downloads require the private backend.

### `automate`: YAML rules

```bash
igctl automate list-triggers                       # triggers and their options
igctl automate validate rules.yaml
igctl --dry-run automate run rules.yaml --force    # preview everything, change nothing
igctl automate run rules.yaml                      # run due rules once
igctl automate run rules.yaml --loop               # daemon
igctl automate status rules.yaml                   # last/next run per rule
igctl automate log -n 50                           # recent automated actions
```

| Trigger | What it does | Backend |
|---|---|---|
| `comment_autoreply` | Replies to new comments on your recent posts that match a filter, and hides spam from a blocklist | graph or private |
| `dm_autoreply` | Replies to DM threads whose last message matches | private |
| `welcome_dm` | DMs new followers. The first run only records a baseline | private |
| `hashtag_engage` | Likes, and optionally comments on, top or recent hashtag posts. Never touches the same post twice | private |
| `unfollow_nonfollowers` | Unfollows accounts that don't follow back once they've been followed for at least N days, skipping a whitelist | private |
| `follow_from` | Follows followers, following, likers or commenters of an account or post, and never re-follows | private |

A minimal rules file:

```yaml
defaults:
  limits: {min_delay: 20, max_delay: 60}
rules:
  - name: price-replies
    every: 15m                       # or cron, in UTC: "0 9,18 * * *"
    trigger: {type: comment_autoreply, posts: 5, max_age_hours: 48}
    filter: {keywords: [price, "how much"], match: any}
    action:
      reply: "Hi @{username}! Details are in your DMs."
      hide_blocklist: ["free followers", "promo"]
    limits: {max_per_run: 20}
```

See [`examples/rules.yaml`](examples/rules.yaml) for every trigger with comments.

- **Scheduling:** each rule runs on its own schedule.
- **Limits:** all actions go through the account's guard (delays, daily caps and the action log), so they count against the same limits as manual commands.
- **Errors:** a rule that hits a cap ends as `capped`. `run` exits with code 1 if any rule ends in `error`.

---

## Safety limits

Caps are per account, measured over a rolling 24 hours. They apply to every guarded write, whether it comes from a manual command, a bulk command, the scheduler or an automation rule.

```bash
igctl auth set-limit alt like 80
igctl auth set-limit alt --min-delay 15 --max-delay 45
igctl automate log              # what has been done, and whether it succeeded
```

Some practical guidance for the private backend:
- Warm up new or rarely used accounts slowly.
- Keep one IP per account.
- Avoid running bulk actions around the clock.
- If you see `RateLimited` or "feedback required" errors, stop for at least 24–48 hours.

---

## Files and configuration

Everything lives under `IGCTL_HOME`, which defaults to `~/.igctl`:

```text
~/.igctl/config.yaml           accounts, default account, media_host
~/.igctl/igctl.db              SQLite: action log, schedule queue, snapshots, dedup state
~/.igctl/sessions/<alias>.json private-API sessions
```

`config.yaml` (written by `igctl auth ...`, safe to edit by hand):

```yaml
default_account: brand
media_host: s3://my-bucket/igctl/
accounts:
  brand:
    name: brand
    backend: graph
    username: mybrand
    ig_user_id: "17841400000000000"
    graph_version: v23.0
    graph_host: graph.facebook.com     # or graph.instagram.com
    limits: {min_delay: 8, max_delay: 25, daily: {like: 150, comment: 40, follow: 60, unfollow: 80, dm: 30, publish: 25}}
  alt:
    name: alt
    backend: private
    username: myalt
    proxy: http://user:pass@host:port
```

Secrets are never written to `config.yaml`. They are stored in the OS keyring under service `igctl`, with keys `<alias>:token` and `<alias>:password`.

### Environment variables

| Variable | Purpose |
|---|---|
| `IGCTL_HOME` | Data directory (default `~/.igctl`) |
| `IGCTL_ACCOUNT` | Account alias to use when `--account` is not given |
| `IGCTL_<ALIAS>_TOKEN` | Graph token for an account. It overrides the keyring, which makes it useful for CI and servers. The alias is upper-cased, and characters other than letters and digits become `_` |
| `IGCTL_<ALIAS>_PASSWORD` | Private-API password for an account (overrides the keyring) |
| `IGCTL_APP_SECRET` | Meta app secret for `auth token-refresh` on Facebook tokens |
| your `imgbb:` variable | imgbb API key when `media_host: imgbb:<VAR>` |
| `AWS_*` | Standard AWS credentials for `media_host: s3://...` |

---

## Use it from AI agents

The repo includes an [Agent Skill](https://agentskills.io) at `skills/igctl/`. Any agent that supports skills can use it. Copy or symlink the folder into your agent's skills directory, for example:

```bash
uv tool install .                              # puts `igctl` on PATH for the agent
cp -r skills/igctl ~/.claude/skills/igctl      # Claude Code (user-level)
cp -r skills/igctl .claude/skills/igctl        # or project-level
```

The skill tells the agent how to call the CLI (`--json`, `--dry-run`, `-a`). It also makes the agent ask for approval before anything other people can see, keep bulk actions small, and never handle your password. `AGENTS.md` covers agents that work *on* this codebase.

The files under `skills/igctl/references/` are generated from the CLI. Regenerate them after changing commands:

```bash
uv run python scripts/gen_skill_reference.py
```

## Development

```bash
uv sync
uv run pytest -q          # offline: HTTP is mocked with respx, instagrapi with fakes
uv run ruff check . && uv run ruff format --check .
```

The code is laid out as follows:

- `src/igctl/backends/` holds the backend contract (`base.py`) and its two implementations: `graph.py` (httpx) and `private.py` (instagrapi).
- `src/igctl/core/` contains:
  - config, storage and context
  - the rate-limit guard
  - `media_host`
  - `validation` (pre-publish checks)
  - `refs` (shortcode, URL and username parsing)
- `src/igctl/scheduler/` is time parsing, the queue and the runner.
- `src/igctl/analytics/` is metrics, reports, follower diffs and export.
- `src/igctl/automation/` is rules, the trigger registry, handlers, the engine and state.
- `src/igctl/cli/` holds one Typer module per command group.

## Contributing

Issues and pull requests are welcome. Before you open a PR:

```bash
uv sync
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run python scripts/gen_skill_reference.py   # if you changed any command or option
```

Tests run offline. Never add tests that hit Instagram or need real credentials. See [AGENTS.md](AGENTS.md) for the architecture and conventions, which apply to human contributors as well.

## License

[MIT](LICENSE) © muneebhashone

## Disclaimer

igctl is not affiliated with or endorsed by Instagram or Meta. You are responsible for complying with Instagram's Terms of Use and Platform Policy. Use of the private backend is at your own risk.
