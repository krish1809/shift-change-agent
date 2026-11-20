"""Fill the evaluation report with the numbers from eval/results.*.jsonl and compile it to PDF.

python -m scripts.build_report        # -> docs/EVAL_REPORT.pdf   (needs pdflatex)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEX = ROOT / "docs" / "report" / "eval_report.tex"
OUT = ROOT / "docs" / "EVAL_REPORT.pdf"
LABELS = {
    "isolation": "Cross-company isolation", "cutoff": "Cut-off rejection",
    "resume_after_restart": "Resume after restart",
    "tool_selection": "Tool selection", "tool_failure": "Tool failure", "clarification": "Clarification",
    "capacity": "Capacity \\& policy", "concurrency_bonus": "Concurrency (bonus)", "input_safety": "Input safety",
}  # fmt: skip


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (ROOT / "eval" / name).read_text().splitlines() if line.strip()]


def main() -> None:
    cases = {c["id"]: c["category"] for c in load("cases.jsonl")}
    fake, live = load("results.fake.jsonl"), load("results.live.jsonl")
    tally: dict[str, dict[str, list[bool]]] = defaultdict(lambda: {"fake": [], "live": []})
    for mode, rows in (("fake", fake), ("live", live)):
        for r in rows:
            tally[cases[r["id"]]][mode].append(r["pass"])
    rows = []
    for cat in LABELS:
        f, lv = tally[cat]["fake"], tally[cat]["live"]
        live_cell = f"{sum(lv)} / {len(lv)}" if lv else "--"
        rows.append(f"{LABELS[cat]} & {sum(f)} / {len(f)} & {live_cell} \\\\")

    latencies = sorted(v for r in live for v in r["actual"]["latency_ms"])
    pct = lambda q: latencies[min(len(latencies) - 1, int(q * len(latencies)))] / 1000  # noqa: E731
    failed = [r["id"] for r in live if not r["pass"]]
    fail_note = (
        f"In the live run reported above, {len(failed)} case(s) failed: {', '.join(failed)}; the case notes in "
        "\\texttt{eval/results.live.jsonl} record each failed check."
        if failed
        else "In the live run reported above every live-eligible case passed."
    )
    tex = TEX.read_text()
    for key, value in {
        "LIVEPASS": str(sum(r["pass"] for r in live)), "LIVETOTAL": str(len(live)),
        "LIVEP50": f"{pct(0.5):.1f}", "LIVEP95": f"{pct(0.95):.1f}", "LIVEMAX": f"{latencies[-1] / 1000:.1f}",
        "LIVETURNS": str(len(latencies)), "LIVEDATE": "measured on the author's machine",
        "LIVEROWS": "\n".join(rows), "LIVEFAILNOTE": fail_note,
    }.items():  # fmt: skip
        tex = tex.replace(key, value)

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "eval_report.tex").write_text(tex)
        for _ in range(2):  # twice so references settle
            subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "eval_report.tex"],
                cwd=tmp,
                check=True,
                capture_output=True,
            )
        shutil.copy(Path(tmp) / "eval_report.pdf", OUT)
    print(f"wrote {OUT.relative_to(ROOT)}  (live {sum(r['pass'] for r in live)}/{len(live)}, p95 {pct(0.95):.1f}s)")


if __name__ == "__main__":
    main()
