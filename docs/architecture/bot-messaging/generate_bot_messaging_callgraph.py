#!/usr/bin/env python3
"""Generate the bot-to-bot messaging call-graph artifact.

Self-contained styled HTML. Every component is grounded at a named revision
(file:line, a live probe, or a port that answers); nothing exists in prose only.

Grounding:
  fork      yoyodine-industries/hermes-agent
  live      main @ 51aee02d1b          (probed 2026-09-17, gateway PID 9591)
  unmerged  feat/bot-message-api @ 8ac9a2d3a93b8d4e31355e1256362aece60c46c5
            PR #21 OPEN (mergedAt null, base main)

Live probes (gateway port 8644, 2026-09-17):
  POST /v1/messages          -> 404  (route NOT registered on live main)
  GET  /v1/messages          -> 404  (route NOT registered on live main)
  POST /api/sessions/foo/chat-> 401  (route REGISTERED, unauthenticated)
  POST /health               -> 405  (route REGISTERED, wrong method)
  GET  /health               -> 200  (gateway alive)
"""

import html
import sys

# ---------------------------------------------------------------------------
# Source-of-truth data (all values verified against the fork working tree)
# ---------------------------------------------------------------------------

# Provenance buckets
STOCK = "STOCK"
FORK_PATCHED = "FORK-PATCHED"
FORK_ONLY = "FORK-ONLY"
SPECIFIED_ONLY = "SPECIFIED-ONLY"

# State buckets
LIVE = "LIVE"
UNMERGED = "UNMERGED"
REJECTED = "REJECTED"

# (x, y, w, h) -> viewBox 0 0 1240 720
NODES = [
    # -- send origins (left) --
    dict(id="msg_agent", x=30, y=40, w=300, h=92,
         title="message_agent tool",
         lines=["tools/bot_mode_dm.py:38,659", "_admit_queued_dm | _admit_live_dm",
                "subprocess: hermes -p <t> chat"],
         prov=FORK_PATCHED, state=LIVE),
    dict(id="peer_dm", x=30, y=168, w=300, h=92,
         title="hermes peer dm CLI",
         lines=["hermes_cli/subcommands/peer.py:671", "_delivery_headers:395",
                "cross-host only"],
         prov=FORK_PATCHED, state=LIVE),
    # -- gateway + store (center) --
    dict(id="gateway", x=390, y=40, w=360, h=150,
         title="Hermes Gateway API Server",
         lines=["gateway/platforms/api_server.py", "port 8644  (PID 9591)",
                "route:1549  _handle_session_chat:3506",
                "_handle_bot_send:3260"],
         prov=FORK_PATCHED, state=LIVE),
    dict(id="delivery_keys", x=390, y=230, w=360, h=70,
         title="delivery_keys",
         lines=["hermes_cli/delivery_keys.py:41,75,99"],
         prov=FORK_ONLY, state=LIVE),
    dict(id="queue", x=390, y=340, w=360, h=96,
         title="bot_delivery_queue  (durable store)",
         lines=["tools/bot_delivery_queue.py", "build_envelope:1130",
                "validate_delivery_id:420"],
         prov=FORK_ONLY, state=LIVE),
    dict(id="drainer", x=390, y=476, w=360, h=92,
         title="drainer  (api_server_bot_delivery)",
         lines=["gateway/platforms/api_server_bot_delivery.py",
                "sweep_loop:344 drain_once:289",
                "drain_under_lock:234 run_record:155"],
         prov=FORK_ONLY, state=LIVE),
    # -- delivery (right) --
    dict(id="turn_lease", x=810, y=40, w=320, h=80,
         title="turn lease",
         lines=["agent/turn_facade_lease.py", "tools/bot_relay.py:485",
                "acquire_delivery_turn_lock"],
         prov=FORK_PATCHED, state=LIVE),
    dict(id="bot_chat", x=810, y=160, w=320, h=80,
         title="Bot Chat  (recipient)",
         lines=["profiles/<lane>/state.db", "the agent turn writes here"],
         prov=STOCK, state=LIVE),
    # -- unmerged lane (right, below delivery) --
    dict(id="v1messages", x=810, y=300, w=350, h=176,
         title="/v1/messages routes  (UNMERGED)",
         lines=["api_server.py:1571-1573", "@ feat/bot-message-api 8ac9a2d3",
                "PR #21 OPEN  (mergedAt null)",
                "POST /v1/messages -> _handle_message_send:3545",
                "GET  /v1/messages/{id} -> _handle_message_status:3598",
                "POST /v1/messages/{id}/ack -> _handle_message_ack:3621"],
         prov=FORK_PATCHED, state=UNMERGED),
    dict(id="hub_auth", x=810, y=510, w=350, h=72,
         title="_check_hub_auth  (UNMERGED)",
         lines=["api_server.py:3476  @ branch 8ac9a2d3",
                "Bearer token vs gateway root API_SERVER_KEY"],
         prov=FORK_PATCHED, state=UNMERGED),
    dict(id="hub", x=810, y=616, w=350, h=66,
         title='"hub"  (hub-authenticated)',
         lines=["prose-only name; no code entity", "canonical = Gateway API Server"],
         prov=SPECIFIED_ONLY, state=REJECTED),
]

