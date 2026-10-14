"""Independent piecewise-polygon / SciPy generator. Never imported by solver."""
import numpy as np
from scipy.signal import sawtooth


def waveform(kind,p,duty=.5,rise=0.,fall=0.):
    p=np.asarray(p)%1
    if kind=='sine':return np.sin(2*np.pi*p)
    if kind=='triangle':return sawtooth(2*np.pi*(p+.25),width=.5)
    if kind=='sawtooth':return -sawtooth(2*np.pi*p)
    r=max(rise,1e-12);f=max(fall,1e-12)
    return np.interp(p,[-r/2,r/2,duty-f/2,duty+f/2,1-r/2,1+r/2],[-1.,1.,1.,-1.,-1.,1.])


def generate(row,seed):
    nominal_fs=row.get('fs',20000.);fs=nominal_fs*(1+row.get('clock_error_ppm',0.)*1e-6)
    n=row['samples'];f=row['frequency'];duty=row.get('duty',.5)
    rise,fall=row.get('edges',(row.get('rise',0.),row.get('fall',0.)))
    p=np.arange(n)*f/fs+row['phase'];clean=waveform(row['kind'],p,duty,rise,fall)
    rng=np.random.default_rng(seed);noise=np.zeros(n)
    if row['noise']:
        if row.get('noise_model')=='multitone':
            frequencies=np.asarray(row['interference_frequencies_hz'])
            phases=rng.uniform(0,2*np.pi,len(frequencies));weights=rng.uniform(.4,1,len(frequencies))
            noise=np.sin(2*np.pi*np.arange(n)[:,None]/fs*frequencies+phases)@weights
            noise*=row['noise']*row['vpp']/np.ptp(noise)
        elif row.get('noise_model')=='white':
            noise=rng.normal(size=n);noise*=row['noise']*row['vpp']/np.ptp(noise)
        elif row['replay']:
            loop=row['replay'];size=4096;table=rng.normal(size=size);spec=np.fft.rfft(table)
            spec[np.fft.rfftfreq(size,1/size)>row['bandwidth']*loop]=0;spec[0]=0
            table=np.fft.irfft(spec,n=size);table*=row['noise']*row['vpp']/np.ptp(table)
            noise=np.interp((p/loop)%1,np.arange(size+1)/size,np.r_[table,table[0]])
        else:
            spec=np.fft.rfft(rng.normal(size=n));spec[np.fft.rfftfreq(n,1/fs)>f*row['bandwidth']]=0;spec[0]=0
            noise=np.fft.irfft(spec,n=n);noise*=row['noise']*row['vpp']/np.ptp(noise)
    baseline=.5*row['vpp']*clean+noise;room=3.3-.08-np.ptp(baseline)
    if room<0:raise ValueError('test envelope exceeds ADC')
    dc=.04-baseline.min()+row.get('dc_position',.5)*room
    adc=(dc+baseline)*4095/3.3
    if row.get('adc_model')=='inl':adc=adc+.35*np.sin(2*np.pi*adc/37)
    codes=np.rint(adc).astype('<u2')
    if np.any(codes>4095):raise ValueError('test has invalid ADC code')
    return codes,nominal_fs,dict(frequency_hz=f,vpp_v=row['vpp'],dc_v=float(dc),duty_fraction=duty if row['kind']=='square' else 0.)


def stress_rows(seed=617391):
    rng=np.random.default_rng(seed);rows=[]
    for kind in ('sine','square','triangle','sawtooth'):
        for i in range(24):
            fs=float(rng.uniform(19000,23500));f=float(rng.uniform(55,1100));d=float(rng.uniform(.06,.94))
            r=float(rng.uniform(.002,min(.12,.7*min(d,1-d))));fall=float(rng.uniform(.002,min(.12,.7*min(d,1-d))))
            row=dict(kind=kind,frequency=f,vpp=float(rng.uniform(.1,1.8)),phase=float(rng.random()),
                fs=fs,samples=4096 if i%2 else 8192,duty=d,rise=r,fall=fall,noise=float(rng.uniform(.1,.5)),
                bandwidth=float(rng.uniform(1.5,10)),replay=(1,7,13,0)[i%4],dc_position=float(rng.random()),
                scope='additional_in_scope',scenario='continuous_unseen_loop')
            if i<4:
                row.update(fs=20000.,frequency=(200.,200.,800.,800.)[i],phase=(.003,.619,.999999,.173)[i],
                    noise=(0.,.5,.2,.4)[i],duty=(.05,.95,.2,.8)[i],rise=.01,fall=.02,scenario='locked_or_duty_boundary')
            if 16<=i<20:row.update(noise_model='white',replay=0,scope='broadband_stress',scenario='white_noise')
            if i in (20,21):row.update(bandwidth=(18.,25.)[i-20],replay=1,scope='out_of_nuisance_prior',scenario='wide_coherent_band')
            if i==22:row.update(clock_error_ppm=35.,scope='metadata_error',scenario='clock_35ppm')
            if i==23:row.update(adc_model='inl',scope='metadata_error',scenario='unknown_inl')
            rows.append(row)
    return rows
