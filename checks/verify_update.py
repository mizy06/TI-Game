#!/usr/bin/env python3
"""Reproduce same-input 5.0/6.0 comparisons and the original 296-case regression."""
import argparse,importlib.util,json,time,hashlib,sys
from pathlib import Path
import numpy as np
from reference import generate
HERE=Path(__file__).resolve().parent.parent

def module(path,name):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m);return m

def measure(fun,codes,fs,truth,case):
 tic=time.perf_counter()
 try:
  r=fun(codes,fs,quantization_step=1.);f=r['frequency_hz'];a=r['vpp']*3.3/4095;dc=r['dc']*3.3/4095;d=r['duty_fraction'];correct=r['name']==case['kind']
  errors=[abs(f/truth['frequency_hz']-1)*1e6,abs(a/truth['vpp_v']-1)*1e6,abs(d/truth['duty_fraction']-1)*1e6 if truth['duty_fraction'] else 0.,abs(dc/truth['dc_v']-1)*1e6]
  absolute=[abs(f-truth['frequency_hz']),abs(a-truth['vpp_v']),abs(dc-truth['dc_v']),abs(d-truth['duty_fraction'])*100 if truth['duty_fraction'] else 0.]
  return dict(correct=correct,errors_ppm=errors,competition_1ppm=bool(correct and max(errors[:3])<1),including_dc_1ppm=bool(correct and max(errors)<1),absolute_1e_6=bool(correct and max(absolute)<1e-6),elapsed_s=time.perf_counter()-tic,result=r)
 except Exception as e:return dict(correct=False,error=type(e).__name__+': '+str(e),elapsed_s=time.perf_counter()-tic)

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--project',required=True,type=Path);p.add_argument('--scope',choices=('paired','fresh','regression296'),default='paired');p.add_argument('--output',type=Path);p.add_argument('--limit',type=int);a=p.parse_args()
 root=a.project.resolve();out=a.output or root/'validation'/('algorithm_6.0_'+a.scope);out.mkdir(exist_ok=True,parents=True)
 if a.scope=='regression296':
  sys.path.insert(0,str(root));from tools.self_test import matrix,generate as gen,evaluate;from tools.host_waveform import analyze
  rows=matrix();records=[]
  if a.limit is not None:rows=rows[:a.limit]
  with (out/'results.jsonl').open('w') as log:
   for case in rows:
    tic=time.perf_counter()
    try:
     payload,meta,facts=gen(case);line,details=analyze(None,payload,meta);result=evaluate(case,line,details,time.perf_counter()-tic,facts)
    except Exception as e:result=dict(passed=False,error=str(e))
    item=dict(id=case.id,result=result);records.append(item);log.write(json.dumps(item)+'\n');log.flush()
  print('passed',sum(x['result']['passed'] for x in records),'/',len(records));return
 newer=module(root/'tools/unified_solver.py','patched6');older=module(HERE/'checks/baseline5.py','reference5')
 rows=json.loads((HERE/'validation'/('matrix_221.json' if a.scope=='paired' else 'matrix_fresh96.json')).read_text());seed0=20261012 if a.scope=='paired' else 72014391
 if a.limit is not None:
  if not 1<=a.limit<=len(rows):p.error('limit out of range')
  rows=rows[:a.limit]
 hashes=json.loads((HERE/'validation'/('fingerprints_221.json' if a.scope=='paired' else 'fingerprints_fresh96.json')).read_text())
 records=[]
 with (out/'results.jsonl').open('w') as log:
  for i,row in enumerate(rows):
   codes,fs,truth=generate(row,seed0+i);fingerprint=hashlib.sha256(codes.tobytes()).hexdigest()
   if fingerprint!=hashes[i]:raise RuntimeError('Reference input differs from the shipped fingerprint: '+str(i+1))
   item=dict(id=i+1,case=row,seed=seed0+i,truth=truth,payload_sha256=fingerprint,v5=measure(older.estimate,codes,fs,truth,row),v6=measure(newer.estimate,codes,fs,truth,row))
   records.append(item);log.write(json.dumps(item)+'\n');log.flush()
   if (i+1)%20==0:print('completed',i+1,flush=True)
 for version in ('v5','v6'):
  print(version,'classification',sum(x[version]['correct'] for x in records),'/',len(records),'competition_1ppm',sum(x[version].get('competition_1ppm',False) for x in records))
if __name__=='__main__':main()
