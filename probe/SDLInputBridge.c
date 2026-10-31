#include <string.h>
#include "SDL_internal.h"
#include "events/SDL_keyboard_c.h"
#include "events/SDL_mouse_c.h"
#include "video/SDL_sysvideo.h"

#define FPI_EXPORT __attribute__((visibility("default")))

// Use SDL's input path so both queued events and polled state stay consistent.
FPI_EXPORT void FPIKeyboard(int scan, int down) {
    SDL_SendKeyboardKey(0, SDL_GLOBAL_KEYBOARD_ID, 0, (SDL_Scancode)scan, down != 0);
}
// The on-screen keyboard has no visible UITextField behind it, so mark text
// input active for the focused window before sending composed characters.
FPI_EXPORT void FPIText(const char *text) {
    SDL_Window *focus = SDL_GetKeyboardFocus();
    if (!focus) return;
    focus->text_input_active = true;
    SDL_SendKeyboardText(text);
}
// Hardware keyboard key event: send the raw scancode as usual, and separately
// compose+send the character ourselves (mirroring the on-screen keyboard's own
// text composition). This avoids ever needing UIKit's hidden text field to
// become first responder for hardware typing, which is what triggers the
// system's press-and-hold accented-character picker.
FPI_EXPORT void FPIPhysicalKey(int scan, int down) {
    SDL_SendKeyboardKey(0, SDL_GLOBAL_KEYBOARD_ID, 0, (SDL_Scancode)scan, down != 0);
    if (!down) return;
    // SDL3 only delivers SDL_EVENT_TEXT_INPUT to a window whose text input is
    // "active". SDL_StartTextInput would also show UIKit's hidden UITextField
    // (the press-and-hold trigger), so flip the flag directly instead and
    // re-assert it on every key press in case the game stopped text input.
    SDL_Window *focus = SDL_GetKeyboardFocus();
    if (!focus) return;
    focus->text_input_active = true;
    SDL_Keymod mod = SDL_GetModState();
    if (mod & (SDL_KMOD_CTRL|SDL_KMOD_ALT|SDL_KMOD_GUI)) return;
    SDL_Keycode key = SDL_GetKeyFromScancode((SDL_Scancode)scan, SDL_KMOD_NONE, false);
    if (key < 0x20 || key > 0x7e) return;
    char c = (char)key;
    int shift = (mod & SDL_KMOD_SHIFT) != 0;
    int caps = (mod & SDL_KMOD_CAPS) != 0;
    if (c >= 'a' && c <= 'z') {
        if (shift != caps) c = (char)(c - 'a' + 'A');
    } else if (shift) {
        static const char plain[]   = "`1234567890-=[]\\;',./";
        static const char shifted[] = "~!@#$%^&*()_+{}|:\"<>?";
        const char *at = strchr(plain, c);
        if (at) c = shifted[at - plain];
    }
    char text[2] = {c, 0};
    SDL_SendKeyboardText(text);
}
static SDL_Window *inputWindow(void) {
    SDL_Window *window=SDL_GetMouseFocus();
    return window ? window : SDL_GetKeyboardFocus();
}
FPI_EXPORT void FPIMouseButton(int button, int down) {
    SDL_SendMouseButton(0,inputWindow(),SDL_GLOBAL_MOUSE_ID,(Uint8)button,down != 0);
}
FPI_EXPORT void FPIWheel(float y) {
    SDL_SendMouseWheel(0,inputWindow(),SDL_GLOBAL_MOUSE_ID,0,y,SDL_MOUSEWHEEL_NORMAL);
}
FPI_EXPORT void FPIPointer(float x,float y) {
    SDL_Window *window=inputWindow();
    if (!window) return;
    int width,height; SDL_GetWindowSize(window,&width,&height);
    SDL_SendMouseMotion(0,window,SDL_GLOBAL_MOUSE_ID,false,x*width,y*height);
}
FPI_EXPORT int FPIGetPointer(float *x,float *y) {
    SDL_Window *window=inputWindow();
    if (!window) return 0;
    int width,height;
    float mouseX,mouseY;
    SDL_GetWindowSize(window,&width,&height);
    if (width<=0 || height<=0) return 0;
    SDL_GetMouseState(&mouseX,&mouseY);
    *x=mouseX/width; *y=mouseY/height;
    return 1;
}