# Edges: (src_id, dst_id, label, kind, cls)
# kind: "call" | "wrap" ; cls: "live" | "unmerged" | "rejected"
EDGES = [
    ("msg_agent", "queue", "1 in-process _admit_queued_dm (local admission)", "call", "live"),
    ("peer_dm", "gateway", "1 HTTP POST /api/sessions/{id}/chat  -> 8644", "call", "live"),
    ("gateway", "delivery_keys", "2 _handle_bot_send -> derive_delivery_key", "call", "live"),
    ("delivery_keys", "queue", "3 idempotency check -> enqueue record", "call", "live"),
    ("gateway", "drainer", "starts sweep_loop", "wrap", "live"),
    ("queue", "drainer", "4 sweep_loop reads next queued record", "call", "live"),
    ("drainer", "turn_lease", "5 acquire_delivery_turn_lock", "call", "live"),
    ("turn_lease", "bot_chat", "6 run turn -> state.db", "call", "live"),
    ("v1messages", "gateway", "POST /v1/messages -> _handle_message_send -> _handle_bot_send (converges)", "call", "unmerged"),
    ("v1messages", "queue", "GET /v1/messages/{id} reads ledger; POST .../ack -> acknowledged", "call", "unmerged"),
    ("hub", "gateway", "rejected name; role maps to Gateway API Server", "call", "rejected"),
]

# One real end-to-end trace (the path in use today, peer dm, cross-host)
TRACE = [
    ("1 send", "hermes peer dm <peer>/<agent>",
     "process: hermes CLI (sender)", "hermes_cli/subcommands/peer.py:671"),
    ("2 transport", "HTTP POST {peer.url}/api/sessions/{session_id}/chat",
     "port 8644, TLS, bearer key header", "peer.py:395 _delivery_headers"),
    ("3 admit", "Gateway API Server -> _handle_session_chat -> _handle_bot_send",
     "route detects bot_send payload", "api_server.py:1549,3506,3260"),
    ("4 idem", "delivery_keys.derive_delivery_key + delivery_fingerprint",
     "idempotency key + delivery_id = run_id", "delivery_keys.py:41,75,99"),
    ("5 store", "bot_delivery_queue write (STATUS_QUEUED)",
     "durable record on disk", "tools/bot_delivery_queue.py"),
    ("6 drain", "drainer sweep_loop -> drain_once -> drain_under_lock -> run_record",
     "30s sweep of queued records", "api_server_bot_delivery.py:344,289,234,155"),
    ("7 deliver", "acquire_delivery_turn_lock -> turn runs in recipient Bot Chat",
     "non-blocking turn lease", "tools/bot_relay.py:485 + turn_facade_lease.py"),
    ("8 receipt", "build_envelope -> {delivery_id, status} returned to sender",
     "sender receives delivery receipt", "tools/bot_delivery_queue.py:1130"),
]

