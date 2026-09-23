"""区分"参数差异"与"时间窗口"两种可能：反复尝试两种打开方式。

方式 A = 探针用法：位置参数 + timeout=0.2
方式 B = 记录器用法：位置参数 + timeout=0.1 + write_timeout=2
每次尝试都记录成败与异常，间隔 1 s，共 5 轮。只读，不发送任何命令。
输出 probe_open_race.txt。
"""

import time
from pathlib import Path

import serial

lines = []
for round_index in range(1, 6):
    for label, kwargs in (
            ('A_probe', dict(port='COM15', baudrate=115200, timeout=0.2,
                             rtscts=False, dsrdtr=False)),
            ('B_recorder', dict(port='COM15', baudrate=115200, timeout=0.1,
                                write_timeout=2, rtscts=False, dsrdtr=False))):
        try:
            handle = serial.Serial(**kwargs)
            try:
                handle.dtr = False
                handle.rts = False
                # 顺带读一下是否已有字节，可判断是否有别的程序在收发。
                pending = handle.in_waiting
            finally:
                handle.close()
            lines.append(f'round={round_index} {label} OK in_waiting={pending}')
        except Exception as error:
            lines.append(f'round={round_index} {label} FAILED '
                         f'{type(error).__name__}: {error}')
        time.sleep(0.4)
    lines.append('---')
    time.sleep(1.0)

target = Path(__file__).resolve().parent / 'probe_open_race.txt'
target.write_text('\n'.join(lines), encoding='utf-8')
print('written', target)
