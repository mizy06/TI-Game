#!/usr/bin/env python3
"""Guarded three-file replacement from an exact 4.0 or 5.0 project."""
import argparse,hashlib,json,os,tempfile
from pathlib import Path
HERE=Path(__file__).resolve().parent
MANIFEST=json.loads((HERE/'manifest.json').read_text())
def digest(data):return hashlib.sha256(data).hexdigest()
def safe(root,name):
 path=root/name
 for p in [path,*list(path.parents)[:len(Path(name).parts)-1]]:
  if p.is_symlink():raise RuntimeError('Refusing symlink: '+str(p))
 return path
def checked(root,name,expected):
 data=safe(root,name).read_bytes()
 if digest(data)!=expected:raise RuntimeError('Hash mismatch: '+str(root/name))
 return data
def atomic(path,data):
 path.parent.mkdir(parents=True,exist_ok=True)
 with tempfile.NamedTemporaryFile(dir=path.parent,prefix='algorithm-',suffix='.tmp',delete=False) as f:
  tmp=Path(f.name);f.write(data);f.flush();os.fsync(f.fileno())
 try:os.replace(tmp,path)
 finally:
  if tmp.exists():tmp.unlink()
def transact(root,old,new):
 written=[]
 try:
  for name,data in new.items():
   checked(root,name,digest(old[name]));atomic(root/name,data);written.append(name)
 except Exception:
  for name in written:atomic(root/name,old[name])
  raise
def apply(root,check=False,rollback=False):
 root=root.resolve();names=list(MANIFEST['files']);backup=safe(root,MANIFEST['backup_directory'])
 current={name:safe(root,name).read_bytes() for name in names};state={name:digest(data) for name,data in current.items()}
 installed=all(state[name]==MANIFEST['files'][name] for name in names)
 if rollback:
  if not installed:raise RuntimeError('Current files differ from this exact 8.0 package; refusing rollback.')
  identity=json.loads(safe(backup,'source_version.json').read_text())
  version=identity['source_version'];expected=MANIFEST['source_versions'][version]
  restored={name:checked(backup,name,expected[name]) for name in names}
  if check:print('Rollback hashes verified; no files changed.');return
  transact(root,current,restored);print('Restored original '+version+' algorithm files.');return
 if installed:print('8.0 already installed; no files changed.');return
 version=next((v for v,hashes in MANIFEST['source_versions'].items() if all(state[n]==hashes[n] for n in names)),None)
 if not version:raise RuntimeError('Project differs from supported 4.0/5.0/6.0/7.0 hashes; no files changed.')
 updated={name:checked(HERE,name,MANIFEST['files'][name]) for name in names}
 if check:print('Project '+version+' and 8.0 package hashes verified; no files changed.');return
 metadata=json.dumps({'source_version':version},indent=2).encode()
 if (backup/'source_version.json').exists():
  if checked(backup,'source_version.json',digest(metadata))!=metadata:raise RuntimeError('Backup version differs.')
 for name in names:
  if (backup/name).exists():checked(backup,name,state[name])
 for name,data in current.items():
  if not (backup/name).exists():atomic(backup/name,data)
 atomic(backup/'source_version.json',metadata)
 transact(root,current,updated)
 print('Installed 8.0; updated exactly three algorithm files. Backup: '+str(backup))
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--project',required=True,type=Path);p.add_argument('--check',action='store_true');p.add_argument('--rollback',action='store_true');a=p.parse_args()
 try:apply(a.project,a.check,a.rollback)
 except (OSError,RuntimeError,KeyError,ValueError) as e:p.exit(2,str(e)+'\n')
if __name__=='__main__':main()
