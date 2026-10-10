"""The Contractor Co. proposal, rendered from a layout, the phrase library and ledger rows.

Shared by the golden fixtures (tools/build_fixture.py renders each fixture's
hand-made layout) and the Scope Writer (pipeline/scope_writer.py renders the
layout a model selected). Rendering is code: identical ledgers and layouts give
identical proposals (architecture p.10, "Rendered, not written"). Every
paragraph is a library phrase, a ledger row's own statement, or a fixed heading,
and `xref` lists the claim and phrase IDs each paragraph draws on (p.6,
"prove-your-work output").

A row is a dict in the fixture's shape (`id`, `source`, `statement`, ...);
`row_of` turns a ledger Claim into one.
"""
from __future__ import annotations

import csv
import io


def fmt_value(v):
    if v is None:
        return "FIELD"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    # a range is printed as the ledger holds it: a figure shortened here would be one the ledger does not hold (p.5)
    return f"{v:,}" if isinstance(v, int) else str(v)


def tag(r):
    t = r.get("tag") or f"{r['source']} {r.get('locator', '')}".strip()
    return t


def marks(r):
    out = []
    if r.get("method") == "scaled":
        out.append("scaled, FIELD verify")
    if r.get("flag"):
        out.append(r["flag"])
    if r.get("question"):
        out.append(f"see {r['question']}")
    return f" [{'; '.join(out)}]" if out else ""


def row_text(r):
    return f"{r['statement']} ({tag(r)}){marks(r)}"


