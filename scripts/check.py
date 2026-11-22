#!/usr/bin/env python3
"""Weekly check for the reading room.

Reads the ledger in data/, asks Crossref what each watched journal has registered
since the last run, asks Claude which of those fit the reader's profile, and appends
the results to data/papers.json and data/runs.json. Nothing already in the ledger is
changed or repeated. Standard library only.

Environment
  ANTHROPIC_API_KEY   required. Claude API key.
  NOTES_TOKEN         optional. GitHub token that can read the private notes repository.
  NOTES_REPO          optional. owner/name of the notes repository.
  READING_ROOM_MODEL  optional. Model id; defaults to claude-sonnet-5-5.
  CROSSREF_MAILTO     optional. Contact address sent to Crossref with each request.
"""
import base64
import datetime as dt
import difflib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
API_VERSION = os.environ.get("ANTHROPIC_VERSION", "").strip() or "2026-10-21"
MODEL = os.environ.get("READING_ROOM_MODEL", "").strip() or "claude-sonnet-5-5"
NOTES_TOKEN = os.environ.get("NOTES_TOKEN", "").strip()
NOTES_REPO = os.environ.get("NOTES_REPO", "").strip()
MAILTO = os.environ.get("CROSSREF_MAILTO", "").strip()
CLASSICS = os.environ.get("READING_ROOM_CLASSICS", "1").strip() != "0"

RESERVE_CAP = 12      # near-misses kept per run
BATCH = 30            # candidates per Claude request
ABSTRACT_CHARS = 1500
UA = "reading-room-weekly-check/1.0"

SKIP_TITLE = re.compile(
    r"^(issue information|front ?matter|back ?matter|table of contents|contents|contributors|"
    r"editorial board|editors'? note|erratum|corrigendum|correction\b|retraction|books? received|"
    r"call for (papers|submissions)|index\b|announcements?|masthead|title pending|list of reviewers)",
    re.I,
)


# ---------- small helpers ----------

def load(name, default=None):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(name, obj):
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def slug(doi):
    """Ledger id for a DOI: lower case, anything outside a-z 0-9 _ . ~ : @ + - becomes _."""
    return re.sub(r"[^a-z0-9_.~:@+-]", "_", doi.lower())


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def tidy_title(text):
    """A Crossref title as a reader would write it: no series numeral, no space before a colon."""
    text = re.sub(r"^[IVXL]+\*?\s*[—–]\s*", "", clean(text))
    return re.sub(r"\s+([:;,])", r"\1", text)


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def http(url, headers=None, body=None, tries=4, timeout=90):
    """GET, or POST when a body is given. Returns parsed JSON. Retries busy or flaky servers."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    host = urllib.parse.urlsplit(url).netloc
    for attempt in range(tries):
        req = urllib.request.Request(
            url, data=data, headers={"User-Agent": UA, **(headers or {})},
            method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code in (408, 429, 500, 502, 503, 504, 529) and attempt < tries - 1:
                time.sleep(4 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code} from {host}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            if attempt < tries - 1:
                time.sleep(4 * (attempt + 1))
                continue
            raise RuntimeError(f"could not read {host}: {e}") from None


# ---------- Crossref ----------

def crossref_new(issn, since):
    params = {"filter": f"from-created-date:{since}", "rows": "200",
              "select": "DOI,title,author,created,abstract,type"}
    if MAILTO:
        params["mailto"] = MAILTO
    url = f"https://api.crossref.org/journals/{issn}/works?" + urllib.parse.urlencode(params, safe=":,")
    return (http(url).get("message") or {}).get("items") or []


def names(item):
    out = []
    for a in item.get("author") or []:
        n = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name") or ""
        if n:
            out.append(n)
    return ", ".join(out)


def record(item, venue):
    """A Crossref item as a candidate, or None for notices and front matter."""
    doi = (item.get("DOI") or "").strip()
    title = tidy_title(" ".join(t for t in (item.get("title") or []) if t))
    title = re.sub(r"\s+in advance$", "", title).strip()
    if not doi or not title or SKIP_TITLE.match(title):
        return None
    if item.get("type", "journal-article") != "journal-article":
        return None
    created = item.get("created") or {}
    date = (created.get("date-time") or "")[:10]
    if not date:
        parts = (created.get("date-parts") or [[]])[0]
        date = "-".join(f"{int(p):02d}" for p in parts) if parts else ""
    abstract = re.sub(r"^abstract[:.\s]*", "", clean(item.get("abstract")), flags=re.I)
    return {"id": slug(doi), "doi": doi, "title": title, "authors": names(item), "venue": venue,
            "date": date, "url": "https://doi.org/" + doi, "abstract": abstract[:ABSTRACT_CHARS]}


# ---------- the reader's private state ----------

def notes_state():
    """The reader's board, notes, dismissals and threads from the private notes repository."""
    if not (NOTES_TOKEN and NOTES_REPO):
        return None, "No notes token was set, so the public default profile was used."
    try:
        j = http(f"https://api.github.com/repos/{NOTES_REPO}/contents/user.json",
                 headers={"Authorization": f"Bearer {NOTES_TOKEN}",
                          "Accept": "application/vnd.github+json",
                          "X-GitHub-Api-Version": "2022-11-28"})
        return json.loads(base64.b64decode(j["content"]).decode("utf-8")), ""
    except Exception as e:  # the check still runs on the public profile
        return None, f"The notes repository could not be read ({e}), so the public default profile was used."


