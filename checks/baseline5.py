"""Independent joint waveform, coherent interference and ADC-bin inference.

All source families and nuisance orders share one bounded projection objective.
FFT and phase profiles provide initialization. Quantization consistency is an
optional local refinement, never a 1-ppm certificate. No truth or legacy solver.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import time
import numpy as np
from scipy.optimize import least_squares, minimize_scalar, linprog
from scipy.linalg import solve_triangular
from scipy.signal import find_peaks
from threadpoolctl import threadpool_limits

NAMES = ("sine", "square", "triangle", "sawtooth")
TAU = 2 * math.pi


@dataclass(frozen=True)
class SolverConfig:
    frequency_min_hz: float = 0.  # zero selects the observable frame range
    frequency_max_hz: float = 0.
    starts: int = 2
    max_nfev: int = 65
    max_seconds: float = 10.
    duty_min: float = .05
    duty_max: float = .95
    edge_max: float = .15
    low_harmonic_weight: float = .01
    noise_harmonics: int = 5
    coherent_rms_max: float = .06
    high_band_rms_max: float = .04
    nuisance_orders: tuple = (0, 1, 2, 3, 5, 7, 10)
    max_noise_ratio: float = .55
    quantization_refine: bool = True
    refinement_candidates: int = 4
    coherent_extra_orders: tuple = (15, 20, 25)


def shape(kind, phase, duty=.5, rise=.03, fall=.03):
    """Unit half-amplitude; square duty is between edge midpoints."""
    p = np.asarray(phase) % 1
    if kind == 0:
        return np.sin(TAU*p)
    if kind == 2:
        return 1-4*np.abs((p+.25) % 1-.5)
    if kind == 3:
        return 1-2*p
    out = np.where(p < duty, 1., -1.)
    a, b = (p+.5) % 1-.5, (p-duty+.5) % 1-.5
    out = np.where(np.abs(a) < rise/2, 2*a/rise, out)
    return np.where(np.abs(b) < fall/2, -2*b/fall, out)


def _basis(kind, q, t):
    p = (q[0]*t+q[1]) % 1
    s = shape(kind, p, *(q[2:] if kind == 1 else ()))
    if kind == 0:
        slope = TAU*np.cos(TAU*p)
    elif kind == 2:
        slope = np.where((p+.25) % 1 < .5, 4., -4.)
    else:
        duty, rise, fall = q[2:]
        a, b = (p+.5) % 1-.5, (p-duty+.5) % 1-.5
        mr, mf = np.abs(a) < rise/2, np.abs(b) < fall/2
        slope = np.where(mr, 2/rise, np.where(mf, -2/fall, 0.))
    ds = [slope*t, slope]
    if kind == 1:
        ds.extend((np.where(mf, 2/fall, 0.),
                   np.where(mr, -2*a/rise**2, 0.),
                   np.where(mf, 2*b/fall**2, 0.)))
    return s, np.column_stack(ds)


def _project(y, s, ds=None):
    """Exact elimination of offset/amplitude and exact reduced Jacobian."""
    z = s-s.mean()
    energy = float(z@z)
    if energy < 1e-12:
        raise ValueError("unobservable template")
    a = float((y-y.mean())@z/energy)
    b = float(y.mean()-a*s.mean())
    residual = b+a*s-y
    if ds is None:
        return residual, a, b
    dz = ds-ds.mean(axis=0)
    da = ((y-y.mean())@dz-2*a*(z@dz))/energy
    return residual, a, b, a*dz+z[:, None]*da


def _frequency_starts(y, fs, lo, hi, count):
    size = 1 << int(math.ceil(math.log2(len(y)*8)))
    spectrum = np.abs(np.fft.rfft((y-y.mean())*np.hanning(len(y)), size))
    f = np.fft.rfftfreq(size, 1/fs)
    spectrum[(f < lo) | (f > hi)] = 0
    peaks, _ = find_peaks(spectrum, distance=max(1, size//len(y)//2))
    peaks = sorted(peaks, key=lambda k: spectrum[k], reverse=True)
    if not peaks:
        raise ValueError("no observable periodic component")
    seeds = []
    # Include a subharmonic when a narrow-duty pulse's second harmonic wins.
    candidates = [f[peaks[0]], f[peaks[0]]/2]+[f[k] for k in peaks[1:count]]
    for value in candidates:
        if lo <= value <= hi and all(abs(value-s) > .65*fs/len(y) for s in seeds):
            seeds.append(float(value))
        if len(seeds) == count:
            break
    # A narrow pulse can have less fundamental power than allowed smooth
    # noise. Regular large changes offer a label-free period candidate.
    # Keep an already represented period and retain the strongest spectral seed.
    if count>=2:
        delta=np.diff(y);height=.2*float(np.ptp(y))
        for signed in (delta,-delta):
            edges,_=find_peaks(signed,height=height,distance=max(1,int(.4*fs/hi)))
            if len(edges)<4:continue
            gaps=np.diff(edges);period=float(np.median(gaps))
            if period<=0 or np.median(np.abs(gaps-period))/period>.08:continue
            cycles=np.r_[0,np.cumsum(np.maximum(1,np.rint(gaps/period)).astype(int))]
            slope,intercept=np.polyfit(cycles,edges,1)
            if slope<=0 or np.percentile(np.abs(edges-(slope*cycles+intercept)),95)>max(1.5,.04*period):continue
            candidate=float(fs/slope)
            if lo<=candidate<=hi and all(abs(candidate-value)>.65*fs/len(y) for value in seeds):
                seeds=[candidate]+seeds[:count-1]
    return seeds




def _nuisance(f, t, order, kind):
    # The sine fundamental cannot be separated from an arbitrary same-frequency
    # interferer. Leave it in the observable carrier and declare the ambiguity.
    h = np.arange(2 if kind == 0 else 1, order+1, dtype=float)
    ang = TAU*f*t[:, None]*h
    B = np.column_stack((np.ones(len(t)), np.sin(ang), np.cos(ang)))
    dB = np.column_stack((np.zeros(len(t)),
        np.cos(ang)*TAU*t[:, None]*h, -np.sin(ang)*TAU*t[:, None]*h))
    return B, dB


def _joint(y, s, ds, B, dB=None, amin=0., amax=math.inf, factor=None):
    """Eliminate bounded source amplitude, DC and explicit interference.

    The full reduced derivative includes the derivative of the nuisance basis.
    Unlike a frozen whitening matrix, frequency moves both source and noise.
    """
    Q,R=factor if factor is not None else np.linalg.qr(B,mode='reduced')
    if np.min(np.abs(np.diag(R))) < 1e-9:
        raise ValueError('rank-deficient nuisance')
    z = s-Q@(Q.T@s); yy=y-Q@(Q.T@y); en=float(z@z)
    if en < 1e-10*max(1., float(s@s)):
        raise ValueError('source absorbed by nuisance')
    a0=float(yy@z/en)
    a=math.copysign(float(np.clip(abs(a0),amin,amax)),a0)
    cb=solve_triangular(R,Q.T@(y-a*s),check_finite=False)
    r=a*s+B@cb-y
    if ds is None: return r,a,cb,en
    v=a*ds.copy()
    if dB is not None: v[:,0] += dB@cb
    if dB is None:
        dz=ds-Q@(Q.T@ds)
        da=(yy@dz-2*a*(z@dz))/en if abs(a-a0)<1e-12 else np.zeros(ds.shape[1])
        return r,a,cb,en,a*dz+z[:,None]*da
    if abs(a-a0)<1e-12:
        U=np.column_stack((s,B)); qu,ru=np.linalg.qr(U,mode='reduced')
        correction=np.zeros((U.shape[1],ds.shape[1]))
        correction[0]=ds.T@r
        if dB is not None:correction[1:,0]=dB.T@r
        J=v-qu@(qu.T@v)-qu@solve_triangular(ru.T,correction,lower=True,check_finite=False)
    else:
        correction=np.zeros((B.shape[1],ds.shape[1]))
        if dB is not None:correction[:,0]=dB.T@r
        J=v-Q@(Q.T@v)-Q@solve_triangular(R.T,correction,lower=True,check_finite=False)
    return r,a,cb,en,J


def _source(kind,q,t,cycle=None):
    if kind!=3:return _basis(kind,q,t)
    s=2*(cycle-cycle.mean()-q[0]*t)
    return s,(-2*t)[:,None]


def _quantized_refine(y,t,fit,step,check_time):
    """Local set-membership centre; accept only after nonlinear bin recheck.

    This is not a global interval enclosure, a noise model test, or a ppm
    certificate. Unknown ADC transition levels require separate calibration.
    """
    info=dict(attempted=False,accepted=False,all_samples_in_bins=False)
    if step<=0:return fit,info
    kind,K,q=fit['kind'],fit['order'],fit['q'].copy()
    B,_=_nuisance(q[0],t,K,kind)
    s,_=_source(kind,q,t,fit.get('cycle'))
    theta=np.r_[q,fit['a'],fit['cb']]; nq=len(q)
    def model(theta,jac=False):
        qq=theta[:nq];a=theta[nq];cb=theta[nq+1:]
        s,ds=_source(kind,qq,t,fit.get('cycle'))
        B,dB=_nuisance(qq[0],t,K,kind)
        m=a*s+B@cb
        if not jac:return m
        v=a*ds;v[:,0]+=dB@cb
        return m,np.column_stack((v,s,B))
    m=model(theta)
    if np.sqrt(np.mean((m-y)**2))>.8*step:return fit,info
    info['attempted']=True
    initial=theta.copy();accepted=None
    for iteration in range(3):
        check_time();m,J=model(theta,True)
        rms=np.sqrt(np.mean(J*J,axis=0))
        scale=step/np.maximum(rms,1e-20)
        scale[rms<1e-9]=0.
        scale[:nq]=np.minimum(scale[:nq],np.r_[.01/max(np.ptp(t),1e-9),np.repeat(.0005,nq-1)])
        M=J*scale[None,:]/step
        residual=(m-y)/step
        A=np.vstack((np.column_stack((M,np.ones(len(y)))),np.column_stack((-M,np.ones(len(y))))))
        rhs=np.r_[.5-residual,.5+residual]
        bounds=[]
        for j in range(len(theta)):
            low,high=(-8.,8.) if scale[j] else (0.,0.)
            if j<nq and scale[j]:
                low=max(low,(fit['low'][j]-theta[j])/scale[j]);high=min(high,(fit['high'][j]-theta[j])/scale[j])
            bounds.append((low,high))
        bounds.append((None,.5))
        objective=np.r_[np.zeros(len(theta)),-1.]
        lp=linprog(objective,A_ub=A,b_ub=rhs,bounds=bounds,method='highs',
            options={'time_limit':.4,'primal_feasibility_tolerance':1e-9,'dual_feasibility_tolerance':1e-9})
        check_time()
        if not lp.success:info['lp_status']=int(lp.status);break
        trial=theta+scale*lp.x[:-1]
        actual=model(trial)
        violation=float(np.max(np.abs(actual-y))/step-.5)
        if violation<=1e-7:accepted=trial.copy()
        theta=trial
        if violation<=1e-7:break
    if accepted is None:return fit,info
    # Average feasible extrema for the two competition quantities. This
    # avoids choosing an arbitrary LP vertex as the amplitude estimate.
    check_time();m,J=model(accepted,True)
    rms=np.sqrt(np.mean(J*J,axis=0));scale=step/np.maximum(rms,1e-20);scale[rms<1e-9]=0.
    scale[:nq]=np.minimum(scale[:nq],np.r_[.01/max(np.ptp(t),1e-9),np.repeat(.0005,nq-1)])
    M=J*scale[None,:]/step;rr=(m-y)/step
    A=np.vstack((M,-M));rhs=np.r_[.5-rr,.5+rr]
    bounds=[]
    for j in range(len(theta)):
        low,high=(-8.,8.) if scale[j] else (0.,0.)
        if j<nq and scale[j]:
            low=max(low,(fit['low'][j]-accepted[j])/scale[j]);high=min(high,(fit['high'][j]-accepted[j])/scale[j])
        bounds.append((low,high))
    endpoints=[];ranges={}
    for index,label in ((0,'frequency_hz'),(nq,'half_amplitude_normalized')):
        values=[]
        for sign in (-1.,1.):
            check_time();objective=np.zeros(len(theta));objective[index]=sign
            lp=linprog(objective,A_ub=A,b_ub=rhs,bounds=bounds,method='highs',
                options={'time_limit':.3,'primal_feasibility_tolerance':1e-9,'dual_feasibility_tolerance':1e-9})
            if lp.success:
                trial=accepted+scale*lp.x;endpoints.append(trial);values.append(float(trial[index]))
        if len(values)==2:ranges[label]=sorted(values)
    if endpoints:
        trial=np.mean(endpoints,axis=0)
        if np.max(np.abs(model(trial)-y))/step<=.5000001:accepted=trial
    info['local_linearized_ranges']=ranges
    # Reject a local centre that alters a discontinuity's observed topology.
    if kind==3:
        p=q[0]*(t-t[0])-fit['cycle']
        newp=accepted[0]*(t-t[0])-fit['cycle']
        if np.ptp(newp)>1+1e-9:return fit,info
    fit=dict(fit,q=accepted[:nq],a=float(accepted[nq]),cb=accepted[nq+1:])
    fit['residual']=model(accepted)-y
    info.update(accepted=True,all_samples_in_bins=True,
        maximum_bin_violation_lsb=max(0.,float(np.max(np.abs(fit['residual']))/step-.5)),
        local_parameter_change=(accepted[:nq]-initial[:nq]).tolist(),
        interpretation='Local nonlinear rechecked bin consistency; not a global accuracy bound.')
    return fit,info


def estimate(samples,sample_rate_hz,config=None,*,quantization_step=0.):
    """Independent source + nuisance inference with quantization consistency.

    Samples are in arbitrary linear units. quantization_step must be supplied
    in the same units; 0 disables the interval step. No truth or test labels.
    """
    cfg=config or SolverConfig();started=time.perf_counter()
    raw=np.asarray(samples,dtype=float)
    if (raw.ndim!=1 or len(raw)<256 or not np.all(np.isfinite(raw)) or
            not math.isfinite(sample_rate_hz) or sample_rate_hz<=0):raise ValueError('invalid samples or sample rate')
    if (not isinstance(cfg.starts,int) or cfg.starts<1 or not isinstance(cfg.max_nfev,int) or cfg.max_nfev<1 or
            not math.isfinite(cfg.max_seconds) or cfg.max_seconds<=0 or
            not 0<cfg.duty_min<cfg.duty_max<1 or not 0<cfg.edge_max<=.2 or
            not math.isfinite(cfg.max_noise_ratio) or not 0<=cfg.max_noise_ratio<.9 or
            not isinstance(cfg.refinement_candidates,int) or cfg.refinement_candidates<1 or
            not isinstance(cfg.nuisance_orders,tuple) or not cfg.nuisance_orders or
            any(not isinstance(k,int) or not 0<=k<=32 for k in cfg.nuisance_orders) or
            not isinstance(cfg.coherent_extra_orders,tuple) or
            any(not isinstance(k,int) or not 0<=k<=32 for k in cfg.coherent_extra_orders) or
            not math.isfinite(quantization_step) or quantization_step<0 or
            not math.isfinite(cfg.low_harmonic_weight) or not 0<cfg.low_harmonic_weight<=1):raise ValueError('invalid solver configuration')
    center=float(raw.mean());spread=float(raw.std())
    if spread<=1e-12:raise ValueError('flat signal')
    y=(raw-center)/spread;n=len(y);fs=float(sample_rate_hz);step=quantization_step/spread
    t=(np.arange(n)-(n-1)/2)/fs
    lo=cfg.frequency_min_hz or 4*fs/n;hi=cfg.frequency_max_hz or fs/12
    if not math.isfinite(lo+hi) or not 0<lo<hi<fs/2:raise ValueError('invalid frequency search range')
    def check_time():
        if time.perf_counter()-started>cfg.max_seconds:raise TimeoutError('unified solver deadline exceeded')
    check_time()
    amin=max(0.,(float(np.ptp(y))-step)/(2*(1+cfg.max_noise_ratio)))
    amax=(float(np.ptp(y))+step)/(2*(1-cfg.max_noise_ratio))*1.15
    variance_floor=max(step*step/12,1e-24)
    fits=[];starts=[];orders_used=set(cfg.nuisance_orders)
    def score(fit):
        r=fit['residual'];q=fit['q'];kind=fit['kind']
        if kind==1 and .5*(q[3]+q[4])>min(q[2],1-q[2])+1e-12:return math.inf
        s,_=_source(kind,q,t,fit.get('cycle'))
        ratio=float(np.ptp(y-fit['a']*s)/(2*abs(fit['a'])))
        # Explicit physical envelope prior, with three LSBs for rounding.
        limit=cfg.max_noise_ratio+3*step/max(2*abs(fit['a']),1e-12)+.015
        fit['edge_timing_uncertainty_samples']=0.
        penalty=0.
        if ratio>limit:
            if kind!=1:return math.inf
            # A zero-order-held DAC and an ADC can disagree at one edge sample
            # even when both clocks are stable. Allow a bounded edge-local
            # timing model; flat portions must still meet the same noise bound.
            phase=(q[0]*t+q[1])%1
            up=(phase+.5)%1-.5;down=(phase-q[2]+.5)%1-.5
            aperture=1.5*q[0]/fs
            uncertain=(np.abs(up)<=q[3]/2+aperture)|(np.abs(down)<=q[4]/2+aperture)
            if np.mean(uncertain)>.2 or np.count_nonzero(~uncertain)<n//2:return math.inf
            stable_ratio=float(np.ptp((y-fit['a']*s)[~uncertain])/(2*abs(fit['a'])))
            if stable_ratio>limit:return math.inf
            fit['edge_timing_uncertainty_samples']=1.5
            # A strong explicit prior ensures that an ordinary admissible model
            # wins over edge uncertainty. This is an extra observation model in
            # the same candidate score, not a separate waveform classifier.
            penalty=1e6+float(np.count_nonzero(uncertain))*math.log(n)
        p=len(q)+1+len(fit['cb'])
        return n*math.log(max(float(r@r/n),variance_floor))+p*math.log(n)+penalty
    with threadpool_limits(limits=1,user_api='blas'):
        seeds=_frequency_starts(y,fs,lo,hi,cfg.starts)
        for seed_index,seed in enumerate(seeds):
            check_time();lower=max(lo,seed-.7*fs/n);upper=min(hi,seed+.7*fs/n)
            hmax=max(cfg.nuisance_orders)
            def harmonic_loss(f):
                check_time();B,_=_nuisance(f,t,max(1,hmax),2)
                c=np.linalg.lstsq(B,y,rcond=None)[0];r=B@c-y
                return float(r@r)
            seed=float(minimize_scalar(harmonic_loss,bounds=(lower,upper),method='bounded',options={'xatol':1e-9}).x)
            starts.append(seed)
            design=np.column_stack((np.ones(n),np.sin(TAU*seed*t),np.cos(TAU*seed*t)))
            c=np.linalg.lstsq(design,y,rcond=None)[0];phase=float(np.arctan2(c[2],c[1])/TAU)
            levels=np.percentile(y,[2,10,90,98]);middle=.5*(levels[0]+levels[3]) if levels[2]-levels[1]<.2*(levels[3]-levels[0]) else .5*(levels[1]+levels[2])
            duty=float(np.clip(np.mean(y>middle),cfg.duty_min+.001,cfg.duty_max-.001))
            span=float(np.ptp(y));delta=np.diff(y);spacing=max(1,int(.55*fs/seed))
            jumps,_=find_peaks(delta,height=.32*span,distance=spacing)
            if delta[0]>.32*span and (not len(jumps) or jumps[0]>=spacing):jumps=np.r_[0,jumps]
            if delta[-1]>.32*span and (not len(jumps) or len(delta)-1-jumps[-1]>=spacing):jumps=np.r_[jumps,len(delta)-1]
            cycle=None;flo,fhi=lower,upper
            if len(jumps)>=3:
                cycle=np.searchsorted(jumps+1,np.arange(n),side='right')
                indices=np.unique(np.r_[0,n-1,jumps,jumps+1]);counts=cycle[indices]
                di=indices[None,:]-indices[:,None];dk=counts[None,:]-counts[:,None];mask=di>0
                flo=max(lower,float(np.max((dk[mask]-1)*fs/di[mask])))
                fhi=min(upper,float(np.min((dk[mask]+1)*fs/di[mask])))
                if flo>=fhi:cycle=None
            # Every order and source is one candidate in the same penalized
            # objective. Warm starts only accelerate optimization of that model.
            warm={};seedfits=[]
            orders=list(cfg.nuisance_orders);expanded=False
            for K in orders:
                check_time()
                for kind in (0,2,1,3):
                    if kind==3 and cycle is None:continue
                    B,_=_nuisance(seed,t,K,kind)
                    factor=np.linalg.qr(B,mode='reduced')
                    base=phase+(duty/2-.25 if kind==1 else 0.)
                    edge_bound=min(cfg.edge_max,.9*min(duty,1-duty))
                    edge_starts=(.008,.07) if kind==1 else (0.,)
                    for edge in edge_starts:
                        if kind==3:
                            q0=np.array([np.clip(seed,flo,fhi)]);low=np.array([flo]);high=np.array([fhi]);scales=[fs/n]
                        else:
                            q0=np.array([seed,base]+([duty,min(edge,edge_bound*.8),min(edge,edge_bound*.8)] if kind==1 else []))
                            if (kind,edge) in warm and kind==1:q0=warm[kind,edge].copy()
                            low=np.array([lower,base-.24]+([cfg.duty_min,1e-5,1e-5] if kind==1 else []))
                            high=np.array([upper,base+.24]+([cfg.duty_max,edge_bound,edge_bound] if kind==1 else []))
                            if kind==2:
                                phases=np.linspace(base-.239,base+.239,65)
                                sg=shape(kind,seed*t[:,None]+phases[None,:])
                                Q=factor[0];zg=sg-Q@(Q.T@sg);yy=y-Q@(Q.T@y)
                                en=np.sum(zg*zg,axis=0);av=(yy@zg)/np.maximum(en,1e-20)
                                av=np.sign(av)*np.clip(np.abs(av),amin,amax)
                                losses=np.sum((zg*av[None,:]-yy[:,None])**2,axis=0)
                                q0[1]=phases[int(np.argmin(losses))]
                            q0=np.clip(q0,low+1e-12,high-1e-12);scales=[fs/n,.1]+([.1,.05,.05] if kind==1 else [])
                        cached=None;cache=None
                        def evaluate(q,jac=False):
                            nonlocal cached,cache
                            check_time()
                            if cached is None or not np.array_equal(cached,q):
                                s,ds=_source(kind,q,t,cycle)
                                cache=_joint(y,s,ds,B,amin=amin,amax=amax,factor=factor);cached=q.copy()
                            return cache[4] if jac else cache[0]
                        try:
                            opt=least_squares(evaluate,q0,jac=lambda q:evaluate(q,True),bounds=(low,high),x_scale=scales,
                                max_nfev=cfg.max_nfev,ftol=2e-11,xtol=2e-11,gtol=2e-11)
                            evaluate(opt.x);r,a,cb,en=cache[:4]
                            item=dict(kind=kind,order=K,q=opt.x.copy(),a=a,cb=cb,residual=r,low=low,high=high,
                                cycle=cycle if kind==3 else None,converged=bool(opt.success),evaluations=opt.nfev,innovation_energy=en,scales=scales)
                            item['bic']=score(item)
                            if math.isfinite(item['bic']):seedfits.append(item)
                            warm[kind,edge]=opt.x.copy()
                        except (ValueError,np.linalg.LinAlgError):continue
                # Expand the same nuisance-order search only if the current
                # residual is mostly explained by harmonics of this period.
                # Noncoherent broadband records do not earn extra free modes.
                if K==cfg.nuisance_orders[-1] and not expanded and cfg.coherent_extra_orders and seedfits:
                    expanded=True
                    trial=min(seedfits,key=lambda x:x['bic'])
                    residual=trial['residual'];rss=float(residual@residual)
                    if rss/n>max(9*step*step,1e-12):
                        probe,_=_nuisance(trial['q'][0],t,max(cfg.coherent_extra_orders),2)
                        coeff=np.linalg.lstsq(probe,residual,rcond=None)[0]
                        remaining=residual-probe@coeff
                        if float(remaining@remaining)<.49*rss:
                            extra=[k for k in cfg.coherent_extra_orders if k not in orders]
                            orders.extend(extra);orders_used.update(extra)
            # A harmonic nuisance needs evidence of actual coherent residual,
            # not just the small projection of a broadband realization on a
            # large dictionary. This support gate is part of model admissibility.
            base_rss={k:min((float(x['residual']@x['residual']) for x in seedfits if x['kind']==k and x['order']==0 and not x.get('edge_timing_uncertainty_samples')),default=math.inf) for k in range(4)}
            for x in seedfits:
                rss=float(x['residual']@x['residual'])
                if x['order'] and rss>max(.6*min(base_rss.values()),4*n*variance_floor):
                    x['bic']=math.inf
            fits.extend(x for x in seedfits if math.isfinite(x['bic']))
            # Clearly wrong FFT aliases need not spend the same iteration budget
            # as the dominant fundamental; all initial starts remain candidates.
        if not fits:raise ValueError('no waveform consistent with configured noise envelope')
        fits.sort(key=lambda x:x['bic'])
        shortlist=[]
        for kind in range(4):
            group=[r for r in fits if r['kind']==kind]
            shortlist.extend(group[:1])
        shortlist.sort(key=lambda x:x['bic'])
        for item in shortlist[:cfg.refinement_candidates]:
            kind,K=item['kind'],item['order'];cycle=item.get('cycle')
            cached=None;cache=None
            def evaluate(q,jac=False):
                nonlocal cached,cache
                check_time()
                if cached is None or not np.array_equal(cached,q):
                    s,ds=_source(kind,q,t,cycle);B,dB=_nuisance(q[0],t,K,kind)
                    cache=_joint(y,s,ds,B,dB,amin,amax);cached=q.copy()
                return cache[4] if jac else cache[0]
            try:
                opt=least_squares(evaluate,item['q'],jac=lambda q:evaluate(q,True),bounds=(item['low'],item['high']),
                    x_scale=item['scales'],max_nfev=cfg.max_nfev,ftol=2e-12,xtol=2e-12,gtol=2e-12)
                evaluate(opt.x);r,a,cb,en=cache[:4]
                item.update(q=opt.x.copy(),a=a,cb=cb,residual=r,innovation_energy=en,converged=bool(opt.success),evaluations=item['evaluations']+opt.nfev)
                item['bic']=score(item)
            except (ValueError,np.linalg.LinAlgError):continue
        # Compare the refined winner of every source; frozen candidates are
        # initialization only and cannot override their corresponding refinement.
        finals=[r for r in shortlist if math.isfinite(r['bic'])]
        if not finals:raise ValueError('no refined source model')
        finals.sort(key=lambda x:x['bic']);best=finals[0]
        before=dict(best,q=best['q'].copy(),cb=best['cb'].copy())
        quant=dict(attempted=False,accepted=False,all_samples_in_bins=False)
        if cfg.quantization_refine:
            best,quant=_quantized_refine(y,t,best,step,check_time)
            if not amin<=abs(best['a'])<=amax or not math.isfinite(score(best)):
                best=before
                quant.update(accepted=False,all_samples_in_bins=False,rejection_reason='violates source/noise constraints')
        check_time()
    kind,q,a,cb=best['kind'],best['q'],best['a'],best['cb'];dc=float(cb[0])
    if kind==1 and a<0:
        q=q.copy();q[1]-=q[2];q[2]=1-q[2];q[3],q[4]=q[4],q[3];a=-a
    phase_interval=None
    if kind==3:
        cycle=best['cycle'];p=np.arange(n)*q[0]/fs-cycle
        left,right=float(-p.min()),float(1-p.max());phase_interval=[left,right]
        if left>right+1e-8:raise ValueError('reset timing inconsistent with frequency')
        dc+=a*(-1+left+right+q[0]*(n-1)/fs-2*cycle.mean())
    s,_=_source(best['kind'],best['q'],t,best.get('cycle'))
    energy=float((s-s.mean())@(s-s.mean()))
    observed_noise_ratio=float(np.ptp(y-best['a']*s)/(2*abs(best['a'])))
    residual=float(np.sqrt(np.mean(best['residual']**2))*spread)
    source_identifiable=kind!=0 and best['innovation_energy']/max(energy,1e-12)>1e-8
    diagnostics=dict(method='joint_morphology_nuisance_quantization',solver_version='5.0.0',seeds_hz=seeds,
        refined_starts_hz=starts,candidates=len(fits),nuisance_order=best['order'],nuisance_orders=sorted(orders_used),configured_base_orders=list(cfg.nuisance_orders),
        coherent_extra_orders=list(cfg.coherent_extra_orders),
        nuisance_assumption='Interference on signal harmonics; other noise remains in residual.',
        noise_envelope_limit=cfg.max_noise_ratio,observed_noise_ratio=observed_noise_ratio,
        residual_rms=residual,normalized_residual_rms=residual/spread,converged=best['converged'],evaluations=best['evaluations'],
        source_innovation_fraction=best['innovation_energy']/max(energy,1e-12),
        source_amplitude_identifiable_under_nuisance_prior=bool(source_identifiable),
        sine_carrier_includes_unknown_coherent_fundamental=kind==0,
        edge_identifiable=bool(kind!=1 or (min(q[3:])*fs/q[0]>=2 and not best.get('edge_timing_uncertainty_samples'))),
        edge_timing_uncertainty_samples=best.get('edge_timing_uncertainty_samples',0.),
        square_rise_fraction=float(q[3]) if kind==1 else 0.,square_fall_fraction=float(q[4]) if kind==1 else 0.,
        phase_interval_cycles=phase_interval,quantization=quant,
        before_quantization=dict(frequency_hz=float(before['q'][0]),vpp=float(2*abs(before['a'])*spread)),
        model_bic={NAMES[r['kind']]:float(r['bic']) for r in finals},
        model_margin_bic=float(finals[1]['bic']-finals[0]['bic']) if len(finals)>1 else 0.,
        accuracy_1ppm_certified=False,
        uncertainty_note='Neither MDL score nor local bin consistency bounds coherent noise, clock, gain, INL or model error.',
        elapsed_s=time.perf_counter()-started)
    return dict(type=kind,name=NAMES[kind],frequency_hz=float(q[0]),vpp=float(2*abs(a)*spread),
        dc=float(dc*spread+center),duty_fraction=float(q[2]) if kind==1 else 0.,diagnostics=diagnostics)
