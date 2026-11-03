/* Lanzador nativo de "PS5 Directo.app" (Contents/MacOS/PS5 Directo), compilado para Apple Silicon e
 * Intel a la vez. Solo arranca Contents/Resources/launcher.sh con /bin/bash y espera a que termine.
 * Hace falta porque si el ejecutable de la app es un script, en los Mac con chip de Apple macOS pide
 * instalar Rosetta, y tampoco ofrece el permiso de «Red local» a nombre de PS5 Directo. */
#include <mach-o/dyld.h>
#include <limits.h>
#include <libgen.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <errno.h>

extern char **environ;

int main(int argc, char **argv) {
    char exe[PATH_MAX], real[PATH_MAX], dir[PATH_MAX], script[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) return 1;
    if (!realpath(exe, real)) strncpy(real, exe, sizeof(real) - 1);
    strncpy(dir, dirname(real), sizeof(dir) - 1);
    snprintf(script, sizeof(script), "%s/../Resources/launcher.sh", dir);

    char **args = calloc((size_t)argc + 3, sizeof(char *));
    if (!args) return 1;
    int n = 0;
    args[n++] = "/bin/bash";
    args[n++] = script;
    for (int i = 1; i < argc; i++)
        if (strncmp(argv[i], "-psn_", 5) != 0) args[n++] = argv[i];   /* lo añadia el Finder antiguo */
    args[n] = NULL;

    pid_t pid;
    if (posix_spawn(&pid, "/bin/bash", NULL, NULL, args, environ) != 0) return 1;
    int st = 0;
    while (waitpid(pid, &st, 0) < 0 && errno == EINTR) {}
    return WIFEXITED(st) ? WEXITSTATUS(st) : 1;
}
