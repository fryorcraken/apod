"""apod broker ui: a terminal UI over the broker's admin socket."""
from core import BrokerError, client, fmt_scope, fmt_target, fmt_time, now

TUI_HELP = {
    "requests": "↑↓ select  a allow once  s for session  p for persona  d deny  Tab view  q quit",
    "sessions": "↑↓ select  x remove scope entry or grant  Tab view  q quit",
    "grants": "↑↓ select  x remove grant  Tab view  q quit",
}
TUI_VIEWS = ["requests", "sessions", "grants"]


def tui_rows(view, st):
    """Rows of a view: (key, text, style, item); key None for headings (not selectable)."""
    rows = []
    if st is None:
        return rows
    names = {x["id"]: x.get("name") for x in st["sessions"]}
    who = lambda e: (names.get(e["session"]) or e.get("name") or e["profile"])[:24]
    if view == "requests":
        pend = st["pending"]
        rows.append((None, f"Waiting for approval ({len(pend)})", "head", None))
        for e in pend:
            left = max(0, int(e["ts"] + st["ask_timeout"] - now()))
            rows.append((("ev", e["id"]), f"{e['id']:>5} {left:>3}s  {who(e):<24} {e['request']}", "pending", e))
        if not pend:
            rows.append((None, "      nothing waiting", "dim", None))
        rows.append((None, "", "dim", None))
        rows.append((None, "Recent requests", "head", None))
        for e in reversed([e for e in st["events"] if e["decision"] != "pending"]):
            rows.append((("ev", e["id"]), f"{e['id']:>5} {fmt_time(e['ts'])[6:]}  {who(e):<24} "
                         f"{e['decision']:<17} {e['request']}", tui_style(e["decision"]), e))
    elif view == "sessions":
        for s in st["sessions"]:
            login = (s["cfg"].get("gh") or {}).get("login")
            rows.append((("s", s["id"]), (f"{s['name']}  ·  " if s.get("name") else "") + f"{s['profile']}  ·  {s['persona']}" + (f" as {login}" if login else "")
                         + f"  ·  {s['project']}  ·  {s['id']}", "head", s))
            entries = [(k, fmt_scope(k, v), v) for k, vs in s["scope"].items() for v in vs] + \
                [("granted", v, v) for v in s["grants"]["actions"] + s["grants"]["unscoped"]]
            for kind, label, value in entries:
                rows.append((("e", s["id"], kind, value), f"    {'granted' if kind == 'granted' else 'scope':<8} {label}",
                             "grant" if kind == "granted" else "", {"session": s["id"], "kind": kind, "value": value}))
            if not entries:
                rows.append((None, "    no scope", "dim", None))
            rows.append((None, "", "dim", None))
        if not st["sessions"]:
            rows.append((None, "No sessions (pods that use the broker show up here).", "dim", None))
    else:
        for p, lst in st["grants"].items():
            for a in lst:
                rows.append((("g", p, a), f"{p:<12} {a}", "", {"persona": p, "action": a}))
        if not rows:
            rows.append((None, "No persona grants (\"allow for persona\" adds them here, on top of personas.toml).", "dim", None))
    return rows


def tui_style(decision):
    if decision.startswith(("allowed", "approved")):
        return "ok"
    if decision == "pending":
        return "pending"
    return "bad"


def tui_detail(e, st, width):
    import textwrap
    if e is None:
        return []
    name = next((x.get("name") for x in st["sessions"] if x["id"] == e["session"]), None) or e.get("name")
    lines = [f"#{e['id']}  {e['request']}",
             (f"{name} · " if name else "") + f"{e['profile']} · persona {e['persona']}" + (f" as {e['login']}" if e.get("login") else "") + f" · {e['project']}",
             f"actions: {', '.join(e['actions']) or '-'}",
             f"targets: {', '.join(fmt_target(tuple(t)) for t in e['targets']) or '-'}"]
    if e.get("reason"):
        lines.append(f"why: {e['reason']}")
    if e["decision"] == "pending":
        left = max(0, int(e["ts"] + st["ask_timeout"] - now()))
        lines.append(f"waiting, {left}s left: a allow once · s allow for this session · p allow for persona {e['persona']} · d deny")
    else:
        d = e["decision"] + (f", exit {e['exit']}" if e.get("exit") not in (None, 0) else "")
        if d.startswith(("rejected", "timed out", "denied by")) and (e["missing"] or e["out"]):
            d += " · s grant for this session · p grant for the persona (the agent can then retry)"
        lines.append(d)
    out = []
    for line in lines:
        out += textwrap.wrap(line, max(20, width - 2), subsequent_indent="  ") or [""]
    return out


