#!/usr/bin/env python3
"""Admission judgement for a NEW skill — the PEN side of the rule the skills publish door
already applies.

ONE RULE, TWO DOORS. The publish door (`scripts/skills_publish_nodes.py::admission` in
`yaan-platform`, card t_afaf7e6f) decides whether a proposed skill should exist AT ALL:
similarity against the landing corpus on the intent surface, materiality against the context
the skill costs on every load, and exactly one disposition — `new`, `fold into <skill>` or
`reject`. That judgement fires at PUBLISH time, by which point the skill already exists on
disk, is loadable, and has done its proliferating. This module is the same judgement applied
at CREATE time, so the pen cannot manufacture what the door would refuse.

It is a deliberate, line-for-line port of the door's arithmetic — the same constants, the same
intent surface, the same IDF weighting, the same disposition order — and
`tests/conformance/test_skill_admission_door_parity.py` runs one fixture through BOTH
implementations and fails on any divergence. It is not a second, independently invented check:
if a threshold or a rule changes on one side, that test is the alarm.

Two places where the pen LEGITIMATELY differs from the door, both stated so the difference is
reviewable rather than accidental:

* **The corpus.** The door compares against the landing corpus (`route_config().SHARED`), a
  deployment artifact that must exist: if it is unreadable the door fails CLOSED, because
  admitting blind is worse than refusing. The pen compares against the skills tree the agent
  can actually see; an EMPTY corpus there is not a wiring fault, it is a fresh install with
  nothing to duplicate, so the draft is admitted with that stated in its reason. Nothing is
  ever refused on a corpus that could not be enumerated — and a verdict that could not be
  computed ADMITS: the pen fails OPEN, because a bug in a policy gate must not stop every lane
  from learning. That fail-open is not silent, which is the part that matters — the caller
  writes it to the admission ledger as a `fault` row
  (`tools.skill_usage.record_admission_fault`), naming what could not be judged and why, so a
  broken gate can never read to an audit as a gate that found nothing to refuse.
* **The unit.** The door judges `<skill>/SKILL.md` blobs. The pen also judges a NEW
  `references/<topic>.md` support file, because "add a file under another skill" is the other
  cheap way to grow the corpus; a support file has no frontmatter, so its intent surface is
  its own opening paragraph plus headings (see :func:`draft_from_text`).

Stdlib only, no imports from the Hermes package: the door's script loads this module by file
path for the parity test, and the tool imports it in-process.
"""

import math
import re
from pathlib import Path

# --- The rule (verbatim from the door; change only WITH the door) ----------------------

#: `skill_quality_lint.py`'s duplicate-description line (two descriptions for one job).
ADMISSION_DESCRIPTION_DUPLICATE = 0.80
#: Measured corpus ceiling for a genuine pair (0.51) plus a margin: at/above this, a draft is
#: a restatement of one existing skill and folds into it.
ADMISSION_FOLD_COVERAGE = 0.60
#: The corpus's 10th-percentile novelty (measured 2026-10-18: 0.1500 over 82 skills). Below it,
#: nothing it says is new. Only rejects when no single skill owns the content.
ADMISSION_MIN_NOVELTY = 0.15
#: `skill_quality_lint.py`'s stub line: below this a SKILL.md is not a skill.
ADMISSION_STUB_BYTES = 200
#: How many similarity matches a verdict prints (the door: "top matches with their level").
ADMISSION_MATCHES = 3
#: Directories that are NOT skills: the landing tree keeps retired (`absorbed`), reference-only
#: (`depth`) and tooling (`scripts`) trees at the top level; the pen also never reads a skill's
#: own support directories as if they were skills.
ADMISSION_SKIP_DIRS = frozenset(("absorbed", "depth", "scripts", "_shared", "docs"))
#: A skill's own support directories: a SKILL.md inside one of these is data, not a skill.
_SUPPORT_DIRS = frozenset(("references", "templates", "scripts", "assets"))
#: English function words only. Shared DOMAIN vocabulary is deliberately left in: IDF already
#: discounts it, and removing it here would make a genuine new skill look novel by fiat.
ADMISSION_STOPWORDS = frozenset((
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are", "be",
    "it", "its", "this", "that", "as", "at", "by", "from", "not", "no", "you", "your", "use",
    "when", "if", "then", "so", "any", "all", "do", "does", "only", "one", "two", "must",
    "never", "always", "each", "every", "can", "will", "has", "have", "was", "were", "which",
    "what", "how", "why", "who", "there", "their", "them", "they", "we", "our", "out", "up",
    "down", "into", "over", "also", "but", "just", "like", "than", "more", "most", "such",
    "same", "other", "first", "yoyodine",
))