PROV_TABLE = [
    (FORK_ONLY, "our own code (new file, absent upstream)", "#d97706",
     ["tools/bot_delivery_queue.py", "gateway/platforms/api_server_bot_delivery.py",
      "hermes_cli/delivery_keys.py"]),
    (FORK_PATCHED, "stock Hermes, modified by the fork (non-empty diff vs upstream/main)", "#7c3aed",
     ["gateway/platforms/api_server.py", "gateway/platforms/api_server_run_idempotency.py",
      "hermes_cli/subcommands/peer.py", "tools/bot_live_delivery.py",
      "agent/turn_facade_lease.py", "tools/bot_mode_dm.py", "tools/bot_relay.py",
      "tools/bot_failure_reasons.py"]),
    (STOCK, "untouched stock Hermes (no diff vs upstream/main)", "#475569",
     ["tools/bot_mode_probe.py", "agent/turn_facade.py", "agent/turn_context.py",
      "profiles/<lane>/state.db (conversation store)"]),
]

STATE_TABLE = [
    (LIVE, "code on main @ 51aee02d1b; live probe answers on 8644",
     "solid green border", "the bot_send queue path (in use today)"),
    (UNMERGED, "code on feat/bot-message-api @ 8ac9a2d3; PR #21 OPEN, mergedAt null",
     "dashed amber border", "the /v1/messages API (send + status + ack)"),
    (REJECTED, "exists only in prose; maps to no code entity",
     "dashed red border", "the 'hub' name (canonical: Gateway API Server)"),
]

GROUNDING = [
    ("fork", "yoyodine-industries/hermes-agent"),
    ("live HEAD", "main @ 51aee02d1b"),
    ("unmerged branch", "feat/bot-message-api @ 8ac9a2d3a93b8d4e31355e1256362aece60c46c5"),
    ("PR", "#21 OPEN (mergedAt null, base main)"),
    ("probe date", "2026-09-17 (gateway PID 9591, port 8644)"),
]

PROBES = [
    ("POST /v1/messages", "404", "route NOT registered on live main"),
    ("GET  /v1/messages", "404", "route NOT registered on live main"),
    ("POST /api/sessions/foo/chat", "401", "route REGISTERED, unauthenticated"),
    ("POST /health", "405", "route REGISTERED, wrong method"),
    ("GET  /health", "200", "gateway alive"),
]


# ---------------------------------------------------------------------------
# Geometry self-check (the "verify by rendering" discipline)
# ---------------------------------------------------------------------------

def check_geometry():
    viewbox = (0, 0, 1240, 720)
    ids = set()
    for n in NODES:
        assert n["w"] > 0 and n["h"] > 0, "zero-size box: %s" % n["id"]
        assert n["id"] not in ids, "duplicate id: %s" % n["id"]
        ids.add(n["id"])
        assert n["x"] >= 0 and n["y"] >= 0, "negative origin: %s" % n["id"]
        assert n["x"] + n["w"] <= viewbox[2], "box past right edge: %s" % n["id"]
        assert n["y"] + n["h"] <= viewbox[3], "box past bottom edge: %s" % n["id"]
    boxes = {n["id"]: n for n in NODES}
    keys = list(boxes)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = boxes[keys[i]], boxes[keys[j]]
            ax1, ay1 = a["x"], a["y"]
            ax2, ay2 = a["x"] + a["w"], a["y"] + a["h"]
            bx1, by1 = b["x"], b["y"]
            bx2, by2 = b["x"] + b["w"], b["y"] + b["h"]
            overlap = not (ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1)
            assert not overlap, "overlapping boxes: %s vs %s" % (a["id"], b["id"])
    for e in EDGES:
        assert e[0] in boxes, "edge src missing: %s" % e[0]
        assert e[1] in boxes, "edge dst missing: %s" % e[1]
    print("GEOMETRY OK: %d nodes, %d edges, all in viewBox, no overlap"
          % (len(NODES), len(EDGES)))


