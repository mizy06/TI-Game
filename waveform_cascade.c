/* Independent frequency and robust time-domain backstops for RT3.
 * No heap, no extra frame buffer; both routines read the same immutable ADC frame.
 */
#include "waveform_analyzer.h"
#include <stdint.h>
#include <string.h>

#define CBINS 64U
#define YMAX 128U
#define QPHASE 4294967296ULL

/* sin(2*pi*(i+1/2)/128), Q10. Flash-resident; interpolate neighboring samples. */
static const int16_t sine128[128] = {
 25,75,125,175,224,273,321,369,415,460,505,548,590,630,669,706,
 742,775,807,837,865,891,915,936,955,972,987,999,1009,1016,1021,1024,
 1024,1021,1016,1009,999,987,972,955,936,915,891,865,837,807,775,742,
 706,669,630,590,548,505,460,415,369,321,273,224,175,125,75,25,
 -25,-75,-125,-175,-224,-273,-321,-369,-415,-460,-505,-548,-590,-630,-669,-706,
 -742,-775,-807,-837,-865,-891,-915,-936,-955,-972,-987,-999,-1009,-1016,-1021,-1024,
 -1024,-1021,-1016,-1009,-999,-987,-972,-955,-936,-915,-891,-865,-837,-807,-775,-742,
 -706,-669,-630,-590,-548,-505,-460,-415,-369,-321,-273,-224,-175,-125,-75,-25
};
static int32_t sin_q10(uint32_t phase)
{
    uint32_t pos = phase >> 25;
    uint32_t fraction = (phase >> 17) & 255U;
    uint32_t a = (pos + 127U) & 127U, b = pos;
    /* Table is centered at i+1/2, so the interpolation pivot is half a bin. */
    if (fraction >= 128U) {
        a = pos;
        b = (pos + 1U) & 127U;
        fraction -= 128U;
    } else {
        fraction += 128U;
    }
    return ((int32_t)sine128[a] * (int32_t)(256U-fraction) +
            (int32_t)sine128[b] * (int32_t)fraction) / 256;
}
static uint32_t isqrt64(uint64_t n)
{
    uint64_t bit = 1ULL << 62, root = 0;
    while (bit > n) bit >>= 2;
    while (bit) {
        if (n >= root + bit) { n -= root + bit; root = (root >> 1) + bit; }
        else root >>= 1;
        bit >>= 2;
    }
    return (uint32_t)root;
}
static uint32_t absdiff(uint32_t a, uint32_t b) { return a>b ? a-b : b-a; }
static void init_result(const waveform_frame_t *f, waveform_result_t *r)
{
    memset(r,0,sizeof(*r));
    r->type = WAVEFORM_TYPE_UNKNOWN;
    r->status = WAVEFORM_STATUS_BAD_ARGUMENT;
    r->algorithm_version = WAVEFORM_ALGORITHM_VERSION;
    if (f) r->sequence = f->sequence;
}
static bool check_frame(const waveform_frame_t *f, waveform_result_t *r)
{
    uint32_t i, low=0, high=0;
    if (!f || !f->samples) return false;
    if (f->sample_count < 1024 || f->sample_count > 4096) {
        r->status=WAVEFORM_STATUS_INSUFFICIENT_SAMPLES; return false;
    }
    if (f->adc_full_scale < 255 || f->adc_full_scale > 16383 ||
        f->sample_rate_millihz < 10000000 || f->sample_rate_millihz > 50000000 ||
        f->adc_reference_microvolts < 1000000 || f->adc_reference_microvolts > 5500000 ||
        f->frontend_gain_ppm < 100000 || f->frontend_gain_ppm > 10000000 ||
        !(f->flags & WAVEFORM_FRAME_FLAG_CONTIGUOUS)) {
        r->status=WAVEFORM_STATUS_BAD_METADATA; return false;
    }
    if (!(f->flags & WAVEFORM_FRAME_FLAG_CLOCK_CALIBRATED))
        r->quality_flags |= WAVEFORM_QUALITY_CLOCK_UNCALIBRATED;
    if (!(f->flags & WAVEFORM_FRAME_FLAG_VREF_CALIBRATED))
        r->quality_flags |= WAVEFORM_QUALITY_VREF_UNCALIBRATED;
    for (i=0;i<f->sample_count;i++) {
        uint32_t v=f->samples[i];
        if (v>f->adc_full_scale) r->invalid_samples++;
        if (v<=3) low++;
        if (v+3>=f->adc_full_scale) high++;
    }
    r->low_clip_samples=(uint16_t)low;
    r->high_clip_samples=(uint16_t)high;
    if (r->invalid_samples) r->status=WAVEFORM_STATUS_ADC_SAMPLE_INVALID;
    else if (low > f->sample_count/20) r->status=WAVEFORM_STATUS_ADC_LOW_CLIPPED;
    else if (high > f->sample_count/20) r->status=WAVEFORM_STATUS_ADC_HIGH_CLIPPED;
    else return true;
    return false;
}
static bool finish(const waveform_frame_t *f, waveform_result_t *r,
                   waveform_type_t type, uint32_t freq_mhz,
                   uint32_t vpp_counts_q16, uint32_t duty_ppm,
                   uint16_t score, uint16_t second, uint16_t required_margin)
{
    uint64_t uv;
    if (freq_mhz < 98000 || freq_mhz > 402000) {
        r->status=WAVEFORM_STATUS_FREQUENCY_OUT_OF_RANGE; return false;
    }
    if (vpp_counts_q16 < (20UL << 16)) {
        r->status=WAVEFORM_STATUS_SIGNAL_TOO_SMALL; return false;
    }
    if (score < 760 || score < second + required_margin) {
        r->status=WAVEFORM_STATUS_CLASSIFICATION_AMBIGUOUS; return false;
    }
    r->type=type; r->frequency_millihz=freq_mhz;
    r->period_q16_samples=(uint32_t)(((uint64_t)f->sample_rate_millihz << 16)/freq_mhz);
    r->vpp_counts_q16=vpp_counts_q16;
    uv=(uint64_t)vpp_counts_q16*f->adc_reference_microvolts;
    uv/=((uint64_t)f->adc_full_scale*65536ULL);
    uv=uv*1000000ULL/f->frontend_gain_ppm;
    if (uv>UINT32_MAX) uv=UINT32_MAX;
    r->vpp_microvolts=(uint32_t)uv;
    r->vpp_millivolts=(uint32_t)((uv+500)/1000);
    if (type==WAVEFORM_TYPE_SQUARE) {
        r->duty_ppm=duty_ppm; r->duty_permille=(uint16_t)((duty_ppm+500)/1000);
    }
    r->best_score_permille=score; r->second_score_permille=second;
    r->confidence_permille=score; r->model_margin_permille=score-second;
    if (score-second < 15) r->quality_flags|=WAVEFORM_QUALITY_LOW_MODEL_MARGIN;
    r->status=WAVEFORM_STATUS_OK; return true;
}
/* Direct narrowband Fourier projection. The complex coefficient is retained
 * for phase-aware high-harmonic evidence; no FFT RAM or floating point. */
