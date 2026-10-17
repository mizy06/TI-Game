"""Finals-only Gaussian replay with an identifiable clean periodic source.

The generator's disjoint spectral support is test policy, not information
available to the measurement algorithm. No truth or noise sample is passed
to the solver. Old coherent counterexamples remain available separately.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

REVISION = "finals-identifiable-v2-296"
DESIGN_SEED = 154660146
MATRIX_PATH = Path(__file__).resolve().parents[1] / "matrices" / "finals_296.json"
KINDS = ("sine", "square", "triangle", "sawtooth")


def design_rows():
    """Balanced boundary and continuous holdout coverage, without seed search."""
    rng = np.random.default_rng(DESIGN_SEED)
    rows = []
    # 37 matched weak/strong pairs per family: 296 finals cases. Frequency
    # limits are user policy; voltage/DC/duty limits remain local test policy.
    # Independent permutations break the old amplitude/duty/noise coupling.
    schedules = {}
    for kind in KINDS:
        frequencies = np.r_[100., 100.1, 125., 200., 300., 400., 599.9, 600.,
                            100.+(np.arange(29)+rng.random(29))*500./29]
        schedules[kind] = dict(
            frequency=frequencies[rng.permutation(37)],
            vpp=rng.permutation(np.r_[.3, .8, 1.5, 2., rng.uniform(.3, 2., 33)]),
            duty=rng.permutation(np.r_[.2, .5, .8, rng.uniform(.2, .8, 34)]),
            rise=rng.permutation(np.r_[.005, .037, .1, rng.uniform(.002, .1, 34)]),
            fall=rng.permutation(np.r_[.005, .081, .1, rng.uniform(.002, .1, 34)]),
            dc=rng.permutation(np.r_[.1, .5, .9, rng.uniform(.1, .9, 34)]),
            phase=rng.permutation(np.r_[.0001, .5, .9999, rng.uniform(0., 1., 34)]),
            weak_noise=rng.permutation(np.r_[np.tile([0., .05, .1, .15, .2], 5), rng.uniform(0., .2, 12)]),
            strong_noise=rng.permutation(np.r_[np.tile([.3, .4, .5], 8), rng.uniform(.3, .5, 13)]),
            weak_band=rng.permutation(np.r_[np.tile([5., 7., 10.], 8), rng.uniform(5., 10., 13)]),
            strong_band=rng.permutation(np.r_[np.tile([1.5, 2.25, 3.], 8), rng.uniform(1.5, 3., 13)]))
    for index in range(37):
        for kind in KINDS:
            values = schedules[kind]
            requested_f = float(values['frequency'][index])
            vpp = float(values['vpp'][index])
            # Close *the full record*, including the interference, exactly.
            cycles = round(requested_f * 8192 / 20000.)
            fs = requested_f * 8192 / cycles
            common = dict(kind=kind, frequency=requested_f, vpp=vpp,
                          phase=float(values['phase'][index]), fs=fs, samples=8192,
                          duty=float(values['duty'][index]),
                          rise=float(values['rise'][index]),
                          fall=float(values['fall'][index]),
                          dc_position=float(values['dc'][index]),
                          noise_model="gaussian_notched_replay", replay=cycles,
                          guard_ratio=.18, cycles=cycles, inverted=False,
                          scope=REVISION,
                          tag="expanded_boundary_and_stratified_holdout")
            # Matched weak/strong pairs preserve all source parameters.
            for group in ("weak", "strong"):
                rows.append(dict(common, group=group,
                                 noise=float(values[group+'_noise'][index]),
                                 bandwidth=float(values[group+'_band'][index]),
                                 seed=int(rng.integers(1, 2**32-1))))
    return rows


def noise_samples(row, seed):
    """Real Gaussian Fourier process on the *whole* cyclic ARB record."""
    n, fs, f = int(row["samples"]), float(row["fs"]), float(row["frequency"])
    cycles = n * f / fs
    if abs(cycles - round(cycles)) > 1e-9:
        raise ValueError("identifiable replay must contain integer carrier cycles")
    freq = np.fft.rfftfreq(n, 1. / fs)
    guard = float(row["guard_ratio"]) * f
    # Protect DC and every source harmonic below Nyquist. The same mask is
    # used for every family; no source coefficients enter its construction.
    distance = np.abs(freq - np.rint(freq / f) * f)
    mask = (freq <= float(row["bandwidth"]) * f) & (distance >= guard)
    if np.count_nonzero(mask) < 8:
        raise ValueError("insufficient independent Gaussian noise modes")
    spec = np.fft.rfft(np.random.default_rng(int(seed)).normal(size=n))
    spec[~mask] = 0.
    noise = np.fft.irfft(spec, n=n)
    target = float(row["noise"]) * float(row["vpp"])
    noise *= target / float(np.ptp(noise))
    return noise, dict(model="gaussian_notched_replay", carrier_cycles=round(cycles),
                       guard_hz=guard, active_fourier_bins=int(mask.sum()),
                       maximum_protected_coefficient=float(np.max(np.abs(np.fft.rfft(noise)[~mask]))),
                       period_samples=n, source_noise_support_disjoint=True)


def load_rows():
    # No silent fallback to the old, non-identifiable matrix.
    data = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    if data["revision"] != REVISION or data["rows"] != design_rows():
        raise ValueError("finals matrix differs from its deterministic design")
    return data["rows"]


def publish():
    from tools.capture_store import atomic_json
    from tools.ro_test import _generate_finals_row
    rows = design_rows()
    proofs = []
    for row in rows:
        payload, _, facts = _generate_finals_row(row, row["seed"])
        proofs.append(dict(seed=row["seed"], group=row["group"], kind=row["kind"],
                           adc_sha256=hashlib.sha256(payload).hexdigest(), **facts))
    MATRIX_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(MATRIX_PATH, dict(revision=REVISION, design_seed=DESIGN_SEED,
                                frequency_range_hz=[100.,600.], rows=rows))
    atomic_json(MATRIX_PATH.with_name("finals_identifiable_audit.json"),
                dict(revision=REVISION, cases=len(rows), groups={g:sum(r["group"]==g for r in rows) for g in ("weak","strong")},
                     policy="Finite cyclic Gaussian process with guard bands around DC and all carrier harmonics. Clean source truth unchanged.",
                     proofs=proofs))
    return MATRIX_PATH


if __name__ == "__main__":
    print(publish())
