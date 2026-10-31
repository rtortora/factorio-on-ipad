// Narrow, explicit absence responses for APIs that are optional for headless use.
// These are not implementations of macOS graphics or input.
#include <stdint.h>
#include <stdio.h>
#include <stdbool.h>

bool SteamAPI_RestartAppIfNecessary(uint32_t appID) {
    fprintf(stderr, "FACTORIO_PROBE: Steam client unavailable on iPadOS\n");
    return false;
}

bool SteamAPI_Init(void) {
    fprintf(stderr, "FACTORIO_PROBE: Steam initialization unavailable\n");
    return false;
}

// Newer Steamworks SDKs make SteamAPI_Init an inline wrapper around this call.
// Returns k_ESteamAPIInitResult_NoSteamClient (2) with a message in the 1024-byte buffer.
int32_t SteamInternal_SteamAPI_Init(const char *interfaceVersions, char *errorMessage) {
    fprintf(stderr, "FACTORIO_PROBE: Steam initialization unavailable\n");
    if (errorMessage) snprintf(errorMessage, 1024, "Steam is not available on iPadOS");
    return 2;
}

// OpenCL extension cl_khr_icd: no OpenCL platforms are available on iPadOS.
int32_t clGetPlatformIDs(uint32_t capacity, void **platforms, uint32_t *count) {
    if (count) *count = 0;
    fprintf(stderr, "FACTORIO_PROBE: OpenCL unavailable (CL_PLATFORM_NOT_FOUND_KHR)\n");
    return -1001;
}