typedef struct {int64_t re,im;} spectral_coefficient_t;
static uint64_t projection(const waveform_frame_t *f, uint32_t freq_mhz,
                           uint16_t stride, int32_t dc, uint32_t *amplitude,
                           spectral_coefficient_t *coefficient)
{
    int64_t re=0, im=0;
    uint32_t phase=0, i;
    uint32_t step=(uint32_t)(((uint64_t)freq_mhz*stride*QPHASE +
                    f->sample_rate_millihz/2)/f->sample_rate_millihz);
    uint32_t n=(f->sample_count+stride-1U)/stride;
    for (i=0;i<f->sample_count;i+=stride) {
        int32_t x=(int32_t)f->samples[i]-dc;
        im+=(int64_t)x*sin_q10(phase);
        re+=(int64_t)x*sin_q10(phase+0x40000000UL);
        phase+=step;
    }
    /* Shift before squaring: 14-bit full-scale inputs must not overflow u64. */
    re >>= 4; im >>= 4;
    if (coefficient) {coefficient->re=re;coefficient->im=im;}
    if (amplitude) *amplitude=(uint32_t)((32ULL*isqrt64((uint64_t)(re*re+im*im))+
                                          (uint64_t)n*1024/2)/((uint64_t)n*1024));
    return (uint64_t)(re*re+im*im);
}
static uint16_t spectral_fit(const uint32_t a[5], uint16_t ratios[5],
                             uint32_t *duty, const int16_t high[3][2])
{
    uint64_t best[4]={UINT64_MAX,UINT64_MAX,UINT64_MAX,UINT64_MAX};
    uint32_t d, k, norm=a[0] ? a[0] : 1;
    for (k=0;k<5;k++) ratios[k]=(uint16_t)((((uint64_t)a[k]*1000U)/norm)>65535U ?
                          65535U : (((uint64_t)a[k]*1000U)/norm));
    best[0]=best[1]=best[2]=0;
    for (k=1;k<5;k++) {
        int32_t e[3]={0, (k%2U==0U)?(int32_t)(1000U/((k+1U)*(k+1U))):0,
                      (int32_t)(1000U/(k+1U))};
        uint32_t t;
        for(t=0;t<3;t++) {
            int32_t diff=(int32_t)ratios[k]-e[t];
            best[t]+=(uint64_t)diff*diff;
        }
    }
    /* A coherent third harmonic can be injected or cancel the natural
     * triangle third. Limit its influence, then test the phase of the fifth,
     * seventh and ninth harmonics. Triangle signs are +,-,+ relative to its
     * fundamental; a sine has no systematic high-harmonic signature. */
    for (k=0;k<2;k++) {
        int32_t third=(int32_t)ratios[2]-(k ? 111 : 0);
        uint64_t old=(uint64_t)third*third, capped=old<1500U ? old : 1500U;
        uint64_t high_loss=0;
        uint32_t h;
        const int32_t expected[3]={40,-20,12};
        for(h=0;h<3;h++) {
            int32_t re=(int32_t)high[h][0]-(k ? expected[h] : 0);
            int32_t im=high[h][1];
            high_loss+=(uint64_t)((int64_t)re*re+(int64_t)im*im);
        }
        best[k]=best[k]-old+capped+3U*high_loss;
    }
    *duty=500000;
    for (d=200000;d<=800000;d+=10000) {
        uint32_t fundamental;
        uint64_t loss=0;
        fundamental=(uint32_t)(sin_q10((uint32_t)(((uint64_t)d*QPHASE)/2000000ULL)));
        if (!fundamental) continue;
        for (k=1;k<5;k++) {
            int32_t sine=sin_q10((uint32_t)(((uint64_t)d*(k+1U)*QPHASE)/2000000ULL));
            int32_t diff=(int32_t)ratios[k] -
                         (int32_t)(1000ULL*(uint32_t)(sine<0?-sine:sine)/
                                   ((k+1U)*fundamental));
            loss+=(uint64_t)diff*diff;
        }
        if (loss<best[3]) {best[3]=loss;*duty=d;}
    }
    {
        uint32_t winner=0,runner=1;
        for(k=1;k<4;k++) if (best[k]<best[winner]) winner=k;
        for(k=0;k<4;k++) if (k!=winner && (runner==winner || best[k]<best[runner])) runner=k;
        /* Return winner in high byte; margin derived from runner's fit. */
        uint32_t s=1000U-(uint32_t)(best[winner]/1000U);
        uint32_t next=1000U-(uint32_t)(best[runner]/1000U);
        if (s>1000) s=0;
        if (next>1000) next=0;
        ratios[0]=(uint16_t)next;
        return (uint16_t)((winner<<12) | (s>1000 ? 0 : s));
    }
}
/* Align the odd Fourier coefficients to the fundamental phase. An odd
 * triangle has 5th/7th/9th sine ratios +1/25, -1/49, +1/81. The imaginary
 * part is kept in the loss so a narrowband interferer at the right magnitude
 * but wrong relative phase does not masquerade as a triangle. */
