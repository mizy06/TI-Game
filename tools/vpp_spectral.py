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
    # Remove the fitted carrier comb before measuring sidebands. Otherwise
    # finite-record leakage from the source itself fills its noise guard bands.
    orders=min(15,int(.4*fs/f))
    t=(np.arange(n)-(n-1)/2)/fs
    columns=[np.ones(n)]
    for order in range(1,orders+1):
        angle=2*np.pi*order*f*t
        columns.extend((np.cos(angle),np.sin(angle)))
    design=np.column_stack(columns)
    residual=y-design@np.linalg.lstsq(design,y,rcond=None)[0]
    w=blackmanharris(n,sym=False)
    ff=np.fft.rfftfreq(n,1/fs)
    carrier_power=np.abs(np.fft.rfft((y-np.mean(y))*w))**2
    power=np.abs(np.fft.rfft((residual-residual.mean())*w))**2
    distance=np.abs(ff-np.rint(ff/f)*f)/f
    common=(ff>.5*f)&(ff<min(3.5*f,.4*fs))
    near=common&(distance>.055)&(distance<.12)
    far=common&(distance>.22)&(distance<.43)
    if min(near.sum(),far.sum())<12:
        return dict(accepted=False,reason='too_few_background_bins')
    pn=float(np.mean(power[near]))
    pf=float(np.mean(power[far]))
    ratio=pn/max(pf,1e-30)
    carrier=common&(distance<.025)
    peak=float(np.max(carrier_power[carrier])) if carrier.any() else 0.
    background_fraction=float(np.sqrt(pn/max(peak,1e-30)))
    # A quiet quantized reference has no meaningful near/far noise ratio.
    # Accept its negligible background without treating broadband noise as a gap.
    negligible=background_fraction<1e-4
    return dict(accepted=ratio<.12 or negligible,background_gap_ratio=ratio,
                background_coefficient_fraction=background_fraction,
                background_negligible=negligible,
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
        tail=float(np.linalg.norm(c[1:])/max(abs(c[0]),1e-20))
        return float(4*abs(c[0])),dict(method='isolated_fundamental',harmonics=1,
            relative_window_scatter=scatter,harmonic_tail_ratio=tail,relative_rms=tail)
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


def sine_model_rescue(samples, fs, frequency, triangle_info, separation):
    """Validate a triangle label against the isolated sine harmonic profile."""
    if not separation.get('accepted') or not np.isfinite(triangle_info.get('relative_rms', np.inf)):
        return None
    sine_vpp,sine_info=harmonic_amplitude(samples,fs,0,frequency)
    triangle_error=float(triangle_info['relative_rms'])
    sine_tail=float(sine_info['harmonic_tail_ratio'])
    if triangle_error < .02 or sine_tail > .02:
        return None
    return dict(vpp_v=sine_vpp,info=sine_info,evidence=dict(
        selected='sine',rejected='triangle',triangle_harmonic_error=triangle_error,
        sine_harmonic_tail_ratio=sine_tail,background_separation=separation))


def joint_frame_amplitude(frames, kind):
    """Fit a shared clean Vpp across independently timed ADC frames."""
    import time
    from tools.amplitude_calibration import voltage_values
    from tools.unified_solver import shape

    started=time.perf_counter()
    if len(frames)<2 or kind not in (0,1,3):
        return None
    if kind==3:
        estimates=[]
        for frame in frames:
            y=voltage_values(frame['samples'],frame['transfer'])
            value,info=harmonic_amplitude(y,frame['fs'],kind,frame['frequency'],
                frame['duty'],frame['rise'],frame['fall'])
            estimates.append(value)
        result=float(np.median(estimates))
        return dict(vpp_v=result,method='median_reset_harmonic_across_frames',
                    frame_vpp=estimates,elapsed_s=time.perf_counter()-started)

    frequencies=np.asarray([float(frame['frequency']) for frame in frames])
    rates=np.asarray([float(frame['fs']) for frame in frames])
    harmonics=1 if kind==0 else min(16,int(np.ceil(.32*np.min(rates/frequencies)-1e-9)))
    if harmonics<1:
        return None
    h=np.arange(1,harmonics+1,dtype=float)
    coefficients=[]
    for frame,f,fs in zip(frames,frequencies,rates):
        raw=np.asarray(frame['samples'],dtype=float)
        y=voltage_values(raw,frame['transfer'])
        t=(np.arange(len(y))-(len(y)-1)/2)/fs
        columns=[np.ones(len(y))]
        for order in h:
            angle=2*np.pi*order*f*t
            columns.extend((np.cos(angle),np.sin(angle)))
        beta=np.linalg.lstsq(np.column_stack(columns),y,rcond=None)[0]
        coefficients.append(np.asarray([(beta[1+2*i]-1j*beta[2+2*i])/2
                                        for i in range(len(h))]))

    duty=float(np.median([frame['duty'] for frame in frames]))
    rise=max(float(np.median([frame['rise'] for frame in frames])),1e-5)
    fall=max(float(np.median([frame['fall'] for frame in frames])),1e-5)
    phase_grid=np.arange(32768,dtype=float)/32768

    def square_coeff(d,r,l):
        return (np.sinc(h*r)-np.sinc(h*l)*np.exp(-2j*np.pi*h*d))/(1j*np.pi*h)

    if kind==1:
        source=square_coeff(duty,rise,fall)
        initial=[float(np.median([frame['vpp'] for frame in frames]))/2,
                 duty,rise,fall,0.]
        lower=[.35*initial[0],max(.02,duty-.12),1e-6,1e-6,0.]
        upper=[1.65*initial[0],min(.98,duty+.12),.1,.1,
               2*float(np.max(frequencies/rates))]
    else:
        source_shape=shape(kind,phase_grid)
        source=np.asarray([np.mean(source_shape*np.exp(-2j*np.pi*order*phase_grid))
                           for order in h])
        initial=[float(np.median([frame['vpp'] for frame in frames]))/2]
        lower=[.35*initial[0]]
        upper=[1.65*initial[0]]
        if kind!=0:
            initial.append(0.)
            lower.append(0.)
            upper.append(2*float(np.max(frequencies/rates)))

    phases=[float(np.angle(c[0]/source[0])/(2*np.pi)) for c in coefficients]
    initial.extend(phases)
    lower.extend([phase-.5 for phase in phases])
    upper.extend([phase+.5 for phase in phases])
    weights=1/h

    def residual(q):
        if kind==1:
            amplitude,d,r,l,aperture=q[:5]
            source_now=square_coeff(d,r,l)
            frame_phases=q[5:]
        else:
            amplitude=q[0]
            source_now=source
            if kind==0:
                aperture=0.
                frame_phases=q[1:]
            else:
                aperture=q[1]
                frame_phases=q[2:]
        model=amplitude*source_now*np.sinc(h*aperture)*weights
        output=[]
        for observed,phase in zip(coefficients,frame_phases):
            delta=(model*np.exp(2j*np.pi*h*phase)-observed*weights)/max(amplitude,1e-9)
            output.extend(delta.real)
            output.extend(delta.imag)
        return output

    fit=least_squares(residual,initial,bounds=(lower,upper),loss='soft_l1',
                      f_scale=.02,max_nfev=160)
    result=float(2*fit.x[0])
    baseline=float(np.median([frame['vpp'] for frame in frames]))
    if not np.isfinite(result) or not .85*baseline<result<1.15*baseline:
        return None
    return dict(vpp_v=result,method='phase_aligned_joint_harmonics',
                harmonics=harmonics,frames=len(frames),cost=float(fit.cost),
                elapsed_s=time.perf_counter()-started)
