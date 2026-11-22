#!/usr/bin/env python3
"""The desk: a few papers per issue, read in full and reviewed twice.

For each paper it selects, the desk
  1. finds an open copy and downloads the PDF (Unpaywall when CONTACT_EMAIL is set,
     otherwise Crossref's own record for openly licensed articles),
  2. states the author's argument,
  3. has a first reviewer try to break it, formal machinery first,
  4. has a second reviewer, who sees only the paper and the objections, check each
     objection against the text, score what survives, and say who can still use the paper.
The entries become a new issue in data/desk.json. A paper with no open copy is left alone,
and a paper already reviewed is never reviewed again.

Environment (besides what check.py reads)
  READING_ROOM_DESK_MODEL   model for the desk; defaults to claude-opus-5-5.
  READING_ROOM_DESK_MAX     most papers per issue; defaults to 4. 0 turns the desk off.
  READING_ROOM_DESK_TOKENS  stop starting new papers past this many tokens read; default 1500000.
  CONTACT_EMAIL             your address, sent to Unpaywall and Crossref as the contact.
"""
import base64
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import check as lib


def env_int(name, default):
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


DESK_MODEL = os.environ.get("READING_ROOM_DESK_MODEL", "").strip() or "claude-opus-5-5"
MAX_PAPERS = max(0, env_int("READING_ROOM_DESK_MAX", 4))
TOKEN_BUDGET = env_int("READING_ROOM_DESK_TOKENS", 1500000)
CONTACT = os.environ.get("CONTACT_EMAIL", "").strip()
PER_AREA = 2
MAX_PDF_BYTES = 10 * 1024 * 1024
RECHECK_DAYS = 28      # look again for an open copy after this long
POOL_DAYS = 70         # how far back the desk looks for candidates
MAX_ATTEMPTS = 2       # failed reviews of one paper before the desk stops trying
PDF_UA = "Mozilla/5.0 (compatible; reading-room-desk/1.0)"
VERSION = {"publishedVersion": "published version", "acceptedVersion": "accepted manuscript",
           "submittedVersion": "submitted manuscript"}
CACHE = {"on": True}

SYSTEM = """You work on a private philosophy research desk that prepares short reviewed entries for one reader, an analytic philosopher. You are given one paper as a PDF and one task. Do the task from the paper itself and follow its output format exactly. Write plainly: no praise, no filler, no em dashes, and never address the reader."""

TASK_ARGUMENT = """Task: state the author's argument.

Write one paragraph of 90 to 130 words in six to eight sentences. Begin with the question the paper asks, in the form "<Surname> asks ...". Then give the answer and the main steps in the order the paper takes them, and end with the conclusion as the author qualifies it. Report only: do not evaluate, and do not mention this desk or the PDF. Keep the author's technical terms.

{examples}Reply with only the paragraph."""

TASK_ATTACK = """Task: try to break the paper's argument.

Look hardest at formal machinery: definitions, constructions, theorems, and the steps that rely on them. Then test the key inferences, and whether the conclusion claims more than the premises support. Check the footnotes and stated qualifications before raising a point the author has already dealt with.

Return up to five objections, strongest first, as a JSON array:
[{"objection": "one or two sentences", "where": "section or page", "kind": "formal" | "inferential" | "scope" | "exegetical", "severity": 0-10}]
Return [] if none survives your own scrutiny. Reply with only the JSON."""

TASK_CHECK = """Task: check a first reviewer's objections against the paper, then write the review.

The first reviewer raised these objections:
{objections}

For each one, decide from the paper itself whether it lands. Does the paper already answer it, including in footnotes and qualifications? Does it misread the paper? If it stands, how much does it damage, and what does it leave intact?

Return JSON:
{{"checked": [{{"objection": "short restatement", "verdict": "stands" | "partly stands" | "answered in the paper" | "misreads the paper", "reason": "one sentence"}}],
 "severity": an integer from 0 (nothing survives) to 10 (the main result fails), for what survives,
 "review": "two to four sentences, at most 70 words, on the concerns that survive and on what does not follow from them. If a finding is this desk's own, say that it is a reviewer finding and not an erratum.",
 "use": "who can still use the paper and for what, at most 25 words, starting in lower case and ending with a full stop",
 "tag": "Optional advanced technical reading" if the paper is heavily formal, otherwise ""}}

{examples}Reply with only the JSON."""

