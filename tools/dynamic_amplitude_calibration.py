"""Dynamic AC calibration from dedicated sine references, independent of cases."""
from dataclasses import replace
import time
import numpy as np
from numpy.polynomial.chebyshev import chebvander, chebval
from scipy.interpolate import BSpline
from scipy.ndimage import gaussian_filter1d
from tools.capture_store import atomic_json
from tools.amplitude_calibration import validate_transfer, voltage_values, corrected_vpp
from tools.acquisition_calibration import sine_period

REFERENCES=tuple((offset,.72) for offset in (.40,.72,1.04,1.36,1.68,2.00,2.32,2.64,2.90))
ORIGINAL_CHECK_OFFSETS=(.57,1.77,2.67)
# Archived 50-point experiments remain available to offline tools only.
GAIN_REFERENCES=tuple((round(1.4+.1*j,6),round(.8+.8*i/9,6))
                      for i in range(10) for j in range(5))
GAIN_CHECKS=tuple((offset,amp) for amp in (.87,1.07,1.23,1.47,1.57) for offset in (1.45,1.75))


def fit_dynamic(observations,domain,degree=9):
    """Inverse transfer from known AC spans; every block has its own phase/DC.

    Alternate linear polynomial fitting and sine phase estimation. DC reference
    offsets determine only one global integration constant, never local gains.
    """
    blocks=[]
    gram=np.zeros((degree,degree))
    for o in observations:
        x=np.asarray(o['values'],float)
        z=2*x/domain-1
        B=chebvander(z,degree)[:,1:]
        Bc=B-B.mean(axis=0)
        angle=2*np.pi*np.arange(len(x))/o['period_samples']
        tone=np.column_stack((np.sin(angle),np.cos(angle),np.ones(len(x))))
        blocks.append((o,B,Bc,tone))
        gram+=Bc.T@Bc
    coefficients=np.zeros(degree)
    coefficients[0]=domain/2
    for _ in range(30):
        rhs=np.zeros(degree)
        for o,B,Bc,tone in blocks:
            fit=np.linalg.lstsq(tone,B@coefficients,rcond=None)[0]
            direction=fit[:2]/np.linalg.norm(fit[:2])
            target=tone[:,:2]@direction*(o['amplitude_v']/2)
            rhs+=Bc.T@(target-target.mean())
        updated=np.linalg.solve(gram,rhs)
        if np.max(np.abs(updated-coefficients))<1e-11:
            coefficients=updated
            break
        coefficients=updated
    constant=float(np.mean([o['offset_v']-np.mean(B@coefficients) for o,B,_,_ in blocks]))
    grid=np.linspace(0,domain,513)
    volts=chebval(2*grid/domain-1,np.r_[constant,coefficients])
    if not np.isfinite(volts).all() or np.any(np.diff(volts)<=0):
        raise ValueError('dynamic transfer is not monotone')
    return dict(input=grid.tolist(),output=volts.tolist(),domain=domain,
                coefficients=np.r_[constant,coefficients].tolist(),method='independent_AC_Chebyshev')


def tone_amplitude(values,period):
    angle=2*np.pi*np.arange(len(values))/period
    B=np.column_stack((np.ones(len(values)),np.sin(angle),np.cos(angle)))
    c=np.linalg.lstsq(B,values,rcond=None)[0]
    return float(2*np.hypot(c[1],c[2]))


def calibrated_tone_values(raw,period,transfer):
    """Use the full amplitude calibration when comparing ARB references."""
    values=voltage_values(raw,transfer)
    angle=2*np.pi*np.arange(len(values))/period
    B=np.column_stack((np.ones(len(values)),np.sin(angle),np.cos(angle)))
    c=np.linalg.lstsq(B,values,rcond=None)[0]
    span=float(2*np.hypot(c[1],c[2]))
    corrected=corrected_vpp(span,float(c[0]),transfer)
    return c[0]+(values-c[0])*(corrected/span)


