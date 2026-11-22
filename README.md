# Reading Room

A personal philosophy reading room: new journal articles that fit your areas, a desk of reviewed
papers, a reserve of classics and near-misses, and a board for your own notes.

- The site is static and served by GitHub Pages from this repository.
- Every Monday a GitHub Action asks Crossref what the watched journals have registered, asks
  Claude which of those fit your profile, and commits the results to `data/`.
- Your board, notes, dismissals and threads live in a second, private repository. The site reads
  and writes them with a token you paste into it once per browser.

## Set up

1. **Create two repositories** on GitHub.
   - `reading-room`, public. Upload everything in this folder to it, including `.github`.
   - `reading-room-notes`, private. Tick "Add a README" so it is not empty.
2. **Turn on Pages.** In `reading-room`: Settings, Pages, Source "Deploy from a branch",
   branch `main`, folder `/ (root)`. The site appears at
   `https://<your-username>.github.io/reading-room/` after a minute or two.
3. **Create a token for your notes.** GitHub Settings, Developer settings, Personal access
   tokens, Fine-grained tokens, Generate new token. Repository access: only
   `reading-room-notes`. Permissions: Contents, read and write. Copy it.
4. **Add two secrets** to `reading-room`: Settings, Secrets and variables, Actions, New
   repository secret.
   - `ANTHROPIC_API_KEY`: your Claude API key.
   - `NOTES_TOKEN`: the token from step 3, so the weekly check can read your threads and notes.
5. **Connect the site to your notes.** Open the site, go to Ledger, paste the token under
   "Notes sync" and press Connect. Repeat on each browser you use.
6. **Run the first check.** In `reading-room`: Actions, Weekly check, Run workflow. When it
   finishes, a new row appears under Runs in the Ledger.

## Change how it behaves

| What | Where |
| --- | --- |
| Threads you are working on | Ledger, on the site |
| Journals watched | `data/journals.json` (name, ISSN, group) |
| Areas | `areas` in your notes file, or `data/profile.json` |
| Schedule | `cron` in `.github/workflows/weekly-check.yml` |
| Model | Repository variable `READING_ROOM_MODEL`, for example `claude-opus-5-5` |
| Classics | Repository variable `READING_ROOM_CLASSICS` set to `0` turns them off |
| Notes repository name | Repository variable `NOTES_REPO`, as `owner/name` |

## What is public and what is not

The site repository is public, so the feed, the desk reviews and the run log can be read by
anyone who has the address. Your board, notes, dismissals and threads are in the private
repository and are read only with your token. The token is stored in your browser and sent only
to GitHub.

## Files

- `index.html`: the whole site.
- `data/papers.json`: every paper the checks have kept, with the reason.
- `data/runs.json`: one entry per check, including the ids it looked at and passed over.
- `data/desk.json`, `data/issues.json`: reviewed issues.
- `data/journals.json`, `data/profile.json`: what is watched and the default profile.
- `data/user-seed.json`: the starting board for a browser that has no notes yet.
- `scripts/check.py`: the weekly check. Standard library only.