#: The dispositions, most specific first — the order `judge` tests them in.
DISPOSITIONS = ("reject", "fold", "new")


# --- Parsing and the intent surface ----------------------------------------------------

def frontmatter(raw):
    """(mapping, body) from a SKILL.md's YAML frontmatter; stdlib only, never raises.

    Only the handful of scalar keys admission reads are extracted (`description`, `name` and
    the nested `tier`). Block scalars (`description: >` / `|`) are joined from their indented
    continuation lines, because a folded description is still the description.
    """
    lines = str(raw or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, str(raw or "")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, str(raw or "")
    block = lines[1:end]
    fm, key = {}, None
    for line in block:
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            fm[key] = "" if value in (">", "|", ">-", "|-", ">+", "|+") else value.strip("'\"")
            continue
        if key and line[:1] in (" ", "\t") and line.strip():
            fm[key] = (str(fm.get(key) or "") + " " + line.strip()).strip()
    tier = re.search(r"^\s+tier:\s*(\S+)\s*$", "\n".join(block), re.M)
    if tier:
        fm["tier"] = tier.group(1).strip("'\"")
    return fm, "\n".join(lines[end + 1:])


def intent_tokens(text):
    """The weighted-comparison token set: lower-case word tokens of 3+ characters, no filler."""
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower())
            if len(w) > 2 and w not in ADMISSION_STOPWORDS}


def intent_text(fm, body):
    """The INTENT SURFACE: description + opening paragraph + headings.

    What a skill is FOR, not how long it is or which incident prompted it: two skills that
    promise the same job over the same triggers must collide here even when one is three times
    the length and written by a different author.
    """
    opening = re.split(r"^##+\s", body, maxsplit=1, flags=re.M)[0].strip()
    headings = re.findall(r"^##+\s+(.+)$", body, re.M)
    return " ".join([str(fm.get("description") or ""), opening] + headings)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def draft_from_skill_md(name, raw):
    """A comparable draft from a SKILL.md's text (the door's `_admission_entry` input shape)."""
    fm, body = frontmatter(raw)
    return {"name": name, "tier": str(fm.get("tier") or "unknown"),
            "description_text": str(fm.get("description") or ""),
            "description": intent_tokens(str(fm.get("description") or "")),
            "intent": intent_tokens(intent_text(fm, body)),
            "bytes": len(str(raw).encode("utf-8")), "path": ""}


def draft_from_text(name, text, description=""):
    """A comparable draft from a SUPPORT FILE's text (a new `references/<topic>.md`).

    A support file carries no frontmatter, so its intent surface is built the SAME way as a
    SKILL.md's — opening paragraph plus headings, with the first non-empty line standing in
    for the description — so a file and a skill that promise the same thing collide, and one
    that merely shares vocabulary somewhere in its body does not.
    """
    body = str(text or "")
    if not description:
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        description = re.sub(r"^#+\s*", "", first)
    opening = re.split(r"^##+\s", body, maxsplit=1, flags=re.M)[0].strip()
    headings = re.findall(r"^##+\s+(.+)$", body, re.M)
    return {"name": name, "tier": "unknown", "description_text": description,
            "description": intent_tokens(description),
            "intent": intent_tokens(" ".join([description, opening] + headings)),
            "bytes": len(body.encode("utf-8")), "path": ""}


# --- The corpus ------------------------------------------------------------------------

