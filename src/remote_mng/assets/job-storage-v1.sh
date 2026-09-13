# Included in the installed job helper. Immutable chunks keep byte cursors stable.
storage_health() {
    free_kb=$(df -Pk "$root" 2>/dev/null | awk 'END {print $4}')
    used_kb=$(du -sk "$root/jobs" 2>/dev/null | awk '{print $1}')
    case "$free_kb:$used_kb" in *[!0-9:]*|:*|*:) fail storage_unknown 'Cannot determine remote storage';; esac
    emit free_bytes "$((free_kb * 1024))"
    emit used_bytes "$((used_kb * 1024))"
}

collect_stream() {
    stream=$2
    maximum=$3
    parts="$dir/$stream.parts"
    # Open the FIFO before touching storage. Even a failed mkdir must consume
    # the producer's output, otherwise opening/writing the FIFO can hang it.
    trap '' TERM INT HUP
    exec 3< "$dir/$stream.pipe" || exit 1
    drain_failed_stream() {
        : > "$dir/log-incomplete"
        cat <&3 > /dev/null
        exec 3<&-
        exit 1
    }
    mkdir -p "$parts" || drain_failed_stream
    total=0
    chunk_size=65536
    [ "$chunk_size" -le "$maximum" ] || chunk_size=$maximum
    while :; do
        if ! dd bs="$chunk_size" count=1 <&3 > "$parts/.incoming" 2>/dev/null; then
            drain_failed_stream
        fi
        bytes=$(wc -c < "$parts/.incoming")
        case "$bytes" in ''|*[!0-9]*) drain_failed_stream;; esac
        [ "$bytes" -gt 0 ] || break
        mv "$parts/.incoming" "$parts/$total" || drain_failed_stream
        next=$((total + bytes))
        old_index=$(cat "$dir/$stream.index") || drain_failed_stream
        base=$total
        kept=""
        obsolete=""
        # Index starts with base/total, remaining lines identify immutable chunks.
        entries=$(printf '%s\n' "$old_index" | tail -n +2)
        while IFS=' ' read -r begin length; do
            [ -n "$begin" ] || continue
            if [ "$((next - begin))" -le "$maximum" ]; then
                [ "$begin" -ge "$base" ] || base=$begin
                kept="$kept$begin $length
"
            else
                obsolete="$obsolete $begin"
            fi
        done <<EOF
$entries
EOF
        if ! { printf '%s %s\n' "$base" "$next"; printf '%s' "$kept"; printf '%s %s\n' "$total" "$bytes"; } > "$dir/$stream.index.tmp"; then
            drain_failed_stream
        fi
        mv "$dir/$stream.index.tmp" "$dir/$stream.index" || drain_failed_stream
        for begin in $obsolete; do rm -f "$parts/$begin" || drain_failed_stream; done
        total=$next
    done
    rm -f "$parts/.incoming"
    exec 3<&-
}

chunk_logs() {
    attempt=0
    while [ "$attempt" -lt 5 ]; do
        attempt=$((attempt + 1))
        index=$(cat "$dir/$stream.index") || fail log_unavailable 'Log index unavailable'
        header=$(printf '%s\n' "$index" | head -n 1)
        IFS=' ' read -r base size <<EOF
$header
EOF
        case "$base:$size" in *[!0-9:]*|:*|*:) fail log_unavailable 'Corrupt log index';; esac
        [ "$offset" -le "$size" ] || fail log_offset_range 'Offset is beyond the current log'
        start=$offset
        [ "$start" -ge "$base" ] || start=$base
        end=$((start + limit))
        [ "$end" -le "$size" ] || end=$size
        entries=$(printf '%s\n' "$index" | tail -n +2)
        data=$(
            while IFS=' ' read -r begin length; do
                [ -n "$begin" ] || continue
                case "$begin:$length" in *[!0-9:]*|:*|*:) exit 1;; esac
                finish=$((begin + length))
                [ "$finish" -gt "$start" ] && [ "$begin" -lt "$end" ] || continue
                left=$start; [ "$left" -ge "$begin" ] || left=$begin
                right=$end; [ "$right" -le "$finish" ] || right=$finish
                tail -c "+$((left - begin + 1))" "$dir/$stream.parts/$begin" 2>/dev/null | head -c "$((right - left))"
            done <<EOF | base64 | tr -d '\n'
$entries
EOF
        )
        decoded=$(( ${#data} / 4 * 3 ))
        case "$data" in *==) decoded=$((decoded - 2));; *=) decoded=$((decoded - 1));; esac
        # Concurrent eviction must not yield a silently short/stitched response.
        [ "$decoded" -eq "$((end - start))" ] || continue
        emit job_id "$id"; emit stream "$stream"
        emit offset "$start"; emit next_offset "$end"; emit snapshot_size "$size"; emit base_offset "$base"
        if [ "$offset" -lt "$base" ]; then emit gap true; else emit gap false; fi
        if [ "$end" -ge "$size" ]; then emit eof true; else emit eof false; fi
        if [ -f "$dir/logs-cleaned" ]; then emit logs_deleted true; else emit logs_deleted false; fi
        if [ -f "$dir/log-incomplete" ]; then emit log_incomplete true; else emit log_incomplete false; fi
        emit data_base64 "$data"
        return
    done
    fail log_changed_during_read 'Log chunks rotated while reading; query again with the same cursor'
}

cleanup_job() {
    expected=$2
    [ ! -L "$dir" ] && [ ! -L "$dir/stdout.parts" ] && [ ! -L "$dir/stderr.parts" ] && [ ! -L "$dir/launcher.parts" ] || fail unsafe_path 'Refusing linked job storage'
    for name in exit logs-cleaned stdout.index stderr.index launcher.index stdout.index.tmp stderr.index.tmp launcher.index.tmp; do
        [ ! -L "$dir/$name" ] || fail unsafe_path 'Refusing linked cleanup metadata'
    done
    [ -f "$dir/exit" ] || fail job_not_terminal 'Only confirmed finished jobs can be cleaned'
    current=$(cat "$dir/exit")
    [ "$current" = "$expected" ] || fail cleanup_conflict 'Exit evidence changed after preview'
    case "$current" in *[!0-9\ ]*|'') fail job_not_terminal 'Invalid exit evidence';; esac
    # Preserve missing-evidence metadata even if a later deletion fails.
    : > "$dir/logs-cleaned" || fail cleanup_failed 'Cannot persist cleanup evidence'
    for stream in stdout stderr launcher; do
        file="$dir/$stream"; [ "$stream" != launcher ] || file="$dir/launcher.log"
        if [ -f "$dir/$stream.index" ]; then
            IFS=' ' read -r base total < "$dir/$stream.index"
        elif [ -f "$file" ]; then total=$(wc -c < "$file")
        else total=0; fi
        case "$total" in ''|*[!0-9]*) fail log_unavailable 'Invalid retained log size';; esac
        printf '%s %s\n' "$total" "$total" > "$dir/$stream.index.tmp" || exit 1
        mv "$dir/$stream.index.tmp" "$dir/$stream.index" || exit 1
        set +f
        for part in "$dir/$stream.parts/"*; do
            name=${part##*/}
            case "$name" in ''|*[!0-9]*) continue;; esac
            rm -f "$part" || exit 1
        done
        set -f
        rm -f "$file" || exit 1
    done
    emit cleaned true
    job_status
}