SELECT_SYSTEM = """You choose which papers a private philosophy research desk reads in full for its next issue. Full reviews are expensive, so choose only papers that merit one.

You are given the reader's areas and threads, their notes on saved papers, and candidates that have an open copy. Choose at most {per_area} per area and at most {most} in all. This is a limit, not a quota: choose fewer, or none, if nothing merits it. Prefer papers the reader saved and has not finished, then papers that bear directly on a named thread. Do not choose a paper only because it is new.

Reply with only a JSON array of candidate ids, best first."""

TITLE_SYSTEM = """You name an issue of a philosophy reading digest. Given the titles of the papers in it, reply with one line of at most eight words that names the issue's topics, such as "Public reason, theoretical terms, value and art". Sentence case, no quotation marks, no full stop."""


# ---------- finding and fetching open copies ----------

def doi_of(url):
    m = re.search(r"10\.\d{4,9}/\S+", str(url or ""))
    return m.group(0) if m else ""


def open_copies(doi):
    """PDF addresses for a DOI, best first, as (url, version label)."""
    q = urllib.parse.quote(doi, safe="/")
    found = []
    if CONTACT:
        j = lib.http(f"https://api.unpaywall.org/v2/{q}?email={urllib.parse.quote(CONTACT)}")
        for loc in [j.get("best_oa_location")] + list(j.get("oa_locations") or []):
            if isinstance(loc, dict) and loc.get("url_for_pdf"):
                found.append((loc["url_for_pdf"], VERSION.get(loc.get("version"), "open copy")))
    else:
        rec = lib.http(f"https://api.crossref.org/works/{q}").get("message") or {}
        if any("creativecommons.org" in (lic.get("URL") or "") for lic in rec.get("license") or []):
            for link in rec.get("link") or []:
                if link.get("content-type") == "application/pdf" and link.get("URL"):
                    found.append((link["URL"], "published version"))
    seen, out = set(), []
    for url, label in found:
        if url.startswith("http") and url not in seen:
            seen.add(url)
            out.append((url, label))
    return out


def fetch_pdf(url):
    req = urllib.request.Request(url, headers={"User-Agent": PDF_UA, "Accept": "application/pdf,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise RuntimeError("the PDF is larger than 10 MB")
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise RuntimeError("the address did not return a PDF")
    return data


# ---------- the reviewers ----------

def ask(pdf_b64, task, max_tokens):
    """One request about the paper. The PDF is marked for caching so the three passes share it."""
    doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}}
    if CACHE["on"]:
        try:
            return lib.claude(SYSTEM, [dict(doc, cache_control={"type": "ephemeral"}), {"type": "text", "text": task}],
                              max_tokens, DESK_MODEL)
        except RuntimeError as e:
            if "HTTP 400" not in str(e) or "cache" not in str(e).lower():
                raise
            CACHE["on"] = False   # this API version refuses the cache marker; carry on without it
    return lib.claude(SYSTEM, [doc, {"type": "text", "text": task}], max_tokens, DESK_MODEL)


def plain(text, limit):
    text = re.sub(r"\s*[—–]\s*", ", ", lib.clean(str(text or "")))
    return text[:limit].strip()


