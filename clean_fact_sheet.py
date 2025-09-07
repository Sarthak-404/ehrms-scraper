"""
Utility to repair eHRMS fact-sheet output where anchors like "1. Name" leak into the next row.
No CSV files are created; output is printed to stdout or returned as a DataFrame.
"""

from __future__ import annotations
import re, json, sys
from typing import List, Tuple
import pandas as pd


CANONICAL = {
 1: "Name",
 2: "eHRMS Code",
 3: "Father's Name",
 4: "Employee Type",
 5: "Date of Birth",
 6: "PH/DFF/ExSer",
 7: "Home District",
 8: "Seniority No.",
 9: "Cadre",
 10: "Level in Cadre",
 11: "Gender",
 12: "Appointment Date",
 13: "Service Start Date",
 14: "Confirmation Date",
 15: "Spouse eHRMS Code",
 16: "eSalary Code",
 17: "Class",
 18: "Health Status",
 19: "Date of Retirement",
 20: "Salary Office",
 21: "Current Status",
 22: "Posting Department/Directorate",
 23: "Present Posting Details",
 24: "Qualification with Specialization",
 25: "Past Posting Details",
 26: "Professional Training Completed",
 27: "Departmental Enquiry/Proceedings (if any)",
}


def _anchors(text: str):
    return list(re.finditer(r'(?P<num>\d{1,2})\.\s+(?P<label>[^0-9][^0-9]*?)\s', text))


def _collapse_ws(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()


def parse_malformed_blocks(raw_mapping_like: str) -> pd.DataFrame:
    """
    Accepts a JSON-like string that contains broken key/value pairs like:
      "1. Name": "MANOJ KUMAR 2. eHRMS Code", ...
    and rebuilds a clean table with columns: No., Field, Value.
    """
    try:
        from collections import OrderedDict
        data = json.loads(raw_mapping_like, object_pairs_hook=dict)
        parts: List[str] = []
        for k, v in data.items():
            parts.append(str(k))
            parts.append(str(v))
        joined = " ".join(parts)
    except Exception:
        joined = str(raw_mapping_like)

    joined = _collapse_ws(joined)

    all_a = _anchors(joined)
    if not all_a:
        return pd.DataFrame([[None, None, joined]], columns=["No.", "Field", "Value"])

    # Group contiguous runs with the same number
    runs = []
    i = 0
    while i < len(all_a):
        j = i
        while j + 1 < len(all_a) and all_a[j+1].group('num') == all_a[i].group('num'):
            j += 1
        runs.append((i, j))
        i = j + 1

    rows: List[Tuple[int, str, str]] = []
    for ridx, (si, ei) in enumerate(runs):
        last_a  = all_a[ei]
        num     = int(last_a.group('num'))
        label   = _collapse_ws(last_a.group('label'))
        start_val = last_a.end()
        end_val = all_a[runs[ridx+1][0]].start() if ridx + 1 < len(runs) else len(joined)
        value = _collapse_ws(joined[start_val:end_val])
        rows.append((num, label, value))

    def strip_label_prefix(val: str, label: str) -> str:
        if not val:
            return val
        v = _collapse_ws(val)
        lbl = _collapse_ws(label)
        changed = True
        lbl_tokens = set(lbl.split())
        while changed and v:
            changed = False
            if v.startswith(lbl + " "):
                v = v[len(lbl)+1:].lstrip()
                changed = True
            toks = v.split()
            if toks and toks[0] in lbl_tokens:
                v = " ".join(toks[1:])
                changed = True
        return v.strip()

    rows.sort(key=lambda x: x[0])
    clean = []
    for num, lbl, val in rows:
        canon = CANONICAL.get(num, lbl)
        val = strip_label_prefix(val, canon)
        clean.append((num, canon, val))

    return pd.DataFrame(clean, columns=["No.", "Field", "Value"])


def to_pretty_text(df: pd.DataFrame) -> str:
    lines = []
    for _, r in df.iterrows():
        n, f, v = int(r["No."]), str(r["Field"]), str(r["Value"])
        if len(v) > 800:
            v = v[:800].rstrip() + "…"
        lines.append(f"{n}. {f} — {v}")
    return "\n".join(lines)


def to_clean_json(df: pd.DataFrame) -> str:
    m = {int(r["No."]): {"Field": str(r["Field"]), "Value": str(r["Value"])} for _, r in df.iterrows()}
    return json.dumps(m, ensure_ascii=False, indent=2)


def main(argv: List[str]) -> int:
    import pathlib
    as_json = False
    paths = []
    for a in argv:
        if a == "--json":
            as_json = True
        else:
            paths.append(a)

    if paths:
        text = pathlib.Path(paths[0]).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()

    df = parse_malformed_blocks(text)

    if as_json:
        print(to_clean_json(df))
    else:
        print(to_pretty_text(df))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
