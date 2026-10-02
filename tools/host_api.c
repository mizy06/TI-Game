/* Export the version-2 cascade entry point when building a host DLL. */
#include "waveform_analyzer.h"
#if defined(_WIN32)
#define HOST_API __declspec(dllexport)
#else
#define HOST_API __attribute__((visibility("default")))
#endif
HOST_API bool ti_waveform_analyze_frame(const waveform_frame_t *frame,
                                        waveform_result_t *result)
{
    return waveform_analyze_frame(frame, result);
}
