"""Independent voltage transfers for the amplitude path and ARB stimulus.

Dynamic sine references provide both transfers in dynamic_amplitude_calibration.
These shared helpers validate, apply, and invert only those saved transfers.
"""
import numpy as np
from scipy.interpolate import PchipInterpolator


def validate_transfer(transfer):
    if transfer.get('preprocessing') is not None:
        raise ValueError('filtered Vpp calibration is no longer supported; rebuild from unfiltered references')
    x,y=np.asarray(transfer['codes'],float),np.asarray(transfer['volts'],float)
    if (x.ndim!=1 or len(x)<8 or x.shape!=y.shape or
            not np.isfinite(x).all() or not np.isfinite(y).all() or
            np.any(np.diff(x)<=0) or np.any(np.diff(y)<=0) or
            x[0]<0 or x[-1]>4095 or y[0]<0 or y[-1]>3.3):
        raise ValueError('invalid monotone ADC voltage transfer')
    slope=np.diff(y)/np.diff(x)
    if np.any((slope<.7*3.3/4095)|(slope>1.3*3.3/4095)):
        raise ValueError('ADC transfer has an implausible local gain')
    response=transfer.get('operating_response')
    if response is not None:
        validate_operating_response(response)


def validate_operating_response(response):
    amplitudes=np.asarray(response['reference_vpp_v'],float)
    offsets=np.asarray(response['offset_v'],float)
    measured=np.asarray(response['measured_vpp_v'],float)
    if (amplitudes.ndim!=1 or offsets.ndim!=1 or len(amplitudes)<4 or len(offsets)<2 or
            measured.shape!=(len(offsets),len(amplitudes)) or
            not all(np.isfinite(v).all() for v in (amplitudes,offsets,measured)) or
            np.any(np.diff(amplitudes)<=0) or np.any(np.diff(offsets)<=0) or
            np.any(np.diff(measured,axis=1)<=0) or np.any(amplitudes<=0) or
            np.any(np.abs(measured/amplitudes-1)>.02)):
        raise ValueError('invalid operating amplitude response')


def corrected_vpp(vpp,dc,transfer):
    """Invert independently measured amplitude response at the fitted DC.

    Input is the denoised, voltage-calibrated source span, never noisy peaks.
    Outside the reference span use the nearest reference's relative gain.
    """
    response=transfer.get('operating_response') if transfer else None
    if response is None:
        return float(vpp)
    validate_operating_response(response)
    if not np.isfinite(vpp) or vpp<=0 or not np.isfinite(dc):
        raise ValueError('invalid fitted amplitude/offset for calibration')
    amplitudes=np.asarray(response['reference_vpp_v'],float)
    offsets=np.asarray(response['offset_v'],float)
    measured=np.asarray(response['measured_vpp_v'],float)
    # Interpolate the forward response between DC references, then invert it.
    forward=np.array([np.interp(dc,offsets,measured[:,i]) for i in range(len(amplitudes))])
    if vpp<=forward[0]:
        return float(vpp*amplitudes[0]/forward[0])
    if vpp>=forward[-1]:
        return float(vpp*amplitudes[-1]/forward[-1])
    return float(PchipInterpolator(forward,amplitudes)(vpp))


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
