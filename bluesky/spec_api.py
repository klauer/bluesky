"""
This module implements plan generators that close over the "global state"
singleton, ``bluesky.global_state.gs``. Changing the attributes of ``gs``
changes the behavior of these plans.

    DETS  # list of detectors
    MASTER_DET  # detector to use for tw
    MASTER_DET_FIELD  # detector field to use for tw
    TH_MOTOR
    TTH_MOTOR

Page numbers in the code comments refer to the SPEC manual at
http://www.certif.com/downloads/css_docs/spec_man.pdf
"""
from inspect import signature
import matplotlib.pyplot as plt
from bluesky import plans, Msg
from bluesky.callbacks import LiveTable, LivePlot, LiveRaster
from bluesky.scientific_callbacks import PeakStats
from boltons.iterutils import chunked
from bluesky.global_state import gs
from bluesky.utils import (normalize_subs_input, Subs, DefaultSubs,
                           first_key_heuristic, apply_sub_factories,
                           update_sub_lists)
from bluesky.plan_tools import (subscription_wrapper, count, scan,
                                relative_scan, relative_inner_product_scan,
                                outer_product_scan, inner_product_scan,
                                tweak)
from collections import defaultdict
import itertools
from itertools import filterfalse, chain

### Factory functions for generating callbacks


def _figure_name(base_name):
    """Helper to compute figure name

    This takes in a base name an return the name of the figure to use.

    If gs.OVERPLOT, then this is a no-op.  If not gs.OVERPLOT then append '(N)'
    to the end of the string until a non-existing figure is found

    """
    if not gs.OVERPLOT:
        if not plt.fignum_exists(base_name):
            pass
        else:
            for j in itertools.count(1):
                numbered_template = '{} ({})'.format(base_name, j)
                if not plt.fignum_exists(numbered_template):
                    base_name = numbered_template
                    break
    return base_name


def setup_plot(motors):
    """Setup a LivePlot by inspecting motors and gs.

    If motors is empty, use sequence number.
    """
    y_key = gs.PLOT_Y
    if motors:
        x_key = first_key_heuristic(list(motors)[0])
        fig_name = _figure_name('BlueSky {} v {}'.format(y_key, x_key))
        fig = plt.figure(fig_name)
        return LivePlot(y_key, x_key, fig=fig)
    else:
        fig_name = _figure_name('BlueSky: {} v sequence number'.format(y_key))
        fig = plt.figure(fig_name)
        return LivePlot(y_key, fig=fig)


def setup_peakstats(motors):
    "Set up peakstats"
    key = first_key_heuristic(list(motors)[0])
    ps = PeakStats(key, gs.MASTER_DET_FIELD, edge_count=3)
    gs.PS = ps
    return ps


### Counts (p. 140) ###


def ct(num=1, delay=None, time=None, *, md=None):
    """
    Take one or more readings from the global detectors.

    Parameters
    ----------
    num : integer, optional
        number of readings to take; default is 1
    delay : iterable or scalar, optional
        time delay between successive readings; default is 0
    time : float, optional
        applied to any detectors that have a `count_time` setting
    md : dict, optional
        metadata
    """
    subs = {'all': [LiveTable(gs.TABLE_COLS + [gs.PLOT_Y])]}
    if num is not None and num > 1:
        subs['all'].append(setup_plot([]))
    plan = count(gs.DETS, num, delay, md=md)
    if time is not None:
        plan = configure_count_time(plan, time)
    plan = subscription_wrapper(plan, subs)
    ret = yield from plan
    return ret


### Motor Scans (p. 146) ###

