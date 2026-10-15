#!/usr/bin/env python3
"""Reuse the uploaded 8.0 reference inputs, without copying them into this patch."""
import argparse,hashlib,json,sys,time
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--baseline',required=True,type=Path,help='Unpacked original TI-algorithm-patch-8.0 folder')
p.add_argument('--scope',choices=('historical221','fresh96'),required=True)
p.add_argument('--output',required=True,type=Path)
a=p.parse_args();base=a.baseline.resolve();here=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(base/'checks'));sys.path.insert(0,str(here))
from reference import generate
from tools.unified_solver import estimate
filename,hashname,seed=('matrix_221.json','fingerprints_221.json',20261012) if a.scope=='historical221' else ('matrix_fresh96.json','fingerprints_fresh96.json',72014391)
cases=json.loads((base/'validation'/filename).read_text());hashes=json.loads((base/'validation'/hashname).read_text())
a.output.parent.mkdir(parents=True,exist_ok=True);count=0;correct=0;threshold=0
with a.output.open('w',encoding='utf-8') as log:
    for i,c in enumerate(cases):
        codes,fs,truth=generate(c,seed+i);sha=hashlib.sha256(codes.tobytes()).hexdigest()
        if sha!=hashes[i]:raise ValueError('Original reference input fingerprint differs: '+str(i+1))
        item=dict(id=i+1,case=c,truth=truth,payload_sha256=sha);tic=time.perf_counter()
        try:
            r=estimate(codes,fs,quantization_step=1.)
            errors=[abs(r['frequency_hz']/truth['frequency_hz']-1),abs(r['vpp']*3.3/4095/truth['vpp_v']-1),abs(r['duty_fraction']-truth['duty_fraction'])]
            ok=r['name']==c['kind'];within=ok and errors[0]<.01 and errors[1]<.1 and errors[2]<.025
            item.update(result=r,correct=ok,frequency_vpp_duty_pass=within,errors=errors)
        except Exception as e:item.update(correct=False,frequency_vpp_duty_pass=False,error=type(e).__name__+': '+str(e))
        item['elapsed_s']=time.perf_counter()-tic;count+=1;correct+=item['correct'];threshold+=item['frequency_vpp_duty_pass']
        log.write(json.dumps(item)+'\n');log.flush()
        if (i+1)%20==0:print('completed',i+1,flush=True)
print(dict(cases=count,classification_correct=correct,frequency_vpp_duty_pass=threshold))