static void high_harmonics(const waveform_frame_t *f, uint32_t freq_mhz,
                           int32_t center, spectral_coefficient_t first,
                           int16_t high[3][2])
{
    uint64_t magnitude=isqrt64((uint64_t)(first.re*first.re+first.im*first.im));
    int32_t cosine, sine, qre=16384, qim=0;
    uint32_t k, next=0;
    uint16_t stride=(freq_mhz*36U<f->sample_rate_millihz) ? 2U : 1U;
    if(!magnitude) {memset(high,0,3U*2U*sizeof(int16_t));return;}
    cosine=(int32_t)((first.im*16384)/((int64_t)magnitude));
    sine=(int32_t)((first.re*16384)/((int64_t)magnitude));
    for(k=1;k<=9;k++) {
        int32_t re=(int32_t)(((int64_t)qre*cosine-(int64_t)qim*sine)/16384);
        int32_t im=(int32_t)(((int64_t)qre*sine+(int64_t)qim*cosine)/16384);
        qre=re;qim=im;
        if(k==5U || k==7U || k==9U) {
            spectral_coefficient_t c;
            int64_t v;
            (void)projection(f,freq_mhz*k,stride,center,0,&c);
            v=(c.re*qim+c.im*qre)*1000/
              ((int64_t)magnitude*16384);
            if(v>32767) v=32767;
            if(v< -32768) v= -32768;
            high[next][0]=(int16_t)v;
            v=(c.re*qre-c.im*qim)*1000/
              ((int64_t)magnitude*16384);
            if(v>32767) v=32767;
            if(v< -32768) v= -32768;
            high[next][1]=(int16_t)v;
            next++;
        }
    }
}
/* For square waves, magnitude harmonics cannot distinguish d from 1-d.
 * Find the center between robust 5/95-percentile levels, then average the
 * high and low populations to estimate duty and Vpp despite sparse spikes. */
