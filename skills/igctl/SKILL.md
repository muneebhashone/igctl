---
name: igctl
description: Publish, schedule, analyze and automate Instagram through the igctl CLI. Use when the user wants to post photos/videos/reels/carousels/stories, schedule a content calendar, analyze an Instagram profile, posts, hashtags or followers (unfollowers, not-following-back), like/comment/follow/unfollow, read or send DMs, download posts or stories, or run Instagram automation rules (auto-replies, welcome DMs, hashtag engagement).
compatibility: Requires the igctl CLI (Python 3.12+, installed with `uv tool install`) and network access to Instagram. Works on Windows, macOS and Linux.
metadata:
  version: "0.1.0"
---

# igctl — Instagram from the terminal

`igctl` is a CLI with two backends:

- **graph** — the official Instagram Graph API. Business/Creator accounts only. Safe. Supports publishing, comments, insights and Business Discovery.
- **private** — instagrapi (Instagram's private mobile API). Works with any account and can do everything (likes, follows, DMs, stories, downloads), but it breaks Instagram's Terms and can get the account restricted.

Each account alias in the config has a default backend. `-b graph|private` overrides it for one command.

## Before anything else

1. Check that the CLI is installed: `igctl --version`. If it isn't, look for the repo and install it with `uv tool install <repo-path>`. Inside the repo, `uv run igctl ...` also works.
2. Check that an account is set up: `igctl --json auth list`. An empty list means no account.
3. **Never log in for the user.** Logging in takes a password, a token, or a 2FA or security code. Tell the user to run `igctl auth login <alias> --backend private -u <username> --default` (or the Graph variant in [references/commands.md](references/commands.md)) in their own terminal. Never ask for the password in chat, and never pass it on the command line yourself.
4. Run `igctl auth whoami` to confirm the session still works before starting a multi-step task.

## How to call it

- Global options go **before** the command group: `igctl --json -a brand analyze profile nike`.
  - `--json`: machine-readable output. **Always use it when you parse the output.**
  - `-a/--account <alias>`: which account to act as.
  - `-b/--backend graph|private`: override the backend.
  - `--dry-run`: show what would happen without changing anything.
- Commands that ask for confirmation take `--yes/-y`. Pass it only after the user has approved that specific action.
- Exit code 0 means success. A non-zero code comes with an `error:` line on stderr. If the error says an operation isn't supported by one backend, retry with the other backend (it prints a hint), but only if the user is fine with the private backend.
- Media can be referenced by id, shortcode (`DeDi__VoRlh`) or full URL.

## Safety rules (follow these strictly)

- **Read-only commands are always fine**: `analyze *`, `post list`, `engage comments|followers|following|search|hashtag`, `dm inbox|read`, `download *`, `schedule list|show`, `automate validate|status|log|list-triggers`.
- **Writes that are visible to other people need explicit approval from the user** for the exact content and targets: publishing, comments, DMs, follows, unfollows, likes, deleting posts or comments. Show the caption, message or list first. For anything bulk (`bulk-*`, `dm broadcast`, `purge-comments`, `automate run`), **run with `--dry-run` first**, show the result, then run for real.
- Keep bulk numbers small (`--max 10–20`). The CLI enforces human-like delays and rolling 24-hour caps per action (`like 150, comment 40, follow 60, unfollow 80, dm 30, publish 25` by default). Treat a `Daily limit ... reached` error as a hard stop. Never raise limits with `auth set-limit` unless the user asks.
- Bulk actions take a long time on purpose (8–25 s between actions). Run them in the background or warn the user. Never try to work around the delays.
- Don't write spammy DMs or comments, and don't impersonate anyone.

## Common tasks

| Goal | Command |
|---|---|
| Account health report | `igctl --json analyze profile [USERNAME] --posts 30 --tz Asia/Karachi` |
| Compare accounts | `igctl --json analyze compare user1 user2 user3` |
| Who unfollowed me | `igctl --json analyze followers` (takes a snapshot and compares it with the previous one; the first run only records a baseline) |
| Not following back / fans | same command, fields `not_following_back` and `fans` |
| Hashtag research | `igctl --json analyze hashtag streetphotography --limit 50` |
| Post a photo | `igctl post photo pic.jpg -c "caption" -y` |
| Post a reel | `igctl post reel clip.mp4 --cover cover.jpg -c "caption" -y` |
| Carousel | `igctl post carousel a.jpg b.jpg c.mp4 -c "caption" -y` |
| Story | `igctl post story pic.jpg -y` |
| Schedule a post | `igctl schedule add pic.jpg --at "tomorrow 18:00" --tz Asia/Karachi -c "caption"` |
| Bulk schedule | `igctl schedule import calendar.csv --tz Asia/Karachi` |
| Publish due posts | `igctl schedule run --once` (or `--loop` as a service) |
| Recent comments | `igctl --json engage comments <media> --limit 50` |
| Reply to a comment | `igctl engage comment <media> "thanks!" --reply-to <comment-id>` |
| DM inbox | `igctl --json dm inbox --unread` |
| Send a DM | `igctl dm send username --text "hi"` |
| Download a profile | `igctl download profile username --limit 20 --dest ./dl` |
| Run automation | `igctl --dry-run automate run rules.yaml --force`, then `igctl automate run rules.yaml` |

Every command, option and output field: [references/commands.md](references/commands.md).
Automation rules (YAML schema, triggers, examples): [references/automation.md](references/automation.md).

## Analyzing results

When you summarize an `analyze profile` report, lead with what the user can act on:

- Engagement rate against typical benchmarks (roughly 1–3 % is normal, above 3 % is strong for accounts under 100k).
- The best weekday and hour to post (in the user's time zone).
- Which media type performs best.
- The top hashtags by average engagement.
- Follower growth since the last snapshot.

Run `analyze profile` with `--snapshot` (the default) regularly so growth can be tracked over time.

## Troubleshooting

- **Login seems to hang**: Instagram is probably showing a "Was this you?" check. The user approves it in the Instagram app, or types the code the terminal asks for.
- **`challenge_required` / `login_required`**: the user runs `igctl auth login <alias> ...` again in their own terminal.
- **`feedback_required` / `please wait a few minutes`**: Instagram is throttling the account. Stop all write actions for at least 24 hours.
- **Graph publishing from local files** needs `media_host` in `~/.igctl/config.yaml` (s3, imgbb or cmd), or pass a public `--url` instead.
