"""Project-specific, human-graded model trials. Python 3.10+, standard library."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent
ROLES = ("lead", "reviewer", "specialist")
CHANNELS = ("subscription", "api", "replay", "demo")
OUTCOMES = ("complete", "partial", "failed")
VERDICTS = ("met", "partial", "missed", "unobserved")
DECISIONS = ("try_on_new_work", "use_with_review", "hold", "insufficient_evidence")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dump(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Expected nonempty text: {label}")
    return value


def string_list(value, label):
    if not isinstance(value, list) or not value:
        raise ValueError(f"Expected a nonempty list: {label}")
    return [nonempty(item, label) for item in value]


def freeze(folder):
    folder = folder.resolve()
    case = json.loads((folder / "case.json").read_text(encoding="utf-8"))
    if case["role"] not in ROLES:
        raise ValueError("Unknown role")
    for field in ("name", "conditions"):
        nonempty(case[field], field)
    rubric = case["rubric"]
    if not isinstance(rubric, dict) or not rubric:
        raise ValueError("At least one rubric item is required")
    for name, rule in rubric.items():
        nonempty(name, "rubric identifier")
        nonempty(rule, "rubric criterion")
    stages = case["stages"]
    if not isinstance(stages, list) or not stages:
        raise ValueError("At least one stage is required")

    packets = []
    for stage in stages:
        parts = []
        for filename in string_list(stage, "stage files"):
            rel = Path(filename)
            path = (folder / rel).resolve()
            if rel.is_absolute() or not path.is_relative_to(folder) or path == folder / "case.json":
                raise ValueError(f"Invalid prompt member: {filename}")
            body = path.read_bytes().decode("utf-8")
            nonempty(body, filename)
            parts.append(body if len(stage) == 1 else f"--- {filename} ---\n{body}")
        packets.append("\n\n".join(parts))
    return {k: case[k] for k in ("name", "role", "conditions", "rubric")}, packets


def start(folder, db, model, channel, client, effort):
    spec, packets = freeze(folder)
    fingerprint = digest(dump([spec, packets]).encode())
    if channel not in CHANNELS:
        raise ValueError("Unknown access channel")
    meta = dict(spec, fingerprint=fingerprint, created=now(), model=nonempty(model, "model"),
                channel=channel, client=nonempty(client, "client"), effort=nonempty(effort, "effort"))
    db.parent.mkdir(parents=True, exist_ok=True)
    # Publish an initialized database in one step; an existing run is never replaced.
    temp = db.with_name(f".{db.name}.{uuid.uuid4().hex}.tmp")
    try:
        with closing(sqlite3.connect(temp)) as conn, conn:
            conn.execute("CREATE TABLE run (meta TEXT NOT NULL, grade TEXT)")
            conn.execute("""CREATE TABLE steps (
                number INTEGER PRIMARY KEY, prompt TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'ready',
                answer TEXT, note TEXT, receipt TEXT, started TEXT, ended TEXT)""")
            conn.execute("""CREATE TABLE artifacts (
                step INTEGER, name TEXT, body BLOB NOT NULL, PRIMARY KEY(step, name))""")
            conn.execute("INSERT INTO run VALUES (?, NULL)", (dump(meta),))
            conn.executemany("INSERT INTO steps(number,prompt) VALUES (?,?)", enumerate(packets, 1))
        os.link(temp, db)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def connect(path):
    uri = path.resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            yield conn


def metadata(conn):
    return json.loads(conn.execute("SELECT meta FROM run").fetchone()[0])


def steps(conn):
    return conn.execute("SELECT * FROM steps ORDER BY number").fetchall()


def active(conn):
    for row in steps(conn):
        if row["state"] == "complete":
            continue
        if row["state"] in ("ready", "sending"):
            return row
        break
    raise ValueError("This run has ended")


def finish(conn, number, previous, status, answer, note, receipt, attachments=()):
    if status in ("complete", "partial") and not (answer.strip() or attachments):
        raise ValueError("A delivered or partial result needs an answer or an artifact; use failed for an empty result")
    if status in ("failed", "partial") and not note.strip():
        raise ValueError("Record why the delivery failed or was partial")
    result = conn.execute("""UPDATE steps SET state=?, answer=?, note=?, receipt=?, ended=?
                             WHERE number=? AND state=?""",
                          (status, answer, note, dump(receipt), now(), number, previous))
    if result.rowcount != 1:
        raise ValueError("This stage already has an attempt")
    conn.executemany("INSERT INTO artifacts VALUES (?,?,?)", [(number, name, body) for name, body in attachments])


def manual(args):
    files = [(p.name, p.read_bytes()) for p in args.artifact]
    answer = args.file.read_bytes().decode("utf-8") if args.file else ""
    receipt = json.loads(args.receipt.read_text(encoding="utf-8")) if args.receipt else {}
    if not isinstance(receipt, dict):
        raise ValueError("Receipt must be a JSON object")
    with connect(args.run) as conn:
        conn.execute("BEGIN IMMEDIATE")
        step = active(conn)
        if args.stage != step["number"] or step["state"] != "ready":
            raise ValueError("Stage number does not match the current available prompt")
        if metadata(conn)["channel"] == "api":
            raise ValueError("Use the api command for an API run")
        finish(conn, args.stage, "ready", args.status, answer, args.note, receipt, files)


def api_stage(args):
    import chat_api
    key = chat_api.settings(args.endpoint, args.timeout)
    with connect(args.run) as conn:
        conn.execute("BEGIN IMMEDIATE")
        meta = metadata(conn)
        if meta["channel"] != "api":
            raise ValueError("Use an API run to send a request")
        current = active(conn)
        if current["number"] != args.stage or current["state"] != "ready":
            raise ValueError("Stage unavailable or already dispatched")
        messages = []
        for row in steps(conn):
            messages.append({"role": "user", "content": row["prompt"]})
            if row["number"] == args.stage:
                break
            messages.append({"role": "assistant", "content": row["answer"]})
        conn.execute("UPDATE steps SET state='sending',started=? WHERE number=?", (now(), args.stage))
    # Commit the dispatch before network I/O so concurrent runs cannot send twice.
    outcome, answer, note, receipt = chat_api.complete(
        args.endpoint, key, args.timeout, meta["model"], meta["effort"], messages)
    with connect(args.run) as conn:
        finish(conn, args.stage, "sending", outcome, answer, note, receipt)
    if outcome != "complete":
        raise ValueError(f"Recorded {outcome}: {note}")
    print(f"Recorded stage {args.stage}")


def grade_template(meta):
    return {"decision": "", "reason": "", "human_minutes": None,
            "criteria": {k: {"rating": "", "evidence": ""} for k in meta["rubric"]}}


def blind_pack(conn, destination):
    """Export the observed text and frozen rubric, excluding run metadata and grades."""
    rows = steps(conn)
    submitted = [row for row in rows if row["state"] != "ready"]
    if any(row["state"] == "sending" for row in rows):
        raise ValueError("Finish the in-flight attempt before blind review")
    if not submitted or (rows[-1]["state"] == "ready" and submitted[-1]["state"] not in ("partial", "failed")):
        raise ValueError("Record the remaining stage before blind review")
    if conn.execute("SELECT 1 FROM artifacts LIMIT 1").fetchone():
        raise ValueError("This text-only export cannot include attachments; prepare a reviewed packet")
    if not any((row["answer"] or "").strip() for row in submitted):
        raise ValueError("There is no delivered text to review")
    meta = metadata(conn)
    instructions = ["# 匿名文字评审", "", "按轮次阅读 prompt.txt 和 answer.txt。空答卷表示该轮没有文字交付。",
                    "按冻结判据填写 scorecard.json，每项 evidence 引用答卷行号或原句。", ""]
    for key, rule in meta["rubric"].items():
        instructions.append(f"- **{key}**　{rule}")
    instructions.extend(["", "rating 可选 " + "、".join(VERDICTS) + "。",
                         "decision 可选 " + "、".join(DECISIONS) + "。",
                         "reason 写明适用任务和理由。human_minutes 有实测时填写，其余保留 null。"])
    entries = {"review.md": "\n".join(instructions) + "\n",
               "scorecard.json": dump(grade_template(meta)) + "\n"}
    for row in submitted:
        prefix = "" if len(rows) == 1 else f"stage-{row['number']:02d}/"
        entries[prefix + "prompt.txt"] = row["prompt"]
        entries[prefix + "answer.txt"] = row["answer"] or ""
    # Fixed ZIP metadata keeps identical inputs byte-identical across repeated exports.
    # Publish only a finished archive. An existing destination is never overwritten.
    tmp = tempfile.NamedTemporaryFile(dir=destination.parent, delete=False)
    try:
        with tmp, zipfile.ZipFile(tmp, "w") as packet:
            for name, body in entries.items():
                member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                member.create_system = 3
                member.external_attr = 0o100600 << 16
                packet.writestr(member, body.encode("utf-8"))
        os.link(tmp.name, destination)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def set_grade(conn, values):
    states = [row["state"] for row in steps(conn)]
    if "sending" in states:
        raise ValueError("Finish the in-flight attempt before grading")
    if states[-1] == "ready" and not ({"partial", "failed"} & set(states)):
        raise ValueError("Record the outcome of the remaining stage first")
    meta = metadata(conn)
    if values["decision"] not in DECISIONS or set(values["criteria"]) != set(meta["rubric"]):
        raise ValueError("Invalid decision or changed rubric identifiers")
    nonempty(values["reason"], "decision reason")
    minutes = values["human_minutes"]
    if minutes is not None and (type(minutes) not in (float, int) or not math.isfinite(minutes) or minutes < 0):
        raise ValueError("human_minutes must be a nonnegative number or null")
    for name, item in values["criteria"].items():
        if item["rating"] not in VERDICTS:
            raise ValueError(f"Unknown rating for {name}")
        nonempty(item["evidence"], name)
    if conn.execute("UPDATE run SET grade=? WHERE grade IS NULL", (dump(values),)).rowcount != 1:
        raise ValueError("A grade has already been recorded")


def view(conn):
    meta = metadata(conn)
    grade = json.loads(conn.execute("SELECT grade FROM run").fetchone()[0] or "null")
    return meta, grade


def report(conn):
    meta, grade = view(conn)
    print(f"# {meta['name']}\n\n{meta['role']} · {meta['model']} · {meta['channel']} · {meta['client']} · {meta['effort']}")
    print(f"\n{meta['conditions']}\n\nCase SHA-256: {meta['fingerprint']}")
    stopped = False
    for row in steps(conn):
        label = "not reached" if stopped and row["state"] == "ready" else row["state"]
        print(f"\n## Stage {row['number']} · {label}\n")
        if row["state"] in ("partial", "failed"):
            stopped = True
        if row["ended"]:
            print(f"Recorded {row['ended']}\n")
        if row["note"]:
            print(row["note"], "\n")
        if row["receipt"] and json.loads(row["receipt"]):
            print(dump(json.loads(row["receipt"])), "\n")
        if row["answer"]:
            print(row["answer"])
        for name, body in conn.execute("SELECT name,body FROM artifacts WHERE step=?", (row['number'],)):
            print(f"\nArtifact: {name} · sha256 {digest(body)}")
    if grade:
        print(f"\n## {grade['decision']}\n\n{grade['reason']}\n")
        for k, item in grade["criteria"].items():
            print(f"- {k} ({item['rating']}) — {item['evidence']}")
        if grade["human_minutes"] is not None:
            print(f"\nHuman review: {grade['human_minutes']} min")


def compare(paths):
    rows = []
    for p in paths:
        with connect(p) as conn:
            meta, grade = view(conn)
            rows.append((meta, grade, ", ".join(r["state"] for r in steps(conn))))
    if len({meta["fingerprint"] for meta, _, _ in rows}) != 1:
        raise ValueError("Different case fingerprints; compare matching inputs and rubrics")
    print(f"{rows[0][0]['name']} · {rows[0][0]['fingerprint']}")
    for meta, grade, states in rows:
        print(f"\n{meta['model']} · {meta['client']} · {meta['channel']} · {meta['effort']} | {states}")
        if grade:
            print(grade["decision"], "—", grade["reason"])
            minutes = grade["human_minutes"]
            print("Human review:", "unmeasured" if minutes is None else f"{minutes} min")
            for name, item in grade["criteria"].items():
                print(f"  {name}: {item['rating']} — {item['evidence']}")
        else:
            print("Ungraded")


def demo(db):
    folder = ROOT / "examples/reviewer"
    start(folder, db, "illustrative answer", "demo", "offline fixture", "n/a")
    with connect(db) as conn:
        finish(conn, 1, "ready", "complete", (folder / "demo-answer.md").read_text(encoding="utf-8"),
               "Synthetic walkthrough", {})
        set_grade(conn, json.loads((folder / "demo-grade.json").read_text(encoding="utf-8")))
        report(conn)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("start", help="freeze a private run")
    p.add_argument("case", type=Path)
    p.add_argument("run", type=Path)
    p.add_argument("--model", required=True)
    p.add_argument("--channel", choices=("subscription", "api", "replay"), required=True)
    p.add_argument("--client", required=True, help="actual UI or API client/version")
    p.add_argument("--effort", default="unspecified")
    for action in ("prompt", "record", "rubric", "grade", "report", "demo", "api", "interrupt", "export", "blind-pack"):
        p = sub.add_parser(action)
        p.add_argument("run", type=Path)
        if action in ("record", "api", "interrupt", "export"):
            p.add_argument("--stage", type=int, required=True)
        if action == "record":
            p.add_argument("--status", choices=OUTCOMES, required=True)
            p.add_argument("--file", type=Path)
            p.add_argument("--note", default="")
            p.add_argument("--artifact", type=Path, action="append", default=[])
            p.add_argument("--receipt", type=Path, help="optional JSON metadata for this delivery")
        elif action == "grade":
            p.add_argument("scorecard", type=Path)
        elif action == "rubric":
            p.add_argument("--template", action="store_true")
        elif action == "api":
            p.add_argument("--endpoint", required=True, help="complete HTTPS chat completions URL")
            p.add_argument("--timeout", type=int, default=180)
        elif action == "interrupt":
            p.add_argument("--note", required=True)
        elif action == "export":
            p.add_argument("name")
            p.add_argument("destination", type=Path)
        elif action == "blind-pack":
            p.add_argument("destination", type=Path, help="new ZIP for a text-only review")
    p = sub.add_parser("compare")
    p.add_argument("runs", type=Path, nargs="+")
    args = parser.parse_args()
    if args.action == "start":
        return start(args.case, args.run, args.model, args.channel, args.client, args.effort)
    if args.action == "demo":
        return demo(args.run)
    if args.action == "compare":
        return compare(args.runs)
    if args.action == "record":
        return manual(args)
    if args.action == "api":
        return api_stage(args)
    with connect(args.run) as conn:
        if args.action == "prompt":
            sys.stdout.write(active(conn)["prompt"])
        elif args.action == "rubric":
            meta = metadata(conn)
            print(dump(grade_template(meta) if args.template else meta["rubric"]))
        elif args.action == "grade":
            set_grade(conn, json.loads(args.scorecard.read_text(encoding="utf-8")))
        elif args.action == "blind-pack":
            blind_pack(conn, args.destination)
        elif args.action == "interrupt":
            finish(conn, args.stage, "sending", "failed", "", args.note, {"cancelled_locally": True})
        elif args.action == "export":
            row = conn.execute("SELECT body FROM artifacts WHERE step=? AND name=?", (args.stage, args.name)).fetchone()
            if not row:
                raise ValueError("Artifact does not exist")
            with args.destination.open("xb") as f:
                f.write(row[0])
        else:
            report(conn)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        sys.exit(f"Error: {exc}")