def render(data, by_id, phrases, register):
    p = data["proposal"]
    out, xref = [], []
    para = 0

    def emit(text, claims=(), phr=(), section=""):
        nonlocal para
        out.append(text)
        if claims or phr:
            para += 1
            xref.append({"para": para, "section": section, "claims": " ".join(claims),
                         "phrases": " ".join(phr), "text": text.strip()[:120]})

    def ph(key, **slots):
        return phrases[key]["text"].format(**slots)

    h = p["header"]
    # A header slot left empty means the ledger has no row for it: the proposal
    # says FIELD there rather than leaving a gap or inventing a name.
    emit(f"# {by_id[h['project']]['statement'] if h['project'] else 'FIELD: project name not in the ledger'}\n")
    emit(f"_{data['status_line']}_\n")
    emit(ph("license_line") + "\n", phr=["license_line"], section="header")
    if h["address"]:
        emit(f"Job Address:  \n{by_id[h['address']]['statement']} ({tag(by_id[h['address']])})\n",
             claims=[h["address"]], section="header")
    else:
        emit("Job Address:  \nFIELD: job address not in the ledger\n")
    emit("  \n".join(ph("contractor_block").split("\n")) + "\n", phr=["contractor_block"], section="header")
    if h["client"]:
        client = by_id[h["client"]]
        who = "FIELD" if client["method"] == "FIELD" else client["statement"]
        emit(ph("greeting", client=who) + f" [client: see {client['id']}]" * (client["method"] == "FIELD") + "\n",
             claims=[h["client"]], phr=["greeting"], section="header")
    else:
        emit(ph("greeting", client="FIELD") + " [client: not in the ledger]\n", phr=["greeting"], section="header")
    emit("**Scope of Work:**\n")

    for sec in p["sections"]:
        emit(f"## {sec['title']}\n")
        if sec.get("intro_phrase"):
            emit(ph(sec["intro_phrase"]) + f" (PFE {phrases[sec['intro_phrase']]['loc']})\n",
                 phr=[sec["intro_phrase"]], section=sec["title"])
        for task in sec.get("tasks", []):
            if task.get("title"):
                emit(f"### {task['title']}\n")
            for i, it in enumerate(task.get("items", []), 1):
                bullet = f"{i}." if sec.get("numbered") else "-"
                if "row" in it:
                    r = by_id[it["row"]]
                    emit(f"{bullet} {row_text(r)}", claims=[r["id"]], section=task.get("title", sec["title"]))
                else:
                    emit(f"{bullet} {ph(it['phrase'])} (PFE {phrases[it['phrase']]['loc']})", phr=[it["phrase"]], section=task.get("title", sec["title"]))
            out.append("")
            if task.get("allowance"):
                parts, claims = [], []
                for a in task["allowance"]:
                    r = by_id[a]
                    claims.append(a)
                    text = ph("allowance", qty=fmt_value(r.get("value")), unit=r.get("unit", ""),
                              what=r["statement"]).replace("  ", " ").rstrip(".")
                    parts.append(f"{text} ({tag(r)}){marks(r)}.")
                tail = (" " + ph(task["close"])) if task.get("close") else ""
                emit(f"_({' '.join(parts)}{tail})_\n", claims=claims,
                     phr=["allowance"] + ([task["close"]] if task.get("close") else []),
                     section=task.get("title", sec["title"]))

    emit("## Exclusions\n")
    for key in p.get("exclusion_phrases", []):
        emit(f"- {ph(key)} (PFE {phrases[key]['loc']})", phr=[key], section="Exclusions")
    for r in data["rows"]:
        if r["role"] == "exclusion":
            emit(f"- {row_text(r)}", claims=[r["id"]], section="Exclusions")
    out.append("")

    emit("## Open questions for the estimator before pricing\n")
    emit("Nothing below is resolved in this bid. Where two readings exist both are shown and the figure stays unverified.\n")
    for r in data["rows"]:
        if r["role"] == "question":
            emit(f"- **{r['id']}** {row_text(r)}", claims=[r["id"]], section="Open questions")
    out.append("")

    emit("## Field takeoff (FIELD rows)\n")
    emit("Not in any source document. Each must be measured or answered before the allowance is priced.\n")
    for r in data["rows"]:
        if r["method"] == "FIELD" and r["role"] in ("quantity", "header"):
            emit(f"- **{r['id']}** {r['statement']} ({tag(r)})", claims=[r["id"]], section="Field takeoff")
    out.append("")

    mats = [r for r in data["rows"] if r["role"] == "material"]
    if mats:
        emit("## Materials (no pricing)\n")
        emit("Order quantities are printed only where the ledger has a dimensioned or counted value, or a takeoff figure and a stated rate (the spec's own rate over the data sheet's, replayed at both ends of a range); spares, pack sizes and stock-length rounding are Contractor Co. purchasing decisions and are not in the sources.\n")
        emit("| Claim | Material | Qty | Unit | Source | Method |\n|---|---|---|---|---|---|")
        for r in mats:
            emit(f"| {r['id']} | {r['statement']}{marks(r)} | {fmt_value(r.get('value'))} | {r.get('unit', '')} | {tag(r)} | {r['method']} |",
                 claims=[r["id"]], section="Materials")
        out.append("")

    codes = [r for r in data["rows"] if r["role"] == "code"]
    if codes:
        emit("## Codes, permits and standards\n")
        emit("Every row was fetched on the date shown. Rows marked unverified did not open or did not show the quoted text and must not be relied on until re-fetched. "
             + data.get("codes_note", "") + "\n")
        emit("| Claim | What applies | Evidence | Source |\n|---|---|---|---|")
        for r in codes:
            q = (r.get("quote") or "").replace("|", "\\|").replace("\n", " ")
            src = f"[{r.get('tag') or 'link'}]({r['url']}) ({r['retrieved']})" if r.get("url") else tag(r)
            emit(f"| {r['id']} | {r['statement']}{marks(r)} | {('“' + q + '”') if q else '—'} | {src} |",
                 claims=[r["id"]], section="Codes")
        out.append("")

    emit("## Terms\n")
    for key in p.get("terms", []):
        emit(f"{ph(key)} (PFE {phrases[key]['loc']})\n", phr=[key], section="Terms")

    emit("---\n")
    emit("_Source tags_  ")
    for sid, s in register.items():
        emit(f"_{sid}_ = {s['title']}{'' if s['status'] == 'present' else ' — **' + s['status'] + '**'}  ")
    out.append("")

    return "\n".join(out).rstrip() + "\n", xref


def xref_csv(xref):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["para", "section", "claims", "phrases", "text"], lineterminator="\n")
    w.writeheader()
    w.writerows(xref)
    return buf.getvalue()


def row_of(c) -> dict:
    """A ledger Claim in the dict shape the renderer reads."""
    value = None
    if c.value != "":
        value = int(c.value_num) if c.value_num is not None and float(c.value_num).is_integer() else c.value
    return {
        "id": c.claim_id, "statement": c.statement, "source": c.source_id, "locator": c.locator,
        "tag": c.tag, "method": c.method, "role": c.role, "flag": c.flag, "question": c.question,
        "value": value, "unit": c.unit, "url": c.url, "retrieved": c.retrieved, "quote": c.quote,
        "division": c.division, "part": c.part,
    }