def fit_operating_response(transfer,observations):
    """Measure amplitude response across independent spans and DC offsets.

    The local ADC lookup and operating span response have different roles.
    Do not force amplitude-dependent gain into a single static code lookup.
    """
    groups={}
    for o in observations:
        if o.get('validation'):
            raise ValueError('operating response accepts fitting references only')
        measured=tone_amplitude(voltage_values(o['values'],transfer),o['period_samples'])
        groups.setdefault((o['offset_v'],o['amplitude_v']),[]).append(measured/o['amplitude_v'])
    if (len(groups)<4 or min(k[1] for k in groups)>.8 or
            max(k[1] for k in groups)<1.6):
        raise ValueError('operating gain requires independent references covering 0.8..1.6 Vpp')
    amplitudes=sorted({key[1] for key in groups})
    offsets=sorted({key[0] for key in groups})
    if len(offsets)<2 or len(groups)!=len(amplitudes)*len(offsets):
        raise ValueError('operating response requires a complete span/offset grid')
    measured=[[float(np.mean(groups[(offset,amp)]))*amp for amp in amplitudes] for offset in offsets]
    result=dict(transfer)
    result['operating_response']=dict(method='independent_sine_span_offset_response',
        reference_vpp_v=amplitudes,offset_v=offsets,measured_vpp_v=measured,
        extrapolation='nearest_reference_relative_gain',
        independent_reference_used=True,absolute_accuracy_verified=False)
    validate_transfer(result)
    return result


def check_operating_amplitude(transfer,blocks,offset,amplitude,frequency):
    amps=[tone_amplitude(calibrated_tone_values(o['values'],o['period_samples'],transfer),
                         o['period_samples']) for o in blocks]
    mean=float(np.mean(amps))
    bias=mean/amplitude-1
    check=dict(offset_v=offset,reference_vpp_v=amplitude,frequency_hz=frequency,
               vpp_v=mean,relative_bias=bias,
               block_standard_deviation_v=float(np.std(amps,ddof=1)) if len(amps)>1 else 0.)
    if not np.isfinite(bias) or abs(bias)>.001:
        raise ValueError(f'dynamic ADC operating amplitude check failed: '
                         f'{amplitude:g} Vpp at {offset:g} V / {frequency:g} Hz: {bias*100:g}%')
    return check


def fit_dynamic_spline(observations,domain):
    """Resolve local ADC nonlinearity using overlapping AC sweeps."""
    degree=3
    breaks=np.linspace(0,domain,65)
    knots=np.r_[np.repeat(0.,degree),breaks,np.repeat(domain,degree)]
    count=len(knots)-degree-1
    blocks=[]
    gram=np.zeros((count,count))
    for o in observations:
        x=np.asarray(o['values'],float)
        B=BSpline.design_matrix(x,knots,degree,extrapolate=True).toarray()
        Bc=B-B.mean(axis=0)
        angle=2*np.pi*np.arange(len(x))/o['period_samples']
        tone=np.column_stack((np.sin(angle),np.cos(angle),np.ones(len(x))))
        blocks.append((o,B,Bc,tone))
        gram+=Bc.T@Bc
    # A small curvature penalty suppresses noise-driven local oscillation.
    D=np.diff(np.eye(count),n=2,axis=0)
    ridge=1e-4
    gram+=10*(D.T@D)+ridge*np.eye(count)
    grid=np.linspace(0,domain,513)
    Bg=BSpline.design_matrix(grid,knots,degree).toarray()
    coefficients=np.linalg.lstsq(Bg,grid*(3.3/domain),rcond=None)[0]
    prior=coefficients.copy()
    for _ in range(30):
        rhs=ridge*prior
        for o,B,Bc,tone in blocks:
            fit=np.linalg.lstsq(tone,B@coefficients,rcond=None)[0]
            direction=fit[:2]/np.linalg.norm(fit[:2])
            target=tone[:,:2]@direction*(o['amplitude_v']/2)
            rhs+=Bc.T@(target-target.mean())
        updated=np.linalg.solve(gram,rhs)
        if np.max(np.abs(updated-coefficients))<1e-10:
            coefficients=updated
            break
        coefficients=updated
    constant=float(np.mean([o['offset_v']-np.mean(B@coefficients) for o,B,_,_ in blocks]))
    output=Bg@coefficients+constant
    if np.any(np.diff(output)<=0):
        raise ValueError('dynamic ADC spline is not monotone')
    return dict(input=grid.tolist(),output=output.tolist(),method='independent_AC_monotone_spline')