def _is_skill_md(path, root):
    """True when ``path`` is a skill root's SKILL.md (never a support file's, never skipped)."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    if len(parts) < 2:
        return False
    dirs = parts[:-1]
    if any(d.startswith(".") or d in ADMISSION_SKIP_DIRS for d in dirs):
        return False
    if any(d in _SUPPORT_DIRS for d in dirs):
        return False
    return True


def corpus_docs(roots, exclude=()):
    """Every skill the agent can see, as a comparable doc, sorted by path (deterministic).

    ``roots`` may be a single path or an iterable. ``exclude`` holds paths (or directories) to
    leave out — the draft's own skill directory, or a duplicate root, so a skill never reads as
    its own neighbour. Unreadable roots and files are skipped: the corpus is a live tree, and
    one unreadable skill must not make every create fail.
    """
    if isinstance(roots, (str, Path)):
        roots = [roots]
    excluded = set()
    for item in exclude or ():
        try:
            excluded.add(Path(item).resolve())
        except OSError:
            continue
    docs, seen = [], set()
    for root in roots or ():
        root = Path(root)
        try:
            if not root.is_dir():
                continue
            paths = sorted(root.rglob("SKILL.md"))
        except OSError:
            continue
        for path in paths:
            if not _is_skill_md(path, root):
                continue
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen or resolved in excluded or resolved.parent in excluded:
                continue
            seen.add(resolved)
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm, body = frontmatter(raw)
            docs.append({"name": path.parent.name, "path": str(path),
                         "tier": str(fm.get("tier") or "unknown"),
                         "description": intent_tokens(str(fm.get("description") or "")),
                         "intent": intent_tokens(intent_text(fm, body))})
    docs.sort(key=lambda d: d["path"])
    return docs


def idf_of(docs):
    """(weight per token, weight of a token no existing skill uses) over the corpus."""
    df = {}
    for doc in docs:
        for token in doc["intent"]:
            df[token] = df.get(token, 0) + 1
    n = len(docs)
    unseen = math.log(n + 1) + 1.0
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}, unseen


def coverage(draft_tokens, existing, idf, unseen):
    """The share of the DRAFT's weighted intent the existing skill already states."""
    total = sum(idf.get(t, unseen) for t in draft_tokens)
    if not total:
        return 0.0
    return sum(idf.get(t, unseen) for t in draft_tokens if t in existing) / total


def load(roots, exclude=()):
    """(docs, idf, unseen) — the corpus ready to judge against."""
    docs = corpus_docs(roots, exclude=exclude)
    idf, unseen = idf_of(docs)
    return docs, idf, unseen


# --- The verdict -----------------------------------------------------------------------

