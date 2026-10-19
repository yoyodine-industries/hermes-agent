"""The live hermes-agent suite's own environment declaration.

``hermes_cli/kanban_gate_invariants.py`` -- the board's write-door invariants -- refuses a
completion that declares no evidence class. Its kernel default is ``refuse``, which is correct
for a real board, where a lane knows what backs its own claim. The SUITE is not a real board:
its cases close cards with no run and no artifact behind them by the hundred, and every one of
them would have to declare a class that means nothing. So the suite makes the same declaration
a migrating board makes in its own registry (``kanban/evidence_gate``): ``measure``, which
keeps the measurement and drops only the refusal.

Loaded for the WHOLE suite: pytest loads the rootdir ``conftest.py`` before any test module, so
every case under ``tests/`` inherits it, and a case that needs ``refuse`` arms it itself with
``monkeypatch.setenv`` (``tests/hermes_cli/test_kanban_gate_invariants.py`` does exactly that).

Carried as a small untracked override (``untracked-overrides/conftest.py``, pinned in
``untracked-overrides/MANIFEST.md``) rather than as a whole-file copy of ``tests/conftest.py``:
a whole-file copy of an upstream file is the compose defect named O-1 in ruling t_f3eefbdb
(2026-10-11). The suite declares exactly one variable, so it ships as one small file.

Restored 2026-10-11 by t_50952579: the declaration the write-door-invariants landing made in
``tests/conftest.py`` (commit 7a1f7729ce) was never transplanted into this home of record, so
wiring the evidence door without it turns the pinned deploy-proof suite RED.
"""

import os

os.environ.setdefault("HERMES_KANBAN_EVIDENCE_GATE", "measure")