static bool square_levels(const waveform_frame_t *f,uint32_t *duty,
                          uint32_t *vpp_counts_q16)
{
    uint16_t histogram[256]={0};
    uint32_t i,acc=0,lo=0,hi=255,threshold;
    uint64_t high_sum=0,low_sum=0;
    uint32_t high_count=0,low_count=0;
    for(i=0;i<f->sample_count;i++)
        histogram[((uint32_t)f->samples[i]*256U)/((uint32_t)f->adc_full_scale+1U)]++;
    for(i=0;i<256;i++) {
        acc+=histogram[i];
        if(acc>=f->sample_count/20U) {lo=i;break;}
    }
    acc=0;
    for(i=0;i<256;i++) {
        acc+=histogram[i];
        if(acc>=19U*f->sample_count/20U) {hi=i;break;}
    }
    if(hi<=lo+1U) return false;
    threshold=((lo+hi+1U)*((uint32_t)f->adc_full_scale+1U))/512U;
    for(i=0;i<f->sample_count;i++) {
        uint32_t v=f->samples[i];
        if(v>=threshold) {high_count++;high_sum+=v;}
        else {low_count++;low_sum+=v;}
    }
    if(!high_count || !low_count) return false;
    *duty=(uint32_t)(((uint64_t)high_count*1000000U+f->sample_count/2U)/f->sample_count);
    *vpp_counts_q16=(uint32_t)(((high_sum*65536U)/high_count)-
                               ((low_sum*65536U)/low_count));
    return true;
}
bool waveform_analyze_frequency(const waveform_frame_t *f, waveform_result_t *r)
{
    uint32_t i,center=0,best_freq=0,a[5],duty;
    uint64_t best=0;
    uint16_t ratios[5],fit,score,second;
    int16_t high[3][2];
    spectral_coefficient_t first;
    waveform_type_t type;
    uint32_t vpp;
    if (!r) return false;
    init_result(f,r);
    if (!check_frame(f,r)) return false;
    for(i=0;i<f->sample_count;i++) center+=f->samples[i];
    center/=f->sample_count;
    r->dc_offset_counts=(uint16_t)center;
    for(i=98000;i<=402000;i+=2000) {
        uint64_t p=projection(f,i,8,(int32_t)center,0,0);
        if(p>best) {best=p; best_freq=i;}
    }
    best=0;
    for(i=best_freq-2000;i<=best_freq+2000;i+=100) {
        uint64_t p=projection(f,i,4,(int32_t)center,0,0);
        if(p>best) {best=p;best_freq=i;}
    }
    for(i=0;i<5;i++) projection(f,best_freq*(i+1U),4,(int32_t)center,&a[i],
                                i ? 0 : &first);
    if(a[0]<12) {r->status=WAVEFORM_STATUS_SIGNAL_TOO_SMALL;return false;}
    high_harmonics(f,best_freq,(int32_t)center,first,high);
    fit=spectral_fit(a,ratios,&duty,high);
    { static const waveform_type_t map[4]={WAVEFORM_TYPE_SINE,
        WAVEFORM_TYPE_TRIANGLE,WAVEFORM_TYPE_SAWTOOTH,WAVEFORM_TYPE_SQUARE};
      type=map[fit>>12]; }
    score=fit&4095U; second=ratios[0];
    /* Fundamental amplitude is in counts, using Fourier coefficients of
     * sine/triangle/saw/square. pi is approximated as 31416/10000. */
    if(type==WAVEFORM_TYPE_SINE) vpp=a[0]*2U;
    else if(type==WAVEFORM_TYPE_TRIANGLE) vpp=(uint32_t)(((uint64_t)a[0]*24674U+5000)/10000);
    else if(type==WAVEFORM_TYPE_SAWTOOTH) vpp=(uint32_t)(((uint64_t)a[0]*31416U+5000)/10000);
    else {
        uint32_t s=(uint32_t)sin_q10((uint32_t)(((uint64_t)duty*QPHASE)/2000000ULL));
        if(!s) {r->status=WAVEFORM_STATUS_CLASSIFICATION_AMBIGUOUS;return false;}
        vpp=(uint32_t)(((uint64_t)a[0]*31416U*1024U+10000U*s)/(20000U*s));
    }
    r->accepted_cycles=(uint16_t)(((uint64_t)f->sample_count*best_freq)/f->sample_rate_millihz);
    if(type==WAVEFORM_TYPE_SQUARE) {
        uint32_t level_vpp;
        (void)square_levels(f,&duty,&level_vpp);
    }
    return finish(f,r,type,best_freq,vpp<<16,duty,score,second,5U);
}

/* Fixed-window cumulative mean normalized squared difference. The first
 * good local minimum is preferred to a deeper multiple-period minimum. */