def judge(draft, docs, idf, unseen):
    """One draft's similarity matches, materiality sentence and disposition.

    Order of testing is the door's: a draft with no description cannot be judged at all, a
    stub is not a skill, a restatement of ONE skill folds into it, a spread restatement of
    SEVERAL skills with no single owner is rejected, everything else is new.
    """
    name = str(draft.get("name") or "")
    intent = draft.get("intent") or set()
    desc_tokens = draft.get("description") or set()
    size = int(draft.get("bytes") or 0)
    description = str(draft.get("description_text") or "")
    cost = max(1, round(size / 4.0))
    spirit = set()
    for doc in docs:
        spirit |= doc["intent"]
    total = sum(idf.get(t, unseen) for t in intent)
    novelty = 1.0 - (sum(idf.get(t, unseen) for t in intent if t in spirit) / total) if total else 0.0
    matches = []
    for doc in docs:
        if doc["name"] == name:
            continue
        matches.append({"name": doc["name"], "tier": doc["tier"], "path": doc["path"],
                        "coverage": round(coverage(intent, doc["intent"], idf, unseen), 4),
                        "description_overlap": round(jaccard(desc_tokens, doc["description"]), 4)})
    matches.sort(key=lambda m: (-m["coverage"], -m["description_overlap"], m["name"]))
    fold = next((m for m in matches
                 if m["description_overlap"] >= ADMISSION_DESCRIPTION_DUPLICATE
                 or m["coverage"] >= ADMISSION_FOLD_COVERAGE), None)
    nearest = matches[0] if matches else None
    if not description.strip() and not draft.get("allow_empty_description"):
        disposition = "reject"
        reason = "no description: the skill cannot say what it is for"
    elif size < ADMISSION_STUB_BYTES:
        disposition = "reject"
        reason = "stub: %d bytes is below the %d-byte line" % (size, ADMISSION_STUB_BYTES)
    elif fold:
        disposition = "fold"
        reason = "fold into %s: %d%% of its intent surface is already stated there" % (
            fold["name"], round(fold["coverage"] * 100))
    elif novelty < ADMISSION_MIN_NOVELTY:
        disposition = "reject"
        reason = "spread restatement: only %d%% of its intent surface is new to the corpus " \
                 "and no single skill owns it (nearest %s at %d%%)" % (
                     round(novelty * 100), nearest["name"] if nearest else "-",
                     round((nearest["coverage"] if nearest else 0) * 100))
    else:
        disposition = "new"
        reason = "new: %d%% of its intent surface is new to the corpus (nearest %s covers %d%%)" % (
            round(novelty * 100), nearest["name"] if nearest else "-",
            round((nearest["coverage"] if nearest else 0) * 100))
    if fold is not None and disposition == "fold":
        materiality = "costs ~%d tokens per load (%d bytes); %d%% of its intent surface is " \
                      "already stated by %s (%s), so the knowledge lands there and this " \
                      "proposal is displaced" % (
                          cost, size, round(fold["coverage"] * 100), fold["name"], fold["path"])
        lands_at = fold["path"]
    elif disposition == "new":
        materiality = "costs ~%d tokens per load (%d bytes); %d%% of its intent surface is new, " \
                      "displaces nothing - no existing skill states it (nearest %s covers %d%%)" % (
                          cost, size, round(novelty * 100),
                          nearest["name"] if nearest else "-",
                          round((nearest["coverage"] if nearest else 0) * 100))
        lands_at = str(draft.get("path") or "")
    else:
        materiality = "costs ~%d tokens per load (%d bytes) and displaces nothing: %s" % (
            cost, size, reason)
        lands_at = ""
    shown = matches[:ADMISSION_MATCHES]
    if fold and fold not in shown:
        shown = [fold] + shown[:-1]
    return {"path": str(draft.get("path") or ""), "name": name,
            "tier": str(draft.get("tier") or "unknown"), "bytes": size, "context_tokens": cost,
            "novelty": round(novelty, 4), "matches": shown, "disposition": disposition,
            "fold_target": fold["name"] if fold else "", "fold_path": fold["path"] if fold else "",
            "lands_at": lands_at, "materiality": materiality, "reason": reason}


def verdict_for_content(name, content, roots, exclude=()):
    """The judgement for a SKILL.md draft against the corpus under ``roots``.

    Returns the verdict dict plus ``corpus_size``. An EMPTY corpus admits with that stated: a
    fresh install has nothing to duplicate, and refusing every first skill would be a gate that
    cannot be satisfied.
    """
    docs, idf, unseen = load(roots, exclude=exclude)
    draft = draft_from_skill_md(name, content)
    verdict = judge(draft, docs, idf, unseen)
    verdict["corpus_size"] = len(docs)
    if not docs:
        verdict["disposition"] = "new"
        verdict["reason"] = ("new: the corpus holds no comparable skill, so there is nothing "
                             "to duplicate (similarity was not computable)")
        verdict["materiality"] = ("costs ~%d tokens per load (%d bytes); nothing in the corpus "
                                  "overlaps it" % (verdict["context_tokens"], verdict["bytes"]))
    return verdict


def verdict_for_support_file(name, text, roots, exclude=()):
    """The judgement for a NEW `references/<topic>.md` under ``name``'s neighbours.

    A support file that restates an ENTIRE other skill is misplaced knowledge, not depth: it
    belongs in that skill. Only the fold line applies here (a support file has no standalone
    description to duplicate and cannot be a "spread restatement" of the whole corpus).
    """
    docs, idf, unseen = load(roots, exclude=exclude)
    draft = draft_from_text(name, text)
    draft["allow_empty_description"] = True
    verdict = judge(draft, docs, idf, unseen)
    verdict["corpus_size"] = len(docs)
    return verdict


def summary(verdict):
    """One line: what was decided."""
    if verdict.get("disposition") == "fold":
        return "admission: fold into %s" % verdict.get("fold_target")
    return "admission: %s" % (verdict.get("disposition") or "?")


