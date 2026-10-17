"""Independent simulated instruments; never imported by the measurement code."""
import numpy as np
from scipy.signal import lfilter, sawtooth
from tools.vpp_reference import CodeCalibration, SynchronousObservation, calibrate

FS = 21317.173
NOISE_PATH = np.array([.63, .24, -.07, .03, -.01])
SIGNAL_PATH = np.array([.88, .15, -.03])


def analog_code(v, channel):
    linear = np.asarray(v)*4095/3.3
    gain, offset, inl = (1.007, -4.2, 1.4) if channel == 'main' else (.994, 3.1, -.9)
    return gain*linear+offset+inl*np.sin(2*np.pi*linear/257)


def adc(v, channel):
    c = np.rint(analog_code(v, channel))
    if np.any((c <= 0) | (c >= 4095)):
        raise ValueError('simulator input clips; do not silently clamp')
    return c.astype('<u2')


def dc_table(channel, rng):
    # Independent DC sweep with sub-code uniform dither to measure mean codes.
    # Calibration set contains no runtime waveform, case ID, frequency or Vpp.
    volts = np.linspace(.02, 3.27, 2049)
    means = np.mean(np.rint(analog_code(volts, channel)[:, None]+
        rng.uniform(-.5, .5, (len(volts), 1024))), axis=1)
    return CodeCalibration(tuple(means), tuple(volts), 'simulated independent DC sweep')


def make_calibration(seed=73199):
    rng = np.random.default_rng(seed)
    m, r = dc_table('main', rng), dc_table('reference', rng)
    reference = rng.uniform(-.65, .65, 24000)
    standard = rng.uniform(-.9, .9, 24000)
    return calibrate(sample_rate_hz=FS, main_adc=m, reference_adc=r,
        noise_main_codes=adc(1.65+lfilter(NOISE_PATH, [1.], reference), 'main'),
        noise_reference_codes=adc(1.6+reference, 'reference'),
        standard_main_codes=adc(1.65+lfilter(SIGNAL_PATH, [1.], standard), 'main'),
        standard_input_v=standard, main_channel='main', reference_channel='reference',
        frontend_id='simulator-A', clock_id='shared-simulator-clock',
        source_off_evidence='simulation: source physically absent from calibration',
        standard_evidence='simulation: independent synchronous ideal reference digitizer')


def signal(kind, p, duty=.5, rise=.037, fall=.081):
    p = np.asarray(p) % 1
    if kind == 0:
        return np.sin(2*np.pi*p)
    if kind == 2:
        return sawtooth(2*np.pi*(p+.25), .5)
    if kind == 3:
        return -sawtooth(2*np.pi*p)
    # Independent polygon, rather than the runtime's branch expression.
    return np.interp(p, [-rise/2, rise/2, duty-fall/2, duty+fall/2,
        1-rise/2, 1+rise/2], [-1., 1., 1., -1., -1., 1.])


def measurement(kind, *, seed=7, n=8192, frequency=237.193, vpp=1.517,
                phase=.619, duty=.63173, noise_ratio=.4, distortion=0., dc_position=.5):
    rng = np.random.default_rng(seed)
    p = np.arange(n)*frequency/FS+phase
    wave = signal(kind, p, duty)
    if distortion:
        wave += distortion*np.sin(4*np.pi*p+.41)
    # Dense independent truth is consumed by the evaluator only.
    q = np.arange(262144)/262144
    full = signal(kind, q, duty)+distortion*np.sin(4*np.pi*q+.41)
    half = (full.max()-full.min())/2
    wave = (wave-(full.max()+full.min())/2)/half
    source = vpp/2*wave
    # Includes an EXACTLY coherent interferer, a subharmonic, and fresh noise.
    noise = (.8*np.sin(2*np.pi*p+.37)+.3*np.cos(4*np.pi*p+.11)+
        .2*np.sin(2*np.pi*p/2.37)+lfilter([.2, .3, .3, .2], [1.], rng.normal(0, .1, n)))
    noise *= noise_ratio*vpp/np.ptp(lfilter(NOISE_PATH, [1.], noise)[64:])
    ac = lfilter(SIGNAL_PATH, [1.], source)+lfilter(NOISE_PATH, [1.], noise)
    lo, hi = .08-float(ac.min()), 3.24-float(ac.max())
    if hi < lo:
        raise ValueError('requested analog envelope cannot fit the ADC')
    dc = lo+dc_position*(hi-lo)
    main = adc(dc+ac, 'main')
    reference = adc(1.6+noise, 'reference')
    obs = SynchronousObservation(reference, FS, 'shared-simulator-clock', 17807, 17807,
        'main', 'reference', 'simulator-A', True)
    return main, obs, vpp
