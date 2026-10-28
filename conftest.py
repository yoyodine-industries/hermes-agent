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

Carried as one small rootdir file rather than as a whole-file copy of ``tests/conftest.py``:
a whole-file copy of an upstream file is the compose defect named O-1 in ruling t_f3eefbdb
(2026-10-11). The suite declares exactly one variable, so it ships as one small file.

Restored 2026-10-11 by t_50952579: the declaration the write-door-invariants landing made in
``tests/conftest.py`` (commit 7a1f7729ce) was never transplanted into this home of record, so
wiring the evidence door without it turns the pinned deploy-proof suite RED.

Landed 2026-10-19 by t_987793ae, because the override-set merge (102212ef87, t_eefe2e23) dropped
this file silently: the live capture ``bb7ee7c6ab`` added it, and it was missing from the merged
tree. The decision is to KEEP it, and the measurement behind that decision is that it is INERT on
the reconciled tip -- ``kanban_gate_invariants.gate_completion_evidence`` has no call site in this
tree, and ``tests/hermes_cli/test_kanban_gate_invariants.py`` reports the identical 10 failed /
19 passed with and without it (the only visible difference is the declared mode: ``refuse``
without this file, ``measure`` with it). It is kept as the DECLARATION, not as a fix, because that
suite already asserts the WIRED behaviour of the door; on the day the door is wired into the
completion path again, a suite that closes cards with no evidence class by the hundred would
refuse en masse without this file, and the failure would look like a product defect.

THAT DAY ARRIVED, 2026-10-20 (card t_041d673a, ruling ESC-01650 / platform t_88cbc437 ->
t_52183ba9): the door's call site was restored in ``kanban_db.complete_task`` (after
``_gate_deploy_proof``, before ``prepare_acceptance``) and ``gate_assignee`` was wired onto the
assign path with it. So this file is no longer INERT -- it is LOAD-BEARING: with it the suite
closes its hundreds of evidence-free cards in ``measure`` (each one recording
``evidence_gate_measure``), without it they would all refuse on the first run. Do not delete it,
and do not "fix" it by turning it into a copy of ``tests/conftest.py`` (compose defect O-1,
ruling t_f3eefbdb). The LIVE boards are a separate question: ``<kanban_home>/kanban/evidence_gate``
is the prod switch, and the door must not reach the live tree before that file reads ``measure``.
"""

import os

os.environ.setdefault("HERMES_KANBAN_EVIDENCE_GATE", "measure")
