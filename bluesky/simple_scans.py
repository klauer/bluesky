"""
These "scans" bundle a Message generator with an instance of the RunEngine,
combining two separate concepts -- instructions and execution -- into one
object. This makes the interface less flexible and somewhat less "Pythonic"
but more condensed.

This module is meant to be run in a namespace where several global
variables have been defined. If some variables are left undefined, the
associated scans will be not usable.

    DETS  # list of detectors
    MASTER_DET  # detector to use for tw
    MASTER_DET_FIELD  # detector field to use for tw
    H_MOTOR
    K_MOTOR
    L_MOTOR
    TH_MOTOR
    TTH_MOTOR
    TEMP_CONTROLLER

Page numbers in the code comments refer to the SPEC manual at
http://www.certif.com/downloads/css_docs/spec_man.pdf
"""
from inspect import signature
import matplotlib.pyplot as plt
from bluesky import scans
from bluesky.callbacks import LiveTable, LivePlot, LiveRaster, _get_obj_fields
from bluesky.scientific_callbacks import PeakStats
from boltons.iterutils import chunked
from bluesky.global_state import gs
from bluesky.utils import normalize_subs_input, Subs, DefaultSubs
from collections import defaultdict
from itertools import filterfalse, chain

# ## Factory functions acting a shim between scans and callbacks ###


def table_from_motors(scan):
    "Setup a LiveTable by inspecting a scan and gs."
    # > 1 motor
    return LiveTable(scan.motors + gs.TABLE_COLS)


def table_from_motor(scan):
    "Setup a LiveTable by inspecting a scan and gs."
    # 1 motor
    return LiveTable([scan.motor] + gs.TABLE_COLS)


def table_gs_only(scan):
    "Setup a LiveTable by inspecting a scan and gs."
    # no motors
    return LiveTable(gs.TABLE_COLS)


def plot_first_motor(scan):
    "Setup a LivePlot by inspecting a scan and gs."
    fig = None
    if not gs.OVERPLOT:
        fig = plt.figure()
    return LivePlot(gs.PLOT_Y, scan.motors[0]._name, fig=fig)


def plot_motor(scan):
    "Setup a LivePlot by inspecting a scan and gs."
    fig = None
    if not gs.OVERPLOT:
        fig = plt.figure()
    return LivePlot(gs.PLOT_Y, scan.motor._name, fig=fig)


def raster(scan):
    "Set up a LiveRaster by inspect a scan and gs."
    if len(scan.shape) != 2:
        return None
    # first motor is 'slow' -> Y axis
    ylab, xlab = _get_obj_fields(scan.motors)
    # shape goes in (rr, cc)
    # extents go in (x, y)
    return LiveRaster(scan.shape, gs.MASTER_DET_FIELD, xlabel=xlab,
                      ylabel=ylab, extent=list(chain(*scan.extents[::-1])))


def peakstats_first_motor(scan):
    "Set up peakstats"
    ps = PeakStats(_get_obj_fields([scan.motors[0]])[0],
                   gs.MASTER_DET_FIELD, edge_count=3)
    gs.PS = ps
    return ps


def peakstats(scan):
    "Set up peakstats"
    ps = PeakStats(_get_obj_fields([scan.motor])[0],
                   gs.MASTER_DET_FIELD, edge_count=3)
    gs.PS = ps
    return ps


class _BundledScan:
    default_subs = DefaultSubs({})
    default_sub_factories = DefaultSubs({})
    # These are set to the defaults at init time.
    subs = Subs({})
    sub_factories = Subs({})

    def __init__(self):
        # subs and sub_factories can be set individually per instance
        self.subs = dict(self.default_subs)
        self.sub_factories = dict(self.default_sub_factories)
        self.params = list(signature(self.scan_class).parameters.keys())
        self.configuration = {}
        self.flyers = []

    def __call__(self, *args, subs=None, sub_factories=None, time=None,
                 **kwargs):
        scan_kwargs = dict()
        # Any kwargs valid for the scan go to the scan, not the RE.
        for k, v in kwargs.copy().items():
            if k in self.params:
                scan_kwargs[k] = kwargs.pop(k)
        from bluesky.global_state import gs

        RE_params = list(signature(gs.RE.__call__).parameters.keys())
        if set(RE_params) & set(self.params):
            raise AssertionError("The names of the scan's arguments clash "
                                 "the RunEngine arguments. Use different "
                                 "names. Avoid: {0}".format(RE_params))

        self.scan = self.scan_class(gs.DETS, *args, **scan_kwargs)
        # Combine subs passed as args and subs set up in subs attribute.
        _subs = defaultdict(list)
        _update_lists(_subs, normalize_subs_input(subs))
        _update_lists(_subs, normalize_subs_input(self.subs))
        # Create a sub from each sub_factory.
        _update_lists(_subs, _run_factories(sub_factories, self.scan))
        _update_lists(_subs, _run_factories(self.sub_factories, self.scan))

        # Set up scan attributes.
        self.scan.configuration = self.configuration
        self.scan.flyers = self.flyers

        # Any remainging kwargs go the RE. To be safe, no args are passed
        # to RE; RE args effectively become keyword-only arguments.
        return gs.RE(self.scan, _subs, exposure_time=time, **kwargs)


