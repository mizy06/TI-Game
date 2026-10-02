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
