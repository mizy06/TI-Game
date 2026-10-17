"""Dynamic AC calibration from dedicated sine references, independent of cases."""
from dataclasses import replace
import time
import numpy as np
from numpy.polynomial.chebyshev import chebvander, chebval
from scipy.interpolate import BSpline
from scipy.ndimage import gaussian_filter1d
from tools.capture_store import atomic_json
from tools.amplitude_calibration import validate_transfer, voltage_values
from tools.acquisition_calibration import sine_period

REFERENCES=tuple((offset,.72) for offset in (.40,.72,1.04,1.36,1.68,2.00,2.32,2.64,2.90))


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


def refine_adc_codes(observations,fitted,sigma=6.):
    """Average independent phase-referenced voltages within each ADC code.

    Overlapping sine sweeps distinguish local converter error from a smooth
    voltage gain. Slight smoothing prevents reference noise from creating DNL.
    """
    from scipy.interpolate import PchipInterpolator
    grid=np.arange(4096,dtype=float)
    lookup=PchipInterpolator(fitted['input'],fitted['output'])(grid)
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
    # A monotone quantizer must preserve code ordering after noise averaging.
    selected=lookup[keep]
    selected=np.maximum.accumulate(selected-np.arange(len(selected))*1e-7)+np.arange(len(selected))*1e-7
    return dict(codes=grid[keep].tolist(),volts=selected.tolist(),
                method='independent_AC_phase_referenced_ADC_code_average',
                supporting_samples=int(sum(len(o['values']) for o in observations)),
                smoothing_sigma_codes=sigma)


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
    audit=dict(complete=False,references_independent_of_matrix=True,observations=[],error='')
    observations=[]
    def configure(offset,amp): instrument.reference('sine',offset,amp,173.371)
    try:
        for i,(offset,amp) in enumerate(REFERENCES):
            print(f'RO dynamic ADC reference {i+1}/{len(REFERENCES)}: DC={offset:g}, Vpp={amp:g}',file=stderr,flush=True)
            observations+=_collect(service,store,args,instrument,audit,path,offset,amp,False,configure)
        fitted=fit_dynamic_spline(observations,4095.)
        # Retain the old midpoint convention. The AC fit supplies voltage
        # differences; its arbitrary integration constant never changes DC.
        transfer=refine_adc_codes(observations,fitted)
        validate_transfer(transfer)
        checks=[]
        for offset in (.57,1.77,2.67):
            # Keep the DDS amplifier range constant while varying its offset.
            # The separate ARB checks vary digital amplitude on a fixed range.
            blocks=_collect(service,store,args,instrument,audit,path,offset,.72,True,configure)
            amps=[tone_amplitude(voltage_values(o['values'],transfer),o['period_samples']) for o in blocks]
            bias=float(np.mean(amps)/.72-1)
            checks.append(dict(offset_v=offset,vpp_v=float(np.mean(amps)),relative_bias=bias))
            if abs(bias)>.001:
                raise ValueError(f'dynamic ADC amplitude check failed: {bias*100:g}%')
        calibration=replace(service.calibration,vpp_transfer=transfer)
        info['calibration']=calibration.to_dict()
        info['vpp_transfer_report']=path.name
        atomic_json(report/'calibration.json',info)
        service.publish_calibration(report/'calibration.json')
        audit.update(complete=True,transfer=transfer,checks=checks,absolute_accuracy_verified=False)
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
                o['values']=voltage_values(o['values'],service.calibration.vpp_transfer)
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
            amps=[tone_amplitude(voltage_values(o['values'],service.calibration.vpp_transfer),o['period_samples']) for o in blocks]
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
