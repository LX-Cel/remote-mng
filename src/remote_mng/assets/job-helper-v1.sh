#!/bin/sh
# remote-mng persistent job helper, protocol version 1.
# No daemon or listening socket. Requires Linux /proc and common BusyBox applets.
set -f
umask 077
LC_ALL=C
export LC_ALL

root=$1
action=$2
shift 2
self="$root/job-helper-v1.sh"

emit() { printf '%s\t%s\n' "$1" "$2"; }
fail() { emit error "$1"; emit message "$2"; exit 1; }
valid_id() {
    case "$1" in ''|*[!a-zA-Z0-9_.-]*|[!a-zA-Z0-9]*) return 1;; esac
    [ "${#1}" -le 64 ]
}
use_job() {
    valid_id "$1" || fail invalid_job_id 'Invalid job identifier'
    id=$1
    dir="$root/jobs/$id"
}
require_job() { [ -d "$dir" ] || fail job_not_found 'Job does not exist'; }

# REMOTE_MNG_STORAGE_EXTENSION

# Field 22 is the kernel start time. Strip the entire comm field, which can
# contain spaces and parentheses, before counting fields.
read_identity() {
    case "$1" in ''|*[!0-9]*) return 1;; esac
    raw=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
    rest=${raw##*) }
    set -- $rest
    [ "$#" -ge 20 ] || return 1
    PROC_STATE=$1
    PROC_GROUP=$3
    PROC_SESSION=$4
    shift 19
    PROC_START=$1
}
read_meta() {
    [ -f "$dir/identity" ] || return 1
    # Never source files from the remote filesystem as shell code.
    IFS=' ' read -r pid started boot started_at < "$dir/identity" || return 1
    case "$pid:$started:$started_at" in *[!0-9:]*|::*|:*|*:) return 1;; esac
    [ -n "$boot" ] || return 1
}
identity_matches() {
    read_meta || return 1
    current_boot=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null) || return 1
    [ "$boot" = "$current_boot" ] || return 1
    read_identity "$pid" || return 1
    [ "$started" = "$PROC_START" ] && [ "$PROC_GROUP" = "$pid" ] &&
        [ "$PROC_SESSION" = "$pid" ] && [ "$PROC_STATE" != Z ]
}
job_status() {
    emit job_id "$id"
    script_known=false
    if [ -f "$dir/script-exit" ]; then
        IFS=' ' read -r script_result script_finished_at < "$dir/script-exit"
        case "$script_result:$script_finished_at" in
            *[!0-9:]*|:*|*:) :;;
            *) script_known=true; emit script_exit_code "$script_result"; emit script_finished_at "$script_finished_at";;
        esac
    fi
    if [ -f "$dir/logs-cleaned" ]; then emit logs_deleted true; else emit logs_deleted false; fi
    if [ -f "$dir/log-incomplete" ]; then emit log_incomplete true; else emit log_incomplete false; fi
    if [ -f "$dir/cancel-requested" ]; then emit cancel_requested true; else emit cancel_requested false; fi
    if read_meta; then emit pid "$pid"; emit started_at "$started_at"; fi
    if [ -f "$dir/exit" ]; then
        emit logs_draining false
        IFS=' ' read -r result finished_at < "$dir/exit"
        case "$result:$finished_at" in *[!0-9:]*|:*|*:) emit state unknown; emit reason corrupt_exit_record; return;; esac
        if [ "$result" -eq 0 ]; then emit state succeeded; else emit state failed; fi
        emit exit_code "$result"
        emit finished_at "$finished_at"
    elif identity_matches; then
        emit state running
        if [ "$script_known" = true ]; then
            emit logs_draining true
            emit phase waiting_for_output_close
            emit advice 'The direct script exited but output capture remains open. Do not resubmit. Inspect descendants holding stdout/stderr; future daemon launches should explicitly redirect all standard streams.'
        else emit logs_draining false; fi
    else
        emit state unknown
        if [ -f "$dir/launch-error" ]; then emit reason launch_failed
        elif ! read_meta; then emit reason launch_not_confirmed
        elif [ "$boot" != "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)" ]; then emit reason host_restarted
        elif ! read_identity "$pid"; then emit reason missing_exit_record
        else emit reason process_identity_changed; fi
    fi
}