def ascan(motor, start, finish, intervals, time=None, *, md=None):
    """
    Scan over one variable in equally spaced steps.

    Parameters
    ----------
    motor : object
        any 'setable' object (motor, temp controller, etc.)
    start : float
        starting position of motor
    finish : float
        ending position of motor
    intervals : int
        number of strides (number of points - 1)
    time : float, optional
        applied to any detectors that have a `count_time` setting
    md : dict, optional
        metadata
    """
    subs = {'all': [LiveTable([motor] + gs.TABLE_COLS + [gs.PLOT_Y]),
                    setup_plot([motor]),
                    setup_peakstats([motor])]}
    plan = scan(gs.DETS, motor, start, finish, 1 + intervals, md=md)
    if time is not None:
        plan = configure_count_time(plan, time)
    plan = subscription_wrapper(plan, subs)
    ret = yield from plan
    return ret


def dscan(motor, start, finish, intervals, time=None, *, md=None):
    """
    Scan over one variable in equally spaced steps relative to current pos.

    Parameters
    ----------
    motor : object
        any 'setable' object (motor, temp controller, etc.)
    start : float
        starting position of motor
    finish : float
        ending position of motor
    intervals : int
        number of strides (number of points - 1)
    time : float, optional
        applied to any detectors that have a `count_time` setting
    md : dict, optional
        metadata
    """
    subs = {'all': [LiveTable([motor] + gs.TABLE_COLS + [gs.PLOT_Y]),
                    setup_plot([motor]),
                    setup_peakstats([motor])]}
    plan = relative_scan(gs.DETS, motor, start, finish, 1 + intervals, md=md)
    if time is not None:
        plan = configure_count_time(plan, time)
    plan = subscription_wrapper(plan, subs)
    ret = yield from plan
    return ret


def mesh(*args, time=None, md=None):
    """
    Scan over a mesh; each motor is on an independent trajectory.

    Parameters
    ----------
    *args
        patterned like (``motor1, start1, stop1, num1,```
                        ``motor2, start2, stop2, num2,,``
                        ``motor3, start3, stop3, num3,,`` ...
                        ``motorN, startN, stopN, numN,``)

        The first motor is the "slowest", the outer loop.
    md : dict, optional
        metadata
    """
    if len(args) % 4 != 0:
        raise ValueError("wrong number of positional arguments")
    motors = []
    shape = []
    extents = []
    for motor, start, stop, num, in chunked(args, 4):
        shape.append(num)
        extents.append([start, stop])

    subs = {'all': [LiveTable(gs.DETS + motors)]}
    if len(motors) == 2:
        # first motor is 'slow' -> Y axis
        ylab, xlab = [first_key_heuristic(m) for m in motors]
        # shape goes in (rr, cc)
        # extents go in (x, y)
        raster = LiveRaster(shape, gs.MASTER_DET_FIELD, xlabel=xlab,
                            ylabel=ylab, extent=list(chain(*extents[::-1])))
        subs['all'].append(raster) 

    # outer_product_scan expects a 'snake' param for all but fist motor
    chunked_args = iter(chunked(args, 4))
    new_args = list(next(chunked_args))
    for chunk in chunked_args:
        new_args.extend(list(chunk) + [False])

    plan = outer_product_scan(gs.DETS, *new_args, md=md)
    if time is not None:
        plan = configure_count_time(plan, time)
    plan = subscription_wrapper(plan, subs)
    ret = yield from plan
    return ret


def a2scan(*args, time=None, md=None):
    """
    Scan over one multi-motor trajectory.

    Parameters
    ----------
    *args
        patterned like (``motor1, start1, stop1,`` ...,
                        ``motorN, startN, stopN, intervals``)
        where 'intervals' in the number of strides (number of points - 1)
        Motors can be any 'setable' object (motor, temp controller, etc.)
    time : float, optional
        applied to any detectors that have a `count_time` setting
    md : dict, optional
        metadata
    """
    if len(args) % 3 != 1:
        raise ValueError("wrong number of positional arguments")
    motors = []
    for motor, start, stop, in chunked(args[:-1], 3):
        motors.append(motor)
        subs = {'all': [LiveTable(gs.DETS + motors),
                        setup_plot(motors),
                        setup_peakstats(motors)]}
    intervals = list(args)[-1]
    num = 1 + intervals
    plan = inner_product_scan(gs.DETS, num, *args[:-1], md=md)
    if time is not None:
        plan = configure_count_time(plan, time)
    plan = subscription_wrapper(plan, subs)
    ret = yield from plan
    return ret

