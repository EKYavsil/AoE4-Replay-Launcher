/*
 * AoE4 Replay Launcher - Steam launch shim.
 *
 * Steam runs this (via the game's Launch Options) as:  shim.exe %command%
 * where %command% is Steam's real AoE4 launch command. The shim lives in
 * %LocalAppData% (outside the deletable app folder), so even if the user deletes
 * the launcher it survives and keeps normal Play working.
 *
 * It reads "shim.cfg" (next to itself), UTF-16LE, two lines:
 *     line 1: check_path        (a file whose existence means "launcher present")
 *     line 2: dispatcher_prefix (command string to prepend to the forwarded args)
 *
 * If check_path exists  -> run:  <dispatcher_prefix> <forwarded %command%>
 *   (the launcher's dispatcher decides: play a reconstructed build, or pass through)
 * If check_path is gone -> run:  <forwarded %command%>   (plain normal game launch)
 *
 * Built windowed (-mwindows) so no console window appears, and static so it has
 * no MinGW DLL dependencies.
 */
#include <windows.h>
#include <wchar.h>
#include <string.h>

#define CFG_MAX 8192
#define CMD_MAX 32768

/* True if path names an existing file. */
static int file_exists(const wchar_t *path)
{
    DWORD attr = GetFileAttributesW(path);
    return attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY);
}

/* Return a pointer into cmdline just past argv[0] (the shim path), preserving
 * the original quoting of everything after it. */
static wchar_t *skip_argv0(wchar_t *cmdline)
{
    wchar_t *p = cmdline;
    if (*p == L'"') {
        p++;
        while (*p && *p != L'"') p++;
        if (*p == L'"') p++;
    } else {
        while (*p && *p != L' ' && *p != L'\t') p++;
    }
    while (*p == L' ' || *p == L'\t') p++;
    return p;
}

/* Read a UTF-16LE file (BOM-tolerant) into buf (wchar_t count incl. NUL). */
static int read_cfg(const wchar_t *path, wchar_t *buf, int buf_wchars)
{
    HANDLE h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE)
        return 0;
    DWORD got = 0;
    BOOL ok = ReadFile(h, buf, (DWORD)((buf_wchars - 1) * sizeof(wchar_t)), &got, NULL);
    CloseHandle(h);
    if (!ok)
        return 0;
    int n = (int)(got / sizeof(wchar_t));
    buf[n] = L'\0';
    return 1;
}

int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE hPrev, PWSTR pCmd, int nShow)
{
    (void)hInst; (void)hPrev; (void)pCmd; (void)nShow;

    /* Locate shim.cfg next to this exe. */
    wchar_t exe_path[MAX_PATH];
    DWORD len = GetModuleFileNameW(NULL, exe_path, MAX_PATH);
    if (len == 0 || len >= MAX_PATH)
        return 1;
    wchar_t cfg_path[MAX_PATH];
    lstrcpynW(cfg_path, exe_path, MAX_PATH);
    wchar_t *slash = wcsrchr(cfg_path, L'\\');
    if (!slash)
        return 1;
    lstrcpynW(slash + 1, L"shim.cfg", (int)(MAX_PATH - (slash + 1 - cfg_path)));

    static wchar_t cfg[CFG_MAX];
    int have_cfg = read_cfg(cfg_path, cfg, CFG_MAX);

    /* Split cfg into line1 (check_path) and line2 (dispatcher_prefix). */
    wchar_t *check_path = NULL;
    wchar_t *prefix = NULL;
    if (have_cfg) {
        wchar_t *start = cfg;
        if (*start == 0xFEFF) start++;          /* skip BOM if present */
        check_path = start;
        wchar_t *nl = wcschr(start, L'\n');
        if (nl) {
            *nl = L'\0';
            prefix = nl + 1;
            wchar_t *nl2 = wcschr(prefix, L'\n');
            if (nl2) *nl2 = L'\0';
        }
        /* strip trailing CR from check_path and prefix */
        size_t cl = check_path ? wcslen(check_path) : 0;
        if (cl && check_path[cl - 1] == L'\r') check_path[cl - 1] = L'\0';
        size_t pl = prefix ? wcslen(prefix) : 0;
        if (pl && prefix[pl - 1] == L'\r') prefix[pl - 1] = L'\0';
    }

    /* The args Steam passed us (its %command%), preserving quoting. */
    wchar_t *forwarded = skip_argv0(GetCommandLineW());

    static wchar_t cmd[CMD_MAX];
    int use_launcher = have_cfg && prefix && *prefix && check_path && *check_path
                       && file_exists(check_path);
    if (use_launcher) {
        /* "<prefix> <forwarded>" */
        lstrcpynW(cmd, prefix, CMD_MAX);
        size_t n = wcslen(cmd);
        if (n + 1 < CMD_MAX) { cmd[n] = L' '; cmd[n + 1] = L'\0'; }
        lstrcpynW(cmd + wcslen(cmd), forwarded, (int)(CMD_MAX - wcslen(cmd)));
    } else {
        /* launcher gone (or no cfg): just run Steam's command directly */
        lstrcpynW(cmd, forwarded, CMD_MAX);
    }

    if (!*cmd)
        return 2;

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));

    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, 0, NULL, NULL, &si, &pi))
        return 2;

    /* Stay alive for the whole session so Steam keeps the app marked "running". */
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 0;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
}
