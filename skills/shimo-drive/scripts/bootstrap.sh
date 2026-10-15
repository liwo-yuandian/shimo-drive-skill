#!/usr/bin/env bash
# Python-free entrypoint. Never changes shell profiles or authentication.
set -u
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" || exit 1
mode="${1:-check}"
if [ "$#" -gt 0 ]; then shift; fi
case "$mode" in check|ensure|run) ;; *) printf '%s\n' '{"ready":false,"error":"invalid_command"}'; exit 2 ;; esac

python_bin=""
probe_python() {
    local candidate="$1" resolved
    resolved="$(command -v -- "$candidate" 2>/dev/null)" || return 1
    [ -x "$resolved" ] || return 1
    # Avoid triggering Apple's developer-tools installer through its Python stub.
    if [ "$(uname -s)" = Darwin ] && [ "$resolved" = /usr/bin/python3 ]; then
        xcode-select -p >/dev/null 2>&1 || return 1
    fi
    "$resolved" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' >/dev/null 2>&1 || return 1
    python_bin="$resolved"
}
find_python() {
    if [ -n "${SHIMO_DRIVE_PYTHON:-}" ]; then
        case "$SHIMO_DRIVE_PYTHON" in /*) ;; *) return 1 ;; esac
        probe_python "$SHIMO_DRIVE_PYTHON"
        return $?
    fi
    local candidate
    for candidate in python3 python python3.13 \
        /opt/homebrew/opt/python@3.13/bin/python3.13 \
        /usr/local/opt/python@3.13/bin/python3.13 \
        /opt/homebrew/bin/python3 /usr/local/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/3.*/bin/python3; do
        if probe_python "$candidate"; then return 0; fi
    done
    return 1
}
install_python() {
    local manager="" candidate
    case "$(uname -s)" in
        Darwin)
            for candidate in brew /opt/homebrew/bin/brew /usr/local/bin/brew; do
                manager="$(command -v -- "$candidate" 2>/dev/null)" && break
            done
            [ -n "$manager" ] || return 3
            "$manager" install python@3.13 >&2 || return 1
            ;;
        Linux)
            # Privilege elevation belongs to the host's approval flow, never this script.
            [ "$(id -u)" = 0 ] || return 3
            if command -v apt-get >/dev/null 2>&1; then apt-get install -y python3 >&2 || return 1
            elif command -v dnf >/dev/null 2>&1; then dnf install -y python3 >&2 || return 1
            elif command -v apk >/dev/null 2>&1; then apk add python3 >&2 || return 1
            else return 3
            fi
            ;;
        *) return 3 ;;
    esac
}
if ! find_python; then
    if [ -n "${SHIMO_DRIVE_PYTHON:-}" ]; then
        printf '%s\n' '{"ready":false,"error":"python_override_invalid","next":"Fix SHIMO_DRIVE_PYTHON; Python 3.9+ is required."}'
        exit 2
    fi
    if [ "$mode" != ensure ]; then
        printf '%s\n' '{"ready":false,"error":"python_missing","next":"Run bootstrap.sh ensure; if installation is unavailable follow references/setup.md."}'
        exit 2
    fi
    install_python
    install_result=$?
    if [ "$install_result" != 0 ] || ! find_python; then
        printf '%s\n' '{"ready":false,"error":"python_install_required","next":"Use the official Python installer or an administrator-approved package manager, then rerun ensure. See references/setup.md."}'
        exit 2
    fi
fi
if [ "$mode" = run ]; then
    script="${1:-}"
    case "$script" in drive_api.py|document.py|environment.py) ;; *) printf '%s\n' '{"ready":false,"error":"invalid_script"}'; exit 2 ;; esac
    shift
    exec "$python_bin" "$script_dir/$script" "$@"
fi
exec "$python_bin" "$script_dir/environment.py" "$mode" "$@"
