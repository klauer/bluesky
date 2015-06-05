"""Tests for, uh, the state machine of the run engine"""
from nose.tools import raises, assert_raises, assert_equal
from bluesky import RunEngineStateMachine
from super_state_machine.errors import TransitionError
from .utils import (goto_state,
                    tautologically_define_state_machine_transitions,
                    define_state_machine_transitions_from_class)


sm = RunEngineStateMachine()


def _transitioner(state_machine, state_from, state_to, valid_transition):
    print('current state = [[%s]]' % state_machine.state)
    print('attempting to go to state [[%s]]' % state_to)
    if valid_transition:
        state_machine.set_(state_to)
    else:
        assert_raises(TransitionError, state_machine.set_, state_to)


def test_state_machine_transitions():
    transition_map = tautologically_define_state_machine_transitions(sm)
    for state_from, transitions in transition_map.items():
        # set the state correctly
        for state_to, valid_transition in transitions:
            goto_state(sm, state_from)
            yield _transitioner, sm, state_from, state_to, valid_transition


def test_super_state_machine():
    # make sure super state machine is doing what it promises to do...
    assert_equal(tautologically_define_state_machine_transitions(sm),
                 define_state_machine_transitions_from_class(sm))