def fit_sample_curve(observations,domain=4095.,segments=48,curvature=10.,reference_dc=False):
    """Fit a bounded monotone cubic curve directly from ADC/reference pairs.

    Known reference spans provide each sine's scale. Each record has its own
    fitted phase and DC; UART gaps never enter sample timing. The unknowns
    are spline increments, whose bounds constrain the analytic derivative.
    Validation records are forbidden from entering the curve fit.
    """
    from scipy.linalg import cholesky,solve_triangular
    from scipy.optimize import lsq_linear
    if not observations or any(o.get('validation',False) for o in observations):
        raise ValueError('direct curve requires fitting references only')
    degree=3
    breaks=np.linspace(0,domain,segments+1)
    knots=np.r_[np.repeat(0.,degree),breaks,np.repeat(domain,degree)]
    count=len(knots)-degree-1
    # A spline with these coefficient increments is exactly a linear voltage
    # conversion. Each relative increment bounds the derivative everywhere.
    increments=np.diff([np.mean(knots[i+1:i+degree+1]) for i in range(count)])*3.3/domain
    accumulation=np.zeros((count,count-1))
    for i in range(count-1):
        accumulation[i+1:,i]=increments[i]
    if reference_dc:
        accumulation=np.column_stack((np.ones(count),accumulation))
    blocks=[]
    gram=np.zeros((count,count))
    for o in observations:
        raw=np.asarray(o['values'],float)
        if np.any((raw<0)|(raw>domain)):
            raise ValueError('direct curve ADC reference outside range')
        B=BSpline.design_matrix(raw,knots,degree).tocsr()
        means=np.asarray(B.mean(axis=0)).ravel()
        angle=2*np.pi*np.arange(len(raw))/o['period_samples']
        tone=np.column_stack((np.ones(len(raw)),np.sin(angle),np.cos(angle)))
        gram+=(B.T@B).toarray()
        if not reference_dc:
            gram-=len(raw)*np.outer(means,means)
        blocks.append((o,raw,B,means,tone))
    D=np.diff(np.eye(count),n=2,axis=0)
    # Penalize curvature in voltage, not coefficient magnitude or offsets.
    penalty=curvature*(D@accumulation).T@(D@accumulation)
    parameters=accumulation.shape[1]
    normal=accumulation.T@gram@accumulation+penalty+1e-12*np.eye(parameters)
    R=cholesky(normal,lower=False)
    relative=np.r_[0.,np.ones(count-1)] if reference_dc else np.ones(count-1)
    lower=np.r_[-.1,np.full(count-1,.72)] if reference_dc else .72
    upper=np.r_[.1,np.full(count-1,1.28)] if reference_dc else 1.28
    iterations=0
    for iterations in range(1,31):
        coefficients=accumulation@relative
        rhs=np.zeros(count)
        for o,raw,B,means,tone in blocks:
            fitted=np.linalg.lstsq(tone,B@coefficients,rcond=None)[0]
            direction=fitted[1:]/np.hypot(*fitted[1:])
            target=tone[:,1:]@direction*(o['amplitude_v']/2)
            if reference_dc:
                target+=o['offset_v']
            else:
                target-=target.mean()
            rhs+=np.asarray(B.T@target).ravel()
        z=solve_triangular(R.T,accumulation.T@rhs,lower=True)
        fit=lsq_linear(R,z,bounds=(lower,upper),tol=1e-12,lsmr_tol=1e-12,max_iter=200)
        if not fit.success:
            raise ValueError('direct ADC curve optimization did not converge')
        difference=float(np.max(np.abs(fit.x-relative)))
        relative=fit.x
        if difference<1e-9:
            break
    coefficients=accumulation@relative
    constant=(0. if reference_dc else
        float(np.mean([o['offset_v']-np.mean(B@coefficients) for o,_,B,_,_ in blocks])))
    codes=np.arange(0.,domain+1)
    volts=BSpline(knots,coefficients,degree)(codes)+constant
    low=max(0,int(min(raw.min() for _,raw,_,_,_ in blocks))+4)
    high=min(int(domain),int(max(raw.max() for _,raw,_,_,_ in blocks))-4)
    keep=(codes>=low)&(codes<=high)&(volts>.005)&(volts<3.295)
    transfer=dict(codes=codes[keep].tolist(),volts=volts[keep].tolist(),
        method='direct_ADC_reference_monotone_cubic_curve',
        fit=dict(segments=segments,degree=degree,curvature_penalty=curvature,
            iterations=iterations,reference_records=len(blocks),
            supporting_samples=sum(len(raw) for _,raw,_,_,_ in blocks),
            local_gain_bounds=[.72,1.28],
            reference_dc_constrained=reference_dc,
            target='independent sine reference voltage at fitted record phase',
            validation_samples_used=False))
    validate_transfer(transfer)
    return transfer


