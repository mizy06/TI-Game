"""Separate ppm experiment; does not alter the fixed 296-case RT/RO matrix."""
from __future__ import annotations
import argparse
from dataclasses import replace
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import host_waveform as host
from tools.self_test import Case,generate
from tools.waveform_server import build_api
from tools.capture_store import atomic_json
import numpy as np


def run(destination):
    api=build_api();rows=[]
    for noise in (0.,.20,.50):
        for wave in ('sine','square','triangle','sawtooth'):
            for index in range(8):
                c=Case(wave=wave,frequency_hz=127.3917+index*31.9173,
                    vpp_v=1.517,dc_v=1.673,phase=(.619+index*.1719)%1,
                    duty=.27319+index*.06117,rise=.037,fall=.081,
                    fs_hz=21317.173,vref_v=3.3,gain=1.,noise_pp_ratio=noise,
                    bandwidth_ratio=2.75,seed=73819+index*137)
                p,m,_=generate(c)
                m=replace(m,sample_rate_hz_exact=c.fs_hz)
                t=time.perf_counter();line,d=host.analyze(api,p,m);elapsed=time.perf_counter()-t
                rows.append({'wave':wave,'noise_pp_ratio':noise,'index':index,
                    'frequency_hz':c.frequency_hz,'phase':c.phase,'duty':c.duty,
                    'ok':d['ok'],'frequency_error_ppm':abs(d['frequency_hz']/c.frequency_hz-1)*1e6,
                    'vpp_error_ppm':abs(d['vpp_v']/c.vpp_v-1)*1e6,
                    'duty_error_fraction_ppm':abs(d['duty_fraction']-c.duty)*1e6 if wave=='square' else None,
                    'elapsed_s':elapsed,'output':line})
    summary=[]
    for noise in (0.,.20,.50):
        for wave in ('sine','square','triangle','sawtooth'):
            selected=[x for x in rows if x['wave']==wave and x['noise_pp_ratio']==noise]
            s={'wave':wave,'noise_pp_ratio':noise,'count':len(selected)}
            for key in ('frequency_error_ppm','vpp_error_ppm','duty_error_fraction_ppm'):
                v=[x[key] for x in selected if x[key] is not None]
                if v:s.update({key+'_median':float(np.median(v)),key+'_maximum':max(v)})
            summary.append(s)
    atomic_json(destination,{'scope':'Synthetic ideal affine ADC; exact timebase/reference; no INL, drift or analogue filtering.',
        'absolute_hardware_accuracy_verified':False,'records':rows,'summary':summary,
        'note':'Additional experiment only; high noise and coherent quantization need not reach 1 or 0.1 ppm. Duty is absolute fraction ppm, not relative duty error.'})
    print(f'Saved {len(rows)} precision experiments: {destination}')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('validation/precision_experiment.json'))
    args=parser.parse_args();args.output.parent.mkdir(parents=True,exist_ok=True);run(args.output)