# ---------------------------------------------------------------------------
# SVG rendering helpers
# ---------------------------------------------------------------------------

PROV_FILL = {
    STOCK: "#e2e8f0",
    FORK_PATCHED: "#ede9fe",
    FORK_ONLY: "#fef3c7",
    SPECIFIED_ONLY: "#fee2e2",
}
PROV_STROKE = {
    STOCK: "#475569",
    FORK_PATCHED: "#7c3aed",
    FORK_ONLY: "#d97706",
    SPECIFIED_ONLY: "#dc2626",
}
STATE_TAG = {
    LIVE: ("LIVE", "#16a34a"),
    UNMERGED: ("UNMERGED PR#21", "#d97706"),
    REJECTED: ("REJECTED", "#dc2626"),
}


def esc(s):
    return html.escape(s, quote=True)


def box_svg(n):
    stroke = PROV_STROKE[n["prov"]]
    fill = PROV_FILL[n["prov"]]
    dash = "stroke-dasharray:6 4;" if n["state"] != LIVE else ""
    tag_text, tag_color = STATE_TAG[n["state"]]
    parts = []
    parts.append(
        '<rect x="%d" y="%d" width="%d" height="%d" rx="6" '
        'fill="%s" stroke="%s" stroke-width="2" style="%s"/>'
        % (n["x"], n["y"], n["w"], n["h"], fill, stroke, dash)
    )
    ty = n["y"] + 22
    parts.append(
        '<text x="%d" y="%d" font-size="14" font-weight="700" fill="#111827">%s</text>'
        % (n["x"] + 12, ty, esc(n["title"]))
    )
    ty += 18
    for line in n["lines"]:
        parts.append(
            '<text x="%d" y="%d" font-size="11" fill="#374151" '
            'font-family="Menlo,Consolas,monospace">%s</text>'
            % (n["x"] + 12, ty, esc(line))
        )
        ty += 15
    # state tag badge (top-right corner)
    bw = 16 + len(tag_text) * 6.4
    bx = n["x"] + n["w"] - bw - 8
    by = n["y"] - 9
    parts.append(
        '<rect x="%d" y="%d" width="%d" height="18" rx="9" fill="%s"/>'
        % (bx, by, bw, tag_color)
    )
    parts.append(
        '<text x="%d" y="%d" font-size="10" font-weight="700" fill="#ffffff" '
        'text-anchor="middle">%s</text>'
        % (bx + bw / 2, by + 13, esc(tag_text))
    )
    return "\n".join(parts)


def edge_anchor(nid, side):
    n = {x["id"]: x for x in NODES}[nid]
    if side == "r":
        return (n["x"] + n["w"], n["y"] + n["h"] / 2)
    if side == "l":
        return (n["x"], n["y"] + n["h"] / 2)
    if side == "t":
        return (n["x"] + n["w"] / 2, n["y"])
    if side == "b":
        return (n["x"] + n["w"] / 2, n["y"] + n["h"])
    raise ValueError(side)


# explicit per-edge anchor sides + label offsets (kept out of the way)
EDGE_GEO = {
    ("msg_agent", "queue"): ("r", "l", (300, 90)),
    ("peer_dm", "gateway"): ("r", "l", (300, 200)),
    ("gateway", "delivery_keys"): ("b", "t", (760, 224)),
    ("delivery_keys", "queue"): ("b", "t", (760, 334)),
    ("gateway", "drainer"): ("b", "l", (585, 470)),
    ("queue", "drainer"): ("b", "t", (760, 470)),
    ("drainer", "turn_lease"): ("r", "l", (720, 500)),
    ("turn_lease", "bot_chat"): ("b", "t", (980, 152)),
    ("v1messages", "gateway"): ("l", "r", (760, 250)),
    ("v1messages", "queue"): ("l", "r", (760, 330)),
    ("hub", "gateway"): ("l", "r", (700, 650)),
}


