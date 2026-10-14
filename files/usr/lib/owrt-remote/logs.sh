#!/bin/sh
# The private Remote journal lives in RAM. It never changes the system logd.
LOG_DIR="${OWRT_REMOTE_LOG_DIR:-/tmp/owrt-remote-logs}"
LOG_LIMIT=1048576
LOG_KEEP_SECONDS=86400

log_clock() { cut -d. -f1 /proc/uptime; }
log_ram_storage() {
	local resolved parent
	if [ -e "$LOG_DIR" ] || [ -L "$LOG_DIR" ]; then
		resolved="$(readlink -f "$LOG_DIR")" || return 1
	else
		# BusyBox realpath may require the last component to exist.
		# Resolve the existing parent before creating the journal directory.
		parent="$(readlink -f "$(dirname "$LOG_DIR")")" || return 1
		resolved="$parent/$(basename "$LOG_DIR")"
	fi
	awk -v path="$resolved" '
		(path == $2 || index(path, $2 "/") == 1 || $2 == "/") && length($2) > best {
			best = length($2); filesystem = $3
		}
		END { exit(filesystem == "tmpfs" ? 0 : 1) }
	' /proc/mounts
}
log_init() {
	umask 077
	log_ram_storage || { printf 'Remote journal requires RAM (tmpfs); refusing persistent storage.\n' >&2; return 1; }
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
	rm -f "$LOG_DIR/facts" "$LOG_DIR/facts.new" "$LOG_DIR/heartbeat-ok"
	rm -f "$LOG_DIR/journal-id" "$LOG_DIR/total" "$LOG_DIR/sync-ack"
	log_clock >"$LOG_DIR/age"
}
log_cursor_locked() {
	local total
	if [ ! -s "$LOG_DIR/journal-id" ]; then
		cat /proc/sys/kernel/random/uuid >"$LOG_DIR/journal-id" 2>/dev/null ||
			printf '%s-%s-%s\n' "$(date +%s)" "$(log_clock)" "$$" >"$LOG_DIR/journal-id"
	fi
	if [ ! -s "$LOG_DIR/total" ]; then
		total="$(wc -c <"$LOG_DIR/current.log")"
		[ ! -f "$LOG_DIR/previous.log" ] || total=$((total + $(wc -c <"$LOG_DIR/previous.log")))
		printf '%s\n' "$total" >"$LOG_DIR/total"
	fi
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
	LC_ALL=C awk -v fallback="${1:-OK}" '
		{
			line = substr($0, 1, 4096); lower = tolower(line)
			# Old info/access output may still arrive until Xray restarts.
			if (lower ~ /\[(info|debug)\]/ || lower ~ / from .* accepted / || lower ~ /a unified platform for anti-censorship/) next
			level = fallback
			if (line ~ /^\[(OK|WARN|ERROR)\]/) { level = substr(line, 2, index(line, "]") - 2); sub(/^\[[^]]+\][ ]*/, "", line) }
			else if (lower ~ /core: xray .* started/) level = "OK"
			else if (lower ~ /\[error\]|ошибка|Ошибка|ОШИБКА|не удалось|Не удалось|результат=bad|failed|cannot |could not |error:|syntax error|permission denied|network is unreachable|connection refused|connection reset|timed out|not permitted|no selected wan|wan has no|wan direct requires|already used by another rule|does not belong to remote|не применилось|dns failed|crash loop/) level = "ERROR"
			else if (lower ~ /\[warn(ing)?\]|предупреждение|Предупреждение|результат=warn|retry|not ready|missing|не найден|restart postponed|dns unavailable|fallback|reconnecting|restarting|failover/) level = "WARN"
			if (lower ~ /(authorization|hub_token|vless_uuid|ssh_vless_uuid|password|passwd|web[._ -]?key|[?&]key=|[?&]token=|"uuid"|"privatekey"|"password")/ ||
			    lower ~ /[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f-]+-[0-9a-f-]+-[0-9a-f-]+/)
				line = "[строка с ключом или паролем скрыта]"
			print "[" level "] " line
		}'
}
log_new_fact_locked() {
	local key now heartbeat new_fact
	# Compare event text without syslog timestamp, facility, PID or Xray time.
	key="$(printf '%s\n' "$1" | tr '\t' ' ' | sed -e 's/^\[[^]]*\] *//' -e 's/.*owrt-remote\(\[[0-9][0-9]*\]\)\{0,1\}: *//' -e 's/^[0-9][0-9]*\/[0-9/:. ]* *//' -e 's/ *(\([0-9][0-9]*\)\/[0-9][0-9]*)//')"
	heartbeat=0
	case "$key" in
		'Heartbeat: OK;'*)
			heartbeat=1
			if [ "$(cat "$LOG_DIR/heartbeat-ok" 2>/dev/null)" = "$key" ]; then return 1; fi
			printf '%s\n' "$key" >"$LOG_DIR/heartbeat-ok"
			# A recovery lets a later failure with the same cause appear again.
			if [ -f "$LOG_DIR/facts" ]; then
				awk -F '\t' '$2 !~ /Heartbeat:|heartbeat failed/ { print }' "$LOG_DIR/facts" >"$LOG_DIR/facts.new"
				mv -f "$LOG_DIR/facts.new" "$LOG_DIR/facts"
			fi ;;
	esac
	if [ "$heartbeat" = 1 ]; then return 0; fi
	key="${1%%]*}] $key"
	now="$(log_clock)"
	# A bounded cache suppresses repeated identical facts while they continue.
	# After a quiet period a new occurrence is recorded, rather than lost forever.
	awk -F '\t' -v now="$now" -v key="$key" '
		$2 == key { if (now - $1 < 120 && now >= $1) repeated = 1; next }
		{ saved[++n] = $0 }
		END {
			for (i = (n > 63 ? n - 62 : 1); i <= n; i++) print saved[i]
			print now "\t" key
			exit(repeated ? 1 : 0)
		}' "$LOG_DIR/facts" >"$LOG_DIR/facts.new" 2>/dev/null
	new_fact=$?
	mv -f "$LOG_DIR/facts.new" "$LOG_DIR/facts"
	[ "$new_fact" = 0 ] || return 1
	case "$key" in
		*Heartbeat:*|*'heartbeat failed'*)
			case "$1" in '[ERROR]'*|'[WARN]'*) rm -f "$LOG_DIR/heartbeat-ok" ;; esac ;;
		*'Сервис: запуск') rm -f "$LOG_DIR/heartbeat-ok" ;;
	esac
	return 0
}
log_append() {
	local line size
	# Process one bounded line at a time and release the lock between writes.
	log_redact "${1:-OK}" | while IFS= read -r line; do
		log_lock || exit 1
		trap 'log_unlock' EXIT
		trap 'exit 0' INT TERM
		log_maintain_locked
		log_cursor_locked
		[ -f "$LOG_DIR/facts" ] || : >"$LOG_DIR/facts"
		if ! log_new_fact_locked "$line"; then
			log_unlock
			trap - EXIT INT TERM
			continue
		fi
		size="$(wc -c <"$LOG_DIR/current.log")"
		if [ "$size" -ge "$LOG_LIMIT" ]; then
			mv -f "$LOG_DIR/current.log" "$LOG_DIR/previous.log"
			: >"$LOG_DIR/current.log"
		fi
		printf '%s\n' "$line" >>"$LOG_DIR/current.log"
		printf '%s\n' "$(( $(cat "$LOG_DIR/total") + $(printf '%s\n' "$line" | wc -c) ))" >"$LOG_DIR/total"
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
log_export() {
	local journal total previous current ack_id ack start available count clear_ack chunk
	log_lock || return 1
	log_maintain_locked
	log_cursor_locked
	journal="$(cat "$LOG_DIR/journal-id")"; total="$(cat "$LOG_DIR/total")"
	ack_id=''; ack=0
	if [ -s "$LOG_DIR/sync-ack" ]; then read -r ack_id ack <"$LOG_DIR/sync-ack"; fi
	[ "$ack_id" = "$journal" ] || ack=0
	case "$ack" in ''|*[!0-9]*) ack=0 ;; esac
	[ "$ack" -le "$total" ] || ack=0
	previous=0
	if [ -f "$LOG_DIR/previous.log" ]; then previous="$(wc -c <"$LOG_DIR/previous.log")"; exec 3<"$LOG_DIR/previous.log"; else exec 3</dev/null; fi
	current="$(wc -c <"$LOG_DIR/current.log")"; exec 4<"$LOG_DIR/current.log"
	start=$((total - previous - current)); [ "$ack" -le "$start" ] || start="$ack"
	available=$((total - start)); count="$available"; [ "$count" -le 65536 ] || count=65536
	clear_ack="$(cat "$LOG_DIR/sync-clear-ack" 2>/dev/null)"
	log_unlock
	chunk="$LOG_DIR/export.$$"
	trap 'rm -f "$chunk"; exec 3<&- 4<&-' EXIT
	: >"$chunk"
	if [ "$count" -gt 0 ]; then
		{ head -c "$previous" <&3; head -c "$current" <&4; } | tail -c "$available" | head -c "$count" >"$chunk"
	fi
	count="$(wc -c <"$chunk")"
	printf '{"journal_id":"%s","from":%s,"to":%s,"clear_ack":"%s","data":"' "$journal" "$start" "$((start + count))" "$clear_ack"
	base64 <"$chunk" | tr -d '\r\n'
	printf '"}\n'
	rm -f "$chunk"; exec 3<&- 4<&-; trap - EXIT
}
log_ack() {
	local journal offset clear_token
	journal="$1"; offset="$2"; clear_token="${3:-}"
	case "$journal$clear_token" in *[!a-zA-Z0-9._:-]*) return 1 ;; esac
	case "$offset" in ''|*[!0-9]*) return 1 ;; esac
	log_lock || return 1
	log_maintain_locked
	if [ -n "$clear_token" ] && [ "$(cat "$LOG_DIR/sync-clear-ack" 2>/dev/null)" != "$clear_token" ]; then
		log_reset_locked
		printf '%s\n' "$clear_token" >"$LOG_DIR/sync-clear-ack"
	elif [ "$(cat "$LOG_DIR/journal-id" 2>/dev/null)" = "$journal" ]; then
		printf '%s %s\n' "$journal" "$offset" >"$LOG_DIR/sync-ack"
	fi
	log_unlock
}
log_sync_loop() {
	while :; do
		/usr/sbin/owrt-remote logs-sync >/dev/null 2>&1
		sleep 3
	done
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
	check) log_init ;;
	collect) log_collect ;;
	read) log_read "${2:-}" ;;
	clear) log_clear ;;
	export) log_export ;;
	ack) log_ack "$2" "$3" "${4:-}" ;;
	sync) log_sync_loop ;;
	append) log_append ;;
	event)
		case "${3:-ok}" in bad|error) level=ERROR ;; warn) level=WARN ;; *) level=OK ;; esac
		{ printf '%s owrt-remote: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$2"; cat; } | log_append "$level" ;;
	*) printf 'Usage: logs.sh collect|read [live]|clear|append|event MESSAGE\n' >&2; exit 2 ;;
esac