def _update_lists(out, inp):
    """Extends dictionary `out` lists with those in `inp`

    Assumes dictionaries where all values are lists
    """
    for k, v in inp.items():
        try:
            out[k].extend(v)
        except KeyError:
            out[k] = list(v)


def _run_factories(factories, scan):
    '''Run sub factory functions for a scan

    Factory functions should return lists, which will be added onto the
    subscription key (e.g., 'all' or 'start') specified in the factory
    definition.

    If the factory function returns None, the list will not be modified.
    '''
    factories = normalize_subs_input(factories)
    out = {k: list(filterfalse(lambda x: x is None,
                               (sf(scan) for sf in v)))
           for k, v in factories.items()}
    gs._SECRET_STASH = out
    return out

# ## Mid-level base classes ###

# These are responsible for popping off the time arg and adjusting the
# interval. SPEC counts "bonds;" idiomatic Python counts "sites."


class _OuterProductScan(_BundledScan):
    default_sub_factories = DefaultSubs({'all': [table_from_motors]})

    def __call__(self, *args, time=None, subs=None, **kwargs):
        args = list(args)
        if len(args) % 4 == 1:
            if time is None:
                time = args.pop(-1)
            else:
                raise ValueError("wrong number of positional arguments")
        original_times = _set_acquire_time(time)
        for i, _ in enumerate(chunked(list(args), 4)):
            # intervals -> intervals + 1
            args[4*i + 3] += 1
            # never snake; SPEC doesn't know how
            if i != 0:
                args.insert(4*(i + 1), False)
        result = super().__call__(*args, subs=subs, time=time, **kwargs)
        _unset_acquire_time(original_times)
        return result


class _InnerProductScan(_BundledScan):
    default_sub_factories = DefaultSubs(
        {'all': [table_from_motors, plot_first_motor,
                 peakstats_first_motor]})

    def __call__(self, *args, time=None, subs=None, **kwargs):
        args = list(args)
        if len(args) % 3 == 2:
            if time is None:
                time = args.pop(-1)
            else:
                raise ValueError("wrong number of positional arguments")
        intervals = args.pop(-1) + 1
        original_times = _set_acquire_time(time)
        result = super().__call__(intervals, *args, subs=subs, **kwargs)
        _unset_acquire_time(original_times)
        return result


class _StepScan(_BundledScan):
    default_sub_factories = DefaultSubs(
        {'all': [table_from_motor, plot_motor,
                 peakstats]})

    def __call__(self, motor, start, finish, intervals, time=None,
                 subs=None, **kwargs):
        original_times = _set_acquire_time(time)
        result = super().__call__(motor, start, finish, intervals + 1,
                                  subs=subs, time=time, **kwargs)
        _unset_acquire_time(original_times)
        return result


class _HardcodedMotorStepScan(_BundledScan):
    # Subclasses must define self.motor as a property.
    default_sub_factories = DefaultSubs(
        {'all': [table_from_motor, plot_motor]})

    def __call__(self, start, finish, intervals, time=None, subs=None,
                 **kwargs):
        original_times = _set_acquire_time(time)
        result = super().__call__(self.motor, start, finish,
                                  intervals + 1, subs=subs, **kwargs)
        _unset_acquire_time(original_times)
        return result


### Counts (p. 140) ###


class Count(_BundledScan):
    "ct"
    scan_class = scans.Count
    default_sub_factories = DefaultSubs({'all': [table_gs_only]})

    def __call__(self, time=None, subs=None, **kwargs):
        original_times = _set_acquire_time(time)
        result = super().__call__(subs=subs, **kwargs)
        _unset_acquire_time(original_times)
        return result


### Motor Scans (p. 146) ###


class AbsScan(_StepScan):
    "ascan"
    scan_class = scans.AbsScan


class OuterProductAbsScan(_OuterProductScan):
    "mesh"
    default_sub_factories = DefaultSubs({'all': [table_from_motors, raster]})
    scan_class = scans.OuterProductAbsScan


class InnerProductAbsScan(_InnerProductScan):
    "a2scan, a3scan, etc."
    scan_class = scans.InnerProductAbsScan


class DeltaScan(_StepScan):
    "dscan (also known as lup)"
    scan_class = scans.DeltaScan


