#!/usr/bin/env python3
import sys,argparse,types,unittest
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--project',required=True,type=Path);a=p.parse_args();root=a.project.resolve()
sys.path.insert(0,str(root));sys.path.insert(0,str(root/'tests'));sys.path.insert(0,str(Path(__file__).resolve().parent))
try:import serial
except ModuleNotFoundError:
 serial=types.ModuleType('serial')
 def unused(*a,**kw):raise AssertionError('A real serial driver is unavailable; unmocked access is forbidden in these tests.')
 serial.Serial=unused;sys.modules['serial']=serial
 print('Serial import symbol supplied for existing mocks; this does not verify a serial driver.')
suite=unittest.TestSuite()
for name in ('test_unified_solver','test_joint_solver','test_band','test_server','test_precision_pipeline','test_acquisition_calibration'):suite.addTests(unittest.TestLoader().loadTestsFromName(name))
r=unittest.TextTestRunner(verbosity=2).run(suite);raise SystemExit(0 if r.wasSuccessful() else 1)