def top_matches(verdict, limit=ADMISSION_MATCHES):
    """The verdict's matches as `name (tier) at N% intent / M% description` lines."""
    return ["`%s` (%s) at %d%% intent / %d%% description" % (
        m["name"], m["tier"], round(m["coverage"] * 100), round(m["description_overlap"] * 100))
        for m in (verdict.get("matches") or [])[:limit]]


def named_skill(verdict):
    """The existing skill a refusal points the author at: ``(name, path)``, or ``("", "")``.

    A refusal names a skill on TWO dispositions and the message says so in both: ``fold``
    refuses INTO one specific skill, and ``reject`` refuses because no single skill owns the
    content and names the nearest one as where the rule belongs. The prose is not the only
    reader of that fact — the structured refusal carries the same target in ``fold_target`` /
    ``fold_path``, and a consumer that acts on the field instead of parsing a sentence must be
    told the same skill. Resolving it in ONE place is what keeps the two from drifting apart:
    `refusal_message` and every admission payload are built from this function.
    """
    disposition = str(verdict.get("disposition") or "")
    if disposition == "fold" and verdict.get("fold_target"):
        return str(verdict["fold_target"]), str(verdict.get("fold_path") or "")
    if disposition == "reject":
        nearest = next((m for m in (verdict.get("matches") or []) if m.get("name")), None)
        if nearest:
            return str(nearest["name"]), str(nearest.get("path") or "")
    return "", ""


def refusal_message(verdict, references_hint="", subject=""):
    """The author-facing refusal: WHAT collided, WHERE the knowledge goes, and WHY.

    Actionable, not a wall: the agent is told the existing skill by name, its path, the
    `references/` file to extend instead, and the rule it hit — so the next attempt is a
    patch, not another create. Deterministic text; no model judgement in the loop.
    """
    name = str(verdict.get("name") or "this skill")
    subject = subject or ("'%s'" % name)
    disposition = str(verdict.get("disposition") or "")
    if disposition == "fold":
        target, target_path = named_skill(verdict)
        fold_match = next((m for m in verdict.get("matches") or [] if m["name"] == target),
                          (verdict.get("matches") or [{}])[0])
        lines = [
            "Refused: %s restates an existing skill — fold it in instead of creating a new one."
            % subject,
            "Existing skill: %s (%s) already states %d%% of this draft's intent surface."
            % (target, fold_match.get("tier", "unknown"),
               round(float(fold_match.get("coverage") or 0.0) * 100)),
            "Where the knowledge goes: %s" % (target_path or target),
        ]
        if references_hint:
            lines.append("Extend it as: %s" % references_hint)
        lines += [
            "Nearest skills: %s" % "; ".join(top_matches(verdict)),
            "Rule: a draft whose intent surface is >=%d%% covered by one existing skill (or whose "
            "description overlaps it >=%d%%) is a restatement of it and folds into it."
            % (round(ADMISSION_FOLD_COVERAGE * 100), round(ADMISSION_DESCRIPTION_DUPLICATE * 100)),
            "Next step: skill_view('%s') then skill_manage(action='patch', name='%s', ...) — or "
            "skill_manage(action='write_file', name='%s', file_path='references/<topic>.md', ...) "
            "for topical depth." % (target, target, target),
        ]
        return "\n".join(lines)
    if disposition == "reject":
        target, target_path = named_skill(verdict)
        nearest = next((m for m in verdict.get("matches") or [] if m.get("name") == target),
                       (verdict.get("matches") or [{}])[0])
        lines = [
            "Refused: '%s' adds nothing to the corpus — %s." % (name, verdict.get("reason") or ""),
            "Nearest skill: %s (%s) at %s" % (
                target or "-", nearest.get("tier", "unknown"),
                target_path or "-"),
            "Rule: a draft with under %d%% new intent and no single skill that owns its content "
            "is a spread restatement, and a stub below %d bytes is not a skill."
            % (round(ADMISSION_MIN_NOVELTY * 100), ADMISSION_STUB_BYTES),
            "Next step: put the rule in the skill that governs the task — skill_view + "
            "skill_manage(action='patch'); if it is genuinely a new class, say what is new that "
            "no existing skill states.",
        ]
        return "\n".join(lines)
    return ""