def refine_adc_codes(observations,fitted,sigma=6.):
    """Average independent phase-referenced voltages within each ADC code.

    Overlapping sine sweeps distinguish local converter error from a smooth
    voltage gain. Slight smoothing prevents reference noise from creating DNL.
    """
    from scipy.interpolate import PchipInterpolator
    grid=np.arange(4096,dtype=float)
    lookup=PchipInterpolator(fitted['input'],fitted['output'])(grid)
    prior=lookup.copy()
    blocks=[]
    for o in observations:
        raw=np.asarray(o['values'],int)
        angle=2*np.pi*np.arange(len(raw))/o['period_samples']
        B=np.column_stack((np.ones(len(raw)),np.sin(angle),np.cos(angle)))
        blocks.append((o,raw,B))
    count=sum((np.bincount(raw,minlength=4096) for _,raw,_ in blocks),np.zeros(4096))
    for _ in range(12):
        total=np.zeros(4096)
        for o,raw,B in blocks:
            fit=np.linalg.lstsq(B,lookup[raw],rcond=None)[0]
            fit[1:]*=(o['amplitude_v']/2)/np.hypot(fit[1],fit[2])
            target=B@fit
            total+=np.bincount(raw,weights=target,minlength=4096)
        correction=np.divide(total-count*lookup,np.maximum(count,1))
        weight=count/(count+20)
        correction=gaussian_filter1d(correction*weight,sigma)
        # Keep extrapolated ends at the smooth independently fitted gain.
        supported=np.flatnonzero(count>0)
        margin=np.minimum(grid-supported[0],supported[-1]-grid)
        correction*=np.clip(margin/48,0,1)
        lookup+=.5*correction
    # Retain the independently supported interval, including a small margin
    # at either sine turning point where code statistics are least reliable.
    low=int(min(o['values'].min() for o in observations))+4
    high=int(max(o['values'].max() for o in observations))-4
    keep=(grid>=low)&(grid<=high)&(lookup>.005)&(lookup<3.295)
    # Finite reference noise near a sine turning point can produce a large
    # local derivative despite smoothing. Shrink the code correction toward
    # its independently fitted smooth prior until every supported interval
    # has plausible gain. A cumulative monotonicity clamp cannot bound gain.
    baseline=prior[keep]
    correction=lookup[keep]-baseline
    slope=np.diff(baseline)
    delta=np.diff(correction)
    unit=3.3/4095
    lower,upper=.72*unit,1.28*unit
    if np.any((slope<lower)|(slope>upper)):
        raise ValueError('smooth ADC transfer has an implausible local gain')
    limits=[1.]
    if np.any(delta>0):
        limits.append(float(np.min((upper-slope[delta>0])/delta[delta>0])))
    if np.any(delta<0):
        limits.append(float(np.min((slope[delta<0]-lower)/-delta[delta<0])))
    weight=max(0.,min(limits))
    selected=baseline+weight*correction
    return dict(codes=grid[keep].tolist(),volts=selected.tolist(),
                method='independent_AC_phase_referenced_ADC_code_average',
                supporting_samples=int(sum(len(o['values']) for o in observations)),
                smoothing_sigma_codes=sigma,code_refinement_weight=weight,
                local_gain_bounds=[.72,1.28])


