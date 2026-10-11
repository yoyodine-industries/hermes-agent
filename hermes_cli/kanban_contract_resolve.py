"""Resolve a card's completion contract against the target repo's required checks.

Card t_b58332a4. A card born with an ``OWNER/REPO`` (or exact-PR-URL) completion
contract can never satisfy the acceptance gate when that repository requires no
status checks: ``kanban_pr_acceptance.collect_acceptance`` returns
``classification=missing`` with ``required=[]``, so ``complete_task`` parks the
card ``blocked``/``capability`` with its work already done, and nothing a worker
does can change it. Such a card must never be BORN on that contract.

The one moment every filing surface reaches is ``kanban_db.create_task``; this
module answers THERE whether the declared repository actually requires checks,
and — when it does not — what the card's contract must become instead.

The read is a PROVIDER read (repository metadata, branch protection and the
rules API, through the same ``gh`` transport the acceptance gate uses), never an
inference from the card's text or from another card's contract. Four verdicts:

* ``checks``  — the repository requires at least one status check: keep it.
* ``none``    — the repository is readable and requires none: fall back.
* ``gated``   — the repository is readable but its checks APIs are gated (the
  plan/user cannot consult them): treat it as "no required checks" and SAY SO in
  the reason, because the alternative is leaving the card on a contract no worker
  action can satisfy.
* ``unverifiable`` — the repository itself cannot be read at all. That is an
  access gap, which the acceptance gate already surfaces as a fixable ``auth``
  cause, so the declaration is KEPT: stripping the CI gate on an invisible repo
  would hide the very thing that needs fixing.

``landed`` is the deploy-proof class, not a repository check
(``needs_repository_checks`` -> False), so a card carrying a DEPLOYED unit never
reaches this resolver and its deploy-proof gate is never touched. Only an
``OWNER/REPO`` / PR-URL declaration — a repository-CI sign-off intent — is
resolved, and its only fallback is ``local-only``.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

# The fallback a repo/PR declaration takes when the repository requires no checks.
# ``landed`` is deliberately NOT a candidate here: a card carrying a deployed unit
# declares ``landed``, and such a declaration never enters this path -- so the
# deploy-proof gate cannot be stripped by a fallback.
FALLBACK_CONTRACT = "local-only"


@dataclass(frozen=True)
class ContractResolution:
    """What a declared contract resolves to at the set seam.

    ``contract`` is the value the card should carry (equal to ``declared`` when
    nothing moved); ``reason`` is the measured, credential-free explanation and
    is populated only when ``changed``.
    """

    declared: str
    contract: str
    changed: bool
    state: str = "checks"          # "checks" | "none" | "unknown"
    repo: str | None = None
    branch: str | None = None
    reason: str | None = None


def repository_of(contract: str | None) -> str | None:
    """``OWNER/REPO`` for a repo/PR-URL contract, else ``None``."""
    if not isinstance(contract, str):
        return None
    from hermes_cli import kanban_pr_acceptance as _accept
    match = _accept._PR.fullmatch(contract)
    if match:
        return match[1]
    if _accept._REPO.fullmatch(contract):
        return contract
    return None


def _short(exc: BaseException) -> str:
    """A short, first-line description safe to persist (never gh stderr)."""
    text = str(exc).strip()
    if not text:
        return exc.__class__.__name__
    return text.splitlines()[0][:160]


def required_checks_state(repo: str, *, profile_home: str | None = None):
    """``(state, branch, detail)`` for ``repo``'s default branch.

    ``state`` is one of four measured verdicts:

    * ``checks``       — at least one required status check is configured.
    * ``none``         — the repository is readable and requires none.
    * ``gated``        — the repository is readable but the checks APIs could
      not be consulted (the plan/user gates them): the card's "cannot determine"
      case, treated as no required checks.
    * ``unverifiable`` — the REPOSITORY itself could not be read (invisible to the
      login, auth gap, no ``gh``): an access problem the acceptance gate already
      classifies ``auth`` and parks on a fixable cause, so the declaration is
      KEPT rather than silently stripped.

    ``detail`` is a short, credential-free explanation used verbatim in the
    measured reason; it is ``None`` when the answer is ``checks``.
    """
    from hermes_cli import kanban_pr_acceptance as _accept

    try:
        meta = _accept._api(f"repos/{repo}", profile_home=profile_home)
    except Exception as exc:
        return "unverifiable", None, f"repository metadata unreadable ({_short(exc)})"
    branch = meta.get("default_branch") if isinstance(meta, dict) else None
    if not isinstance(branch, str) or not branch:
        # A response that is not repository metadata (an unreadable/private repo, a
        # shape gh never returns for this endpoint) proves nothing about checks.
        return "unverifiable", None, "repository metadata did not resolve a default branch"

    # The modern rules API (repository rulesets applied to this branch).
    rules_detail = None
    try:
        pages = _accept._api(
            f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100",
            paginate=True, profile_home=profile_home,
        )
        rules_ok = True
        rules_required = any(
            isinstance(rule, dict) and rule.get("type") == "required_status_checks"
            for page in pages for rule in page
        )
    except Exception as exc:
        rules_ok, rules_required = False, False
        rules_detail = f"rules API unavailable ({_short(exc)})"

    # Classic branch protection.  GitHub answers 404 when the branch carries no
    # protection at all -- that is a DEFINITIVE "no checks", not a gated read.
    prot_detail = None
    try:
        protection = _accept._api(
            f"repos/{repo}/branches/{quote(branch, safe='')}/protection",
            profile_home=profile_home,
        )
        prot_ok = True
        prot_required = bool((protection or {}).get("required_status_checks"))
    except _accept._GateAuthError as exc:
        if "404" in str(exc):
            prot_ok, prot_required = True, False
        else:
            prot_ok, prot_required = False, False
            prot_detail = f"branch-protection API gated ({_short(exc)})"
    except Exception as exc:
        prot_ok, prot_required = False, False
        prot_detail = f"branch-protection API unavailable ({_short(exc)})"

    if rules_required or prot_required:
        return "checks", branch, None
    if rules_ok and prot_ok:
        return "none", branch, "required_status_checks=null, 0 rulesets"
    details = "; ".join(d for d in (rules_detail, prot_detail) if d)
    return "gated", branch, details or "could not determine required checks"


def resolve_contract_at_set(contract: str, *, profile_home: str | None = None) -> ContractResolution:
    """Resolve a declared contract against the target repo's required checks.

    Returns the contract the card should carry plus, when it moved, the measured
    reason.  Inert for every contract that is not repo/PR backed (``local-only``,
    ``landed``) and for a contract with no parseable repository.
    """
    from hermes_cli import kanban_pr_acceptance as _accept

    if not _accept.needs_repository_checks(contract):
        return ContractResolution(contract, contract, False, "checks")
    repo = repository_of(contract)
    if repo is None:
        return ContractResolution(contract, contract, False, "checks")

    state, branch, detail = required_checks_state(repo, profile_home=profile_home)
    if state in ("checks", "unverifiable"):
        # A repository we cannot read is not a repository we can pronounce
        # check-free: keep the declaration and let the acceptance gate surface the
        # access gap as a fixable ``auth`` cause.
        return ContractResolution(contract, contract, False, state, repo, branch)
    if state == "none":
        reason = (
            f"{repo} requires no status checks on {branch or 'its default branch'} "
            f"({detail}); a repository-CI contract there can never clear, so the card "
            f"was filed with completion_contract={FALLBACK_CONTRACT}"
        )
    else:  # gated
        reason = (
            f"could not determine required checks for {repo} ({detail}); treating the "
            f"repository as requiring no checks and filing the card with "
            f"completion_contract={FALLBACK_CONTRACT}"
        )
    return ContractResolution(contract, FALLBACK_CONTRACT, True, state, repo, branch, reason)