def run(scr):
    import curses
    curses.curs_set(0)
    scr.timeout(1000)
    styles = {"": curses.A_NORMAL, "dim": curses.A_DIM, "head": curses.A_BOLD,
              "ok": curses.A_NORMAL, "bad": curses.A_NORMAL, "pending": curses.A_BOLD, "grant": curses.A_NORMAL}
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_RED, curses.COLOR_YELLOW, curses.COLOR_CYAN), 1):
            curses.init_pair(i, c, bg)
        styles.update(ok=curses.color_pair(1), bad=curses.color_pair(2),
                      pending=curses.color_pair(3) | curses.A_BOLD, grant=curses.color_pair(4),
                      head=curses.color_pair(4) | curses.A_BOLD)
    view, sel, top, msg, msg_t, seen, st, err = "requests", None, 0, "", 0, None, None, None

    def put(y, x, text, attr=0):
        h, w = scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                scr.addnstr(y, x, text, w - x - (1 if y == h - 1 else 0), attr)
            except curses.error:
                pass

    while True:
        try:
            st, err = client("/state", {}, timeout=5), None
        except (OSError, BrokerError, ValueError) as e:
            err = f"broker not reachable: {e}"
        pend_ids = {e["id"] for e in st["pending"]} if st else set()
        if seen is not None and pend_ids - seen:
            curses.beep()
            if view == "requests":
                sel = ("ev", min(pend_ids - seen))
        seen = pend_ids
        rows = tui_rows(view, st)
        keys = [r[0] for r in rows if r[0] is not None]
        if sel not in keys:
            sel = keys[0] if keys else None
        idx = next((i for i, r in enumerate(rows) if r[0] == sel), 0)
        item = rows[idx][3] if rows and rows[idx][0] is not None else None

        scr.erase()
        h, w = scr.getmaxyx()
        title = " apod broker "
        if st:
            title += f"· {len(st['sessions'])} session(s) · {len(st['pending'])} waiting · requests wait {st['ask_timeout']}s "
        put(0, 0, title.ljust(w), curses.A_REVERSE)
        x = 1
        for i, v in enumerate(TUI_VIEWS):
            label = f" {i + 1} {v.capitalize()} "
            put(1, x, label, curses.A_REVERSE if v == view else curses.A_DIM)
            x += len(label) + 1
        detail = tui_detail(item, st, w) if view == "requests" and item else []
        list_h = max(3, h - 4 - (len(detail) + 1 if detail else 0))
        if idx < top:
            top = idx
        elif idx >= top + list_h:
            top = idx - list_h + 1
        top = max(0, min(top, max(0, len(rows) - list_h)))
        for i, (key, text, style, _) in enumerate(rows[top:top + list_h]):
            attr = styles.get(style, 0)
            if key is not None and key == sel:
                attr |= curses.A_REVERSE
                text = text.ljust(w - 2)
            put(3 + i, 1, text, attr)
        if detail:
            y = h - 2 - len(detail)
            put(y - 1, 0, "─" * w, curses.A_DIM)
            for i, line in enumerate(detail):
                put(y + i, 1, line, curses.A_BOLD if i == 0 else 0)
        if err:
            foot, fattr = err, styles["bad"]
        elif msg and now() - msg_t < 6:
            foot, fattr = msg, styles["grant"]
        else:
            foot, fattr = TUI_HELP[view], curses.A_DIM
        put(h - 1, 0, " " + foot, fattr)
        scr.refresh()

        k = scr.getch()
        if k == -1:
            continue
        ch = chr(k) if 0 < k < 256 else ""
        try:
            if ch in ("q", "\x1b"):
                return
            if ch == "\t" or k == curses.KEY_BTAB:
                view = TUI_VIEWS[(TUI_VIEWS.index(view) + (1 if ch == "\t" else -1)) % len(TUI_VIEWS)]
                sel, top = None, 0
            elif ch in ("1", "2", "3"):
                view, sel, top = TUI_VIEWS[int(ch) - 1], None, 0
            elif k in (curses.KEY_DOWN, ord("j"), curses.KEY_UP, ord("k"), curses.KEY_NPAGE, curses.KEY_PPAGE,
                       curses.KEY_HOME, curses.KEY_END, ord("g"), ord("G")) and keys:
                i = keys.index(sel) if sel in keys else 0
                step = {curses.KEY_DOWN: 1, ord("j"): 1, curses.KEY_UP: -1, ord("k"): -1,
                        curses.KEY_NPAGE: list_h, curses.KEY_PPAGE: -list_h}.get(k)
                i = 0 if k in (curses.KEY_HOME, ord("g")) else len(keys) - 1 if k in (curses.KEY_END, ord("G")) else i + step
                sel = keys[max(0, min(len(keys) - 1, i))]
            elif view == "requests" and item and ch and ch in "aspd":
                how = {"a": "once", "s": "session", "p": "persona", "d": "deny"}[ch]
                ev = client("/decide", {"id": item["id"], "how": how})
                msg = f"{'denied' if how == 'deny' else 'allowed ' + how}: {ev['request']}"
                msg_t = now()
            elif view == "sessions" and item and ch == "x" and "value" in item:
                client("/scope/remove", item)
                msg, msg_t = f"removed {item['value']} from session {item['session']}", now()
            elif view == "grants" and item and ch == "x":
                client("/grants/remove", item)
                msg, msg_t = f"removed {item['action']} from persona {item['persona']}", now()
        except (OSError, BrokerError, ValueError) as e:
            msg, msg_t = str(e), now()