def house_examples(desk):
    """Two entries from the earliest issue, used to show the house style."""
    if not desk:
        return "", ""
    first = min(d.get("issue") or "" for d in desk)
    shown = [d for d in desk if d.get("issue") == first and d.get("argument") and d.get("review")][:2]
    if not shown:
        return "", ""
    arg = "Two paragraphs in the house style:\n" + "\n".join(f"- {d['argument']}" for d in shown) + "\n\n"
    rev = "Two reviews in the house style:\n" + "\n".join(
        f"- review: {d['review']} use: {d.get('use', '')}" for d in shown) + "\n\n"
    return arg, rev


def review(paper, pdf, examples):
    b64 = base64.b64encode(pdf).decode("ascii")
    argument = plain(ask(b64, TASK_ARGUMENT.format(examples=examples[0]), 1500), 1400)
    if len(argument) < 200:
        raise RuntimeError("the exposition came back too short")
    raw = lib.parse_json(ask(b64, TASK_ATTACK, 4000))
    objections = [o for o in (raw if isinstance(raw, list) else []) if isinstance(o, dict) and o.get("objection")][:5]
    listed = "\n".join(f"{i + 1}. {plain(o['objection'], 500)} (where: {plain(o.get('where'), 80) or 'not given'})"
                       for i, o in enumerate(objections)) or "None. Check independently whether the argument has a weakness the first reviewer missed."
    verdict = lib.parse_json(ask(b64, TASK_CHECK.format(objections=listed, examples=examples[1]), 4000))
    if not isinstance(verdict, dict) or not verdict.get("review"):
        raise RuntimeError("the second review came back without a review")
    try:
        severity = max(0, min(10, int(round(float(verdict.get("severity"))))))
    except (TypeError, ValueError):
        raise RuntimeError("the second review gave no severity") from None
    use = re.sub(r"^use\s*:\s*", "", plain(verdict.get("use"), 240), flags=re.I)
    checked = [{"objection": plain(c.get("objection"), 300), "verdict": plain(c.get("verdict"), 40),
                "reason": plain(c.get("reason"), 300)}
               for c in verdict.get("checked") or [] if isinstance(c, dict)][:5]
    tag = "Optional advanced technical reading" if "technical" in str(verdict.get("tag") or "").lower() else ""
    return {"argument": argument, "severity": severity, "review": plain(verdict["review"], 700),
            "use": use, "tag": tag, "checked": checked}


def cite_of(p, today):
    venue = p.get("venue") or ""
    if p.get("cite"):
        return f"{venue} {p['cite']}".strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", p.get("date") or "")
    if m:
        month = dt.date(2000, int(m.group(2)), 1).strftime("%B")
        return f"{venue}, registered {int(m.group(3))} {month} {m.group(1)}".strip(", ")
    return venue or str(p.get("year") or today.year)


def choose(avail, areas, threads, board):
    saved = [{"title": b.get("title"), "note": (b.get("note") or "")[:600]} for b in board.values() if isinstance(b, dict)][:60]
    payload = {"areas": areas, "threads": threads, "saved_notes": saved, "candidates": [
        {"id": p["id"], "title": p["title"], "authors": p.get("authors"), "area": p.get("area"),
         "kind": p.get("lane"), "why_listed": p.get("why") or None,
         "saved_by_reader": p["id"] in board,
         "open_copy": p["_version"]} for p in avail]}
    system = SELECT_SYSTEM.format(per_area=PER_AREA, most=MAX_PAPERS)
    ids = lib.parse_json(lib.claude(system, json.dumps(payload, ensure_ascii=False), 1500))
    by_id = {p["id"]: p for p in avail}
    picked, per_area = [], {}
    for i in ids if isinstance(ids, list) else []:
        p = by_id.get(i)
        if not p or p in picked:
            continue
        area = p.get("area") or ""
        if per_area.get(area, 0) >= PER_AREA:
            continue
        per_area[area] = per_area.get(area, 0) + 1
        picked.append(p)
        if len(picked) >= MAX_PAPERS:
            break
    return picked


def tokens_read():
    u = lib.USAGE
    return u["input"] + u["cache_write"] + u["cache_read"]


