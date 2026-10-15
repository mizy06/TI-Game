import sys,importlib.util,hashlib,tempfile,unittest,argparse,zipfile
from pathlib import Path
from unittest.mock import patch
p=argparse.ArgumentParser();p.add_argument('--zip',required=True,type=Path);args=p.parse_args();sys.argv=[sys.argv[0]]
P=Path(__file__).resolve().parent.parent;spec=importlib.util.spec_from_file_location('installer6',P/'install_patch.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class InstallerTests(unittest.TestCase):
 def make(self,folder,version):
  root=Path(folder)/'project';root.mkdir()
  for name in m.MANIFEST['files']:
   (root/name).parent.mkdir(exist_ok=True,parents=True)
   if version=='4.0.0':
    with zipfile.ZipFile(args.zip) as z:
     entry=next(n for n in z.namelist() if n.endswith('/'+name));data=z.read(entry)
   elif name=='tools/unified_solver.py':data=(P/'checks/baseline5.py').read_bytes()
   else:data=(P/name).read_bytes().replace(b'0x00080000',b'0x00050000').replace(b'"8.0.0"',b'"5.0.0"')
   self.assertEqual(m.digest(data),m.MANIFEST['source_versions'][version][name])
   (root/name).write_bytes(data)
  (root/'calibration.txt').write_text('untouched')
  return root
 def hashes(self,root):return {n:m.digest((root/n).read_bytes()) for n in m.MANIFEST['files']}
 def test_check_install_idempotency_rollback_from_both_versions(self):
  for version in ('4.0.0', '5.0.0'):
   with tempfile.TemporaryDirectory() as d:
    root=self.make(d,version);before=self.hashes(root);m.apply(root,check=True);self.assertEqual(before,self.hashes(root))
    m.apply(root);self.assertEqual(self.hashes(root),m.MANIFEST['files']);m.apply(root)
    m.apply(root,check=True,rollback=True);self.assertEqual(self.hashes(root),m.MANIFEST['files'])
    m.apply(root,rollback=True);self.assertEqual(self.hashes(root),before);self.assertEqual((root/'calibration.txt').read_text(),'untouched')
 def test_modified_source_refused_before_any_write(self):
  with tempfile.TemporaryDirectory() as d:
   root=self.make(d,'4.0.0');(root/'tools/host_waveform.py').write_text('modified');before=self.hashes(root)
   with self.assertRaises(RuntimeError):m.apply(root)
   self.assertEqual(before,self.hashes(root));self.assertFalse((root/m.MANIFEST['backup_directory']).exists())
 def test_target_write_failure_restores_prior_state(self):
  for rollback in (False,True):
   with tempfile.TemporaryDirectory() as d:
    root=self.make(d,'4.0.0')
    if rollback:m.apply(root)
    before=self.hashes(root);real=m.os.replace;failed=False
    def interrupted(src,dst):
     nonlocal failed
     if Path(dst)==root/'tools/host_waveform.py' and not failed:failed=True;raise OSError('injected target failure')
     return real(src,dst)
    with patch.object(m.os,'replace',side_effect=interrupted):
     with self.assertRaises(OSError):m.apply(root,rollback=rollback)
    self.assertEqual(before,self.hashes(root))
if __name__=='__main__':unittest.main(verbosity=2)