static uint32_t yin_period(const waveform_frame_t *f, uint16_t *quality)
{
    uint64_t cumulative=0;
    uint64_t d[YMAX+1];
    uint32_t normalized[YMAX+1];
    uint32_t lo=(uint32_t)(((uint64_t)f->sample_rate_millihz)/1608000);
    uint32_t hi=(uint32_t)(((uint64_t)f->sample_rate_millihz+392000)/392000);
    uint32_t lag, i, selected=0, minimum=UINT32_MAX;
    if(lo<6) lo=6;
    if(hi>YMAX) hi=YMAX;
    if(lo>hi) return 0;
    /* Use a fixed comparison window for every lag, keeping d(tau) comparable. */
    for(lag=1;lag<=hi;lag++) {
        uint64_t sum=0;
        for(i=0;i+4U*hi<f->sample_count;i+=4U) {
            int32_t delta=(int32_t)f->samples[i]-(int32_t)f->samples[i+lag*4U];
            sum+=(uint64_t)(delta*delta);
        }
        d[lag]=sum;
        cumulative+=d[lag];
        normalized[lag]=(uint32_t)(((uint64_t)d[lag]*lag*1000U)/(cumulative?cumulative:1));
    }
    for(lag=lo;lag<=hi;lag++) {
        if(normalized[lag]<minimum) {minimum=normalized[lag];selected=lag;}
        if(lag>=lo && lag<hi && normalized[lag]<540 &&
           normalized[lag]<=normalized[lag-1] && normalized[lag]<normalized[lag+1]) {
            selected=lag;minimum=normalized[lag];break;
        }
    }
    *quality=(uint16_t)minimum;
    if(minimum>650 || !selected) return 0;
    /* Refine the four-sample coarse grid at one-sample lag spacing. Every
     * lag uses the same comparison window to keep the minima comparable. */
    {
        uint64_t fine[13];
        uint32_t base=selected*4U, first=base>6U ? base-6U : 1U;
        uint32_t last=base+6U, lag_sample, chosen=base;
        uint64_t lowest=UINT64_MAX;
        int32_t correction=0;
        for(lag_sample=first;lag_sample<=last;lag_sample++) {
            uint64_t sum=0;
            for(i=0;i+4U*hi+6U<f->sample_count;i+=2U) {
                int32_t delta=(int32_t)f->samples[i]-(int32_t)f->samples[i+lag_sample];
                sum+=(uint64_t)(delta*delta);
            }
            fine[lag_sample-first]=sum;
            if(sum<lowest) {lowest=sum;chosen=lag_sample;}
        }
        if(chosen>first && chosen<last) {
            int64_t left=(int64_t)fine[chosen-1U-first];
            int64_t right=(int64_t)fine[chosen+1U-first];
            int64_t mid=(int64_t)fine[chosen-first];
            int64_t denom=2*(left+right-2*mid);
            if(denom>0) correction=(int32_t)(((left-right)*65536)/denom);
            if(correction>32768) correction=32768;
            if(correction< -32768) correction= -32768;
        }
        return (uint32_t)((int32_t)(chosen*65536U)+correction);
    }
}
typedef struct {int32_t sum;uint16_t lo,hi,count;} robust_bin_t;
static robust_bin_t rb[CBINS]; /* 64*12 bytes, reused across backstop calls. */
static int16_t folded[CBINS], model[CBINS];
static void robust_fold(const waveform_frame_t *f, uint32_t freq_mhz)
{
    uint32_t phase=0,i,b;
    uint32_t step=(uint32_t)(((uint64_t)freq_mhz*QPHASE)/f->sample_rate_millihz);
    memset(rb,0,sizeof(rb));
    /* Decimating by four left empty phase bins at integer periods. */
    for(i=0;i<f->sample_count;i++) {
        uint16_t v=f->samples[i];
        robust_bin_t *p=&rb[phase>>26];
        if(!p->count || v<p->lo) p->lo=v;
        if(!p->count || v>p->hi) p->hi=v;
        p->sum+=v; p->count++; phase+=step;
    }
    for(b=0;b<CBINS;b++) {
        folded[b]=(int16_t)(rb[b].count>2 ?
                    (rb[b].sum-rb[b].lo-rb[b].hi)/(rb[b].count-2) :
                    (rb[b].count ? rb[b].sum/rb[b].count : 0));
    }
    /* Exactly 400 Hz at 20 kS/s has only 50 distinct phases. */
    for(b=0;b<CBINS;b++) if(!rb[b].count) {
        uint32_t before=1U,after=1U;
        while(before<CBINS && !rb[(b+CBINS-before)&(CBINS-1U)].count) before++;
        while(after<CBINS && !rb[(b+after)&(CBINS-1U)].count) after++;
        if(before<CBINS && after<CBINS)
            folded[b]=(int16_t)(((int32_t)folded[(b+CBINS-before)&(CBINS-1U)]*(int32_t)after+
                                  (int32_t)folded[(b+after)&(CBINS-1U)]*(int32_t)before)/
                                  (int32_t)(before+after));
    }
}
/* YIN gives a robust coarse period, but sub-sample curvature can be biased
 * by correlated interference. Search nearby frequencies for the sharpest
 * robust folded waveform, entirely in the time domain. */