def _collect(service,store,args,instrument,audit,path,offset,amplitude,validation,configure):
    configure(offset,amplitude)
    time.sleep(max(.2,args.ro_settle))
    blocks=[]
    for _ in range(4):
        payload,meta,record=service.capture(args.ro_timeout)
        raw=np.frombuffer(payload,dtype='<u2').astype(float)
        if np.any((raw<=0)|(raw>=4095)):
            raise ValueError('dynamic voltage reference clipped')
        period=sine_period(payload)
        entry=dict(offset_v=offset,amplitude_v=amplitude,validation=validation,
                   period_samples=period,capture=record.path.relative_to(store.path).as_posix())
        audit['observations'].append(entry)
        record.info['context']['dynamic_amplitude_reference']=entry
        record.save()
        blocks.append(dict(entry,values=raw))
    atomic_json(path,audit)
    return blocks


def calibrate_adc_transfer(service,store,args,stderr,instrument,report,info,full_scale):
    path=report/'amplitude_calibration.json'
    audit=dict(complete=False,references_independent_of_matrix=True,observations=[],error='',
               reference_generator=getattr(instrument,'identity',''),
               planned_fit_points=len(REFERENCES),frames_per_point=4,
               calibration_model='original_local_ADC_voltage_lookup',
               fit_points=[dict(offset_v=offset,vpp_v=amp) for offset,amp in REFERENCES])
    observations=[]
    frequency=173.371
    def configure(offset,amp): instrument.reference('sine',offset,amp,frequency)
    try:
        for i,(offset,amp) in enumerate(REFERENCES):
            print(f'RO local ADC reference {i+1}/{len(REFERENCES)}: '
                  f'DC={offset:g}, Vpp={amp:g}',file=stderr,flush=True)
            observations+=_collect(service,store,args,instrument,audit,path,offset,amp,False,configure)
        fitted=fit_dynamic_spline(observations,4095.)
        transfer=refine_adc_codes(observations,fitted)
        validate_transfer(transfer)
        audit['local_transfer']=transfer
        audit['local_reference_captures']=[o['capture'] for o in observations]
        audit['fit_point_count']=len({(o['offset_v'],o['amplitude_v']) for o in observations})
        audit['fit_capture_count']=len(observations)
        audit['fit_sample_count']=sum(len(o['values']) for o in observations)
        checks=[]
        failures=[]
        amp=.72
        for i,offset in enumerate(ORIGINAL_CHECK_OFFSETS):
            print(f'RO ADC local check {i+1}/{len(ORIGINAL_CHECK_OFFSETS)}: '
                  f'DC={offset:g}, Vpp={amp:g}',file=stderr,flush=True)
            blocks=_collect(service,store,args,instrument,audit,path,offset,amp,True,configure)
            # Archive a failed check too, before raising and retaining the old
            # published calibration. Validation samples never fit the gain.
            amps=[tone_amplitude(calibrated_tone_values(o['values'],o['period_samples'],transfer),
                                 o['period_samples']) for o in blocks]
            checks.append(dict(offset_v=offset,reference_vpp_v=amp,frequency_hz=frequency,
                               vpp_v=float(np.mean(amps)),relative_bias=float(np.mean(amps)/amp-1)))
            audit['checks']=checks
            atomic_json(path,audit)
            try:
                check_operating_amplitude(transfer,blocks,offset,amp,frequency)
            except ValueError as error:
                failures.append(str(error))
                print(str(error),file=stderr,flush=True)
        if failures:
            audit['validation_failures']=failures
            raise ValueError(f'{len(failures)}/{len(checks)} independent operating amplitude checks failed; '
                             'candidate calibration was not published')
        transfer['reference_generator']=getattr(instrument,'identity','')
        calibration=replace(service.calibration,vpp_transfer=transfer)
        info['calibration']=calibration.to_dict()
        info['vpp_transfer_report']=path.name
        atomic_json(report/'calibration.json',info)
        service.publish_calibration(report/'calibration.json')
        audit.update(complete=True,transfer=transfer,checks=checks,absolute_accuracy_verified=False,
                     calibration_model='original_local_ADC_voltage_lookup',
                     validation_independent_of_gain_fit=True)
    except BaseException as error:
        audit['error']=f'{type(error).__name__}: {error}'
        raise
    finally:
        atomic_json(path,audit)


