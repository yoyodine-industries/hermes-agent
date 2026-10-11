"""The lease-checked force push is its own detection key (card t_ca4879f5).

`git push --force-with-lease` is a narrower verb than a bare force push: git refuses it unless
the remote tip still matches the one the push was prepared against, so it cannot silently
discard a peer's work. Before this change the class had exactly one key, emitted by the
`--forc[a-z]*` pattern family, so a profile could not pre-approve the leased form without also
approving bare `--force`, `-f`, and any destination - including `main`.

These tests are the card's proof matrix, and each one is a real call into the detector rather
than a restatement of the pattern:

  1. a leased push to the worker's OWN card branch is detected under the NEW key (approvable);
  2. bare `--force` is still the GENERIC key (never approvable by the lease allowlist);
  3. `-f` is still the SHORT-FLAG key;
  4. a leased push to `main` (and `master`) is still refused, under the generic key - the
     pattern's lookahead drops it through to the force-push rule below it.
"""

import pytest

from tools.approval import detect_dangerous_command

LEASE_KEY = "git push --force-with-lease (lease-checked force push)"
GENERIC_KEY = "git force push (rewrites remote history)"
SHORT_KEY = "git force push short flag (rewrites remote history)"

OWN_BRANCH = "card-t_ca4879f5-force-with-lease"


def detect(command):
    """(dangerous, key, description) for one command, as the approval layer sees it."""
    return detect_dangerous_command(command)


@pytest.mark.parametrize("command", [
    f"git push --force-with-lease origin {OWN_BRANCH}",
    f"git push --force-with-lease=origin/{OWN_BRANCH} origin {OWN_BRANCH}",
])
def test_case_1_leased_push_to_the_own_branch_gets_the_lease_key(command):
    dangerous, key, description = detect(command)
    assert dangerous is True, command
    assert key == LEASE_KEY, (command, key)
    assert description == LEASE_KEY, (command, description)


def test_the_detector_only_sees_the_adjacent_git_push_form():
    """A MEASURED limit of the whole git family, asserted so the next reader knows.

    Every `git push` pattern here requires `git` and `push` to be adjacent, so
    `git -C <dir> push --force ...` matches NONE of them - the generic force-push key included.
    That is pre-existing behaviour shared by all three keys, NOT something this card's lease
    pattern introduces, and it is reported on the card rather than widened here: covering the
    `-C` form changes detection for a far larger set of commands than the lease authorization
    covers, and that call is the design authority's.
    """
    assert detect("git -C /tmp/checkout push --force origin main") == (False, None, None)
    assert detect(f"git -C /tmp/checkout push --force-with-lease origin {OWN_BRANCH}") == \
        (False, None, None)


@pytest.mark.parametrize("command", [
    "git push --force origin " + OWN_BRANCH,
    "git push --force HEAD:refs/heads/" + OWN_BRANCH,
])
def test_case_2_bare_force_is_still_the_generic_key(command):
    dangerous, key, _ = detect(command)
    assert dangerous is True, command
    assert key == GENERIC_KEY, (command, key)
    assert key != LEASE_KEY, command


@pytest.mark.parametrize("command", [
    "git push -f origin " + OWN_BRANCH,
    "git push -f",
])
def test_case_3_short_flag_is_still_the_short_flag_key(command):
    dangerous, key, _ = detect(command)
    assert dangerous is True, command
    assert key == SHORT_KEY, (command, key)


@pytest.mark.parametrize("ref", ["main", "master", "refs/heads/main", "HEAD:main"])
def test_case_4_a_leased_push_to_main_is_still_refused(ref):
    """The authorization's "never to main" - carried by the lookahead's fail-closed fall-through.

    The lease pattern does not match a command naming main/master, so it falls through to the
    force-push rule and is refused under that key: pre-approving the lease key cannot approve
    a push to main.
    """
    command = f"git push --force-with-lease origin {ref}"
    dangerous, key, _ = detect(command)
    assert dangerous is True, command
    assert key == GENERIC_KEY, (command, key)
    assert key != LEASE_KEY, command


@pytest.mark.parametrize("command", [
    "git push origin " + OWN_BRANCH,
    "git status",
])
def test_a_plain_push_is_not_dangerous(command):
    assert detect(command) == (False, None, None), command


def test_a_branch_name_merely_CONTAINING_main_is_refused_fail_closed():
    """Documented cost of the lookahead, asserted so it cannot change silently.

    `\\bmain\\b` matches inside a branch slug such as `card-x-main-fix`, so that push is refused
    under the generic key. That is the safe direction (a false refusal, never a false approval),
    and the remedy is to rename the branch rather than to weaken the guard.
    """
    command = "git push --force-with-lease origin card-x-main-fix"
    dangerous, key, _ = detect(command)
    assert dangerous is True, command
    assert key == GENERIC_KEY, (command, key)