static uint64_t fold_energy(void)
{
    int64_t total=0;
    uint64_t variance=0;
    uint32_t b;
    for(b=0;b<CBINS;b++) total+=folded[b];
    for(b=0;b<CBINS;b++) {
        int64_t deviation=(int64_t)folded[b]*CBINS-total;
        variance+=(uint64_t)(deviation*deviation);
    }
    return variance;
}
static uint32_t refine_fold_frequency(const waveform_frame_t *f,uint32_t freq)
{
    uint32_t best_freq=freq,coarse=freq/500U, fine=freq/5000U;
    uint64_t best=0;
    int32_t offset;
    if(!coarse) coarse=1;
    if(!fine) fine=1;
    for(offset=-15;offset<=15;offset++) {
        int64_t trial=(int64_t)freq+(int64_t)offset*coarse;
        uint64_t score;
        if(trial<98000 || trial>402000) continue;
        robust_fold(f,(uint32_t)trial);
        score=fold_energy();
        if(score>best) {best=score;best_freq=(uint32_t)trial;}
    }
    freq=best_freq;
    for(offset=-10;offset<=10;offset++) {
        int64_t trial=(int64_t)freq+(int64_t)offset*fine;
        uint64_t score;
        if(trial<98000 || trial>402000) continue;
        robust_fold(f,(uint32_t)trial);
        score=fold_energy();
        if(score>best) {best=score;best_freq=(uint32_t)trial;}
    }
    return best_freq;
}
static int16_t template_value(waveform_type_t type, uint32_t phase,
                              uint32_t duty, uint32_t edge)
{
    uint32_t p=phase>>16; /* unit interval in 0..65535 */
    if(type==WAVEFORM_TYPE_SINE) return (int16_t)sin_q10(phase);
    if(type==WAVEFORM_TYPE_TRIANGLE)
        return (int16_t)(p<16384 ? (int32_t)p/16 :
                         (p<49152 ? 2048-(int32_t)p/16 : (int32_t)p/16-4096));
    if(type==WAVEFORM_TYPE_SAWTOOTH) return (int16_t)(1024-(int32_t)p/32);
    {
        int32_t rise=(int32_t)p;
        int32_t fall=(int32_t)p-(int32_t)(duty*65536ULL/1000000ULL);
        int32_t half=(int32_t)(edge*65536ULL/2000000ULL);
        if(rise>32767) rise-=65536;
        if(fall>32767) fall-=65536;
        if(fall< -32768) fall+=65536;
        if(half>0 && rise>=-half && rise<=half)
            return (int16_t)((rise*1024)/half);
        if(half>0 && fall>=-half && fall<=half)
            return (int16_t)(-(fall*1024)/half);
        return (int16_t)(p<duty*65536ULL/1000000ULL ? 1024 : -1024);
    }
}
static void remove_harmonic(int16_t values[CBINS], uint32_t harmonic)
{
    int64_t sin_sum=0,cos_sum=0;
    int32_t sin_amplitude,cos_amplitude;
    uint32_t b;
    for(b=0;b<CBINS;b++) {
        uint32_t phase=(uint32_t)(((uint64_t)b*harmonic*QPHASE)/CBINS);
        sin_sum+=(int32_t)values[b]*sin_q10(phase);
        cos_sum+=(int32_t)values[b]*sin_q10(phase+0x40000000UL);
    }
    sin_amplitude=(int32_t)(2*sin_sum/(CBINS*1024));
    cos_amplitude=(int32_t)(2*cos_sum/(CBINS*1024));
    for(b=0;b<CBINS;b++) {
        uint32_t phase=(uint32_t)(((uint64_t)b*harmonic*QPHASE)/CBINS);
        values[b]=(int16_t)((int32_t)values[b]-
            (sin_amplitude*sin_q10(phase)+
             cos_amplitude*sin_q10(phase+0x40000000UL))/1024);
    }
}
static int16_t notch_observed[2][CBINS];
static uint32_t fit_template(waveform_type_t type, uint32_t duty, uint32_t edge,
                             uint16_t *shift, uint32_t *vpp_q16,
                             const int16_t *observed, uint32_t notch_order,
                             uint32_t *precise_score)
{
    int32_t mean=0,mt=0;
    uint64_t energy_y=0,energy_t=0,best_cov=0;
    uint32_t s,b,best_shift=0;
    for(b=0;b<CBINS;b++) {
        mean+=observed[b];
        model[b]=template_value(type,(uint32_t)(((uint64_t)b*QPHASE)/CBINS),duty,edge);
        mt+=model[b];
    }
    if(notch_order) {
        remove_harmonic(model,notch_order);
        mt=0;
        for(b=0;b<CBINS;b++) mt+=model[b];
    }
    for(b=0;b<CBINS;b++) {
        int32_t y=(int32_t)observed[b]*64-mean, t=(int32_t)model[b]*64-mt;
        energy_y+=(uint64_t)((int64_t)y*y);
        energy_t+=(uint64_t)((int64_t)t*t);
    }
    if(!energy_y || !energy_t) return 0;
    for(s=0;s<CBINS;s++) {
        int64_t cov=0;
        for(b=0;b<CBINS;b++)
            cov+=(int64_t)((int32_t)observed[b]*64-mean)*((int32_t)model[(b+s)&(CBINS-1)]*64-mt);
        if(cov>0 && (uint64_t)cov>best_cov) {best_cov=(uint64_t)cov;best_shift=s;}
    }
    *shift=(uint16_t)best_shift;
    /* Re-scale before squaring to stay within 64-bit on 14-bit input. */
    {
        uint64_t cov=best_cov>>16, ey=energy_y>>16, et=energy_t>>16;
        uint64_t score=et ? (cov*cov*1000ULL)/(ey*et) : 0;
        uint64_t amplitude=et ? (cov*2048ULL*65536ULL)/et : 0;
        if(amplitude>UINT32_MAX) amplitude=UINT32_MAX;
        *vpp_q16=(uint32_t)amplitude;
        if(precise_score) {
            uint64_t c=best_cov>>20, y=energy_y>>20, t=energy_t>>20;
            uint64_t precise;
            while(c>1000000U) {c>>=1;y>>=1;t>>=1;}
            precise=(y && t) ? c*c*1000000ULL/(y*t) : 0;
            *precise_score=(uint32_t)(precise>1000000 ? 1000000 : precise);
        }
        return (uint32_t)(score>1000 ? 1000 : score);
    }
}
bool waveform_analyze_robust(const waveform_frame_t *f, waveform_result_t *r)
{
    uint16_t yin_quality=0,shift=0,chosen_shift=0;
    uint32_t period, freq, d, vpp=0,trial_vpp=0,chosen_duty=0;
    uint32_t class_score[4]={0},class_vpp[4]={0},class_duty[4]={0};
    uint32_t notch_precise[2][2]={{0}},notch_vpp[2][2]={{0}};
    uint16_t required_margin=5U;
    uint32_t t, best=0, second=0, winner=0;
    if(!r) return false;
    init_result(f,r);
    if(!check_frame(f,r)) return false;
    period=yin_period(f,&yin_quality);
    if(!period) {r->status=WAVEFORM_STATUS_PERIOD_NOT_FOUND;return false;}
    freq=(uint32_t)(((uint64_t)f->sample_rate_millihz<<16)/period);
    /* A coarse noisy period can lie just past the permitted 98..402 Hz
     * analysis guard. Start the independent fold search at that guard edge,
     * while rejecting estimates more than 3 Hz outside it. */
    if(freq<95000 || freq>405000) {
        r->status=WAVEFORM_STATUS_FREQUENCY_OUT_OF_RANGE;return false;
    }
    if(freq<98000) freq=98000;
    if(freq>402000) freq=402000;
    freq=refine_fold_frequency(f,freq);
    robust_fold(f,freq);
    for(t=0;t<4;t++) {
        if(t!=WAVEFORM_TYPE_SQUARE) {
            class_score[t]=fit_template((waveform_type_t)t,500000,0,&shift,&trial_vpp,
                                        folded,0,0);
            class_vpp[t]=trial_vpp;
        } else {
            for(d=200000;d<=800000;d+=10000) {
                uint32_t edge;
                for(edge=0;edge<=100000;edge+=50000) {
                    uint32_t score=fit_template(WAVEFORM_TYPE_SQUARE,d,edge,&shift,&trial_vpp,
                                                folded,0,0);
                    if(score>class_score[t]) {
                        class_score[t]=score;class_vpp[t]=trial_vpp;class_duty[t]=d;
                        chosen_shift=shift;
                    }
                }
            }
        }
    }
    (void)chosen_shift;
    /* Fit both smooth templates after removing an arbitrary third or fifth
     * harmonic nuisance from the robust phase-folded signal. */
    for(t=0;t<2;t++) {
        uint32_t order=t ? 5U : 3U;
        uint16_t dummy=0;
        memcpy(notch_observed[t],folded,sizeof(notch_observed[t]));
        remove_harmonic(notch_observed[t],order);
        (void)fit_template(WAVEFORM_TYPE_SINE,500000,0,&dummy,&notch_vpp[t][0],
                           notch_observed[t],order,&notch_precise[t][0]);
        (void)fit_template(WAVEFORM_TYPE_TRIANGLE,500000,0,&dummy,&notch_vpp[t][1],
                           notch_observed[t],order,&notch_precise[t][1]);
    }
    for(t=0;t<4;t++) if(class_score[t]>best) {best=class_score[t];winner=t;}
    for(t=0;t<4;t++) if(t!=winner && class_score[t]>second) second=class_score[t];
    vpp=class_vpp[winner]; chosen_duty=class_duty[winner];
    /* A free third- or fifth-harmonic nuisance can enter either smooth model.
     * Use this independent time-domain fit only when it clearly favors one
     * shape at a near-perfect score. The ordinary five-point margin remains
     * in force for every other result. */
    if(winner==WAVEFORM_TYPE_SINE || winner==WAVEFORM_TYPE_TRIANGLE) {
        uint32_t variant, selected_variant=2U,selected_class=0U;
        uint32_t selected_score=0U;
        for(variant=0;variant<2U;variant++) {
            uint32_t chosen=(notch_precise[variant][1]>
                             notch_precise[variant][0]) ? 1U : 0U;
            uint32_t other=1U-chosen;
            if(notch_precise[variant][chosen]>=990000U &&
               notch_precise[variant][chosen]>=
                   notch_precise[variant][other]+1800U &&
               notch_precise[variant][chosen]>selected_score) {
                selected_variant=variant;selected_class=chosen;
                selected_score=notch_precise[variant][chosen];
            }
        }
        if(selected_variant<2U) {
            uint32_t other=1U-selected_class;
            winner=selected_class ? WAVEFORM_TYPE_TRIANGLE : WAVEFORM_TYPE_SINE;
            vpp=notch_vpp[selected_variant][selected_class];
            best=(selected_score+500U)/1000U;
            second=(notch_precise[selected_variant][other]+500U)/1000U;
            required_margin=1U;
        }
    }
    if(winner==WAVEFORM_TYPE_SQUARE) {
        uint32_t level_vpp;
        (void)square_levels(f,&chosen_duty,&level_vpp);
    }
    r->best_score_permille=(uint16_t)best;
    r->second_score_permille=(uint16_t)second;
    r->frequency_millihz=freq;
    r->vpp_counts_q16=vpp;
    r->accepted_cycles=(uint16_t)(((uint64_t)f->sample_count*freq)/f->sample_rate_millihz);
    r->dc_offset_counts=(uint16_t)((folded[0]+folded[CBINS/2])/2);
    return finish(f,r,(waveform_type_t)winner,freq,vpp,chosen_duty,
                  (uint16_t)best,(uint16_t)second,required_margin);
}
static bool agree(const waveform_result_t *a,const waveform_result_t *b)
{
    if(a->type!=b->type || a->status!=WAVEFORM_STATUS_OK ||
       b->status!=WAVEFORM_STATUS_OK) return false;
    if((uint64_t)absdiff(a->frequency_millihz,b->frequency_millihz)*1000ULL >
       (uint64_t)a->frequency_millihz*5ULL) return false;
    if((uint64_t)absdiff(a->vpp_microvolts,b->vpp_microvolts)*100ULL >
       (uint64_t)a->vpp_microvolts*8ULL) return false;
    if(a->type==WAVEFORM_TYPE_SQUARE && absdiff(a->duty_ppm,b->duty_ppm)>20000)
        return false;
    return true;
}
/* Estimate the baseline from complete cycles, correcting the known nonzero
 * mean of a non-50% square. Quantile midpoints are biased by duty/noise; using
 * two arbitrary phase bins is phase-dependent. Integrate the fractional last
 * interval too so noninteger periods do not bias the mean. */
