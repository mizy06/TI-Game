"""Independent voltage transfers for the amplitude path and ARB stimulus.

Dynamic sine references provide both transfers in dynamic_amplitude_calibration.
These shared helpers validate, apply, and invert only those saved transfers.
"""
import numpy as np
from scipy.interpolate import PchipInterpolator


def validate_transfer(transfer):
    x,y=np.asarray(transfer['codes'],float),np.asarray(transfer['volts'],float)
    if (x.ndim!=1 or len(x)<8 or x.shape!=y.shape or
            not np.isfinite(x).all() or not np.isfinite(y).all() or
            np.any(np.diff(x)<=0) or np.any(np.diff(y)<=0) or
            x[0]<0 or x[-1]>4095 or y[0]<0 or y[-1]>3.3):
        raise ValueError('invalid monotone ADC voltage transfer')
    slope=np.diff(y)/np.diff(x)
    if np.any((slope<.7*3.3/4095)|(slope>1.3*3.3/4095)):
        raise ValueError('ADC transfer has an implausible local gain')


def voltage_values(raw, transfer):
    validate_transfer(transfer)
    raw=np.asarray(raw,float)
    if np.any((raw<0)|(raw>4095)):
        raise ValueError('invalid ADC code in voltage transfer')
    return PchipInterpolator(transfer['codes'],transfer['volts'],extrapolate=True)(raw)


def predistort_dac(data,config,transfer):
    words=np.frombuffer(data,dtype='<i2').astype(float)
    desired=config['offset_v']+.5*config['amplitude_v']*words/32767
    corrected=PchipInterpolator(transfer['volts'],transfer['normalized'],extrapolate=True)(desired)
    if np.any(np.abs(corrected)>1):
        raise ValueError('ARB inverse transfer exceeds DAC range')
    return np.rint(corrected*32767).astype('<i2').tobytes()
