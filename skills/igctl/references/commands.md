# igctl command reference

Generated from the CLI by `scripts/gen_skill_reference.py`; do not edit by hand.

Global options go **before** the group: `igctl [--json] [-a ALIAS] [-b graph|private] [--dry-run] GROUP COMMAND ...`

| Global option | Meaning |
|---|---|
| `--json` | Machine-readable JSON on stdout (use whenever you parse output) |
| `-a, --account ALIAS` | Account alias to act as (default: `default_account`, or env `IGCTL_ACCOUNT`) |
| `-b, --backend graph\|private` | Override the account's backend for this command |
| `--dry-run` | Show what would happen; perform no writes |
| `--version` | Print the version |

## Authentication setup (the user runs this, never the agent)

Private backend (any account; can do everything; violates Instagram's Terms):

    igctl auth login main --backend private -u USERNAME --default   # prompts for password
    igctl auth login main --backend private -u USERNAME --2fa 123456

Graph backend (Business/Creator account plus a Meta app access token):

    igctl auth login brand --backend graph --host graph.instagram.com --token "IGAA..." --default
    igctl auth login brand --backend graph --host graph.facebook.com --token "EAA..." --ig-user-id 1784...

Secrets live in the OS keyring (service `igctl`) or env `IGCTL_<ALIAS>_TOKEN` / `IGCTL_<ALIAS>_PASSWORD`.
Config: `~/.igctl/config.yaml` (override the directory with env `IGCTL_HOME`).

## analyze

Profile, post, hashtag and follower analytics.

### `igctl analyze profile`

Engagement, best posting times, content mix, hashtags and growth for an account.

| Parameter | Kind | Description |
|---|---|---|
| `USERNAME` | argument, optional | |
| `--posts, -n` | int range | Recent posts to analyze. (default: `30`) |
| `--tz` | str | Time zone for best-time analysis. (default: `UTC`) |
| `--top` | int range | Top/bottom posts to list. (default: `5`) |
| `--snapshot / --no-snapshot` | flag | Store counts to track growth over time. |
| `--out, -o` | path | Also write the report to a .json or .csv file. |

### `igctl analyze compare`

Side-by-side profile reports.

| Parameter | Kind | Description |
|---|---|---|
| `USERNAMES...` | argument, required | |
| `--posts, -n` | int range |  (default: `30`) |
| `--tz` | str |  (default: `UTC`) |
| `--out, -o` | path | Also write the report to a .json or .csv file. |

### `igctl analyze followers`

Snapshot followers and show new/lost followers, not-following-back and fans.

Without --save or --diff, does both: snapshot now, then diff against the previous one.

| Parameter | Kind | Description |
|---|---|---|
| `USERNAME` | argument, optional | |
| `--save` | flag | Take a new follower/following snapshot. |
| `--diff` | flag | Diff the two latest snapshots. |
| `--limit` | int range | Max users to fetch (0 = all). |
| `--show` | int range | Max users listed per category. (default: `25`) |
| `--out, -o` | path | Also write the report to a .json or .csv file. |

### `igctl analyze post`

Public counts and private insights (when available) for one post.

| Parameter | Kind | Description |
|---|---|---|
| `MEDIA_ID` | argument, required | |
| `--metrics` | str | Comma-separated insight metrics. |
| `--out, -o` | path | Also write the report to a .json or .csv file. |

### `igctl analyze hashtag`

Engagement, volume and related hashtags for a hashtag.

| Parameter | Kind | Description |
|---|---|---|
| `TAG` | argument, required | |
| `--limit, -n` | int range | Posts to sample. (default: `50`) |
| `--recent` | flag | Sample recent instead of top posts. |
| `--out, -o` | path | Also write the report to a .json or .csv file. |

### `igctl analyze insights`

Account insights over a period (Graph API).

| Parameter | Kind | Description |
|---|---|---|
| `--days, -d` | int range | Look-back window. (default: `30`) |
| `--metrics` | str | Comma-separated metrics. (default: `reach,views,accounts_engaged,total_interactions,follower_count`) |
| `--period` | str | Only 'day' (the Graph API's sole period for these metrics); --days sets the range. (default: `day`) |
| `--out, -o` | path | Also write the report to a .json or .csv file. |

### `igctl analyze audience`

Follower demographics (age, gender, city, country) where the backend supports it.

| Parameter | Kind | Description |
|---|---|---|
| `--metric` | str | follower_demographics \| engaged_audience_demographics (default: `follower_demographics`) |
| `--out, -o` | path | Also write the report to a .json or .csv file. |

## auth

Log in, list and switch accounts.

### `igctl auth login`

Add (or re-authenticate) an account and verify the credentials work.

| Parameter | Kind | Description |
|---|---|---|
| `NAME` | argument, required | |
| `--backend` | str | graph \| private (default: the global --backend, else graph) |
| `--token` | str | Graph API access token. |
| `--ig-user-id` | str | Instagram user id. |
| `--host` | str | graph.facebook.com \| graph.instagram.com (default: `graph.facebook.com`) |
| `--username, -u` | str | Instagram username. |
| `--password` | str | Prompted if omitted. |
| `--2fa` | str | Two-factor / verification code. |
| `--proxy` | str | Proxy URL for the private backend. |
| `--default` | flag | Make this the default account. |

### `igctl auth list`

List configured accounts.

### `igctl auth use`

Set the default account.

| Parameter | Kind | Description |
|---|---|---|
| `NAME` | argument, required | |

### `igctl auth whoami`

Show the active account's live profile.

### `igctl auth logout`

Forget stored credentials and session (and with --purge, the account itself).

| Parameter | Kind | Description |
|---|---|---|
| `NAME` | argument, required | |
| `--purge` | flag | Also remove the account from config. |

### `igctl auth set-limit`

Set a daily cap for an action and/or the delay range between actions.

| Parameter | Kind | Description |
|---|---|---|
| `NAME` | argument, required | |
| `ACTION` | argument, optional | |
| `COUNT` | argument, optional | |
| `--min-delay` | float range |  |
| `--max-delay` | float range |  |

### `igctl auth token-refresh`

Extend a Graph API long-lived access token and store the new one.

| Parameter | Kind | Description |
|---|---|---|
| `NAME` | argument, required | |
| `--app-id` | str | Meta app id (Facebook tokens). |
| `--app-secret` | str | Meta app secret. |

## automate

Run YAML-defined automation rules.

### `igctl automate run`

Run the rules that are due (honours --dry-run, daily caps and delays).

| Parameter | Kind | Description |
|---|---|---|
| `RULES` | argument, required | |
| `--rule, -r` | str | Only run this rule. |
| `--loop / --once` | flag | Keep running due rules forever. |
| `--force` | flag | Ignore schedules; run now (--once). |
| `--max-sleep` | float | Max seconds between checks. (default: `60.0`) |

### `igctl automate validate`

Check a rules file and show what it defines.

| Parameter | Kind | Description |
|---|---|---|
| `RULES` | argument, required | |

### `igctl automate status`

Show each rule's last run and when it is next due.

| Parameter | Kind | Description |
|---|---|---|
| `RULES` | argument, required | |

### `igctl automate list-triggers`

List available triggers with their trigger/action options.

### `igctl automate log`

Show recent automated actions from the action log.

| Parameter | Kind | Description |
|---|---|---|
| `--limit, -n` | int range | How many actions to show. (default: `30`) |
| `--all` | flag | Show every account's actions. |

## dm

Read and send direct messages.

### `igctl dm inbox`

List direct-message threads.

| Parameter | Kind | Description |
|---|---|---|
| `--unread` | flag | Only unread threads. |
| `--limit, -n` | int range | Max threads (0 = all). (default: `20`) |

### `igctl dm read`

Show the latest messages of a thread.

| Parameter | Kind | Description |
|---|---|---|
| `THREAD` | argument, required | |
| `--limit, -n` | int range | Max messages (0 = all). (default: `20`) |

### `igctl dm send`

Send a direct message to one or more users.

| Parameter | Kind | Description |
|---|---|---|
| `USERS...` | argument, required | |
| `--text, -t` | str | Message text. |
| `--file, -f` | file | Read text from file. |
| `--group` | flag | One group thread instead of one DM per user. |

### `igctl dm reply`

Reply in an existing thread.

| Parameter | Kind | Description |
|---|---|---|
| `THREAD` | argument, required | |
| `TEXT` | argument, required | |

### `igctl dm broadcast`

DM a templated message to each user in a file, once per user per template.

| Parameter | Kind | Description |
|---|---|---|
| `--users-file, -f` | file, required | One user per line. |
| `--template, -t` | str, required | Message; placeholders: {username} {full_name} {first_name}. |
| `--max` | int range | Max messages this run. (default: `20`) |
| `--yes, -y` | flag | Skip confirmation. |

## download

Download posts, profiles and stories.

### `igctl download post`

Download one post (all carousel items).

| Parameter | Kind | Description |
|---|---|---|
| `URL_OR_ID` | argument, required | |
| `--dest, -d` | path | Output directory. (default: `.`) |

### `igctl download profile`

Download a profile's recent posts.

| Parameter | Kind | Description |
|---|---|---|
| `USERNAME` | argument, required | |
| `--limit, -n` | int range | Max posts to fetch. (default: `50`) |
| `--dest, -d` | path | Default: ./<username> |

### `igctl download stories`

Download a user's currently active stories.

| Parameter | Kind | Description |
|---|---|---|
| `USERNAME` | argument, required | |
| `--dest, -d` | path | Default: ./<username>/stories |

## engage

Like, comment, follow and moderate.

### `igctl engage like`

Like one or more posts.

| Parameter | Kind | Description |
|---|---|---|
| `MEDIA...` | argument, required | |

### `igctl engage unlike`

Remove your like from one or more posts.

| Parameter | Kind | Description |
|---|---|---|
| `MEDIA...` | argument, required | |

### `igctl engage comment`

Comment on a post (or reply to a comment).

| Parameter | Kind | Description |
|---|---|---|
| `MEDIA` | argument, required | |
| `TEXT` | argument, required | |
| `--reply-to` | str | Comment id to reply to. |

### `igctl engage comments`

List comments on a post.

| Parameter | Kind | Description |
|---|---|---|
| `MEDIA` | argument, required | |
| `--limit, -n` | int | Max comments. (default: `50`) |

### `igctl engage delete-comment`

Delete a comment.

| Parameter | Kind | Description |
|---|---|---|
| `MEDIA` | argument, required | |
| `COMMENT` | argument, required | |

### `igctl engage hide-comment`

Hide (or unhide) a comment.

| Parameter | Kind | Description |
|---|---|---|
| `COMMENT` | argument, required | |
| `--unhide` | flag | Unhide instead. |

### `igctl engage purge-comments`

Delete (or hide) every comment on a post whose text matches a regex.

| Parameter | Kind | Description |
|---|---|---|
| `MEDIA` | argument, required | |
| `--match, -m` | str, required | Regex searched in comment text. |
| `--hide` | flag | Hide matches instead of deleting. |
| `--ignore-case, -i` | flag |  |
| `--limit` | int | Max comments scanned. (default: `500`) |
| `--yes, -y` | flag | Skip confirmation. |

### `igctl engage follow`

Follow one or more users.

| Parameter | Kind | Description |
|---|---|---|
| `USERS...` | argument, required | |

### `igctl engage unfollow`

Unfollow one or more users.

| Parameter | Kind | Description |
|---|---|---|
| `USERS...` | argument, required | |

### `igctl engage followers`

List followers of a user.

| Parameter | Kind | Description |
|---|---|---|
| `USER` | argument, optional | |
| `--limit, -n` | int | Max results (0 = all). (default: `50`) |

### `igctl engage following`

List accounts a user follows.

| Parameter | Kind | Description |
|---|---|---|
| `USER` | argument, optional | |
| `--limit, -n` | int | Max results (0 = all). (default: `50`) |

### `igctl engage search`

Search users.

| Parameter | Kind | Description |
|---|---|---|
| `QUERY` | argument, required | |
| `--limit, -n` | int range |  (default: `20`) |

### `igctl engage hashtag`

List top (or recent) media for a hashtag.

| Parameter | Kind | Description |
|---|---|---|
| `TAG` | argument, required | |
| `--recent` | flag | Recent instead of top media. |
| `--limit, -n` | int range |  (default: `30`) |

### `igctl engage bulk-like`

Like up to --max posts from a hashtag.

| Parameter | Kind | Description |
|---|---|---|
| `--hashtag, -t` | str, required | Hashtag to like posts from. |
| `--max` | int range | Maximum number of actions. (default: `20`) |
| `--recent / --top` | flag | Recent or top media. |

### `igctl engage bulk-follow`

Follow up to --max users listed in a file.

| Parameter | Kind | Description |
|---|---|---|
| `--from-file, -f` | file, required | One user per line. |
| `--max` | int range | Maximum number of actions. (default: `20`) |

### `igctl engage bulk-unfollow`

Unfollow up to --max accounts (by default those not following you back).

| Parameter | Kind | Description |
|---|---|---|
| `--non-followers / --all-following` | flag | Only accounts that don't follow you back (default), or everyone you follow. |
| `--max` | int range | Maximum number of actions. (default: `20`) |
| `--whitelist, -w` | file | Never unfollow. |
| `--yes, -y` | flag | Skip confirmation. |

## post

Publish photos, videos, reels, carousels, stories.

### `igctl post photo`

Publish a single photo to the feed.

| Parameter | Kind | Description |
|---|---|---|
| `FILE` | argument, optional | |
| `--url` | str, repeatable | Public media URL instead of a file (repeatable). |
| `--caption, -c` | str | Caption text. |
| `--caption-file` | path | Read caption from file ('-' = stdin). |
| `--location` | str | Location name or id. |
| `--tag, -t` | str, repeatable | Tag a user (repeatable). |
| `--at` | str | Schedule instead of posting now: ISO-8601, '+2h', 'tomorrow 9am'... |
| `--yes, -y` | flag | Don't ask for confirmation. |

### `igctl post video`

Publish a single feed video.

| Parameter | Kind | Description |
|---|---|---|
| `FILE` | argument, optional | |
| `--url` | str, repeatable | Public media URL instead of a file (repeatable). |
| `--caption, -c` | str | Caption text. |
| `--caption-file` | path | Read caption from file ('-' = stdin). |
| `--location` | str | Location name or id. |
| `--tag, -t` | str, repeatable | Tag a user (repeatable). |
| `--at` | str | Schedule instead of posting now: ISO-8601, '+2h', 'tomorrow 9am'... |
| `--yes, -y` | flag | Don't ask for confirmation. |

### `igctl post reel`

Publish a reel.

| Parameter | Kind | Description |
|---|---|---|
| `FILE` | argument, optional | |
| `--cover` | path | Cover image. |
| `--no-feed` | flag | Only show in the Reels tab. |
| `--url` | str, repeatable | Public media URL instead of a file (repeatable). |
| `--caption, -c` | str | Caption text. |
| `--caption-file` | path | Read caption from file ('-' = stdin). |
| `--location` | str | Location name or id. |
| `--tag, -t` | str, repeatable | Tag a user (repeatable). |
| `--at` | str | Schedule instead of posting now: ISO-8601, '+2h', 'tomorrow 9am'... |
| `--yes, -y` | flag | Don't ask for confirmation. |

### `igctl post carousel`

Publish a carousel (album) of 2-10 items.

| Parameter | Kind | Description |
|---|---|---|
| `FILES...` | argument, optional | |
| `--url` | str, repeatable | Public media URL instead of a file (repeatable). |
| `--caption, -c` | str | Caption text. |
| `--caption-file` | path | Read caption from file ('-' = stdin). |
| `--location` | str | Location name or id. |
| `--tag, -t` | str, repeatable | Tag a user (repeatable). |
| `--at` | str | Schedule instead of posting now: ISO-8601, '+2h', 'tomorrow 9am'... |
| `--yes, -y` | flag | Don't ask for confirmation. |

### `igctl post story`

Publish a story.

| Parameter | Kind | Description |
|---|---|---|
| `FILE` | argument, optional | |
| `--url` | str, repeatable | Public media URL instead of a file (repeatable). |
| `--caption, -c` | str | Caption text. |
| `--caption-file` | path | Read caption from file ('-' = stdin). |
| `--location` | str | Location name or id. |
| `--tag, -t` | str, repeatable | Tag a user (repeatable). |
| `--at` | str | Schedule instead of posting now: ISO-8601, '+2h', 'tomorrow 9am'... |
| `--yes, -y` | flag | Don't ask for confirmation. |

### `igctl post delete`

Delete one of your posts.

| Parameter | Kind | Description |
|---|---|---|
| `MEDIA_ID` | argument, required | |
| `--yes, -y` | flag | Don't ask for confirmation. |

### `igctl post edit`

Replace a post's caption.

| Parameter | Kind | Description |
|---|---|---|
| `MEDIA_ID` | argument, required | |
| `--caption, -c` | str | Caption text. |
| `--caption-file` | path | Read caption from file ('-' = stdin). |

### `igctl post list`

List recent posts.

| Parameter | Kind | Description |
|---|---|---|
| `USERNAME` | argument, optional | |
| `--limit, -n` | int range |  (default: `20`) |

## schedule

Queue posts and run the scheduler.

### `igctl schedule add`

Queue a post to be published later.

| Parameter | Kind | Description |
|---|---|---|
| `FILES...` | argument, required | |
| `--at` | str, required | When to publish: now, +30m, +2h, +1d, 'tomorrow 18:00', 'fri 9am', or ISO 2026-10-12T18:00. |
| `--type` | choice | Default: carousel for 2+ files, reel for video. |
| `--caption, -c` | str | Post caption. (default: ``) |
| `--caption-file` | path | Read the caption from a UTF-8 file. |
| `--tz` | str | Time zone for naive times (IANA name, UTC or +05:00). Default: system local. |
| `--account, -a` | str | Account alias. |
| `--cover` | path | Reel/video cover image. |
| `--location` | str | Location name. |
| `--tag, -t` | str, repeatable | Tag a user (repeatable). |
| `--no-feed` | flag | Reels: don't also share to the feed. |

### `igctl schedule import`

Bulk-schedule posts from a content calendar.

Columns/keys: [cyan]when[/cyan], [cyan]files[/cyan] (';'-separated), and optional
[cyan]type[/cyan], [cyan]caption[/cyan], [cyan]account[/cyan], [cyan]tz[/cyan],
[cyan]cover[/cyan], [cyan]location[/cyan]. Relative paths resolve next to the calendar.
Every row is validated first; if any is invalid nothing is imported.

| Parameter | Kind | Description |
|---|---|---|
| `FILE` | argument, required | |
| `--tz` | str | Time zone for naive times (IANA name, UTC or +05:00). Default: system local. |
| `--account, -a` | str | Account for rows without one. |

### `igctl schedule list`

List scheduled posts (soonest first).

| Parameter | Kind | Description |
|---|---|---|
| `--status, -s` | str | pending \| running \| done \| failed \| cancelled |
| `--account, -a` | str | Only this account. |
| `--tz` | str | Display time zone. |

### `igctl schedule show`

Show one scheduled post in full, including its result or last error.

| Parameter | Kind | Description |
|---|---|---|
| `POST_ID` | argument, required | |
| `--tz` | str | Display time zone. |

### `igctl schedule cancel`

Cancel a pending (or failed) post.

| Parameter | Kind | Description |
|---|---|---|
| `POST_ID` | argument, required | |

### `igctl schedule retry`

Re-queue a failed or cancelled post (resets its attempt count).

| Parameter | Kind | Description |
|---|---|---|
| `POST_ID` | argument, required | |
| `--at` | str | When to publish: now, +30m, +2h, +1d, 'tomorrow 18:00', 'fri 9am', or ISO 2026-10-12T18:00. (default: `now`) |
| `--tz` | str | Time zone for naive times (IANA name, UTC or +05:00). Default: system local. |

### `igctl schedule run`

Publish due posts once (for cron/Task Scheduler) or as a polling daemon.

| Parameter | Kind | Description |
|---|---|---|
| `--loop / --once` | flag | Poll forever, or publish what's due and exit. |
| `--interval, -i` | float range | Seconds between polls (--loop). (default: `60.0`) |
