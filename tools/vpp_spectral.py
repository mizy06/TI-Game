"""Amplitude from isolated waveform harmonics, conditional on spectral separation.

This module receives only ADC samples, their timebase and the existing waveform
classification. No replay parameters, expected amplitude or case identity enter.
"""
import numpy as np
from scipy.optimize import least_squares
from scipy.signal.windows import blackmanharris
from scipy.signal import find_peaks

def spectral_separation(samples, fs, frequency):
    """Compare nearby background bands; this does not certify absence of a coherent tone."""
    y=np.asarray(samples,dtype=float)
    f=float(frequency)
    n=len(y)
    if n*f/fs<32:
        return dict(accepted=False,reason='too_few_cycles')
    w=blackmanharris(n,sym=False)
    ff=np.fft.rfftfreq(n,1/fs)
    power=np.abs(np.fft.rfft((y-np.mean(y))*w))**2
    distance=np.abs(ff-np.rint(ff/f)*f)/f
    common=(ff>.5*f)&(ff<min(3.5*f,.4*fs))
    near=common&(distance>.055)&(distance<.12)
    far=common&(distance>.22)&(distance<.43)
    if min(near.sum(),far.sum())<12:
        return dict(accepted=False,reason='too_few_background_bins')
    pn=float(np.mean(power[near]))
    pf=float(np.mean(power[far]))
    ratio=pn/max(pf,1e-30)
    return dict(accepted=ratio<.12,background_gap_ratio=ratio,
                assumption='source harmonics separated from noncoherent background; coherent overlap remains unidentifiable')

def harmonic_amplitude(samples, fs, kind, frequency, duty=.5, rise=.03, fall=.03, robust=True):
    y=np.asarray(samples,dtype=float)
    n=len(y)
    f=float(frequency)
    t=(np.arange(n)-(n-1)/2)/fs
    h=np.arange(1,min(12,int(.32*fs/f))+1,dtype=float)
    w=blackmanharris(n,sym=False)
    projection=np.exp(-2j*np.pi*f*h[:,None]*t)*w/w.sum()
    c=projection@(y-np.mean(y))
    scatter=0.
    starts=None
    if robust and n*f/fs>=64:
        length=n//3
        win=blackmanharris(length,sym=False)
        coefficients=[]
        projections=[]
        starts=np.linspace(0,n-length,11).astype(int)
        for start in starts:
            sl=slice(start,start+length)
            projections.append(np.exp(-2j*np.pi*f*h[:,None]*t[sl])*win/win.sum())
            coefficients.append(projections[-1]@(y[sl]-np.mean(y[sl])))
        coefficients=np.asarray(coefficients)
        c=np.median(coefficients.real,axis=0)+1j*np.median(coefficients.imag,axis=0)
        scatter=float(np.median(np.abs(coefficients[:,0]-c[0]))/max(abs(c[0]),1e-20))
    def sampled_coefficients(source):
        if starts is None:
            return projection@(source-source.mean())
        values=[]
        for start,P in zip(starts,projections):
            sl=slice(start,start+length)
            values.append(P@(source[sl]-source[sl].mean()))
        values=np.asarray(values)
        return np.median(values.real,axis=0)+1j*np.median(values.imag,axis=0)
    if kind==0:
        return float(4*abs(c[0])),dict(method='isolated_fundamental',harmonics=1,relative_window_scatter=scatter)
    if kind==3:
        span=float(np.diff(np.quantile(y,[.02,.98]))[0])
        jumps,_=find_peaks(np.diff(y),height=.24*span,distance=max(1,int(.65*fs/f)))
        if len(jumps)<5 or np.max(np.abs(np.diff(jumps)*f/fs-1))>.15:
            raise ValueError('unreliable saw reset lattice for amplitude')
        cycles=np.searchsorted(jumps+1,np.arange(n),side='right')
        source=-2*(f*t-cycles)
        s=sampled_coefficients(source)
        amp=float(np.vdot(s,c).real/np.vdot(s,s).real)
        rms=float(np.linalg.norm(amp*s-c)/max(np.linalg.norm(c),1e-20))
        return 2*abs(amp),dict(method='sampled_reset_harmonics',harmonics=len(h),relative_rms=rms)
    if kind in (2,3):
        if kind==2:
            mask=(h%2)==1
            h,c=h[mask],c[mask]
            estimates=(np.pi**2/2)*h*h*np.abs(c)
        else:
            estimates=2*np.pi*h*np.abs(c)
        # Source hold/aperture attenuates higher harmonics. Fit the amplitude
        # and aperture together; extrapolate to the zero-frequency gain.
        scale=estimates[0]
        weight=1/h**(2 if kind==2 else 1)
        def residual(q):
            return weight*(q[0]*np.sinc(h*q[1])-estimates)/scale
        fit=least_squares(residual,[scale,f/fs],bounds=([.5*scale,0],[2*scale,2*f/fs]),
                          xtol=1e-12,ftol=1e-12,gtol=1e-12)
        return float(fit.x[0]),dict(method='harmonic_aperture_extrapolation',harmonics=len(h),aperture_samples=float(fit.x[1]*fs/f),relative_rms=float(np.sqrt(np.mean(fit.fun**2))))
    def coeff(d,r,l):
        return (np.sinc(h*r)-np.sinc(h*l)*np.exp(-2j*np.pi*h*d))/(1j*np.pi*h)
    s=coeff(duty,rise,fall)
    phase=float(np.angle(c[0]/s[0])/(2*np.pi))
    amp=float(abs(c[0]/s[0]))
    def residual(q):
        phase,d,r,l,a,ap=q
        prediction=a*coeff(d,r,l)*np.exp(2j*np.pi*h*phase)*np.sinc(h*ap)
        z=(prediction-c)/amp
        return np.r_[z.real,z.imag]
    q0=[phase,duty,max(.0001,rise),max(.0001,fall),amp,f/fs]
    fit=least_squares(residual,q0,bounds=([phase-.2,.02,.00001,.00001,.5*amp,0],
                                         [phase+.2,.98,.25,.25,2*amp,2*f/fs]),
                      max_nfev=150,xtol=1e-11,ftol=1e-11,gtol=1e-11)
    from tools.unified_solver import shape
    phase,d,r,l,a,ap=fit.x
    def discrete_residual(q):
        ph,du,ri,fa,am=q
        source=shape(1,f*t+ph,du,ri,fa)
        prediction=am*sampled_coefficients(source)
        z=(prediction-c)/amp
        return np.r_[z.real,z.imag]
    discrete=least_squares(discrete_residual,[phase,d,r,l,a],
        bounds=([phase-.03,max(.02,d-.03),.00001,.00001,.7*a],
                [phase+.03,min(.98,d+.03),.25,.25,1.3*a]),
        max_nfev=65,xtol=1e-10,ftol=1e-10,gtol=1e-10)
    return float(2*discrete.x[4]),dict(method='sampled_trapezoid_harmonics',harmonics=len(h),
                                 parameters=discrete.x.tolist(),relative_rms=float(np.sqrt(np.mean(discrete.fun**2))))
