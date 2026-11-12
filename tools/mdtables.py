"""Align Markdown tables (outside code fences) so they read well as plain text.

Usage: python3 tools/mdtables.py FILE.md...  (rewrites the files in place)"""
import re
import sys
import unicodedata


def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c) else 1 for c in s)


def cells(line):
    parts = re.split(r"(?<!\\)\|", line.strip())
    return [p.strip() for p in parts[1:-1]]


def fmt(rows):
    rows = [cells(r) for r in rows]
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    sep = [i for i, r in enumerate(rows) if all(re.fullmatch(r":?-+:?", c) for c in r if c) and any(r)]
    w = [max([width(r[i]) for j, r in enumerate(rows) if j not in sep] + [3]) for i in range(n)]
    out = []
    for j, r in enumerate(rows):
        if j in sep:
            cs = []
            for i, c in enumerate(r):
                l, rt = c.startswith(":"), c.endswith(":")
                cs.append((":" if l else "-") + "-" * (w[i] - 2) + (":" if rt else "-"))
            out.append("| " + " | ".join(cs) + " |")
        else:
            out.append("| " + " | ".join(c + " " * (w[i] - width(c)) for i, c in enumerate(r)) + " |")
    return out


for path in sys.argv[1:]:
    lines = open(path).read().split("\n")
    out, block, fence = [], [], False
    for line in lines + [""]:
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and line.startswith("|"):
            block.append(line)
            continue
        if block:
            out += fmt(block)
            block = []
        out.append(line)
    new = "\n".join(out[:-1])
    if new != "\n".join(lines):
        open(path, "w").write(new)
        print("formatted", path)
