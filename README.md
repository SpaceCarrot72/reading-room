# Reading Room

A personal philosophy reading room: new journal articles that fit your areas, a desk of reviewed
papers, a reserve of classics and near-misses, and a board for your own notes.

- The site is static and served by GitHub Pages from this repository.
- Every Monday a GitHub Action runs two steps and commits the results to `data/`.
  1. **The check** asks Crossref what the watched journals have registered and asks Claude which
     of those fit your profile.
  2. **The desk** picks at most two papers per area that have an open copy, reads each in full,
     and reviews it twice: one reviewer tries to break the argument, a second checks those
     objections against the paper. The result is a new issue on the Desk page.
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
   - `NOTES_TOKEN`: the token from step 3, so the weekly run can read your threads and notes.
5. **Add a third secret** in the same place: `CONTACT_EMAIL`, your email address. Unpaywall
   needs it to look up open copies; without it the desk finds far fewer. Make it a secret, not
   a variable: this repository's run logs are public, and only secrets are masked in them.
6. **Connect the site to your notes.** Open the site, go to Ledger, paste the token under
   "Notes sync" and press Connect. Repeat on each browser you use.
7. **Run it once.** In `reading-room`: Actions, Weekly check, Run workflow. When it finishes,
   new rows appear under Runs and Desk runs in the Ledger.

## How the desk chooses

Candidates are papers from the last ten weeks of checks plus anything on your board that you have
not marked Finished. A paper needs an open copy the desk can download. From those, Claude picks
at most two per area and at most four in all, and may pick none. Papers you saved come first.
A paper is reviewed once, and after two failed attempts the desk stops trying it.

## What it costs

The check sends titles and abstracts, which is small. The desk sends each chosen paper's PDF to
the model three times (the PDF is cached between passes where the API allows it), so its cost
grows with the number and length of papers. Token counts for every run are in the Ledger. If
your API credit runs out, the run fails, writes nothing, and resumes from the same point once
credit is back.

## Change how it behaves

Set these as repository variables: Settings, Secrets and variables, Actions, Variables.

| Variable | Effect | Default |
| --- | --- | --- |
| `READING_ROOM_MODEL` | Model for the check | `claude-sonnet-5-5` |
| `READING_ROOM_CLASSICS` | `0` stops the check adding classics | on |
| `READING_ROOM_DESK_MODEL` | Model for the desk | `claude-opus-5-5` |
| `READING_ROOM_DESK_MAX` | Most papers per desk issue; `0` turns the desk off | `4` |
| `READING_ROOM_DESK_TOKENS` | Stop starting new papers past this many tokens read | `1500000` |
| `NOTES_REPO` | Notes repository, as `owner/name` | `<this repo>-notes` |

Other changes:

- Threads you are working on: Ledger, on the site.
- Journals watched: `data/journals.json` (name, ISSN, group). Each journal also has an `abbr` (the letters on its mark, four at most) and a `color` (six-digit hex, dark enough for white text). The `other` list gives marks to journals that turn up as classics but are not watched.
- Author affiliations: taken from Crossref when the publisher deposits them. Springer journals (Philosophical Studies, Erkenntnis, Linguistics and Philosophy) do not, so the weekly check also asks OpenAlex, on up to four runs per paper. Authors with no known affiliation are shown without brackets. Affiliations are never sent to the model that judges the papers.
- Schedule: `cron` in `.github/workflows/weekly-check.yml`.

## What is public and what is not

The site repository is public, so the feed, the desk reviews and the run logs can be read by
anyone who has the address. Your board, notes, dismissals and threads are in the private
repository and are read only with your token. The token is stored in your browser and sent only
to GitHub.

## Files

- `index.html`: the whole site.
- `data/papers.json`: every paper the checks have kept, with the reason.
- `data/runs.json`: one entry per check, including the ids it looked at and passed over.
- `data/desk.json`, `data/issues.json`: reviewed issues.
- `data/desk-log.json`: one entry per desk run, with token counts.
- `data/fulltext.json`: where the desk found an open copy of each candidate.
- `data/journals.json`, `data/profile.json`: what is watched and the default profile.
- `data/user-seed.json`: the starting board for a browser that has no notes yet.
- `scripts/check.py`, `scripts/desk.py`: the check and the desk. Standard library only.
