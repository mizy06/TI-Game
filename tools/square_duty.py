"""Common pulse-width bounds from independently timed square-wave blocks."""
from __future__ import annotations

import numpy as np
from scipy.optimize import linprog


def edge_interval_duty(prepared, frequencies):
    """Bound one duty and frequency with a separate phase for each frame.

    Every rising/falling midpoint must lie between the last definite plateau
    sample and the first sample on the other plateau. Samples near an edge
    remain uncertain. UART gaps and source settings do not enter this model.
    """
    width = 2+len(prepared)
    rows, bounds = [], []
    counts = []
    plateau_fractions = []
    rates = []
    for frame, (payload, meta, _) in enumerate(prepared):
        y = np.frombuffer(payload, dtype='<u2').astype(float)
        low, high = np.percentile(y, [10, 90])
        for _ in range(3):
            middle = (low+high)/2
            low = float(np.median(y[y < middle]))
            high = float(np.median(y[y >= middle]))
        if not np.isfinite(low+high) or high-low < 32:
            return None, dict(accepted=False, reason='plateaus_absent')
        normalized = (y-low)/(high-low)
        definite = np.flatnonzero((normalized < .4) | (normalized > .6))
        state = normalized[definite] > .5
        changes = np.flatnonzero(state[1:] != state[:-1])
        rate = meta.sample_rate_hz_exact or meta.sample_rate_millihz/1000
        rates.append(rate)
        plateau_fractions.append(len(definite)/len(y))
        rising_number = -1
        first_rising = False
        edges = []
        # Before the first rising edge, any falling edge belongs to cycle -1.
        for change in changes:
            rising = not state[change]
            if rising:
                rising_number += 1
                first_rising = True
            cycle = rising_number
            if not first_rising:
                cycle = -1
            lower = definite[change]/rate
            upper = definite[change+1]/rate
            a = np.zeros(width)
            a[0] = lower
            a[2+frame] = -1
            if not rising:
                a[1] = -1
            rows.append(a)
            bounds.append(cycle)
            a = -a
            a[0] = -upper
            rows.append(a)
            bounds.append(-cycle)
            edges.append((lower, upper))
        counts.append(len(edges))
        if len(edges) < 12:
            return None, dict(accepted=False, reason='insufficient_edges')
    limits = [(1., min(rates)/4), (.02, .98)]+[(0., 1.01)]*len(prepared)
    objective = np.zeros(width)
    objective[1] = 1.
    matrix = np.asarray(rows)
    # Asynchronous DAC/ADC clocks and analogue edge settling can move an
    # observed transition by a small fraction of a sample. Find the minimum
    # common timing tolerance before solving the duty interval. A larger
    # inconsistency invalidates this periodic-edge model instead of receiving
    # an arbitrarily relaxed fit.
    slack_objective = np.r_[np.zeros(width), 1.]
    slack_fit = linprog(slack_objective,
        A_ub=np.c_[matrix, -np.ones(len(bounds))], b_ub=bounds,
        bounds=limits+[(0., None)], method='highs')
    if not slack_fit.success:
        return None, dict(accepted=False, reason='inconsistent_edge_intervals', edges_per_frame=counts)
    timing_slack = max(0., float(slack_fit.x[-1]))
    sample_phase = float(slack_fit.x[0])/max(rates)
    slack_samples = timing_slack/sample_phase
    if slack_samples > .02:
        return None, dict(accepted=False, reason='excessive_edge_timing_variation',
                         timing_slack_samples=slack_samples, edges_per_frame=counts)
    bounds = np.asarray(bounds)+1.1*timing_slack+.001*sample_phase
    low_fit = linprog(objective, A_ub=matrix, b_ub=bounds, bounds=limits, method='highs')
    high_fit = linprog(-objective, A_ub=matrix, b_ub=bounds, bounds=limits, method='highs')
    if not low_fit.success or not high_fit.success:
        return None, dict(accepted=False, reason='inconsistent_edge_intervals', edges_per_frame=counts)
    low, high = float(low_fit.x[1]), float(high_fit.x[1])
    duty = (low+high)/2
    objective[1] = 0.
    objective[0] = 1.
    f_low = linprog(objective, A_ub=matrix, b_ub=bounds, bounds=limits, method='highs')
    f_high = linprog(-objective, A_ub=matrix, b_ub=bounds, bounds=limits, method='highs')
    accepted = (high-low)/duty < .0019 and min(plateau_fractions) >= .9
    return (duty if accepted else None), dict(accepted=accepted,
        method='shared_frequency_and_duty_edge_interval_linear_program',
        duty_lower=low, duty_upper=high, relative_interval_width=(high-low)/duty,
        midpoint=duty, edges_per_frame=counts,
        timing_slack_samples=slack_samples,
        plateau_fractions=plateau_fractions,
        frequency_bounds_hz=[float(f_low.x[0]), float(f_high.x[0])],
        reason='bounded_midpoint' if accepted else 'unresolved_edge_intervals')
