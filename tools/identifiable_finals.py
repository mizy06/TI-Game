"""296-case identifiable Gaussian finals matrix following official ranges.

Each row records the published signal and group ranges with deterministic,
band-limited Gaussian noise for repeatable replay. Narrow guards around DC and
every source harmonic prevent noise from changing a coefficient that is
indistinguishable from the clean periodic source in one ADC record.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

REVISION = "finals-official-identifiable-v2-296"
DESIGN_SEED = 154660146
HARMONIC_GUARD_RATIO = 0.18
MATRIX_PATH = Path(__file__).resolve().parents[1] / "matrices" / "finals_296.json"
KINDS = ("sine", "square", "triangle", "sawtooth")


def design_rows():
    """Balanced boundary and continuous holdout coverage, without seed search."""
    rng = np.random.default_rng(DESIGN_SEED)
    rows = []
    # 37 matched weak/strong pairs per family: 296 finals cases.
    schedules = {}
    for kind in KINDS:
        frequencies = np.r_[100., 100.1, 125., 200., 300., 399.9, 400., 250.,
                            rng.uniform(100., 400., 29)]
        schedules[kind] = dict(
            frequency=frequencies[rng.permutation(37)],
            vpp=rng.permutation(np.r_[.8, 1., 1.2, 1.6, rng.uniform(.8, 1.6, 33)]),
            duty=rng.permutation(np.r_[.2, .5, .8, rng.uniform(.2, .8, 34)]),
            dc=rng.permutation(np.r_[1.4, 1.5, 1.7, 1.8, rng.uniform(1.4, 1.8, 33)]),
            phase=rng.permutation(np.r_[.0001, .5, .9999, rng.uniform(0., 1., 34)]),
            weak_noise=rng.permutation(np.r_[.1, .15, .2, rng.uniform(.1, .2, 34)]),
            strong_noise=rng.permutation(np.r_[.35, .4, .5, rng.uniform(.35, .5, 34)]),
            weak_band=rng.permutation(np.r_[5., 6., 8., rng.uniform(5., 8., 34)]),
            strong_band=rng.permutation(np.r_[1.5, 2.25, 3., rng.uniform(1.5, 3., 34)]))
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
                          rise=0., fall=0., dc_v=float(values['dc'][index]),
                          noise_model="gaussian", replay=cycles,
                          harmonic_guard_ratio=HARMONIC_GUARD_RATIO,
                          cycles=cycles, inverted=False,
                          scope=REVISION,
                          tag="official_ranges_boundary_and_stratified_holdout")
            # Matched weak/strong pairs preserve all source parameters.
            for group in ("weak", "strong"):
                rows.append(dict(common, group=group,
                                 noise=float(values[group+'_noise'][index]),
                                 bandwidth=float(values[group+'_band'][index]),
                                 seed=int(rng.integers(1, 2**32-1))))
    return rows


def noise_samples(row, seed):
    """Band-limited Gaussian Fourier process on the whole cyclic ARB record."""
    n, fs, f = int(row["samples"]), float(row["fs"]), float(row["frequency"])
    cycles = n * f / fs
    if abs(cycles - round(cycles)) > 1e-9:
        raise ValueError("identifiable replay must contain integer carrier cycles")
    freq = np.fft.rfftfreq(n, 1. / fs)
    cutoff = float(row["bandwidth"]) * f
    guard_ratio = float(row.get("harmonic_guard_ratio", HARMONIC_GUARD_RATIO))
    if not 0 < guard_ratio < 0.5:
        raise ValueError("harmonic guard ratio must be between 0 and 0.5")
    harmonic_distance = np.abs(freq - np.rint(freq / f) * f)
    protected = harmonic_distance < guard_ratio * f
    mask = (freq <= cutoff) & ~protected
    spec = np.fft.rfft(np.random.default_rng(int(seed)).normal(size=n))
    spec[~mask] = 0.
    spec[0] = 0.
    noise = np.fft.irfft(spec, n=n)
    target_pp = float(row["noise"]) * float(row["vpp"])
    noise *= (target_pp / 6.0) / float(np.std(noise))
    spectrum = np.fft.rfft(noise)
    protected_coefficients = np.fft.rfft(noise)[protected]
    return noise, dict(model="gaussian", carrier_cycles=round(cycles),
                       bandwidth_hz=cutoff, active_fourier_bins=int(mask.sum()),
                       harmonic_guard_ratio=guard_ratio,
                       harmonic_guard_hz=guard_ratio * f,
                       protected_harmonic_bins=int(protected.sum()),
                       maximum_protected_coefficient=float(np.max(np.abs(protected_coefficients))),
                       maximum_out_of_band_coefficient=float(np.max(np.abs(spectrum[~mask]))),
                       period_samples=n, noise_std_v=float(np.std(noise)),
                       expected_noise_pp_v=target_pp,
                       same_frequency_noise_absent=True)


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
    atomic_json(MATRIX_PATH, dict(
        revision=REVISION, design_seed=DESIGN_SEED,
        frequency_range_hz=[100.,400.], vpp_range_v=[.8,1.6],
        dc_range_v=[1.4,1.8], duty_range_fraction=[.2,.8],
        noise_type="gaussian",
        noise_ratio_by_group={"weak":[.1,.2],"strong":[.35,.5]},
        bandwidth_ratio_by_group={"weak":[5.,8.],"strong":[1.5,3.]},
        gaussian_sigma_formula="noise_ratio * vpp / 6",
        harmonic_guard_ratio=HARMONIC_GUARD_RATIO,
        noise_support="Gaussian Fourier process below published bandwidth, with guard bands around DC and all integer carrier harmonics through Nyquist",
        rows=rows))
    atomic_json(MATRIX_PATH.with_name("finals_identifiable_audit.json"),
                dict(revision=REVISION, cases=len(rows), groups={g:sum(r["group"]==g for r in rows) for g in ("weak","strong")},
                     policy="Finite cyclic zero-mean Gaussian noise below each published bandwidth limit, with ±0.18f guard bands around DC and all source harmonics through Nyquist. A one-channel ADC cannot identify arbitrary noise components at the same frequencies as the clean source harmonics; those modes are excluded so the clean source parameters remain identifiable. Noise standard deviation follows the published Vn,pp≈6σ setting.",
                     proofs=proofs))
    return MATRIX_PATH


if __name__ == "__main__":
    print(publish())