# This implementation works for *all* dimensions, but we follow SPEC naming.
a3scan = a2scan


def d2scan(*args, time=None, md=None):
    """
    Scan over one multi-motor trajectory relative to current positions.

    Parameters
    ----------
    *args
        patterned like (``motor1, start1, stop1,`` ...,
                        ``motorN, startN, stopN, intervals``)
        where 'intervals' in the number of strides (number of points - 1)
        Motors can be any 'setable' object (motor, temp controller, etc.)
    time : float, optional
        applied to any detectors that have a `count_time` setting
    md : dict, optional
        metadata
    """
    if len(args) % 3 != 1:
        raise ValueError("wrong number of positional arguments")
    motors = []
    for motor, start, stop, in chunked(args[:-1], 3):
        motors.append(motor)
        subs = {'all': [LiveTable(gs.DETS + motors),
                        setup_plot(motors),
                        setup_peakstats(motors)]}
    intervals = list(args)[-1]
    num = 1 + intervals
    plan = relative_inner_product_scan(gs.DETS, num, *args[:-1], md=md)
    if time is not None:
        plan = configure_count_time(plan, time)
    plan = subscription_wrapper(plan, subs)
    ret = yield from plan
    return ret

# This implementation works for *all* dimensions, but we follow SPEC naming.
d3scan = d2scan


def th2th(start, finish, intervals, time=None, *, md=None):
    """
    Scan the theta and two-theta motors together.

    gs.TTH_MOTOR scans from ``start`` to ``finish`` while gs.TH_MOTOR scans
    from ``start/2`` to ``finish/2``.

    Parameters
    ----------
    motor : object
        any 'setable' object (motor, temp controller, etc.)
    start : float
        starting position of motor
    finish : float
        ending position of motor
    intervals : int
        number of strides (number of points - 1)
    time : float, optional
        applied to any detectors that have a `count_time` setting
    md : dict, optional
        metadata
    """
    ret = yield from d2scan(gs.TTH_MOTOR, start, finish,
                            gs.TH_MOTOR, start/2, finish/2,
                            intervals, time=time, md=md)
    return ret


def tw(motor, step, time=None, *, md=None):
    """
    Move and motor and read a detector with an interactive prompt.

    ``gs.MASTER_DET`` must be set to a detector, and ``gs.MASTER_DET_FIELD``
    must be set the name of the field to be watched.

    Parameters
    ----------
    target_field : string
        data field whose output is the focus of the adaptive tuning
    motor : Device
    step : float
        initial suggestion for step size
    md : dict, optional
        metadata
    """
    plan = tweak(gs.MASTER_DET, gs.MASTER_DET_FIELD, md=md)
    ret = yield from plan
    if time is not None:
        plan = configure_count_time(plan, time)
    return ret


def configure_count_time(plan, time):
    """
    Preprocessor that sets all devices with a `count_time` to the same time.

    The original setting is stashed and restored at the end.
    """
    devices_seen = set()
    original_times = {}
    ret = None
    try:
        while True:
            msg = plan.send(ret)
            obj = msg.obj
            if obj is not None and obj not in devices_seen:
                devices_seen.add(obj)
                if hasattr(obj, 'count_time'):
                    # TODO Do this with a 'read' Msg once reads can be
                    # marked as belonging to a different event stream (or no
                    # event stream.
                    original_times[obj] = obj.count_time.get()
                    yield Msg('set', obj.count_time, time)
            ret = yield msg
    finally:
        for obj, time in original_times.items():
            yield Msg('set', obj.count_time, time)
    return ret
