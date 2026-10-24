import pytest

from opgrade.orchestration import InvalidTransition, SessionState, check_transition

S = SessionState


def test_nominal_path():
    path = [S.AWAIT_FRONT, S.AWAIT_BACK, S.READY_TO_ANALYZE, S.AWAIT_REVIEW, S.COMPLETED]
    for a, b in zip(path, path[1:]):
        check_transition(a, b)


@pytest.mark.parametrize("a,b", [(S.AWAIT_FRONT, S.READY_TO_ANALYZE), (S.AWAIT_FRONT, S.AWAIT_REVIEW), (S.AWAIT_BACK, S.COMPLETED)])
def test_cannot_skip_steps(a, b):
    with pytest.raises(InvalidTransition):
        check_transition(a, b)


def test_cancel_allowed_until_terminal():
    check_transition(S.AWAIT_BACK, S.CANCELLED)
    with pytest.raises(InvalidTransition):
        check_transition(S.COMPLETED, S.CANCELLED)
