"""Independent waveform fixtures and identifiability counterexamples for tests."""
from __future__ import annotations
import itertools
import numpy as np
from scipy.signal import sawtooth
from tools import host_waveform as host

FACTORS = dict(frequency=(40.,100.,237.193,400.,900.), vpp=(.08,1.5,2.0),
    phase=(0.,.173,.5,.999999), noise=(0.,.2,.3,.4,.5), bandwidth=(1.5,2.25,3.,5.,10.),
    replay=(1,8,0), samples=(4096,8192), dc_position=(0.,.5,1.),
    fs=(20000.,20244.764,21317.173))
SQUARE = dict(duty=(.2,.5,.8), rise=(0.,.001,.037,.1), fall=(0.,.001,.081,.1))


def pair_set(row, names):
    return {(a,row[a],b,row[b]) for a,b in itertools.combinations(names,2)}


def covering_rows(kind, seed=20261012):
    factors = {**FACTORS, **(SQUARE if kind=='square' else {})}
    names = list(factors)
    universe = {(a,x,b,y) for a,b in itertools.combinations(names,2)
                for x in factors[a] for y in factors[b]}
    rng = np.random.default_rng(seed+host.NAMES.index(kind))
    pool = [{name: values[int(rng.integers(len(values)))] for name,values in factors.items()}
            for _ in range(4500)]
    sets = [pair_set(row,names) for row in pool]
    uncovered = set(universe); rows=[]
    while uncovered:
        index = max(range(len(pool)), key=lambda i: len(sets[i] & uncovered))
        if not sets[index] & uncovered:
            # Construct a witness for an uncovered pair; no silent coverage gap.
            a,x,b,y = min(uncovered,key=repr)
            row = {name:values[0] for name,values in factors.items()}
            row.update({a:x,b:y});pool.append(row);sets.append(pair_set(row,names))
            index=len(pool)-1
        rows.append(dict(kind=kind,**pool[index]))
        uncovered -= sets[index]
    observed=set().union(*(pair_set(r,names) for r in rows))
    missing=universe-observed
    assert not missing
    return rows,dict(rows=len(rows),required_pairs=len(universe),covered_pairs=len(observed),missing_pairs=[])


def waveform(kind,p,duty,rise,fall):
    p=np.asarray(p)%1
    if kind=='sine': return np.sin(2*np.pi*p)
    if kind=='triangle': return sawtooth(2*np.pi*(p+.25),width=.5)
    if kind=='sawtooth': return -sawtooth(2*np.pi*p)
    # Independently constructed piecewise polygon, edge midpoint definition.
    r=max(rise,1e-12);f=max(fall,1e-12)
    knots=[-r/2,r/2,duty-f/2,duty+f/2,1-r/2,1+r/2]
    heights=[-1.,1.,1.,-1.,-1.,1.]
    return np.interp(p,knots,heights)


def generate(row, seed):
    fs=row.get('fs',20000.);n=row['samples'];f=row['frequency']
    duty=row.get('duty',.5);rise,fall=row.get('edges',(row.get('rise',0.),row.get('fall',0.)))
    p=np.arange(n)*f/fs+row['phase']
    clean=waveform(row['kind'],p,duty,rise,fall)
    noise=np.zeros(n)
    rng=np.random.default_rng(seed)
    if row['noise']:
        if row['replay']:
            loop=row['replay'];size=4096
            table=rng.normal(size=size);spec=np.fft.rfft(table)
            spec[np.fft.rfftfreq(size,1/size)>row['bandwidth']*loop]=0
            spec[0]=0;table=np.fft.irfft(spec,n=size)
            table*=row['noise']*row['vpp']/np.ptp(table)
            noise=np.interp((p/loop)%1,np.arange(size+1)/size,np.r_[table,table[0]])
        else:
            spec=np.fft.rfft(rng.normal(size=n))
            spec[np.fft.rfftfreq(n,1/fs)>f*row['bandwidth']]=0
            spec[0]=0;noise=np.fft.irfft(spec,n=n)
            noise*=row['noise']*row['vpp']/np.ptp(noise)
    baseline=.5*row['vpp']*clean+noise
    # Move the whole generated waveform; never reduce the requested noise.
    room=3.3-.08-float(np.ptp(baseline))
    assert room>=0, 'requested envelope exceeds ADC'
    dc=.04-float(baseline.min())+row.get('dc_position',.5)*room
    volts=dc+baseline
    codes=np.rint(volts*4095/3.3)
    assert np.all((codes>=0)&(codes<=4095)), 'generator must not hide clipping'
    payload=codes.astype('<u2').tobytes()
    meta=host.Metadata(seed,round(fs*1000),3300000,1000000,4095,7,host.fnv1a(payload),
        sample_rate_hz_exact=fs,payload_bytes=len(payload),protocol_version=3 if n==8192 else 2)
    return volts,payload,meta


def counterexamples():
    fs=20000.;f=200.;p=np.arange(8192)*f/fs+.003
    sine=np.sin(2*np.pi*p)
    signal_a=1.65+.75*sine;delta=15e-6
    signal_b=1.65+(.75+delta/2)*sine
    noise_a=delta/2*sine
    assert np.array_equal(np.rint((signal_a+noise_a)*4095/3.3),np.rint(signal_b*4095/3.3))
    square_a=waveform('square',p,.2,0,0)
    square_b=waveform('square',p,.201,0,0)
    assert np.array_equal(square_a,square_b)
    ratio=1+20e-6
    assert np.allclose(np.arange(8192)*f/fs,np.arange(8192)*(f*ratio)/(fs*ratio),rtol=0,atol=1e-12)
    return dict(coherent_noise=dict(identical_adc=True,vpp_world_a=1.5,vpp_world_b=1.5+delta,
            relative_separation_ppm=delta/1.5*1e6),
        square_locked_sampling=dict(identical_samples=True,duty_world_a=.2,duty_world_b=.201,
            absolute_separation_fraction_ppm=(.201-.2)*1e6),
        clock_scale=dict(identical_sample_phases=True,frequency_separation_ppm=(ratio-1)*1e6))