def reader_state(public):
    """Board, dismissals, areas and threads: from the private notes when readable, else the public profile."""
    user, msg = notes_state()
    user = user if isinstance(user, dict) else {}
    board = user.get("board") if isinstance(user.get("board"), dict) else {}
    dismissed = {k for k, v in (user.get("dismissed") or {}).items() if isinstance(v, dict) and v.get("v")}
    areas = [a for a in (user.get("areas") or public.get("areas") or []) if isinstance(a, str) and a]
    threads = user.get("threads") if isinstance(user.get("threads"), str) else public.get("threads") or ""
    return board, dismissed, areas, threads, msg


# ---------- Claude ----------

USAGE = {"requests": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0}


def claude(system, user, max_tokens=8000, model=None):
    """One Messages request. `user` is a string or a list of content blocks. Returns the reply text."""
    out = http("https://api.anthropic.com/v1/messages",
               headers={"x-api-key": API_KEY, "anthropic-version": API_VERSION,
                        "content-type": "application/json"},
               body={"model": model or MODEL, "max_tokens": max_tokens, "system": system,
                     "messages": [{"role": "user", "content": user}]},
               timeout=600)
    usage = out.get("usage") or {}
    USAGE["requests"] += 1
    USAGE["input"] += int(usage.get("input_tokens") or 0)
    USAGE["output"] += int(usage.get("output_tokens") or 0)
    USAGE["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
    USAGE["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
    return "".join(b.get("text", "") for b in out.get("content") or [] if b.get("type") == "text")


def parse_json(text):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
    end = max(text.rfind("]"), text.rfind("}"))
    if not starts or end < min(starts):
        raise ValueError("no JSON in the reply")
    return json.loads(text[min(starts):end + 1])


TRIAGE_SYSTEM = """You triage newly registered philosophy journal articles for one reader's private reading list.

You are given the reader's areas, the threads they are working on now, evidence of their taste (papers they saved, with any notes, and papers they dismissed), and a batch of candidate records from Crossref.

Give each candidate a lane:
- "new": squarely in one of the areas and something this reader would want to see.
- "reserve": in an area but further from the threads.
- "pass": outside the areas, or a book review, a notice, or other non-article.
There is no quota. Passing every candidate is an acceptable result.

For "new" and "reserve" also give:
- "area": exactly one of the area names, spelled as given.
- "why": one or two plain sentences, at most 40 words, in your own words, saying what the paper claims and, where true, which thread it bears on. Never reuse wording from an abstract. No praise, no filler, no em dashes. Do not address the reader: never write "you" or "your"; name the thread instead.
- "basis": "abstract" if the record has an abstract, otherwise "title". With "title", say only what the title supports.

Reply with only a JSON array holding one object per candidate, best fit first:
{"id": "...", "lane": "new" | "reserve" | "pass", "area": "...", "why": "...", "basis": "abstract" | "title"}
Use each id exactly as given."""

CLASSICS_SYSTEM = """You suggest older philosophy papers for one reader's private reading list.

You are given the reader's areas, the threads they are working on, their notes on saved papers, and the titles already on the list. Suggest at most one journal article per area, published at least five years ago, and only where it bears directly on a named thread or on one of the reader's notes. Suggesting nothing is an acceptable result.

Only suggest papers you are confident exist as described. Each suggestion is checked against Crossref and dropped if it is not found. Do not suggest anything already on the list.

For each suggestion give "why": one or two plain sentences, at most 40 words, saying what the paper does and which thread it serves. No praise, no filler, no em dashes. Do not address the reader: never write "you" or "your"; name the thread instead.

Reply with only a JSON array: {"area": "...", "authors": "...", "title": "...", "year": 1970, "why": "..."}"""


def tidy(why):
    why = re.sub(r"\s*[—–]\s*", ", ", clean(str(why or "")))
    return why[:400]


def triage(batch, profile):
    payload = dict(profile)
    payload["candidates"] = [
        {"id": c["id"], "title": c["title"], "authors": c["authors"], "venue": c["venue"],
         "abstract": c["abstract"] or None} for c in batch]
    reply = claude(TRIAGE_SYSTEM, json.dumps(payload, ensure_ascii=False))
    rows = parse_json(reply)
    if isinstance(rows, dict):
        rows = rows.get("candidates") or rows.get("results") or []
    known = {c["id"]: c for c in batch}
    judged = {}
    for order, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict) or row.get("id") not in known or row["id"] in judged:
            continue
        lane = row.get("lane")
        if lane not in ("new", "reserve", "pass"):
            continue
        area = ""
        if lane != "pass":
            match = difflib.get_close_matches(str(row.get("area") or ""), profile["areas"], 1, 0.6)
            why = tidy(row.get("why"))
            if not match or not why:
                lane = "pass"
            else:
                area = match[0]
        judged[row["id"]] = {
            "lane": lane, "area": area, "order": order,
            "why": tidy(row.get("why")) if lane != "pass" else "",
            "basis": "abstract" if known[row["id"]]["abstract"] and row.get("basis") != "title" else "title"}
    return judged


def find_classic(sugg):
    """Confirm a suggested paper through Crossref. Returns ledger fields or None."""
    query = " ".join(str(sugg.get(k) or "") for k in ("authors", "title", "year"))
    params = {"query.bibliographic": query, "rows": "5",
              "select": "DOI,title,author,container-title,issued,volume,issue,page,type"}
    if MAILTO:
        params["mailto"] = MAILTO
    items = (http("https://api.crossref.org/works?" + urllib.parse.urlencode(params)).get("message") or {}).get("items") or []
    want = norm(sugg.get("title"))
    surnames = set(norm(sugg.get("authors")).split())
    for it in items:
        if it.get("type") != "journal-article":
            continue
        title = tidy_title(" ".join(it.get("title") or []))
        if difflib.SequenceMatcher(None, want, norm(title)).ratio() < 0.9:
            continue
        families = {norm(a.get("family")) for a in it.get("author") or [] if a.get("family")}
        if not families or not any(f and set(f.split()) <= surnames for f in families):
            continue
        year = (((it.get("issued") or {}).get("date-parts") or [[None]])[0] or [None])[0]
        vol, issue = it.get("volume") or "", it.get("issue") or ""
        page = (it.get("page") or "").replace("-", "–")
        cite = vol + (f"({issue})" if issue else "")
        cite = (cite + ": " + page if cite and page else cite or page).strip()
        cite = (cite + f" ({year})" if year else cite).strip()
        doi = it["DOI"]
        return {"id": slug(doi), "title": title, "authors": names(it),
                "venue": clean(" ".join((it.get("container-title") or [])[:1])),
                "cite": cite, "year": year, "url": "https://doi.org/" + doi}
    return None


# ---------- the run ----------

def main():
    if not API_KEY:
        sys.exit("ANTHROPIC_API_KEY is not set. Add it under the repository's Actions secrets.")

    papers = load("papers.json", [])
    desk = load("desk.json", [])
    runs = load("runs.json", [])
    journals = (load("journals.json", {}) or {}).get("list") or []
    public = load("profile.json", {}) or {}
    if not journals:
        sys.exit("data/journals.json lists no journals.")

    notes = []
    board, dismissed, areas, threads, msg = reader_state(public)
    if msg:
        notes.append(msg)
    if not areas:
        sys.exit("No areas are set in the profile.")

    by_id = {p["id"]: p for p in papers}
    profile = {
        "areas": areas,
        "threads": threads,
        "saved": [{"title": b.get("title"), "note": (b.get("note") or "")[:600]} for b in board.values() if isinstance(b, dict)][:60],
        "dismissed": [by_id[i]["title"] for i in dismissed if i in by_id][:60],
    }

    seen = set(by_id) | {d["id"] for d in desk} | set(board)
    for r in runs:
        seen.update(r.get("passed") or [])

    today = dt.datetime.now(dt.timezone.utc).date()
    last = max((r.get("at") or "" for r in runs), default="")
    try:
        since = (dt.date.fromisoformat(last[:10]) - dt.timedelta(days=1)).isoformat()
    except ValueError:
        since = (today - dt.timedelta(days=30)).isoformat()

    # 1. What have the journals registered?
    candidates, failed, quiet, checked = {}, [], 0, 0
    for j in journals:
        try:
            items = crossref_new(j["issn"], since)
        except Exception as e:
            failed.append(j["name"])
            print(f"  {j['name']}: failed ({e})")
            continue
        found = [r for r in (record(it, j["name"]) for it in items) if r]
        checked += len(found)
        if not found:
            quiet += 1
        for r in found:
            if r["id"] not in seen:
                candidates[r["id"]] = r
        print(f"  {j['name']}: {len(found)} articles")
        time.sleep(0.4)
    if len(failed) == len(journals):
        sys.exit("Crossref could not be reached for any journal. Nothing was written.")
    # Records the last run could not judge get another turn.
    for r in runs[-1:]:
        for c in r.get("carry") or []:
            if isinstance(c, dict) and c.get("id") and c["id"] not in seen:
                candidates.setdefault(c["id"], c)

    # 2. Which of them fit?
    todo = list(candidates.values())
    judged = {}
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        got = triage(batch, profile)
        missing = [c for c in batch if c["id"] not in got]
        if missing:
            got.update(triage(missing, profile))
        base = len(judged)
        for k, v in got.items():
            v["order"] += base
            judged[k] = v
    carry = [c for c in todo if c["id"] not in judged]

    run_id = f"{today.isoformat()}-auto"
    taken = {r.get("id") for r in runs}
    n = 2
    while run_id in taken:
        run_id = f"{today.isoformat()}-auto-{n}"
        n += 1

    ordered = sorted((k for k in judged if judged[k]["lane"] != "pass"), key=lambda k: judged[k]["order"])
    reserve_kept, ranks, added, passed = 0, {}, [], [k for k in judged if judged[k]["lane"] == "pass"]
    for k in ordered:
        v, c = judged[k], candidates[k]
        if v["lane"] == "reserve":
            if reserve_kept >= RESERVE_CAP:
                passed.append(k)
                continue
            reserve_kept += 1
        key = (v["lane"], v["area"])
        ranks[key] = ranks.get(key, 0) + 1
        added.append({"id": k, "title": c["title"], "authors": c["authors"], "venue": c["venue"],
                      "date": c["date"], "url": c["url"], "area": v["area"], "lane": v["lane"],
                      "rank": ranks[key], "why": v["why"], "basis": v["basis"], "run": run_id})
    kept = sum(1 for a in added if a["lane"] == "new")

    # 3. Older work tied to the threads.
    classics = []
    if CLASSICS:
        try:
            listed = [p["title"] for p in papers] + [d["title"] for d in desk] + [b.get("title") for b in board.values() if isinstance(b, dict)]
            reply = claude(CLASSICS_SYSTEM, json.dumps(
                {"areas": areas, "threads": threads, "notes": profile["saved"], "already_listed": listed}, ensure_ascii=False), 3000)
            suggestions = parse_json(reply)
            used_areas = set()
            for s in suggestions if isinstance(suggestions, list) else []:
                if not isinstance(s, dict):
                    continue
                match = difflib.get_close_matches(str(s.get("area") or ""), areas, 1, 0.6)
                why = tidy(s.get("why"))
                if not match or match[0] in used_areas or not why:
                    continue
                try:
                    year = int(s.get("year"))
                except (TypeError, ValueError):
                    continue
                if year > today.year - 5:
                    continue
                hit = find_classic(s)
                time.sleep(0.4)
                if not hit or hit["id"] in seen or any(a["id"] == hit["id"] for a in added):
                    continue
                used_areas.add(match[0])
                hit.update({"area": match[0], "lane": "classic", "rank": len(classics) + 1,
                            "why": why, "basis": "known", "run": run_id})
                classics.append(hit)
        except Exception as e:
            notes.append(f"The classics step failed ({e}).")

    # 4. Write the ledger.
    if failed:
        notes.append("Crossref failed for: " + ", ".join(failed) + ".")
    if quiet:
        notes.append(f"{quiet} of {len(journals)} journals registered no research articles since {since}.")
    if carry:
        notes.append(f"{len(carry)} records could not be judged and are carried to the next run.")
    if not candidates:
        notes.append("Nothing new to judge.")
    papers.extend(added + classics)
    runs.append({"id": run_id, "at": today.isoformat(), "kind": "Weekly check", "since": since,
                 "journals": len(journals) - len(failed), "checked": checked, "kept": kept,
                 "reserve": reserve_kept, "classics": len(classics), "model": MODEL, "tokens": dict(USAGE),
                 "passed": sorted(passed),
                 "carry": [{k: c[k] for k in ("id", "title", "authors", "venue", "date", "url", "abstract")} for c in carry],
                 "note": " ".join(notes) or "No problems."})
    save("papers.json", papers)
    save("runs.json", runs)
    print(f"{run_id}: checked {checked}, judged {len(judged)}, kept {kept}, reserve {reserve_kept}, classics {len(classics)}")


if __name__ == "__main__":
    main()