# ---------- the run ----------

def main():
    if MAX_PAPERS == 0:
        print("The desk is off (READING_ROOM_DESK_MAX is 0).")
        return
    if not lib.API_KEY:
        sys.exit("ANTHROPIC_API_KEY is not set. Add it under the repository's Actions secrets.")

    papers = lib.load("papers.json", [])
    desk = lib.load("desk.json", [])
    issues = lib.load("issues.json", [])
    full = lib.load("fulltext.json", {}) or {}
    log = lib.load("desk-log.json", []) or []
    public = lib.load("profile.json", {}) or {}
    board, dismissed, areas, threads, msg = lib.reader_state(public)
    today = dt.datetime.now(dt.timezone.utc).date()
    notes = [msg] if msg else []
    done = {d["id"] for d in desk}

    def finish(issue_id, selected, reviewed, failed_all=False):
        log.append({"id": issue_id or f"{today.isoformat()}-none", "at": today.isoformat(), "selected": selected,
                    "reviewed": reviewed, "model": DESK_MODEL, "tokens": dict(lib.USAGE),
                    "note": " ".join(n for n in notes if n) or "No problems."})
        lib.save("desk-log.json", log)
        lib.save("fulltext.json", full)
        print(f"desk: selected {selected}, reviewed {reviewed}; {log[-1]['note']}")
        if failed_all:
            sys.exit("Every selected paper failed at the review stage. See the note above.")

    # 1. Candidates: recent ledger entries and what the reader saved, minus anything dismissed,
    #    finished, already reviewed, or already tried twice.
    cutoff = today - dt.timedelta(days=POOL_DAYS)
    pool = {}
    for p in papers:
        pid = p["id"]
        if pid in done or pid in dismissed or p.get("lane") not in ("new", "reserve", "classic"):
            continue
        m = re.match(r"\d{4}-\d{2}-\d{2}", p.get("run") or "")
        recent = bool(m) and dt.date.fromisoformat(m.group(0)) >= cutoff
        if pid in board or recent:
            pool[pid] = dict(p)
    for bid, b in board.items():
        if bid in done or bid in pool or not isinstance(b, dict) or not doi_of(b.get("url")):
            continue
        pool[bid] = {"id": bid, "title": b.get("title") or "", "authors": b.get("authors") or "",
                     "venue": b.get("venue") or "", "cite": b.get("cite") or "", "year": b.get("year"),
                     "url": "https://doi.org/" + doi_of(b.get("url")), "area": "From the board", "lane": "saved", "why": ""}
    for pid in list(pool):
        b = board.get(pid)
        if isinstance(b, dict) and b.get("column") == "done":
            del pool[pid]
        elif (full.get(pid) or {}).get("attempts", 0) >= MAX_ATTEMPTS:
            del pool[pid]

    # 2. Open copies.
    lookup_errors = 0
    for pid, p in pool.items():
        rec = full.get(pid) or {}
        age = RECHECK_DAYS
        try:
            age = (today - dt.date.fromisoformat(str(rec.get("checked"))[:10])).days
        except ValueError:
            pass
        if rec.get("url") or (rec and age < RECHECK_DAYS):
            continue
        doi, copies = doi_of(p.get("url")), []
        if doi:
            try:
                copies = open_copies(doi)
            except Exception as e:
                if "HTTP 404" not in str(e):
                    lookup_errors += 1
            time.sleep(0.25)
        full[pid] = {"checked": today.isoformat(), "url": copies[0][0] if copies else None,
                     "version": copies[0][1] if copies else "", "alts": [c[0] for c in copies[1:3]],
                     "attempts": rec.get("attempts", 0)}
    lib.save("fulltext.json", full)
    if lookup_errors:
        notes.append(f"{lookup_errors} open-copy lookups failed.")
    if not CONTACT:
        notes.append("CONTACT_EMAIL is not set, so only openly licensed publisher copies were looked for. Setting it lets the desk find author and repository copies too.")

    avail = []
    for p in pool.values():
        rec = full.get(p["id"]) or {}
        if rec.get("url"):
            p["_version"] = rec.get("version") or "open copy"
            avail.append(p)
    if not avail:
        notes.append(f"None of the {len(pool)} candidates has an open copy.")
        return finish("", 0, 0)

    # 3. Which ones merit a full review?
    chosen = choose(avail, areas, threads, board)
    if not chosen:
        notes.append(f"{len(avail)} candidates had an open copy; none was chosen for review.")
        return finish("", 0, 0)

    issue_id, n = today.isoformat(), 2
    taken = {i.get("id") for i in issues}
    while issue_id in taken:
        issue_id = f"{today.isoformat()}-{n}"
        n += 1

    # 4. Read and review, saving after each paper so a late failure loses nothing.
    order = {a: i for i, a in enumerate(areas)}
    chosen.sort(key=lambda p: order.get(p.get("area"), len(order)))
    examples = house_examples(desk)
    reviewed, skipped, review_failures = [], [], 0
    for p in chosen:
        pid, rec = p["id"], full[p["id"]]
        if tokens_read() > TOKEN_BUDGET:
            notes.append(f"Stopped before \"{p['title']}\": the token limit for one issue was reached.")
            break
        pdf, used, why_not = None, "", ""
        for url in [rec["url"]] + list(rec.get("alts") or []):
            try:
                pdf, used = fetch_pdf(url), url
                break
            except Exception as e:
                why_not = str(e)[:160]
        if pdf is None:
            full[pid] = {"checked": today.isoformat(), "url": None, "version": "", "alts": [],
                         "attempts": rec.get("attempts", 0), "error": why_not}
            skipped.append(f"\"{p['title']}\" (open copy could not be downloaded: {why_not})")
            continue
        try:
            result = review(p, pdf, examples)
        except Exception as e:
            rec["attempts"] = rec.get("attempts", 0) + 1
            rec["error"] = str(e)[:200]
            review_failures += 1
            skipped.append(f"\"{p['title']}\" (review failed: {str(e)[:200]})")
            continue
        entry = {"id": pid, "issue": issue_id, "order": len(reviewed) + 1, "section": p.get("area") or "Papers",
                 "title": p["title"], "authors": p.get("authors") or "", "cite": cite_of(p, today),
                 "year": p.get("year") or (p.get("date") or "")[:4], "url": p["url"],
                 "links": [{"label": "Published paper", "url": p["url"]},
                           {"label": f"Open copy read by the desk ({rec.get('version') or 'open copy'})", "url": used}]}
        entry.update(result)
        desk.append(entry)
        reviewed.append(entry)
        lib.save("desk.json", desk)
        print(f"  reviewed: {p['title']} (severity {result['severity']})")

    if skipped:
        notes.append("Skipped: " + "; ".join(skipped) + ".")
    if not CACHE["on"]:
        notes.append("The API refused the cache marker, so each pass re-read the PDF at full price.")
    if not reviewed:
        return finish("", len(chosen), 0, failed_all=review_failures == len(chosen))

    title = ""
    try:
        title = plain(lib.claude(TITLE_SYSTEM, json.dumps([e["title"] for e in reviewed], ensure_ascii=False), 100), 90)
        title = title.splitlines()[0].strip(" .\"'") if title else ""
    except Exception:
        pass
    issues.append({"id": issue_id, "date": today.isoformat(), "title": title, "source": "Reviewed by the desk",
                   "note": "Objection-severity scores are provisional reviewer judgments, not measures of paper quality or probabilities. "
                           "Each review rests on the open copy linked beside it, which may differ from the published version."})
    lib.save("issues.json", issues)
    finish(issue_id, len(chosen), len(reviewed))


if __name__ == "__main__":
    main()