def edge_svg(edge):
    src, dst, label, kind, cls = edge
    sside, dside, lpos = EDGE_GEO[(src, dst)]
    sx, sy = edge_anchor(src, sside)
    dx, dy = edge_anchor(dst, dside)
    if cls == "live":
        color = "#334155"
        dash = ""
        style = ""
    elif cls == "unmerged":
        color = "#d97706"
        dash = "stroke-dasharray:8 5;"
        style = "marker-end:url(#arrowAmber);"
    else:
        color = "#dc2626"
        dash = "stroke-dasharray:8 5;"
        style = "marker-end:url(#arrowRed);"
    kindmark = "wrap (owns)" if kind == "wrap" else "call"
    parts = []
    parts.append(
        '<path d="M %d %d C %d %d, %d %d, %d %d" fill="none" stroke="%s" '
        'stroke-width="2" style="%s%s"/>'
        % (sx, sy, sx, sy, dx, dy, dx, dy, color, dash,
           ("marker-end:url(#arrowDark);" if cls == "live" else style))
    )
    lx, ly = lpos
    parts.append(
        '<text x="%d" y="%d" font-size="10" fill="%s" '
        'font-family="Menlo,Consolas,monospace">%s [%s]</text>'
        % (lx, ly, color, esc(label), kindmark)
    )
    return "\n".join(parts)


def svg_block():
    defs = (
        '<defs>'
        '<marker id="arrowDark" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#334155"/></marker>'
        '<marker id="arrowAmber" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#d97706"/></marker>'
        '<marker id="arrowRed" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto">'
        '<path d="M0,0 L10,5 L0,10 z" fill="#dc2626"/></marker>'
        '</defs>'
    )
    body = []
    for e in EDGES:
        body.append(edge_svg(e))
    for n in NODES:
        body.append(box_svg(n))
    return (
        '<svg viewBox="0 0 1240 720" width="100%" '
        'style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;">\n'
        + defs + "\n" + "\n".join(body) + "\n</svg>"
    )


def table(headers, rows):
    th = "".join("<th>%s</th>" % esc(h) for h in headers)
    trs = []
    for r in rows:
        tds = "".join("<td>%s</td>" % esc(c) for c in r)
        trs.append("<tr>%s</tr>" % tds)
    return ('<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>'
            % (th, "".join(trs)))


def prov_cell(label, color, files):
    lis = "".join("<li><code>%s</code></li>" % esc(f) for f in files)
    return ('<td><span class="chip" style="background:%s">%s</span></td>'
            '<td>%s</td><td><ul class="files">%s</ul></td>'
            % (color, esc(label), esc(label + " note"), lis))


