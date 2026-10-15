"""Recompute the attached historical RO ADC with the current blind estimator.

Read-only with respect to calibration settings and instruments. Historical
source truth is used only after estimation, for reporting errors.
"""
from __future__ import annotations
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time
from zipfile import ZipFile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import host_waveform as host
from tools.acquisition_calibration import AcquisitionCalibration, fit_calibration
from tools.capture_store import atomic_json
from tools.self_test import Case,evaluate
from tools.waveform_server import build_api


def run(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    api=build_api()
    with ZipFile(archive) as z:
        calpath=next(n for n in z.namelist() if n.endswith('/calibration.json'))
        cal=json.loads(z.read(calpath)); root=calpath.split('/')[0]
        profile=AcquisitionCalibration.from_dict(cal['calibration'])
        observations=[dict(reference=x['reference'],validation=x['validation'],
            meta=host.Metadata(**x['metadata']),payload=z.read(root+'/'+x['capture']+'/samples.adc.bin'))
            for x in cal['captures']]
        refined,checks=fit_calibration(observations)
        atomic_json(destination/'reference_reanalysis.json', {'original':profile.to_dict(),
            'refined':refined.to_dict(),'verification':checks,
            'note':'Offline historical reference ADC only; no new calibration or board measurement.'})
        rows=json.loads(z.read(next(n for n in z.namelist() if n.endswith('/results.json'))))['results']
        comparison=[]
        for row in rows:
            p=z.read(root+'/'+row['capture']+'/samples.adc.bin')
            meta=host.Metadata(**row['wire_metadata'])
            p,meta=profile.apply_exact(p,meta)
            t=time.perf_counter(); line,d=host.analyze(api,p,meta); elapsed=time.perf_counter()-t
            c=Case(**row['case'])
            c=replace(c,fs_hz=meta.sample_rate_millihz/1000,
                vref_v=meta.reference_microvolts/1e6,gain=meta.gain_ppm/1e6,
                vpp_v=row['generator']['truth']['vpp_v'],dc_v=row['generator']['truth']['dc_v'])
            check=evaluate(c,line,d,elapsed,row['generator'])
            comparison.append({'case_id':row['case_id'],'wave':c.wave,
                'sha256':hashlib.sha256(p).hexdigest(),'original':row['result'],
                'current':check,'algorithm':d})
    atomic_json(destination/'historical_ro_comparison.json',{'records':comparison,
        'absolute_accuracy_verified':False,
        'note':'Same historical ADC and original calibration; source setpoints are not an external precision standard.'})
    print(f'Recomputed {len(comparison)} historical RO records')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--output',type=Path,default=Path('validation'))
    a=parser.parse_args();run(a.archive,a.output)
