/* Export the version-2 cascade entry point when building a host DLL. */
#include "waveform_analyzer.h"
#include <math.h>
#if defined(_WIN32)
#define HOST_API __declspec(dllexport)
#else
#define HOST_API __attribute__((visibility("default")))
#endif

/* Host-only backstop for narrowband noise corrupting a triangle fundamental.
 * Fit DC and the 1st/3rd/5th/7th complex coefficients jointly, so finite-window
 * leakage from the fundamental cannot masquerade as a high harmonic. Change
 * amplitude only when all three high harmonics independently agree and the
 * fundamental-based estimate differs materially. Broad-band/noisy harmonics
 * do not satisfy this gate and keep the ordinary cascade measurement. */
static void refine_triangle_amplitude(const waveform_frame_t *f,
                                       waveform_result_t *r)
{
    double normal[9][10]={{0}}, basis[9];
    double sn[4]={0},cs[4]={1,1,1,1},step_s[4],step_c[4];
    double estimates[3], current=(double)r->vpp_counts_q16/65536.0;
    const unsigned harmonics[4]={1,3,5,7};
    const double pi=3.14159265358979323846;
    unsigned i,j,k,pivot;
    uint64_t uv;
    if(r->type!=WAVEFORM_TYPE_TRIANGLE || current<200.0) return;
    for(k=0;k<4;k++) {
        double step=2*pi*harmonics[k]*r->frequency_millihz/f->sample_rate_millihz;
        step_s[k]=sin(step);step_c[k]=cos(step);
    }
    for(i=0;i<f->sample_count;i++) {
        basis[0]=1;
        for(k=0;k<4;k++) {basis[1+2*k]=sn[k];basis[2+2*k]=cs[k];}
        for(j=0;j<9;j++) {
            normal[j][9]+=basis[j]*f->samples[i];
            for(k=0;k<9;k++) normal[j][k]+=basis[j]*basis[k];
        }
        for(k=0;k<4;k++) {
            double next_s=sn[k]*step_c[k]+cs[k]*step_s[k];
            cs[k]=cs[k]*step_c[k]-sn[k]*step_s[k];sn[k]=next_s;
        }
    }
    for(i=0;i<9;i++) {
        double scale;
        pivot=i;
        for(j=i+1;j<9;j++) if(fabs(normal[j][i])>fabs(normal[pivot][i])) pivot=j;
        if(fabs(normal[pivot][i])<1e-9) return;
        if(pivot!=i) for(k=i;k<10;k++) {
            double tmp=normal[i][k];normal[i][k]=normal[pivot][k];normal[pivot][k]=tmp;
        }
        scale=normal[i][i];
        for(k=i;k<10;k++) normal[i][k]/=scale;
        for(j=0;j<9;j++) if(j!=i) {
            scale=normal[j][i];
            for(k=i;k<10;k++) normal[j][k]-=scale*normal[i][k];
        }
    }
    for(k=1;k<4;k++) {
        double a=normal[1+2*k][9],b=normal[2+2*k][9];
        estimates[k-1]=sqrt(a*a+b*b)*pi*pi*harmonics[k]*harmonics[k]/4;
    }
    for(i=0;i<3;i++) for(j=i+1;j<3;j++) if(estimates[i]>estimates[j]) {
        double tmp=estimates[i];estimates[i]=estimates[j];estimates[j]=tmp;
    }
    if(estimates[1]<=0 || estimates[2]-estimates[0]>.03*estimates[1] ||
       fabs(estimates[1]-current)<.08*current ||
       fabs(estimates[1]-current)>.30*current || estimates[1]>f->adc_full_scale)
        return;
    r->vpp_counts_q16=(uint32_t)(estimates[1]*65536+.5);
    uv=(uint64_t)r->vpp_counts_q16*f->adc_reference_microvolts;
    uv/=((uint64_t)f->adc_full_scale*65536ULL);
    uv=uv*1000000ULL/f->frontend_gain_ppm;
    if(uv>UINT32_MAX) uv=UINT32_MAX;
    r->vpp_microvolts=(uint32_t)uv;
    r->vpp_millivolts=(uint32_t)((uv+500)/1000);
    r->quality_flags|=WAVEFORM_QUALITY_HARMONIC_AMPLITUDE;
}
HOST_API bool ti_waveform_analyze_frame(const waveform_frame_t *frame,
                                        waveform_result_t *result)
{
    bool ok=waveform_analyze_frame(frame, result);
    if(ok) refine_triangle_amplitude(frame,result);
    return ok;
}

/* The noise-matrix diagnostics use the same exported ABI to inspect each
 * cascade stage without depending on the DLL's private symbol table. */
HOST_API bool ti_waveform_analyze_frame_with_algorithm(
    const waveform_frame_t *frame, waveform_result_t *result,
    waveform_algorithm_t algorithm)
{
    return waveform_analyze_frame_with_algorithm(frame, result, algorithm);
}

HOST_API bool ti_waveform_analyze_frequency(const waveform_frame_t *frame,
                                            waveform_result_t *result)
{
    return waveform_analyze_frequency(frame, result);
}

HOST_API bool ti_waveform_analyze_robust(const waveform_frame_t *frame,
                                         waveform_result_t *result)
{
    return waveform_analyze_robust(frame, result);
}

HOST_API bool ti_waveform_analyze_cascade(const waveform_frame_t *frame,
                                          waveform_result_t *result,
                                          waveform_cascade_trace_t *trace)
{
    return waveform_analyze_cascade(frame, result, trace);
}

/* The Python client checks these before passing ctypes structures to C. */
HOST_API uint32_t ti_waveform_abi_version(void)
{
    return WAVEFORM_ALGORITHM_VERSION;
}

HOST_API uint32_t ti_waveform_frame_size(void)
{
    return (uint32_t)sizeof(waveform_frame_t);
}

HOST_API uint32_t ti_waveform_result_size(void)
{
    return (uint32_t)sizeof(waveform_result_t);
}
