"""Deterministic noise families for the production three-stage C cascade.

The first five families model bounded Gaussian interference within the manual's
amplitude envelope. Others are engineering challenges, not organizer promises.
Finite 4096-sample traces are normalized to the requested peak-to-peak noise.
"""
import argparse
import json
import math
import random
from collections import Counter
from dataclasses import replace
from pathlib import Path
from competition_stress_suite import make_case, generate_samples
from evaluate_cascade import library, run_case

GAUSSIAN = ('white','lowpass_5f','lowpass_2f','bandpass','highband','looped')
CHALLENGE = ('spikes','narrowband','drift','harmonics','am','clipping')

def filtered(raw,alpha):
    out=[]; state=0.0
    for v in raw:
        state+=alpha*(v-state);out.append(state)
    return out

def noise_samples(case,profile,seed):
    rng=random.Random(seed)
    n=4096; f=case.frequency_hz; fs=case.sample_rate_hz
    raw=[rng.gauss(0,1) for _ in range(n)]
    if profile in ('lowpass_5f','lowpass_2f'):
        cutoff=(5.25 if profile=='lowpass_5f' else 2.5)*f
        raw=filtered(raw,1-math.exp(-2*math.pi*cutoff/fs))
    elif profile=='bandpass':
        low=filtered(raw,1-math.exp(-2*math.pi*1.6*f/fs))
        high=filtered(raw,1-math.exp(-2*math.pi*5.4*f/fs))
        raw=[a-b for a,b in zip(high,low)]
    elif profile=='highband':
        low=filtered(raw,1-math.exp(-2*math.pi*3*f/fs))
        raw=[a-b for a,b in zip(raw,low)]
    elif profile=='looped':
        raw=raw[:512]*8
    elif profile=='spikes':
        for i in range(19,n,211):raw[i]+=rng.choice([-7,7])
    elif profile=='narrowband':
        raw=[.35*v+math.sin(2*math.pi*3.1*f*i/fs+.9)
             for i,v in enumerate(raw)]
    elif profile=='drift':
        raw=[v+.9*math.sin(2*math.pi*2*i/fs) for i,v in enumerate(raw)]
    elif profile=='harmonics':
        raw=[v+.6*math.sin(2*math.pi*3*f*i/fs+.47)
             for i,v in enumerate(raw)]
    elif profile=='am':
        raw=[v*(1+.8*math.sin(2*math.pi*5*i/fs)) for i,v in enumerate(raw)]
    elif profile=='clipping':
        raw=[max(-.8,min(.8,v)) for v in raw]
    low=min(raw);span=max(raw)-low
    return [((v-low)/span-.5)*case.noise_ratio*case.vpp_counts for v in raw]

def evaluate(lib,profiles,seed):
    counts=Counter();failures=[];index=0
    for profile in profiles:
        for ratio in (.2,.5):
            for kind in ('sine','square','triangle','sawtooth'):
                for frequency in (100.0,250.137,399.863):
                    for vpp in (96.0,640.0,1800.0):
                        for duty in ((.2,.5,.8) if kind=='square' else (.5,)):
                            index+=1
                            group=('preliminary_gate' if ratio==.2 else 'competition_challenge')
                            case=make_case(group,'noise_matrix',profile,kind,
                                frequency,vpp,duty,.04,.07,ratio,'white',20000.,
                                ((index*37)%101)/101.,'middle',3300000,1000000)
                            base=generate_samples(replace(case,noise_ratio=0),seed+index*17)
                            noise=noise_samples(case,profile,seed+index*917)
                            samples=[max(0,min(4095,round(v+n))) for v,n in zip(base,noise)]
                            out=run_case(lib,case,seed+index,samples)
                            key=f'{profile}/{int(ratio*100)}'
                            counts[key+'_total']+=1
                            for stage in ('l1','l2','l3','cascade'):
                                counts[key+'_'+stage+'_pass']+=int(out[stage]['pass'])
                            counts[key+'_l3_run']+=out['cascade']['selected']==3
                            if not out['cascade']['pass']:
                                failures.append({'index':index,'profile':profile,'ratio':ratio,
                                    'kind':kind,'freq':frequency,'vpp_counts':vpp,
                                    'duty':duty,'result':out})
    return {'cases':index,'counts':dict(counts),'failures':failures,
            'seed':seed,'gaussian_envelope_profiles':list(GAUSSIAN),
            'extra_challenge_profiles':list(CHALLENGE)}

def main():
    p=argparse.ArgumentParser();p.add_argument('--library',default='tests/waveform_analyzer_host.so')
    p.add_argument('--output',default='tests/results/noise_cascade_matrix.json')
    p.add_argument('--profiles',nargs='+',choices=GAUSSIAN+CHALLENGE,
                   default=GAUSSIAN+CHALLENGE)
    p.add_argument('--seed',type=int,default=20260925)
    a=p.parse_args();report=evaluate(library(a.library),a.profiles,a.seed)
    Path(a.output).parent.mkdir(parents=True,exist_ok=True)
    Path(a.output).write_text(json.dumps(report,ensure_ascii=False,indent=2))
    summary={p:{r:{k:report['counts'].get(f'{p}/{r}_{k}_pass',0)
                       for k in ('l1','l2','l3','cascade')}
                for r in (20,50)} for p in a.profiles}
    print(json.dumps({'cases':report['cases'],'cases_per_profile_ratio':54,
                      'summary':summary,'failures':len(report['failures'])}))
    # Challenge failures are diagnostic; the bounded Gaussian families gate.
    return int(any(not row["result"]["cascade"]["pass"] and
                   row["profile"] in GAUSSIAN for row in report["failures"]))
if __name__=='__main__':raise SystemExit(main())