case "$action" in
    probe)
        [ -r /proc/sys/kernel/random/boot_id ] || fail unsupported_host 'Linux boot identity is required'
        for tool in sh setsid cat mkdir mv chmod date sleep tail head wc base64 tr dd mkfifo rm df du awk; do
            command -v "$tool" >/dev/null 2>&1 || fail missing_dependency "$tool"
        done
        read_identity $$ || fail unsupported_host 'Cannot read process identity'
        emit protocol 1
        emit supported true
        emit storage_protocol 1
        emit log_rotation true
        emit cleanup true
        ;;
    health)
        emit protocol 1
        emit supported true
        emit storage_protocol 1
        storage_health
        ;;
    start)
        use_job "$1"
        digest=$2
        maximum=${3:-16777216}
        minimum=${4:-8388608}
        case "$maximum:$minimum" in *[!0-9:]*|:*|*:) fail invalid_limits 'Invalid storage limits';; esac
        [ "$maximum" -ge 4096 ] && [ "$maximum" -le 1073741824 ] || fail invalid_limits 'Log limit must be 4096 to 1073741824 bytes'
        case "$digest" in ''|*[!a-f0-9]*) fail invalid_digest 'Invalid request digest';; esac
        [ "${#digest}" -eq 64 ] || fail invalid_digest 'Invalid request digest'
        [ -d "$root/jobs" ] || fail helper_not_installed 'Install the helper first'
        if [ ! -d "$dir" ]; then
            storage_health > /dev/null
            [ "$((free_kb * 1024))" -ge "$minimum" ] || fail insufficient_space 'Remote free space is below configured minimum; no job command was executed'
        fi
        if ! mkdir "$dir" 2>/dev/null; then
            require_job
            # A concurrent submission may still be writing its request.
            attempts=0
            while [ ! -f "$dir/request-sha256" ] && [ "$attempts" -lt 20 ]; do
                sleep 0.1
                attempts=$((attempts + 1))
            done
            [ -f "$dir/request-sha256" ] || fail submission_unknown 'Submission was interrupted; inspect this id without resubmitting it'
            existing=$(cat "$dir/request-sha256")
            [ "$existing" = "$digest" ] || fail job_id_conflict 'This job id belongs to a different request'
            emit reused true
            job_status
            exit 0
        fi
        cat > "$dir/request.tmp" || fail submission_unknown 'Could not persist the request'
        mv "$dir/request.tmp" "$dir/request.sh" || fail submission_unknown 'Could not persist the request'
        printf '%s\n' "$digest" > "$dir/request-sha256.tmp" || fail submission_unknown 'Could not persist request identity'
        mv "$dir/request-sha256.tmp" "$dir/request-sha256" || fail submission_unknown 'Could not persist request identity'
        : > "$dir/stdout"
        : > "$dir/stderr"
        printf '%s\n' "$maximum" > "$dir/log-limit"
        printf '0 0\n' > "$dir/stdout.index"
        printf '0 0\n' > "$dir/stderr.index"
        # All three standard streams are detached before returning to SSH.
        # POSIX ignored HUP survives exec, providing nohup semantics even on
        # BusyBox builds where the nohup applet is omitted.
        ( trap '' HUP; exec setsid sh "$self" "$root" _run "$id" ) > "$dir/launcher.log" 2>&1 < /dev/null &
        attempts=0
        while [ ! -f "$dir/identity" ] && [ ! -f "$dir/launch-error" ] && [ "$attempts" -lt 30 ]; do
            sleep 0.1
            attempts=$((attempts + 1))
        done
        emit reused false
        job_status
        ;;
    _run)
        use_job "$1"
        require_job
        [ ! -f "$dir/identity" ] || exit 1
        pid=$$
        read_identity "$pid" || { printf '%s\n' process_identity > "$dir/launch-error"; exit 1; }
        if [ "$PROC_GROUP" != "$pid" ] || [ "$PROC_SESSION" != "$pid" ]; then
            printf '%s\n' process_group > "$dir/launch-error"
            exit 1
        fi
        boot=$(cat /proc/sys/kernel/random/boot_id) || exit 1
        printf '%s %s %s %s\n' "$pid" "$PROC_START" "$boot" "$(date +%s)" > "$dir/identity.tmp" || exit 1
        mv "$dir/identity.tmp" "$dir/identity" || exit 1
        # A TERM interrupts wait. Keep the supervisor alive until its direct
        # child has exited so a cancellation request is not reported as exit.
        interrupted=0
        trap 'interrupted=1' TERM INT HUP
        maximum=$(cat "$dir/log-limit")
        mkfifo "$dir/stdout.pipe" "$dir/stderr.pipe" || { : > "$dir/log-incomplete"; exit 1; }
        sh "$self" "$root" _collect "$id" stdout "$maximum" &
        stdout_collector=$!
        sh "$self" "$root" _collect "$id" stderr "$maximum" &
        stderr_collector=$!
        sh "$dir/request.sh" > "$dir/stdout.pipe" 2> "$dir/stderr.pipe" < /dev/null &
        child=$!
        while :; do
            interrupted=0
            wait "$child"
            result=$?
            [ "$interrupted" -eq 0 ] && break
        done
        # Descendants can inherit FIFO writers after the submitted script exits.
        # Persist that distinct fact before waiting for capture to finish. Never
        # force-close collectors: that could SIGPIPE a user's background process.
        if ! { printf '%s %s\n' "$result" "$(date +%s)" > "$dir/script-exit.tmp" && mv "$dir/script-exit.tmp" "$dir/script-exit"; }; then
            : > "$dir/log-incomplete"
        fi
        # Collectors ignore cancellation signals and drain until the child closes output.
        for collector in "$stdout_collector" "$stderr_collector"; do
            while :; do
                interrupted=0
                wait "$collector"
                collector_result=$?
                [ "$interrupted" -eq 0 ] && break
            done
            [ "$collector_result" -eq 0 ] || : > "$dir/log-incomplete"
        done
        rm -f "$dir/stdout.pipe" "$dir/stderr.pipe"
        printf '%s %s\n' "$result" "$(date +%s)" > "$dir/exit.tmp" || exit 1
        mv "$dir/exit.tmp" "$dir/exit"
        ;;
    _collect)
        use_job "$1"
        collect_stream "$@"
        ;;
    cleanup)
        use_job "$1"
        require_job
        cleanup_job "$@"
        ;;
    status)
        use_job "$1"
        require_job
        job_status
        ;;
    list)
        # Globbing is enabled only here; ids are validated before use.
        set +f
        for candidate in "$root/jobs/"*; do
            [ -d "$candidate" ] || continue
            candidate_id=${candidate##*/}
            valid_id "$candidate_id" || continue
            use_job "$candidate_id"
            emit record begin
            job_status
            emit record end
        done
        ;;
    logs)
        use_job "$1"
        require_job
        stream=$2
        offset=$3
        limit=$4
        case "$stream" in stdout|stderr|launcher) :;; *) fail invalid_stream 'Stream must be stdout, stderr, or launcher';; esac
        case "$offset:$limit" in *[!0-9:]*|:*|*:) fail invalid_log_range 'Invalid byte offset or limit';; esac
        [ "$limit" -ge 1 ] && [ "$limit" -le 262144 ] || fail invalid_log_range 'Limit must be 1 to 262144 bytes'
        if [ -f "$dir/$stream.index" ]; then chunk_logs; exit 0; fi
        file="$dir/$stream"
        [ "$stream" = launcher ] && file="$dir/launcher.log"
        [ -f "$file" ] || fail log_unavailable 'Log file is not available'
        size=$(wc -c < "$file")
        [ "$offset" -le "$size" ] || fail log_offset_range 'Offset is beyond the current log; it may have been truncated'
        count=$((size - offset))
        [ "$count" -le "$limit" ] || count=$limit
        next=$((offset + count))
        emit job_id "$id"
        emit stream "$stream"
        emit offset "$offset"
        emit next_offset "$next"
        emit snapshot_size "$size"
        if [ "$next" -ge "$size" ]; then emit eof true; else emit eof false; fi
        printf 'data_base64\t'
        if [ "$count" -gt 0 ]; then tail -c "+$((offset + 1))" "$file" | head -c "$count" | base64 | tr -d '\n'; fi
        printf '\n'
        ;;
    cancel)
        use_job "$1"
        require_job
        if [ -f "$dir/exit" ]; then job_status; exit 0; fi
        identity_matches || fail job_identity_unknown 'Cannot safely signal this job; process identity is not confirmed'
        : > "$dir/cancel-requested"
        # Recheck after writing the marker to narrow the PID reuse window.
        identity_matches || fail job_identity_unknown 'Process identity changed before cancellation'
        kill -TERM "-$pid" 2>/dev/null || fail cancel_unknown 'Signal delivery could not be confirmed'
        job_status
        ;;
    *) fail invalid_action 'Unknown helper action';;
esac
