"""Identical observations from distinct source parameters, independent of solver."""
import json
import numpy as np


def examples():
    n=8192;fs=20000.;f=200.;p=np.arange(n)*f/fs+.003
    carrier=np.sin(2*np.pi*p);common=.05*np.cos(4*np.pi*p)+.02*np.sin(6*np.pi*p)
    delta=15e-6
    a=1.65+.75*carrier+common+delta/2*carrier
    b=1.65+(.75+delta/2)*carrier+common
    codes_a=np.rint(a*4095/3.3);codes_b=np.rint(b*4095/3.3)
    assert np.array_equal(codes_a,codes_b)
    square_a=np.where(p%1<.2,1.,-1.);square_b=np.where(p%1<.201,1.,-1.)
    assert np.array_equal(square_a,square_b)
    ratio=1+35e-6
    q=np.arange(n)*(f*ratio)/(fs*ratio)+.003
    assert np.array_equal(np.rint((1.65+.75*np.sin(2*np.pi*p))*4095/3.3),np.rint((1.65+.75*np.sin(2*np.pi*q))*4095/3.3))
    return dict(coherent_amplitude=dict(identical_adc=True,source_vpp_world_a=1.5,source_vpp_world_b=1.5+delta,
                separation_ppm=10.,at_least_one_world_absolute_error_v_lower_bound=delta/2),
            unobserved_duty=dict(identical_samples=True,duty_world_a=.2,duty_world_b=.201),
            unknown_clock=dict(identical_adc=True,frequency_hz_world_a=f,frequency_hz_world_b=f*ratio,
                actual_fs_world_a=fs,actual_fs_world_b=fs*ratio,reported_fs_both_worlds=fs))
if __name__=='__main__':print(json.dumps(examples(),indent=2))