class InnerProductDeltaScan(_InnerProductScan):
    "d2scan, d3scan, etc."
    scan_class = scans.InnerProductDeltaScan


class ThetaTwoThetaScan(_InnerProductScan):
    "th2th"
    scan_class = scans.InnerProductDeltaScan

    def __call__(self, start, finish, intervals, time=None, **kwargs):
        TTH_MOTOR = gs.TTH_MOTOR
        TH_MOTOR = gs.TH_MOTOR
        original_times = _set_acquire_time(time)
        result = super().__call__(TTH_MOTOR, start, finish,
                                  TH_MOTOR, start/2, finish/2,
                                  intervals, time, **kwargs)
        _unset_acquire_time(original_times)


### Temperature Scans (p. 148) ###


class _TemperatureScan(_HardcodedMotorStepScan):

    def __call__(self, start, finish, intervals, time=None, sleep=0,
                 **kwargs):
        self._sleep = sleep
        original_times = _set_acquire_time(time)
        self.motor.settle_time = sleep
        result = super().__call__(start, finish, intervals + 1, **kwargs)
        _unset_acquire_time(original_times)
        return result

    @property
    def motor(self):
        from bluesky.global_state import gs
        return gs.TEMP_CONTROLLER


class AbsTemperatureScan(_TemperatureScan):
    "tscan"
    scan_class = scans.AbsScan


class DeltaTemperatureScan(_TemperatureScan):
    "dtscan"
    scan_class = scans.DeltaScan


### Basic Reciprocal Space Scans (p. 147) ###


class HScan(_HardcodedMotorStepScan):
    "hscan"
    scan_class = scans.AbsScan

    @property
    def motor(self):
        from bluesky.global_state import gs
        return gs.H_MOTOR


class KScan(_HardcodedMotorStepScan):
    "kscan"
    scan_class = scans.AbsScan

    @property
    def motor(self):
        from bluesky.global_state import gs
        return gs.K_MOTOR


class LScan(_HardcodedMotorStepScan):
    "lscan"
    scan_class = scans.AbsScan

    @property
    def motor(self):
        from bluesky.global_state import gs
        return gs.L_MOTOR


class OuterProductHKLScan(_BundledScan):
    "hklmesh"
    scan_class = scans.OuterProductAbsScan

    def __call__(self, Q1, start1, finish1, intervals1, Q2, start2, finish2,
                 intervals2, time=None, **kwargs):
        # To be clear, like all other functions in this module, this
        # eye-gouging API is for compatbility with SPEC, not the author's
        # idea of good Python code.
        from bluesky.global_state import gs
        H_MOTOR = gs.H_MOTOR
        K_MOTOR = gs.K_MOTOR
        L_MOTOR = gs.L_MOTOR
        original_times = _set_acquire_time(time)
        _motor_mapping = {'H': H_MOTOR, 'K': K_MOTOR, 'L': L_MOTOR}
        motor1 = _motor_mapping[Q1]
        motor2 = _motor_mapping[Q2]
        # Note that intervals + 1 is handled in the base class.
        result = super().__call__(motor1, start1, finish1, intervals1,
                                  motor2, start2, finish2, intervals2,
                                  **kwargs)
        _unset_acquire_time(original_times)
        return result


class InnerProductHKLScan(_BundledScan):
    "hklscan"
    scan_class = scans.InnerProductAbsScan

    def __call__(self, start_h, finish_h, start_k, finish_k, start_l,
                 finish_l, intervals, time=None, **kwargs):
        from bluesky.global_state import gs
        H_MOTOR = gs.H_MOTOR
        K_MOTOR = gs.K_MOTOR
        L_MOTOR = gs.L_MOTOR
        original_times = _set_acquire_time(time)
        result = super().__call__(intervals, start_h, finish_h, start_k,
                                  finish_k, start_l, finish_l, **kwargs)
        _unset_acquire_time(original_times)


### Special Reciprocal Space Scans ###

# TODO:
# klradial
# hlradial
# hkradial
# klcircle
# hlcircle
# hkcircle


class Tweak(_BundledScan):
    "tw"
    scan_class = scans.Tweak

    def __call__(motor, step, **kwargs):
        from bluesky.global_state import gs
        MASTER_DET = gs.MASTER_DET
        MASTER_DET_FIELD = gs.MASTER_DET_FIELD
        return super().__call__(MASTER_DET, MASTER_DET_FIELD, motor,
                                step, **kwargs)


def _set_acquire_time(time):
    from bluesky.global_state import gs
    if time is None:
        time = gs.COUNT_TIME
    original_times = {}
    for det in gs.DETS:
        if hasattr(det, 'count_time'):
            original_times[det] = det.count_time
            det.count_time = time
    return original_times


def _unset_acquire_time(original_times):
    for det, time in original_times.items():
        det.count_time = time
