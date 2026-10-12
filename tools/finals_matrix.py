"""Reproducible finals coverage audit and honest 1-ppm measurements."""
from __future__ import annotations
import argparse, itertools, json, csv, time
from pathlib import Path
import numpy as np
from scipy.signal import sawtooth
from tools import host_waveform as host
from tools.unified_solver import estimate

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


def run(output, limit=None, compare=False):
    output.mkdir(parents=True,exist_ok=True)
    rows=[];audit={}
    for kind in host.NAMES[:4]:
        cases,coverage=covering_rows(kind);rows.extend(cases);audit[kind]=coverage
    # Explicit cases cannot be lost inside pairwise combinations.
    base=dict(kind='square',frequency=200.,vpp=1.5,phase=.003,noise=0.,bandwidth=5.,replay=1,samples=8192,duty=.2,edges=(0.,0.))
    rows.extend([dict(base,tag='locked_unobservable_edges'),dict(base,frequency=237.193,phase=.999999,edges=(.037,.081),duty=.63173,tag='unequal_edges'),
        dict(base,kind='sawtooth',frequency=100.,phase=0.,tag='reset_on_sample'),
        dict(base,kind='sine',frequency=237.193,fs=21317.173,tag='fractional_clock')])
    # Continuous holdout values and independent seeds, beyond the covering grid.
    rng=np.random.default_rng(8173159)
    for kind in host.NAMES[:4]:
        for index in range(20):
            rows.append(dict(kind=kind,tag='continuous_holdout',frequency=float(rng.uniform(70,750)),
                vpp=float(rng.uniform(.08,2.0)),phase=float(rng.random()),noise=float(rng.uniform(.0,.5)),
                bandwidth=float(rng.uniform(1.5,10)),replay=(1,8,0)[index%3],samples=(4096,8192)[index%2],
                dc_position=float(rng.random()),fs=float(rng.uniform(19000,23500)),
                duty=float(rng.uniform(.2,.8)),rise=float(rng.uniform(0,.1)),fall=float(rng.uniform(0,.1))))
    (output/'matrix.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    (output/'coverage.json').write_text(json.dumps(audit,indent=2))
    (output/'counterexamples.json').write_text(json.dumps(counterexamples(),indent=2))
    selected=rows[:limit] if limit else rows
    results=[];legacy=host.load_api(str(host.ROOT/host.LIBRARY_NAME)) if compare else None
    with (output/'results.jsonl').open('w') as log:
        for index,row in enumerate(selected):
            volts,payload,meta=generate(row,20261012+index)
            t=time.perf_counter();line,d=host.analyze(None,payload,meta);elapsed=time.perf_counter()-t
            ok=d['ok'];classification=ok and host.NAMES[d['type']]==row['kind']
            ferror=abs(d['frequency_hz']/row['frequency']-1)*1e6 if ok else None
            aerror=abs(d['vpp_v']/row['vpp']-1)*1e6 if ok else None
            derror=abs(d['duty_fraction']-row.get('duty',.5))*1e6 if ok and row['kind']=='square' else 0.
            # Also check relative duty, to avoid conflating fraction ppm with relative ppm.
            relative_derror=derror/row.get('duty',.5) if row['kind']=='square' else 0.
            entry=dict(id=index+1,case=row,ok=ok,class_correct=classification,
                frequency_error_ppm=ferror,vpp_error_ppm=aerror,duty_fraction_error_ppm=derror,
                duty_relative_error_ppm=relative_derror,elapsed_s=elapsed,
                all_relative_below_1ppm=bool(classification and max(ferror,aerror,relative_derror)<1),
                engineering_pass=bool(classification and ferror<10000 and aerror<100000 and derror<25000),
                output=line,diagnostics=d.get('unified_solver'),rejection=d.get('rejection_reason'))
            entry.update(frequency_absolute_error_hz=abs(d['frequency_hz']-row['frequency']) if ok else None,
                vpp_absolute_error_v=abs(d['vpp_v']-row['vpp']) if ok else None,
                duty_absolute_error_percentage_points=derror*100/1e6 if ok else None)
            entry['all_absolute_below_1e_6']=bool(classification and max(entry['frequency_absolute_error_hz'],
                entry['vpp_absolute_error_v'],entry['duty_absolute_error_percentage_points'])<1e-6)
            if compare:
                t=time.perf_counter();oldline,old=host.analyze_legacy(legacy,payload,meta)
                entry['legacy']=dict(output=oldline,elapsed_s=time.perf_counter()-t,
                    class_correct=bool(old['ok'] and host.NAMES[old['type']]==row['kind']),
                    frequency_error_ppm=abs(old.get('frequency_hz',0)/row['frequency']-1)*1e6,
                    vpp_error_ppm=abs(old.get('vpp_v',0)/row['vpp']-1)*1e6)
            results.append(entry);log.write(json.dumps(entry)+'\n');log.flush()
            if (index+1)%20==0: print(f'{index+1}/{len(selected)} cases',flush=True)
    with (output/'results.csv').open('w',newline='') as stream:
        keys=['id','ok','class_correct','frequency_error_ppm','vpp_error_ppm','duty_fraction_error_ppm','duty_relative_error_ppm','frequency_absolute_error_hz','vpp_absolute_error_v','duty_absolute_error_percentage_points','elapsed_s','all_relative_below_1ppm','all_absolute_below_1e_6','engineering_pass']
        writer=csv.DictWriter(stream,fieldnames=keys);writer.writeheader()
        writer.writerows({k:e[k] for k in keys} for e in results)
    def group_summary(items):
        return dict(cases=len(items),classified=sum(e['class_correct'] for e in items),
            engineering_pass=sum(e['engineering_pass'] for e in items),
            all_relative_below_1ppm=sum(e['all_relative_below_1ppm'] for e in items),
            maximum_elapsed_s=max(e['elapsed_s'] for e in items),
            maximum_frequency_error_ppm=max((e['frequency_error_ppm'] for e in items if e['ok']),default=None),
            maximum_vpp_error_ppm=max((e['vpp_error_ppm'] for e in items if e['ok']),default=None))
    summary=dict(total_planned=len(rows),complete=len(selected)==len(rows),
        all_absolute_below_1e_6=sum(e['all_absolute_below_1e_6'] for e in results),
        legacy_classified=sum(e.get('legacy',{}).get('class_correct',False) for e in results) if compare else None,
        median_new_elapsed_s=float(np.median([e['elapsed_s'] for e in results])),
        median_legacy_elapsed_s=float(np.median([e['legacy']['elapsed_s'] for e in results])) if compare else None,
        actual=group_summary(results),by_wave={k:group_summary([e for e in results if e['case']['kind']==k]) for k in host.NAMES[:4] if any(e['case']['kind']==k for e in results)},
        noise_groups={str(v):group_summary([e for e in results if e['case']['noise']==v]) for v in FACTORS['noise'] if any(e['case']['noise']==v for e in results)},
        shared_range=dict(new=group_summary([e for e in results if 100<=e['case']['frequency']<=400]),
            legacy_classified=sum(e.get('legacy',{}).get('class_correct',False) for e in results if 100<=e['case']['frequency']<=400)),
        holdout=group_summary([e for e in results if e['case'].get('tag')=='continuous_holdout']) if not limit else None,
        coverage_scope='All enumerated categorical pairs per wave, not every higher-order or continuous combination.',
        tolerance_note='Engineering limits are local test policy, not unpublished competition limits.')
    (output/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary,indent=2))
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=Path('validation/finals_4.0'));p.add_argument('--limit',type=int);p.add_argument('--compare',action='store_true')
    args=p.parse_args();run(args.output,args.limit,args.compare)
