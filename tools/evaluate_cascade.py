"""Deterministic host evaluation of independent cascade stages and arbitration."""
import argparse
import ctypes
import json
from collections import Counter
from pathlib import Path
from competition_stress_suite import (WaveformFrame, WaveformResult, Case,
    gate_cases, challenge_cases, generate_samples)

class Trace(ctypes.Structure):
    _fields_ = [(n, ctypes.c_int) for n in ('l1_status','l2_status','l3_status',
                                         'l1_type','l2_type','l3_type')]+[
        ('selected_level',ctypes.c_uint8),('l3_ran',ctypes.c_bool)]

def library(path):
    lib=ctypes.CDLL(str(Path(path).resolve()))
    for name in ('waveform_analyze_frame_with_algorithm','waveform_analyze_frequency',
                 'waveform_analyze_robust','waveform_analyze_cascade'):
        fn=getattr(lib,name)
        fn.argtypes=[ctypes.POINTER(WaveformFrame),ctypes.POINTER(WaveformResult)]
        if name.endswith('with_algorithm'):fn.argtypes.append(ctypes.c_int)
        if name.endswith('cascade'):fn.argtypes.append(ctypes.POINTER(Trace))
        fn.restype=ctypes.c_bool
    return lib

def run_case(lib,case,seed,samples=None):
    if samples is None:
        samples=generate_samples(case,seed)
    array=(ctypes.c_uint16*len(samples))(*samples)
    frame=WaveformFrame(array,len(samples),4095,round(case.sample_rate_hz*1000),
                        case.reference_uv,case.gain_ppm,1,1)
    outputs={}
    for name in ('l1','l2','l3','cascade'):
        r=WaveformResult();tr=Trace()
        fn={'l1':lib.waveform_analyze_frame_with_algorithm,
            'l2':lib.waveform_analyze_frequency,
            'l3':lib.waveform_analyze_robust,
            'cascade':lib.waveform_analyze_cascade}[name]
        ok=bool(fn(ctypes.byref(frame),ctypes.byref(r),
                   3 if name=='l1' else ctypes.byref(tr))) if name in ('l1','cascade') \
              else bool(fn(ctypes.byref(frame),ctypes.byref(r)))
        expected_vpp=(case.vpp_counts*case.reference_uv/4095*1e6/case.gain_ppm)
        freq_error=abs(r.frequency_millihz/1000-case.frequency_hz)/case.frequency_hz
        vpp_error=abs(r.vpp_microvolts-expected_vpp)/expected_vpp
        duty_error=abs(r.duty_ppm/1e6-case.duty)
        success=(ok and r.type==('sine','square','triangle','sawtooth').index(case.kind)
                 and freq_error<=(.01 if 'challenge' in case.group else .005)
                 and vpp_error<=(.12 if 'challenge' in case.group else .08)
                 and (case.kind!='square' or duty_error<=(.03 if 'challenge' in case.group else .02)))
        outputs[name]={'ok':ok,'pass':success,'status':r.status,'type':r.type,
                       'f':r.frequency_millihz,'vpp':r.vpp_microvolts,'duty':r.duty_ppm,
                       'f_err':round(freq_error,5),'v_err':round(vpp_error,5),
                       'd_err':round(duty_error,5)}
        if name=='cascade':outputs[name]['selected']=tr.selected_level
    return outputs

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--library',default='cascade_host.so');
    ap.add_argument('--gate-per-kind',type=int,default=50)
    ap.add_argument('--challenge-per-profile',type=int,default=12)
    ap.add_argument('--output',default=None)
    args=ap.parse_args();lib=library(args.library)
    cases=gate_cases(20260924,args.gate_per_kind)+challenge_cases(20260924,args.challenge_per_profile)
    counts=Counter();failures=[]
    for i,case in enumerate(cases):
        out=run_case(lib,case,20260924+i*7919)
        for level in out: counts[level+'_pass']+=out[level]['pass']
        counts['l3_invoked']+=out['cascade']['selected']==3
        if not out['cascade']['pass']:
            failures.append({'index':i,'kind':case.kind,'freq':case.frequency_hz,
                             'vpp':case.vpp_counts,'duty':case.duty,'group':case.group,
                             'noise':case.noise_ratio,'outputs':out})
    report={'cases':len(cases),'counts':dict(counts),'failures':failures}
    if args.output:Path(args.output).write_text(json.dumps(report,indent=2))
    print(json.dumps({'cases':len(cases),'counts':dict(counts),'failures':len(failures)}))
    for row in failures[:12]:print(json.dumps(row))
    return 0 if not failures else 1
if __name__=='__main__':raise SystemExit(main())