static void refine_dc(const waveform_frame_t *f, waveform_result_t *r)
{
    uint64_t limit=(uint64_t)(f->sample_count-1U)<<16;
    uint32_t period=r->period_q16_samples, cycles, whole, fraction, i;
    uint64_t end, integral;
    int64_t center;
    if(!period) return;
    cycles=(uint32_t)(limit/period);
    if(!cycles) return;
    end=(uint64_t)cycles*period;
    whole=(uint32_t)(end>>16); fraction=(uint32_t)(end&65535U);
    integral=((uint64_t)f->samples[0]+f->samples[whole])*32768ULL;
    for(i=1;i<whole;i++) integral+=(uint64_t)f->samples[i]*65536ULL;
    if(fraction && whole+1U<f->sample_count) {
        int64_t last=(int64_t)f->samples[whole]*65536LL+
            ((int64_t)f->samples[whole+1U]-f->samples[whole])*fraction;
        integral+=(uint64_t)(((int64_t)f->samples[whole]*65536LL+last)*fraction/131072LL);
    }
    center=(int64_t)((integral+end/2U)/end);
    if(r->type==WAVEFORM_TYPE_SQUARE) {
        int64_t correction=(int64_t)r->vpp_counts_q16*
            ((int64_t)r->duty_ppm-500000LL);
        center-=correction/(65536LL*1000000LL);
    }
    if(center<0) center=0;
    if(center>f->adc_full_scale) center=f->adc_full_scale;
    r->dc_offset_counts=(uint16_t)center;
}
bool waveform_analyze_cascade(const waveform_frame_t *f,waveform_result_t *out,
                              waveform_cascade_trace_t *trace)
{
    waveform_result_t l1,l2,l3;
    bool ok1,ok2,ok3;
    if(!out) return false;
    if(trace) memset(trace,0,sizeof(*trace));
    ok1=waveform_analyze_frame_with_algorithm(f,&l1,WAVEFORM_ALGORITHM_FAST_PHASE_MODEL);
    ok2=waveform_analyze_frequency(f,&l2);
    if(trace) {trace->l1_status=l1.status;trace->l2_status=l2.status;
               trace->l1_type=l1.type;trace->l2_type=l2.type;}
    if(ok1 && ok2 && agree(&l1,&l2)) {
        *out=l1; refine_dc(f,out);
        if(trace) trace->selected_level=1;
        return true;
    }
    ok3=waveform_analyze_robust(f,&l3);
    if(trace) {trace->l3_ran=true;trace->l3_status=l3.status;
               trace->l3_type=l3.type;trace->selected_level=3;}
    /* Smooth-wave time-domain models can be nearly tied when Gaussian noise
     * overlaps the third harmonic. Accept strong independent spectral evidence
     * only if both time-domain estimates found the same period and failed solely
     * on the shape margin. Invalid, clipped and nonperiodic frames stay rejected. */
    if(!ok1 && !ok3 && ok2 &&
       l1.status==WAVEFORM_STATUS_CLASSIFICATION_AMBIGUOUS &&
       l3.status==WAVEFORM_STATUS_CLASSIFICATION_AMBIGUOUS &&
       l2.best_score_permille>=980U && l2.model_margin_permille>=15U &&
       l1.best_score_permille>=980U && l3.best_score_permille>=980U &&
       (uint64_t)absdiff(l1.frequency_millihz,l2.frequency_millihz)*1000ULL<=
           (uint64_t)l2.frequency_millihz*5ULL &&
       (uint64_t)absdiff(l3.frequency_millihz,l2.frequency_millihz)*1000ULL<=
           (uint64_t)l2.frequency_millihz*5ULL) {
        *out=l2; refine_dc(f,out);
        if(trace) trace->selected_level=2;
        return true;
    }
    *out=(!ok3 && !ok1) ? l1 : l3;
    if(ok3) refine_dc(f,out);
    return ok3;
}