def calibrate_dac_transfer(service,store,args,stderr,instrument,report,full_scale):
    path=report/'arb_voltage_transfer.json'
    audit=dict(complete=False,references_independent_of_matrix=True,observations=[],error='')
    observations=[]
    size,rate,frequency=4000,200000.,250.
    apply_inverse=False
    # Five integer cycles at a much finer playback rate than the ADC.
    def configure(offset,amp):
        volts=offset+.5*amp*np.sin(2*np.pi*frequency*np.arange(size)/rate)
        words=np.rint((2*volts/full_scale-1)*32767).astype('<i2')
        config=dict(amplitude_v=full_scale,offset_v=full_scale/2,
                    sample_rate_hz=rate,loop_frequency_hz=rate/size)
        data=words.tobytes()
        if apply_inverse:
            from tools.amplitude_calibration import predistort_dac
            data=predistort_dac(data,config,instrument.playback_transfer)
        config=instrument.playback_calibration.apply(config)
        directory=report/'dynamic-arb-references'/f'{len(audit["observations"]):03d}'
        directory.mkdir(parents=True)
        config['readback']=instrument.configure(data,config,readback_dir=directory)
        atomic_json(directory/'configuration.json',config)
    try:
        for i,(offset,amp) in enumerate(REFERENCES):
            print(f'RO dynamic ARB reference {i+1}/{len(REFERENCES)}: DC={offset:g}, Vpp={amp:g}',file=stderr,flush=True)
            blocks=_collect(service,store,args,instrument,audit,path,offset,amp,False,configure)
            for o in blocks:
                o['values']=calibrated_tone_values(o['values'],o['period_samples'],service.calibration.vpp_transfer)
                o['amplitude_v']*=float(np.sinc(frequency/rate))
            observations+=blocks
        fitted=fit_dynamic_spline(observations,full_scale)
        actual=np.asarray(fitted['input'])
        nominal=np.asarray(fitted['output'])
        observed_low=min(float(o['values'].min()) for o in observations)
        observed_high=max(float(o['values'].max()) for o in observations)
        interior=(actual>=observed_low)&(actual<=observed_high)
        actual,nominal=actual[interior],nominal[interior]
        instrument.playback_transfer=dict(volts=actual.tolist(),normalized=(2*nominal/full_scale-1).tolist(),
                                          full_scale_v=full_scale,method=fitted['method'])
        apply_inverse=True
        checks=[]
        for offset in (.57,1.77,2.67):
            blocks=_collect(service,store,args,instrument,audit,path,offset,.43,True,configure)
            amps=[tone_amplitude(calibrated_tone_values(o['values'],o['period_samples'],service.calibration.vpp_transfer),o['period_samples']) for o in blocks]
            bias=float(np.mean(amps)/(.43*np.sinc(frequency/rate))-1)
            checks.append(dict(offset_v=offset,vpp_v=float(np.mean(amps)),relative_bias=bias))
            if abs(bias)>.001:
                raise ValueError(f'dynamic ARB amplitude check failed: {bias*100:g}%')
        audit.update(complete=True,transfer=instrument.playback_transfer,checks=checks)
    except BaseException as error:
        audit['error']=f'{type(error).__name__}: {error}'
        raise
    finally:
        atomic_json(path,audit)
