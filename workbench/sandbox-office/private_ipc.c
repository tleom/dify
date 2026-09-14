/* LibreOffice 7.4 tests /tmp with access(W_OK), which does not account for
 * Landlock. Make its existing OSL_SOCKET_PATH fallback effective in the Office
 * worker only. No bind/connect call or filesystem permission is overridden.
 * See sal/osl/unx/pipe.cxx in LibreOffice 7.4.7.2.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <string.h>
#include <unistd.h>

int access(const char *path, int mode)
{
    if ((mode & W_OK) &&
        (strcmp(path, "/tmp") == 0 || strcmp(path, "/var/tmp") == 0)) {
        errno = EACCES;
        return -1;
    }
    int (*real_access)(const char *, int) = dlsym(RTLD_NEXT, "access");
    if (!real_access) {
        errno = ENOSYS;
        return -1;
    }
    return real_access(path, mode);
}
