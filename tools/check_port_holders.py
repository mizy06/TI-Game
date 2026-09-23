import subprocess
import serial.tools.list_ports as lp

print('=== COM ports (pyserial) ===')
for p in lp.comports():
    print(f'  {p.device:>6} | {p.description} | {p.hwid}')

print()
print('=== processes ===')
for name in ('DSLite.exe', 'SerialDebug.exe', 'java.exe', 'SerialMonitor.exe'):
    r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ' + name, '/NH'],
                       capture_output=True)
    t = r.stdout.decode('gbk', 'replace').strip()
    print(f'  {name}: {t.replace(chr(10), " | ")[:200] or "(none)"}')
