#!/bin/sh
# The private Remote journal lives in RAM. It never changes the system logd.
LOG_DIR="${OWRT_REMOTE_LOG_DIR:-/tmp/owrt-remote-logs}"
LOG_LIMIT=1048576
LOG_KEEP_SECONDS=86400

log_clock() { cut -d. -f1 /proc/uptime; }
log_init() {
	umask 077
	mkdir -p "$LOG_DIR" || return 1
	chmod 700 "$LOG_DIR"
}
log_lock() {
	local attempt owner
	log_init || return 1
	attempt=0
	while ! mkdir "$LOG_DIR/lock" 2>/dev/null; do
		owner="$(cat "$LOG_DIR/lock/pid" 2>/dev/null)"
		case "$owner" in
			''|*[!0-9]*)
				# Recover a writer killed between mkdir and writing its PID.
				if [ "$attempt" -ge 20 ]; then
					rm -f "$LOG_DIR/lock/pid"
					rmdir "$LOG_DIR/lock" 2>/dev/null || true
				fi ;;
			*) if ! kill -0 "$owner" 2>/dev/null; then
				rm -f "$LOG_DIR/lock/pid"
				rmdir "$LOG_DIR/lock" 2>/dev/null || true
			fi ;;
		esac
		attempt=$((attempt + 1))
		[ "$attempt" -lt 100 ] || return 1
		sleep 0.1
	done
	printf '%s\n' "$$" >"$LOG_DIR/lock/pid"
}
log_unlock() {
	rm -f "$LOG_DIR/lock/pid"
	rmdir "$LOG_DIR/lock" 2>/dev/null || true
}
log_reset_locked() {
	: >"$LOG_DIR/current.log"
	rm -f "$LOG_DIR/previous.log"
	log_clock >"$LOG_DIR/age"
}
log_maintain_locked() {
	local now age
	now="$(log_clock)"
	age="$(cat "$LOG_DIR/age" 2>/dev/null)"
	case "$age" in ''|*[!0-9]*) log_reset_locked; return ;; esac
	if [ "$now" -lt "$age" ] || [ "$((now - age))" -ge "$LOG_KEEP_SECONDS" ]; then
		log_reset_locked
	fi
}
log_redact() {
	# Never retain command blocks containing credentials or authenticated URLs.
	LC_ALL=C awk '
		{
			line = substr($0, 1, 4096); lower = tolower(line)
			if (lower ~ /(authorization|hub_token|vless_uuid|ssh_vless_uuid|password|passwd|web[._ -]?key|[?&]key=|[?&]token=|"uuid"|"privatekey"|"password")/ ||
			    lower ~ /[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f-]+-[0-9a-f-]+-[0-9a-f-]+/)
				print "[строка с ключом или паролем скрыта]"
			else print line
		}'
}
log_append() {
	local line size
	# Process one bounded line at a time and release the lock between writes.
	log_redact | while IFS= read -r line; do
		log_lock || exit 1
		trap 'log_unlock' EXIT
		trap 'exit 0' INT TERM
		log_maintain_locked
		size="$(wc -c <"$LOG_DIR/current.log")"
		if [ "$size" -ge "$LOG_LIMIT" ]; then
			mv -f "$LOG_DIR/current.log" "$LOG_DIR/previous.log"
			: >"$LOG_DIR/current.log"
		fi
		printf '%s\n' "$line" >>"$LOG_DIR/current.log"
		log_unlock
		trap - EXIT INT TERM
	done
}
log_read() {
	local previous_size current_size
	log_lock || return 1
	log_maintain_locked
	# Open the retained files under the lock, then release it before streaming
	# to a slow phone. Downloads must not prevent the collector from writing.
	previous_size=0
	if [ -f "$LOG_DIR/previous.log" ]; then
		previous_size="$(wc -c <"$LOG_DIR/previous.log")"
		exec 3<"$LOG_DIR/previous.log"
	else
		exec 3</dev/null
	fi
	current_size="$(wc -c <"$LOG_DIR/current.log")"
	exec 4<"$LOG_DIR/current.log"
	log_unlock
	if [ "${1:-}" = live ]; then
		{ head -c "$previous_size" <&3; head -c "$current_size" <&4; } | tail -c 65536
	else
		head -c "$previous_size" <&3
		head -c "$current_size" <&4
	fi
	exec 3<&- 4<&-
}
log_clear() {
	log_lock || return 1
	log_reset_locked
	log_unlock
}
log_collect() {
	# These variables must remain visible to EXIT even after this function returns.
	log_init || return 1
	fifo="$LOG_DIR/stream.$$"
	mkfifo "$fifo" || return 1
	reader=''; writer=''; timer=''
	trap 'trap - EXIT; for child in "$reader" "$writer" "$timer"; do [ -z "$child" ] || kill "$child" 2>/dev/null || true; done; wait; rm -f "$fifo"' EXIT
	trap 'exit 0' INT TERM
	# Follow only new Remote/Xray service records: a clear must not reload history.
	logread -f -e 'owrt-remote\(\[[0-9][0-9]*\]\)\{0,1\}:' >"$fifo" 2>/dev/null &
	reader=$!
	while IFS= read -r line; do printf '%s\n' "$line" | log_append; done <"$fifo" &
	writer=$!
	while :; do
		log_lock || return 1
		log_maintain_locked
		log_unlock
		sleep 60 & timer=$!
		wait "$timer" || return 0
		kill -0 "$reader" 2>/dev/null && kill -0 "$writer" 2>/dev/null || return 1
	done
}

case "${1:-}" in
	collect) log_collect ;;
	read) log_read "${2:-}" ;;
	clear) log_clear ;;
	append) log_append ;;
	event) { printf '%s owrt-remote: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$2"; cat; } | log_append ;;
	*) printf 'Usage: logs.sh collect|read [live]|clear|append|event MESSAGE\n' >&2; exit 2 ;;
esac