def build_html():
    prov_rows = []
    for label, desc, color, files in PROV_TABLE:
        lis = "".join("<li><code>%s</code></li>" % esc(f) for f in files)
        prov_rows.append(
            '<tr><td><span class="chip" style="background:%s">%s</span></td>'
            '<td>%s</td><td><ul class="files">%s</ul></td></tr>'
            % (color, esc(label), esc(desc), lis)
        )
    state_rows = []
    for label, desc, border, example in STATE_TABLE:
        state_rows.append(
            '<tr><td><span class="chip">%s</span></td><td>%s</td>'
            '<td><code>%s</code></td><td>%s</td></tr>'
            % (esc(label), esc(desc), esc(border), esc(example))
        )
    trace_rows = []
    for step, what, detail, loc in TRACE:
        trace_rows.append(
            '<tr><td class="step">%s</td><td><code>%s</code></td>'
            '<td>%s</td><td><code>%s</code></td></tr>'
            % (esc(step), esc(what), esc(detail), esc(loc))
        )
    probe_rows = []
    for path, code, meaning in PROBES:
        probe_rows.append(
            '<tr><td><code>%s</code></td><td class="code">%s</td><td>%s</td></tr>'
            % (esc(path), esc(code), esc(meaning))
        )
    ground_rows = []
    for k, v in GROUNDING:
        ground_rows.append('<tr><td>%s</td><td><code>%s</code></td></tr>'
                           % (esc(k), esc(v)))
    return HTML_TEMPLATE % {
        "svg": svg_block(),
        "prov_rows": "".join(prov_rows),
        "state_rows": "".join(state_rows),
        "trace_rows": "".join(trace_rows),
        "probe_rows": "".join(probe_rows),
        "ground_rows": "".join(ground_rows),
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot-to-bot messaging: call graph</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
         Helvetica, Arial, sans-serif; color: #111827; margin: 0;
         background: #ffffff; line-height: 1.5; }
  .wrap { max-width: 1240px; margin: 0 auto; padding: 24px 20px 80px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  h2 { font-size: 16px; margin: 32px 0 10px; border-bottom: 2px solid #e5e7eb;
       padding-bottom: 6px; }
  .sub { color: #6b7280; font-size: 13px; margin-bottom: 16px; }
  code { font-family: Menlo, Consolas, "SF Mono", monospace; font-size: 12px;
         background: #f1f5f9; padding: 1px 5px; border-radius: 4px; }
  .verdict { background: #fff7ed; border: 1px solid #fdba74; border-radius: 8px;
             padding: 14px 16px; margin: 18px 0; }
  .verdict b { color: #c2410c; }
  table { border-collapse: collapse; width: 100%%; font-size: 13px;
          margin: 8px 0 20px; }
  th, td { border: 1px solid #e5e7eb; padding: 8px 10px; text-align: left;
           vertical-align: top; }
  th { background: #f8fafc; font-size: 12px; text-transform: uppercase;
       letter-spacing: .03em; color: #475569; }
  .chip { display: inline-block; padding: 2px 9px; border-radius: 10px;
          font-size: 11px; font-weight: 700; color: #ffffff; }
  ul.files { margin: 0; padding-left: 18px; }
  ul.files code { background: transparent; padding: 0; }
  .step { font-weight: 700; color: #4f46e5; white-space: nowrap; }
  .code { font-family: Menlo, Consolas, monospace; font-weight: 700; }
  .legend { display: flex; flex-wrap: wrap; gap: 10px; margin: 8px 0 14px; }
  .legend .item { display: flex; align-items: center; gap: 7px; font-size: 12px;
                  color: #374151; }
  .swatch { width: 16px; height: 16px; border-radius: 3px; border: 2px solid; }
  .note { color: #6b7280; font-size: 12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Bot-to-bot messaging: call graph</h1>
  <div class="sub">which component calls which, in what order, over what
  transport &middot; stock vs ours &middot; wrap vs call &middot; three states
  never blended</div>

  <div class="verdict">
    <b>Owner question: is target-state bot-to-bot messaging delivered?</b><br>
    <b>No.</b> The target-state /v1/messages API is <b>built but unmerged</b>,
    not live and not merely specified. It exists only on branch
    <code>feat/bot-message-api @ 8ac9a2d3</code> (PR #21 OPEN, mergedAt null).
    Live main <code>@ 51aee02d1b</code> answers <b>404</b> to
    <code>POST /v1/messages</code> and <code>GET /v1/messages</code>. The path in
    use today is <code>bot_send</code> on
    <code>POST /api/sessions/{id}/chat</code> through the durable queue.
  </div>

  <div class="legend">
    <div class="item"><span class="swatch" style="background:#e2e8f0;border-color:#475569"></span> stock Hermes</div>
    <div class="item"><span class="swatch" style="background:#ede9fe;border-color:#7c3aed"></span> fork-patched (stock, modified)</div>
    <div class="item"><span class="swatch" style="background:#fef3c7;border-color:#d97706"></span> our own code (fork-only)</div>
    <div class="item"><span class="swatch" style="background:#fee2e2;border-color:#dc2626"></span> specified-only (rejected)</div>
    <div class="item"><span style="color:#16a34a;font-weight:700">LIVE</span></div>
    <div class="item"><span style="color:#d97706;font-weight:700">UNMERGED (dashed)</span></div>
    <div class="item"><span style="color:#dc2626;font-weight:700">REJECTED (dashed)</span></div>
  </div>

  <h2>1. Call graph</h2>
  %(svg)s
  <p class="note">Solid arrows = call (edge label carries order + transport).
  The thin "wrap (owns)" edge marks the gateway process owning the drainer
  loop. Amber dashed = unmerged (PR #21). Red dashed = rejected prose-only name.</p>

  <h2>2. Provenance: stock vs ours (every component)</h2>
  <table><thead><tr><th>Bucket</th><th>Meaning</th><th>Files</th></tr></thead>
  <tbody>%(prov_rows)s</tbody></table>

  <h2>3. Three states (never blended)</h2>
  <table><thead><tr><th>State</th><th>Definition</th><th>Border</th>
  <th>Here</th></tr></thead><tbody>%(state_rows)s</tbody></table>

  <h2>4. One real message, end to end (peer dm, cross-host)</h2>
  <table><thead><tr><th>Step</th><th>What</th><th>Process / transport</th>
  <th>Grounding</th></tr></thead><tbody>%(trace_rows)s</tbody></table>
  <p class="note">Same-host delivery (message_agent tool) converges on the same
  queue via in-process <code>_admit_queued_dm</code> (bot_mode_dm.py:659), or a
  live-owner direct write, or a <code>hermes chat</code> subprocess &mdash; no
  HTTP hop. Only cross-host <code>peer dm</code> enters over HTTP.</p>

  <h2>5. Live probes (the discriminator)</h2>
  <table><thead><tr><th>Probe</th><th>Code</th><th>Meaning</th></tr></thead>
  <tbody>%(probe_rows)s</tbody></table>
  <p class="note">404 = route not registered (absent). 401 = route registered,
  unauthenticated. 405 = route registered, wrong method. The same three probes
  separate the unmerged /v1/messages routes (404 on live) from the live
  /api/sessions/{id}/chat route (401/405).</p>

  <h2>6. Grounding</h2>
  <table><thead><tr><th>Field</th><th>Value</th></tr></thead>
  <tbody>%(ground_rows)s</tbody></table>

  <h2>7. What deploying PR #21 changes</h2>
  <p>Merging PR #21 registers three routes on the gateway and adds a dedicated
  message API on top of the existing queue and drainer &mdash; it does not
  replace them:</p>
  <ul class="files">
    <li><code>POST /v1/messages</code> &rarr; <code>_handle_message_send</code>
    &rarr; <code>_handle_bot_send</code> (converges on the same admission + queue).</li>
    <li><code>GET /v1/messages/{delivery_id}</code> &rarr; status read from the
    ledger.</li>
    <li><code>POST /v1/messages/{delivery_id}/ack</code> &rarr; receiver ack,
    adding a new <code>acknowledged</code> stage the live path lacks.</li>
    <li>Auth: <code>_check_hub_auth</code> validates a Bearer token against the
    gateway root <code>API_SERVER_KEY</code> (branch-only; the term "hub" is the
    rejected prose name).</li>
  </ul>
  <p class="note">The missing call on live today is exactly the
  <code>/v1/messages</code> route registration at <code>api_server.py:1571-1573</code>
  &mdash; present only on the unmerged branch.</p>
</div>
</body>
</html>
"""


def main():
    check_geometry()
    out = build_html()
    path = "bot-messaging-callgraph.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    # ASCII-only guarantee (write path truncates on non-ASCII)
    try:
        out.encode("ascii")
        print("ASCII OK")
    except UnicodeEncodeError as e:
        print("NON-ASCII FOUND: %s" % e)
        sys.exit(1)
    print("wrote %s (%d bytes)" % (path, len(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
