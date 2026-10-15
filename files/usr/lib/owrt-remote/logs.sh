#!/bin/sh
# Persistent, bounded spool; only the small write buffer and process state use RAM.
RUN_DIR="${OWRT_REMOTE_LOG_RUNTIME:-/tmp/owrt-remote-log-runtime}"
LOG_LIMIT=2097152
LOG_KEEP_SECONDS=86400
LOG_PART_LIMIT=256
LOG_BUFFER_LIMIT=16384
LOG_BATCH_LIMIT=16384
LOG_FLUSH_SECONDS=60
LOG_FREE_KB=2048

log_clock() { cut -d. -f1 /proc/uptime; }
log_epoch() { date +%s; }
log_self_pid() {
	# $$ is the parent shell's PID inside ash subshells/pipelines. Reading proc
	# with the shell builtin yields the actual lock holder, including the writer.
	local self rest
	if read -r self rest </proc/self/stat 2>/dev/null; then printf '%s\n' "$self"; else printf '%s\n' "$$"; fi
}
log_mount() {
	# Resolve symlinks, including the existing ancestor of a new directory.
	local path suffix
	path="$1"; suffix=''
	case "$path" in /*) ;; *) return 1 ;; esac
	while [ ! -e "$path" ]; do
		[ "$path" != / ] || return 1
		suffix="/$(basename "$path")$suffix"; path="$(dirname "$path")"
	done
	path="$(readlink -f "$path")$suffix" || return 1
	awk -v path="$path" '
		(path == $2 || index(path, $2 "/") == 1 || $2 == "/") && length($2) > best {
			best=length($2); mount=$2; fs=$3; flags=$4
		}
		END { if (best) print mount, fs, flags; else exit 1 }
	' /proc/mounts
}
log_ram_storage() {
	local mount fs flags
	read -r mount fs flags <<EOF
$(log_mount "$RUN_DIR")
EOF
	[ "$fs" = tmpfs ]
}
log_flash_storage() {
	local mount fs flags
	read -r mount fs flags <<EOF
$(log_mount "$1")
EOF
	case "$fs" in jffs2|ubifs|ext4|f2fs|btrfs|ext2|ext3) ;; *) return 1 ;; esac
	case ",$flags," in *,rw,*) ;; *) return 1 ;; esac
	# A tmpfs/root overlay and an unmounted /overlay are never persistent storage.
	[ "$mount" != /overlay ] || [ -d /overlay ]
}
log_init() {
	local candidate selected mount fs flags free
	umask 077
	log_ram_storage || { printf 'Remote: runtime directory must be tmpfs.\n' >&2; return 1; }
	mkdir -p "$RUN_DIR" || return 1
	chmod 700 "$RUN_DIR" || return 1
	# Pin one storage choice for this boot; never interleave two journal IDs.
	selected="$(cat "$RUN_DIR/storage" 2>/dev/null)"
	if [ -n "$selected" ]; then
		LOG_DIR="$selected"
	else
		candidate="${OWRT_REMOTE_LOG_DIR:-}"
		if [ -z "$candidate" ]; then
			# Select /overlay only when it really is a writable persistent mount.
			read -r mount fs flags <<EOF
$(log_mount /overlay)
EOF
			if [ "$mount" = /overlay ] && log_flash_storage /overlay; then
				candidate=/overlay/owrt-remote/logs
			elif log_flash_storage /; then
				# OpenWrt installations with a directly writable ext4/UBIFS root.
				candidate=/etc/owrt-remote/logs
			fi
		fi
		if [ -n "$candidate" ] && log_flash_storage "$candidate" &&
			mkdir -p "$candidate" && [ -w "$candidate" ]; then
			LOG_DIR="$candidate"
			chmod 700 "$LOG_DIR" || return 1
			free="$(df -Pk "$LOG_DIR" | awk 'NR==2 {print $4}')"
			case "$free" in ''|*[!0-9]*) free=0 ;; esac
			# Reading an existing queue is still useful on a full disk; starting
			# a new one must not consume the last bytes needed for router settings.
			if [ ! -s "$LOG_DIR/state" ] && [ "$free" -lt "$LOG_FREE_KB" ]; then LOG_DIR="$RUN_DIR/fallback"; fi
		else
			LOG_DIR="$RUN_DIR/fallback"
		fi
	fi
	PERSISTENT=1
	if [ "$LOG_DIR" = "$RUN_DIR/fallback" ]; then
		PERSISTENT=0; LOG_LIMIT=65536; LOG_PART_LIMIT=8
		mkdir -p "$LOG_DIR" || return 1
	fi
}
log_lock() {
	local attempt owner
	log_init || return 1
	attempt=0
	while ! mkdir "$RUN_DIR/lock" 2>/dev/null; do
		owner="$(cat "$RUN_DIR/lock/pid" 2>/dev/null)"
		case "$owner" in
			''|*[!0-9]*)
				[ "$attempt" -lt 20 ] || log_reap_lock "$RUN_DIR/lock" "$attempt" ;;
			*) kill -0 "$owner" 2>/dev/null || log_reap_lock "$RUN_DIR/lock" "$attempt" ;;
		esac
		attempt=$((attempt + 1)); [ "$attempt" -lt 100 ] || return 1
		sleep 0.1
	done
	log_self_pid >"$RUN_DIR/lock/pid"
	# Another first writer may have selected storage while we waited.
	if [ -s "$RUN_DIR/storage" ]; then
		LOG_DIR="$(cat "$RUN_DIR/storage")"
		[ "$LOG_DIR" != "$RUN_DIR/fallback" ] || { PERSISTENT=0; LOG_LIMIT=65536; LOG_PART_LIMIT=8; }
	else
		printf '%s\n' "$LOG_DIR" >"$RUN_DIR/storage"
	fi
	# Change storage only under the same append/export lock. On write/mount
	# failure preserve the Flash queue and switch this boot to a bounded fallback.
	if [ "$LOG_DIR" != "$RUN_DIR/fallback" ] && { ! log_flash_storage "$LOG_DIR" || [ ! -w "$LOG_DIR" ] || [ -f "$RUN_DIR/write-warning" ]; }; then
		log_fallback_locked || { log_unlock; return 1; }
	fi
}
log_reap_lock() {
	local owner reap_owner
	# Serialize stale-lock removal itself, so two contenders cannot remove a
	# freshly acquired lock after observing the same previous dead owner.
	if ! mkdir "$1/reap" 2>/dev/null; then
		reap_owner="$(cat "$1/reap/pid" 2>/dev/null)"
		case "$reap_owner" in
			''|*[!0-9]*) [ "${2:-0}" -ge 20 ] || return 0 ;;
			*) kill -0 "$reap_owner" 2>/dev/null && return 0 ;;
		esac
		rm -f "$1/reap/pid"; rmdir "$1/reap" 2>/dev/null || return 0
		return 0
	fi
	log_self_pid >"$1/reap/pid"
	owner="$(cat "$1/pid" 2>/dev/null)"
	case "$owner" in
		''|*[!0-9]*) rm -f "$1/pid" ;;
		*) if kill -0 "$owner" 2>/dev/null; then rm -f "$1/reap/pid"; rmdir "$1/reap"; return 0; else rm -f "$1/pid"; fi ;;
	esac
	rm -f "$1/reap/pid"; rmdir "$1/reap"; rmdir "$1" 2>/dev/null || true
}
log_fallback_locked() {
	LOG_DIR="$RUN_DIR/fallback"; PERSISTENT=0; LOG_LIMIT=65536; LOG_PART_LIMIT=8
	mkdir -p "$LOG_DIR" || return 1
	printf '%s\n' "$LOG_DIR" >"$RUN_DIR/storage"
	rm -f "$RUN_DIR/recovered" "$RUN_DIR/recovered-v2" "$RUN_DIR/ack"
}
log_unlock() { rm -f "$RUN_DIR/lock/pid"; rmdir "$RUN_DIR/lock" 2>/dev/null || true; }
log_warn_locked() {
	# One warning until successful recovery; never feed our own collector a loop.
	if [ ! -f "$RUN_DIR/$1" ]; then
		local warning size
		warning="$(printf '[WARN] %s owrt-remote: %s' "$(date '+%Y-%m-%d %H:%M:%S')" "$2")"
		size=0; [ ! -f "$RUN_DIR/pending" ] || size="$(wc -c <"$RUN_DIR/pending")"
		if [ "$((size + $(printf '%s\n' "$warning" | wc -c)))" -le "$LOG_BUFFER_LIMIT" ]; then printf '%s\n' "$warning" >>"$RUN_DIR/pending"; fi
		: >"$RUN_DIR/$1"
		printf 'Remote: %s\n' "$2" >&2
	fi
}
log_new_id() {
	cat /proc/sys/kernel/random/uuid 2>/dev/null || printf '%s-%s-%s\n' "$(log_epoch)" "$(log_clock)" "$$"
}
log_checksum() {
	# OpenWrt does not necessarily ship the optional BusyBox cksum applet.
	# Validate command output before publishing a chunk or advancing its cursor.
	local method value
	case "${2:-}" in
		s256-*) method=s256 ;;
		md5-*) method=md5 ;;
		*[!0-9]*|'')
			if command -v sha256sum >/dev/null 2>&1; then method=s256
			elif command -v md5sum >/dev/null 2>&1; then method=md5
			else method=crc; fi ;;
		*) method=crc ;;
	esac
	case "$method" in
		s256) value="$(sha256sum "$1" 2>/dev/null)" || return 1 ;;
		md5) value="$(md5sum "$1" 2>/dev/null)" || return 1 ;;
		crc) value="$(cksum <"$1" 2>/dev/null)" || return 1 ;;
	esac
	# Both BusyBox and GNU utilities separate the digest from the file by space.
	value="${value%% *}"
	case "$method" in
		s256|md5)
			case "$value" in ''|*[!0-9a-f]*) return 1 ;; esac
			if [ "$method" = s256 ]; then [ "${#value}" = 64 ] || return 1; else [ "${#value}" = 32 ] || return 1; fi
			printf '%s-%s\n' "$method" "$value" ;;
		crc) case "$value" in ''|*[!0-9]*) return 1 ;; esac; printf '%s\n' "$value" ;;
	esac
}
log_checkpoint_locked() {
	STATE_CHANGED=0
	# Same filesystem + rename: no partially rewritten cursor is ever observed.
	# Unchanged state causes no Flash write, including idle sync polls.
	if [ "$(cat "$LOG_DIR/state" 2>/dev/null)" = "$JOURNAL $ACK $TOTAL ${CLEAR:--}" ]; then return 0; fi
	printf '%s %s %s %s\n' "$JOURNAL" "$ACK" "$TOTAL" "${CLEAR:--}" >"$LOG_DIR/state.new" &&
		mv -f "$LOG_DIR/state.new" "$LOG_DIR/state" || return 1
	STATE_CHANGED=1
}
log_commit_locked() {
	# Durability is a batch operation, never per log line. BusyBox builds without
	# sync -f use ordinary sync; no extra package or fsync-per-line is required.
	[ "$PERSISTENT" = 0 ] || [ "$STATE_CHANGED" = 0 ] || { sync -f "$LOG_DIR" 2>/dev/null || sync; }
}
log_load_locked() {
	local extra file start end crc actual
	log_finish_migration_locked || return 1
	JOURNAL=''; ACK=0; TOTAL=0; CLEAR='-'
	if [ -s "$LOG_DIR/state" ]; then
		read -r JOURNAL ACK TOTAL CLEAR extra <"$LOG_DIR/state"
		case "$JOURNAL" in ''|*[!a-zA-Z0-9._:-]*) JOURNAL='' ;; esac
		case "$ACK:$TOTAL" in *[!0-9:]*|:*|*:) JOURNAL='' ;; esac
		case "$CLEAR" in ''|*[!a-zA-Z0-9._:-]*) JOURNAL='' ;; esac
		[ "${#JOURNAL}" -le 80 ] && [ "${#CLEAR}" -le 80 ] && [ "${#ACK}" -le 16 ] && [ "${#TOTAL}" -le 16 ] || JOURNAL=''
		[ -z "$extra" ] || JOURNAL=''
	fi
	if [ -z "$JOURNAL" ]; then
		# If a state sector was lost, recover the newest committed generation.
		# Reset only the ack: Hub offsets make re-delivery harmless.
		file="$(ls -t "$LOG_DIR"/part.* 2>/dev/null | head -n 1)"
		if [ -n "$file" ]; then
			JOURNAL="${file##*/part.}"; JOURNAL="${JOURNAL%.*}"; JOURNAL="${JOURNAL%.*}"; JOURNAL="${JOURNAL%.*}"
			case "$JOURNAL" in ''|*[!a-zA-Z0-9._:-]*) JOURNAL='' ;; esac
		fi
		[ -n "$JOURNAL" ] || JOURNAL="$(log_new_id)"
		ACK=0; TOTAL=0; CLEAR='-'
		log_checkpoint_locked && log_commit_locked || return 1
	fi
	# Validate immutable chunks once per collector boot, never load their contents.
	if [ ! -f "$RUN_DIR/recovered-v2" ]; then
		log_repair_legacy_locked || return 1
		# A completed explicit clear may have left old chunks after a power cut.
		for file in "$LOG_DIR"/part.*; do
			[ -f "$file" ] || continue
			case "$file" in "$LOG_DIR/part.$JOURNAL."*) ;; *) rm -f "$file" ;; esac
		done
		for file in "$LOG_DIR"/part."$JOURNAL".*; do
			[ -f "$file" ] || continue
			log_part_fields "$file" || continue
			actual="$(log_checksum "$file" "$crc")" || return 1
			if [ "$actual" != "$crc" ] || [ "$(wc -c <"$file")" -ne "$((end - start))" ]; then
				# Keep one damaged chunk on Flash for inspection; never export it.
				mv -f "$file" "$LOG_DIR/damaged.log" || return 1
				log_warn_locked damaged-warning 'Повреждённый фрагмент журнала изолирован; возможна потеря последнего пакета после сбоя питания.'
			fi
		done
		rm -f "$LOG_DIR/chunk.new" "$LOG_DIR/state.new"
		for file in "$LOG_DIR"/part."$JOURNAL".*; do
			[ -f "$file" ] || continue
			log_part_fields "$file" || continue
			[ "$end" -le "$TOTAL" ] || TOTAL="$end"
		done
		log_checkpoint_locked && log_commit_locked || return 1
		: >"$RUN_DIR/recovered-v2"
	fi
	# A process can die after durable rename/checkpoint but before clearing its
	# RAM buffer. Reconcile that one in-flight batch before accepting another line.
	if [ -s "$RUN_DIR/inflight" ]; then
		read -r extra start end crc <"$RUN_DIR/inflight"
		case "$start:$end" in *[!0-9:]*|:*|*:) return 1 ;; esac
		case "$crc" in ''|*[!0-9a-fsmd-]*) return 1 ;; esac
		file="$LOG_DIR/$(printf 'part.%s.%018d.%018d.%s' "$extra" "$start" "$end" "$crc")"
		if [ "$extra" = "$JOURNAL" ] && [ -f "$file" ]; then
			actual="$(log_checksum "$file" "$crc")" || return 1
			if [ "$actual" = "$crc" ] && [ "$(wc -c <"$file")" -eq "$((end - start))" ]; then
				[ "$TOTAL" -ge "$end" ] || TOTAL="$end"
				log_checkpoint_locked && log_commit_locked || return 1
				: >"$RUN_DIR/pending"
			fi
		fi
		rm -f "$RUN_DIR/inflight"
	fi
	[ "$ACK" -le "$TOTAL" ] || ACK=0
	if [ -s "$RUN_DIR/ack" ]; then
		read -r extra actual <"$RUN_DIR/ack"
		if [ "$extra" = "$JOURNAL" ]; then
			case "$actual" in ''|*[!0-9]*) ;; *) if [ "$actual" -le "$TOTAL" ] && [ "$actual" -gt "$ACK" ]; then ACK="$actual"; fi ;; esac
		fi
	fi
	if [ ! -f "$RUN_DIR/last-flush" ]; then log_clock >"$RUN_DIR/last-flush"; fi
	[ -f "$RUN_DIR/pending" ] || : >"$RUN_DIR/pending"
	if [ "$PERSISTENT" = 0 ]; then log_warn_locked fallback-warning 'Flash недоступна: используется ограниченный журнал в ОЗУ (64 КиБ + буфер 16 КиБ); после перезагрузки он исчезнет.'; fi
}
log_part_fields() {
	local fields
	fields="${1##*/part.$JOURNAL.}"
	start="${fields%%.*}"; fields="${fields#*.}"; end="${fields%%.*}"; crc="${fields#*.}"
	# Windows fixtures strip trailing dots; also recover a legacy missing suffix.
	if [ "$fields" = "$end" ] && [ "${2:-}" = repair ]; then crc=''; fi
	case "$start:$end" in *[!0-9:]*|:*|*:) return 1 ;; esac
	[ "${#start}" -le 18 ] && [ "${#end}" -le 18 ] || return 1
	case "$crc" in
		'') [ "${2:-}" = repair ] || return 1 ;;
		s256-*) fields="${crc#s256-}"; case "$fields" in *[!0-9a-f]*) return 1 ;; esac; [ "${#fields}" = 64 ] || return 1 ;;
		md5-*) fields="${crc#md5-}"; case "$fields" in *[!0-9a-f]*) return 1 ;; esac; [ "${#fields}" = 32 ] || return 1 ;;
		*[!0-9]*) return 1 ;;
		*) [ "${#crc}" -le 10 ] || return 1 ;;
	esac
	# Avoid octal arithmetic for zero-padded names.
	while [ "${start#0}" != "$start" ]; do start="${start#0}"; done; start="${start:-0}"
	while [ "${end#0}" != "$end" ]; do end="${end#0}"; done; end="${end:-0}"
	[ "$end" -gt "$start" ] && [ "$((end - start))" -le "$LOG_BUFFER_LIMIT" ]
}
log_repair_legacy_locked() {
	local file start end crc found base maximum bytes free new_id dest actual
	found=0; base="$TOTAL"; maximum="$TOTAL"; bytes=0
	for file in "$LOG_DIR"/part."$JOURNAL".*; do
		[ -f "$file" ] || continue
		log_part_fields "$file" repair || continue
		[ -n "$crc" ] || found=1
		[ "$start" -ge "$base" ] || base="$start"
		[ "$end" -le "$maximum" ] || maximum="$end"
		bytes=$((bytes + end - start))
	done
	[ "$found" = 1 ] || return 0
	# The previous bug confirmed empty exports without delivering these chunks.
	# Publish a new generation so that the Hub accepts their replay. Only missing
	# digest files of the expected length can be recovered; no invented checksum.
	if [ "$PERSISTENT" = 1 ]; then
		free="$(df -Pk "$LOG_DIR" | awk 'NR==2 {print $4}')"
		case "$free" in ''|*[!0-9]*) return 1 ;; esac
		[ "$free" -ge "$((LOG_FREE_KB + (bytes + 1023) / 1024))" ] || return 1
	fi
	new_id="recovered-$(log_new_id)"
	mkdir -p "$LOG_DIR/migrating" || return 1
	# An interrupted, uncommitted attempt is safe to rebuild from the originals.
	rm -f "$LOG_DIR"/migrating/part.* "$LOG_DIR/migrating/chunk.new"
	for file in "$LOG_DIR"/part."$JOURNAL".*; do
		[ -f "$file" ] || continue
		log_part_fields "$file" repair || return 1
		[ "$(wc -c <"$file")" -eq "$((end - start))" ] || return 1
		if [ -n "$crc" ]; then actual="$(log_checksum "$file" "$crc")" && [ "$actual" = "$crc" ] || return 1; fi
		cp "$file" "$LOG_DIR/migrating/chunk.new" || return 1
		crc="$(log_checksum "$LOG_DIR/migrating/chunk.new")" || return 1
		dest="$(printf 'part.%s.%018d.%018d.%s' "$new_id" "$((start - base))" "$((end - base))" "$crc")"
		mv -f "$LOG_DIR/migrating/chunk.new" "$LOG_DIR/migrating/$dest" || return 1
	done
	printf '%s 0 %s %s\n' "$new_id" "$((maximum - base))" "$CLEAR" >"$LOG_DIR/migrating/state.new" &&
		mv -f "$LOG_DIR/migrating/state.new" "$LOG_DIR/migrating/state" || return 1
	sync -f "$LOG_DIR" 2>/dev/null || sync
	log_finish_migration_locked || return 1
	JOURNAL="$new_id"; ACK=0; TOTAL=$((maximum - base))
	rm -f "$RUN_DIR/ack" "$RUN_DIR/inflight"
	log_warn_locked repaired-warning 'Восстановлены фрагменты без контрольной суммы. Сохранённая история повторно отправляется на VPS; ранее целостность этих фрагментов не проверялась.'
}
log_prune_locked() {
	local file start end crc age now bytes count
	now="$(log_epoch)"; bytes=0; count=0
	for file in "$LOG_DIR"/part."$JOURNAL".*; do
		[ -f "$file" ] || continue
		bytes=$((bytes + $(wc -c <"$file"))); count=$((count + 1))
	done
	for file in "$LOG_DIR"/part."$JOURNAL".*; do
		[ -f "$file" ] || continue
		log_part_fields "$file" || continue
		[ "$end" -le "$ACK" ] || continue
		age="$(stat -c %Y "$file" 2>/dev/null)"; age="${age:-$now}"
		# Only confirmed chunks may expire or make room. Backwards wall-clock is safe.
		if [ "$((now - age))" -ge "$LOG_KEEP_SECONDS" ] || [ "$bytes" -gt "$((LOG_LIMIT - LOG_BUFFER_LIMIT))" ] || [ "$count" -ge "$LOG_PART_LIMIT" ]; then
			rm -f "$file" || return 1
			bytes=$((bytes - (end - start))); count=$((count - 1))
		fi
	done
	STORED_BYTES="$bytes"; STORED_PARTS="$count"
}
log_flush_locked() {
	local force now last bytes free crc dest dropped
	force="${1:-}"; now="$(log_clock)"; last="$(cat "$RUN_DIR/last-flush")"
	[ "$force" = force ] || [ "$now" -lt "$last" ] || [ "$((now - last))" -ge "$LOG_FLUSH_SECONDS" ] || return 0
	# A successful Hub ack is checkpointed before confirmed chunks can be removed.
	log_checkpoint_locked || { log_warn_locked write-warning 'Flash: ошибка записи состояния; временный буфер ограничен, рабочие настройки не изменены.'; return 1; }
	log_commit_locked || return 1
	log_prune_locked || return 1
	bytes="$(wc -c <"$RUN_DIR/pending")"
	if [ "$bytes" -gt 0 ]; then
		if [ "$STORED_BYTES" -gt "$((LOG_LIMIT - bytes))" ] || [ "$STORED_PARTS" -ge "$LOG_PART_LIMIT" ]; then
			log_warn_locked queue-warning 'Очередь логов заполнена: неподтверждённые записи сохранены; новые события сверх лимита пропускаются до освобождения места.'
			log_clock >"$RUN_DIR/last-flush"
			return 0
		fi
		if [ "$PERSISTENT" = 1 ]; then
			free="$(df -Pk "$LOG_DIR" | awk 'NR==2 {print $4}')"
			case "$free" in ''|*[!0-9]*) free=0 ;; esac
			if [ "$free" -lt "$LOG_FREE_KB" ]; then log_warn_locked write-warning 'На Flash менее 2 МиБ свободного места; запись логов приостановлена, буфер в ОЗУ ограничен.'; return 1; fi
		fi
		# An immutable chunk is either fully committed by rename, or ignored on recovery.
		# A fixed-size RAM buffer is the only source; never copy the entire journal.
		cp "$RUN_DIR/pending" "$LOG_DIR/chunk.new" || { log_warn_locked write-warning 'Flash: ошибка записи журнала; данные остаются в ограниченном буфере ОЗУ.'; return 1; }
		crc="$(log_checksum "$LOG_DIR/chunk.new")" || { log_warn_locked checksum-warning 'Не удалось вычислить контрольную сумму; журнал не подтверждён, записи остаются в буфере.'; return 1; }
		printf '%s %s %s %s\n' "$JOURNAL" "$TOTAL" "$((TOTAL + bytes))" "$crc" >"$RUN_DIR/inflight"
		dest="$(printf 'part.%s.%018d.%018d.%s' "$JOURNAL" "$TOTAL" "$((TOTAL + bytes))" "$crc")"
		mv -f "$LOG_DIR/chunk.new" "$LOG_DIR/$dest" || { log_warn_locked write-warning 'Flash: не удалось зафиксировать фрагмент журнала.'; return 1; }
		TOTAL=$((TOTAL + bytes))
		log_checkpoint_locked && log_commit_locked || { log_warn_locked write-warning 'Flash: ошибка фиксации состояния журнала.'; return 1; }
		: >"$RUN_DIR/pending"
		rm -f "$RUN_DIR/inflight"
		rm -f "$RUN_DIR/write-warning" "$RUN_DIR/queue-warning" "$RUN_DIR/buffer-warning"
		dropped="$(cat "$RUN_DIR/dropped" 2>/dev/null)"; dropped="${dropped:-0}"
		if [ "$dropped" -gt 0 ]; then
			printf '[WARN] %s owrt-remote: Восстановлена запись журнала; пропущено новых событий сверх лимита: %s.\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$dropped" >"$RUN_DIR/pending"
			rm -f "$RUN_DIR/dropped"
		fi
	fi
	log_clock >"$RUN_DIR/last-flush"
}
log_open() {
	log_lock || return 1
	trap 'log_unlock' EXIT
	trap 'exit 1' INT TERM
	if ! log_load_locked; then
		if [ "$PERSISTENT" = 1 ]; then
			log_fallback_locked && log_load_locked || { log_close; return 1; }
		else
			log_close; return 1
		fi
	fi
}
log_close() { log_unlock; trap - EXIT INT TERM; }
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
			if [ "$(cat "$RUN_DIR/heartbeat-ok" 2>/dev/null)" = "$key" ]; then return 1; fi
			printf '%s\n' "$key" >"$RUN_DIR/heartbeat-ok"
			# A recovery lets the same failure appear again; keys are fixed-size hashes.
			: >"$RUN_DIR/facts" ;;
	esac
	if [ "$heartbeat" = 1 ]; then return 0; fi
	# Update heartbeat recovery state before replacing the full key with its hash.
	case "$key" in
		*Heartbeat:*|*'heartbeat failed'*)
			case "$1" in '[ERROR]'*|'[WARN]'*) rm -f "$RUN_DIR/heartbeat-ok" ;; esac ;;
		*'Сервис: запуск') rm -f "$RUN_DIR/heartbeat-ok" ;;
	esac
	printf '%s\n' "${1%%]*}] $key" >"$RUN_DIR/fact-key"
	key="$(log_checksum "$RUN_DIR/fact-key")" || { rm -f "$RUN_DIR/fact-key"; return 0; }
	rm -f "$RUN_DIR/fact-key"
	# Dedup needs only a fingerprint, not the full SHA-256. Keep 64 entries <4 KiB.
	key="$(printf '%.40s' "$key")"
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
		}' "$RUN_DIR/facts" >"$RUN_DIR/facts.new" 2>/dev/null
	new_fact=$?
	mv -f "$RUN_DIR/facts.new" "$RUN_DIR/facts"
	[ "$new_fact" = 0 ] || return 1
	return 0
}
log_append() {
	local line size bytes dropped
	log_redact "${1:-OK}" | while IFS= read -r line; do
		log_open || exit 1
		log_flush_locked || true
		[ -f "$RUN_DIR/facts" ] || : >"$RUN_DIR/facts"
		if log_new_fact_locked "$line"; then
			size="$(wc -c <"$RUN_DIR/pending")"; bytes="$(printf '%s\n' "$line" | wc -c)"
			# Reserve room for a single overflow warning, too.
			if [ "$((size + bytes))" -le "$((LOG_BUFFER_LIMIT - 1024))" ]; then
				printf '%s\n' "$line" >>"$RUN_DIR/pending"
			else
				dropped="$(cat "$RUN_DIR/dropped" 2>/dev/null)"; dropped="${dropped:-0}"
				[ "$dropped" -ge 2147483647 ] || dropped=$((dropped + 1))
				printf '%s\n' "$dropped" >"$RUN_DIR/dropped"
				log_warn_locked buffer-warning 'Буфер логов заполнен: новые события сверх лимита пропускаются, неподтверждённый журнал сохранён.'
			fi
		fi
		log_close
	done
}
log_read() {
	local file start end crc snapshot mode owner
	log_open || return 1
	# Capture only file names and the small pending buffer. Slow HTTP clients must
	# never hold the append lock; concurrent expiry may omit an already-acked chunk.
	# One viewer snapshot at a time bounds temporary files even with slow clients.
	if ! mkdir "$RUN_DIR/view-lock" 2>/dev/null; then
		owner="$(cat "$RUN_DIR/view-lock/pid" 2>/dev/null)"
		case "$owner" in ''|*[!0-9]*) ;; *) kill -0 "$owner" 2>/dev/null && { log_close; return 1; } ;; esac
		rm -f "$RUN_DIR/view-lock/pid"; rmdir "$RUN_DIR/view-lock" 2>/dev/null || { log_close; return 1; }
		mkdir "$RUN_DIR/view-lock" || { log_close; return 1; }
	fi
	printf '%s\n' "$$" >"$RUN_DIR/view-lock/pid"
	rm -f "$RUN_DIR"/view.*
	snapshot="$RUN_DIR/view.$$"; mode="${1:-}"
	: >"$snapshot.list"
	for file in "$LOG_DIR"/part."$JOURNAL".*; do
		[ ! -f "$file" ] || printf '%s\n' "$file" >>"$snapshot.list"
	done
	cp "$RUN_DIR/pending" "$snapshot.pending" || { rm -f "$RUN_DIR/view-lock/pid"; rmdir "$RUN_DIR/view-lock"; log_close; return 1; }
	log_close
	trap 'rm -f "$snapshot.list" "$snapshot.pending" "$RUN_DIR/view-lock/pid"; rmdir "$RUN_DIR/view-lock" 2>/dev/null || true' EXIT
	trap 'exit 1' INT TERM
	if [ "$mode" = live ]; then
		{ while IFS= read -r file; do [ ! -f "$file" ] || cat "$file"; done <"$snapshot.list"; cat "$snapshot.pending"; } | tail -c 65536
	else
		while IFS= read -r file; do [ ! -f "$file" ] || cat "$file"; done <"$snapshot.list"
		cat "$snapshot.pending"
	fi
	rm -f "$snapshot.list" "$snapshot.pending" "$RUN_DIR/view-lock/pid"; rmdir "$RUN_DIR/view-lock"; trap - EXIT INT TERM
}
log_reset_locked() {
	local clear
	clear="${1:--}"
	# Commit a new generation before deleting its predecessor; a crash cannot
	# attach old offsets to a new journal. Explicit clear also discards unacked data.
	JOURNAL="$(log_new_id)"; ACK=0; TOTAL=0; CLEAR="$clear"
	# Chunks include their generation ID: atomically switching state makes all
	# previous chunks inactive, even if power fails during their subsequent removal.
	log_checkpoint_locked && log_commit_locked || return 1
	rm -f "$LOG_DIR"/part.* "$LOG_DIR/damaged.log"
	: >"$RUN_DIR/pending"
	rm -f "$RUN_DIR/ack" "$RUN_DIR/facts" "$RUN_DIR/heartbeat-ok" "$RUN_DIR/dropped" "$RUN_DIR/buffer-warning" "$RUN_DIR/inflight"
	log_clock >"$RUN_DIR/last-flush"
}
log_clear() { log_open || return 1; log_reset_locked; result=$?; log_close; return "$result"; }
log_export() {
	local file start end crc from to skip count chunk clear
	log_open || return 1
	log_flush_locked || true
	# Reap a temporary export left by SIGKILL. The journal lock excludes another
	# exporter; there is never more than one raw packet in RAM.
	rm -f "$RUN_DIR"/export.*
	# An empty/status packet proves no bytes beyond the last actual ack.
	from="$ACK"; to="$ACK"
	chunk="$RUN_DIR/export.$$"; : >"$chunk"
	trap 'rm -f "$chunk"; log_unlock' EXIT
	# One immutable chunk per packet, never more than 16 KiB before base64.
	for file in "$LOG_DIR"/part."$JOURNAL".*; do
		[ -f "$file" ] || continue
		log_part_fields "$file" || continue
		[ "$end" -gt "$ACK" ] || continue
		from="$start"; [ "$ACK" -le "$start" ] || from="$ACK"
		skip=$((from - start)); count=$((end - from)); [ "$count" -le "$LOG_BATCH_LIMIT" ] || count="$LOG_BATCH_LIMIT"
		# dd streams a fixed-size segment; no large tail ring or shell string.
		dd if="$file" bs=1 skip="$skip" count="$count" 2>/dev/null >"$chunk" || { rm -f "$chunk"; log_close; return 1; }
		to=$((from + $(wc -c <"$chunk"))); break
	done
	clear="$CLEAR"; [ "$clear" != - ] || clear=''
	printf '{"journal_id":"%s","from":%s,"to":%s,"clear_ack":"%s","data":"' "$JOURNAL" "$from" "$to" "$clear"
	base64 <"$chunk" | tr -d '\r\n'; printf '"}\n'
	rm -f "$chunk"
	log_close
}
log_ack() {
	local journal offset clear
	journal="$1"; offset="$2"; clear="${3:-}"
	case "$journal$clear" in ''|*[!a-zA-Z0-9._:-]*) return 1 ;; esac
	case "$offset" in ''|*[!0-9]*) return 1 ;; esac
	[ "${#journal}" -le 80 ] && [ "${#clear}" -le 80 ] && [ "${#offset}" -le 16 ] || return 1
	log_open || return 1
	if [ -n "$clear" ] && [ "$CLEAR" != "$clear" ]; then
		log_reset_locked "$clear" || { log_close; return 1; }
	elif [ "$JOURNAL" = "$journal" ]; then
		if [ "$offset" -gt "$TOTAL" ] || [ "$offset" -lt "$ACK" ]; then log_close; return 1; fi
		ACK="$offset"
		# Checkpoint with the next periodic flush. A crash only replays confirmed
		# bytes; the existing Hub journal-ID/offset protocol suppresses duplicates.
		printf '%s %s\n' "$JOURNAL" "$ACK" >"$RUN_DIR/ack.new" && mv -f "$RUN_DIR/ack.new" "$RUN_DIR/ack"
	fi
	log_close
}
log_flush() { log_open || return 1; log_flush_locked "${1:-}"; result=$?; log_close; return "$result"; }
log_status() {
	log_open || return 1
	printf 'storage=%s\npersistent=%s\nruntime=%s\nlimit_bytes=%s\nmax_parts=%s\nbuffer_limit_bytes=%s\nflush_seconds=%s\njournal_id=%s\ncommitted_bytes=%s\nack_bytes=%s\npending_bytes=%s\n' "$LOG_DIR" "$PERSISTENT" "$RUN_DIR" "$LOG_LIMIT" "$LOG_PART_LIMIT" "$LOG_BUFFER_LIMIT" "$LOG_FLUSH_SECONDS" "$JOURNAL" "$TOTAL" "$ACK" "$(wc -c <"$RUN_DIR/pending")"
	log_close
}
log_migrate() {
	local legacy name free mount fs flags old_id old_total old_ack ack_id previous current base file bytes index size crc target token
	legacy=/tmp/owrt-remote-logs
	[ -d "$legacy" ] || return 0
	[ -f "$legacy/current.log" ] || [ -f "$legacy/previous.log" ] || return 0
	log_open || return 1
	read -r mount fs flags <<EOF
$(log_mount "$legacy")
EOF
	if [ "$fs" != tmpfs ]; then log_close; return 0; fi
	if [ "$PERSISTENT" != 1 ]; then
		# Never leave the old 2 MiB RAM journal alongside a 64 KiB fallback. Keep
		# one bounded tail, with an explicit warning about the discarded history.
		# Legacy bytes were already redacted. Copy a bounded tail as a stream,
		# avoiding a slow per-line replay or a second long-lived RAM archive.
		free=$((LOG_BUFFER_LIMIT - 1024 - $(wc -c <"$RUN_DIR/pending")))
		[ "$free" -le 8192 ] || free=8192
		if [ "$free" -gt 0 ]; then
			{ cat "$legacy/previous.log" "$legacy/current.log" 2>/dev/null; } | tail -c "$free" >>"$RUN_DIR/pending"
		fi
		log_warn_locked legacy-warning 'Flash недоступна: от старого RAM-журнала сохранён только ограниченный хвост; остальная история отброшена.'
	else
		# Called with the old collector stopped. Retain its journal identity and
		# byte cursor, so undelivered history resumes without duplicating Hub data.
		free="$(df -Pk "$LOG_DIR" | awk 'NR==2 {print $4}')"
		case "$free" in ''|*[!0-9]*) free=0 ;; esac
		if [ "$free" -lt 6144 ]; then log_close; printf 'Remote: legacy archive retained in RAM; migration needs 6 MiB free Flash.\n' >&2; return 1; fi
		old_id="$(cat "$legacy/journal-id" 2>/dev/null)"
		case "$old_id" in ''|*[!a-zA-Z0-9._:-]*) old_id="$JOURNAL" ;; esac
		old_total="$(cat "$legacy/total" 2>/dev/null)"
		previous=0; current=0
		[ ! -f "$legacy/previous.log" ] || previous="$(wc -c <"$legacy/previous.log")"
		[ ! -f "$legacy/current.log" ] || current="$(wc -c <"$legacy/current.log")"
		bytes=$((previous + current))
		if [ "$bytes" -gt "$LOG_LIMIT" ]; then log_close; printf 'Remote: legacy journal exceeds 2 MiB; export it before updating.\n' >&2; return 1; fi
		case "$old_total" in ''|*[!0-9]*) old_total="$bytes" ;; esac
		[ "$old_total" -ge "$bytes" ] || old_total="$bytes"
		base=$((old_total - bytes)); old_ack=0; ack_id=''
		if [ -f "$legacy/sync-ack" ]; then read -r ack_id old_ack <"$legacy/sync-ack"; fi
		case "$old_ack" in ''|*[!0-9]*) old_ack=0 ;; esac
		[ "$ack_id" = "$old_id" ] && [ "$old_ack" -le "$old_total" ] || old_ack=0
		# Do not replace a nonempty new journal (e.g. after a failed retry).
		if [ "$TOTAL" -gt 0 ] && [ "$JOURNAL" != "$old_id" ]; then log_close; printf 'Remote: legacy journal conflict; no history was removed.\n' >&2; return 1; fi
		JOURNAL="$old_id"; TOTAL="$base"; ACK="$old_ack"
		token="$(cat "$legacy/sync-clear-ack" 2>/dev/null)"
		case "$token" in ''|*[!a-zA-Z0-9._:-]*) CLEAR='-' ;; *) CLEAR="$token" ;; esac
		# A staging directory on the same Flash filesystem keeps restart recovery
		# from importing only half a legacy journal after power loss.
		mkdir -p "$LOG_DIR/migrating" || { log_close; return 1; }
		for file in "$legacy/previous.log" "$legacy/current.log"; do
			[ -f "$file" ] || continue
			size="$(wc -c <"$file")"; index=0
			while [ "$size" -gt 0 ]; do
				dd if="$file" bs=16384 skip="$index" count=1 2>/dev/null >"$LOG_DIR/migrating/chunk.new" || { log_close; return 1; }
				bytes="$(wc -c <"$LOG_DIR/migrating/chunk.new")"
				[ "$bytes" -gt 0 ] || { log_close; return 1; }
				crc="$(log_checksum "$LOG_DIR/migrating/chunk.new")" || { log_close; return 1; }
				target="$(printf 'part.%s.%018d.%018d.%s' "$JOURNAL" "$TOTAL" "$((TOTAL + bytes))" "$crc")"
				mv -f "$LOG_DIR/migrating/chunk.new" "$LOG_DIR/migrating/$target" || { log_close; return 1; }
				TOTAL=$((TOTAL + bytes)); size=$((size - bytes)); index=$((index + 1))
			done
		done
		# Commit a recovery manifest before publishing any fragments. On reboot,
		# even when the old /tmp is gone, the entire staged queue can be finished.
		printf '%s %s %s %s\n' "$JOURNAL" "$ACK" "$TOTAL" "$CLEAR" >"$LOG_DIR/migrating/state.new" &&
			mv -f "$LOG_DIR/migrating/state.new" "$LOG_DIR/migrating/state" || { log_close; return 1; }
		sync -f "$LOG_DIR" 2>/dev/null || sync
		log_finish_migration_locked || { log_close; return 1; }
		rm -f "$RUN_DIR/ack"
		sync -f "$LOG_DIR" 2>/dev/null || sync
	fi
	rm -f "$legacy/current.log" "$legacy/previous.log" "$legacy/age" "$legacy/journal-id" "$legacy/total" "$legacy/sync-ack" "$legacy/sync-clear-ack" "$legacy/facts" "$legacy/facts.new" "$legacy/heartbeat-ok"
	log_close
}
log_finish_migration_locked() {
	local file
	[ -s "$LOG_DIR/migrating/state" ] || return 0
	for file in "$LOG_DIR"/migrating/part.*; do [ ! -f "$file" ] || mv -f "$file" "$LOG_DIR/" || return 1; done
	mv -f "$LOG_DIR/migrating/state" "$LOG_DIR/state" || return 1
	sync -f "$LOG_DIR" 2>/dev/null || sync
	rmdir "$LOG_DIR/migrating" 2>/dev/null || true
	rm -f "$RUN_DIR/recovered" "$RUN_DIR/recovered-v2" "$RUN_DIR/ack"
}
log_sync_loop() {
	while :; do
		/usr/sbin/owrt-remote logs-sync >/dev/null 2>&1
		sleep 3
	done
}
log_collect() {
	local owner
	log_open || return 1
	log_close
	if ! mkdir "$RUN_DIR/collector-lock" 2>/dev/null; then
		owner="$(cat "$RUN_DIR/collector-lock/pid" 2>/dev/null)"
		case "$owner" in ''|*[!0-9]*) ;; *) kill -0 "$owner" 2>/dev/null && return 0 ;; esac
		rm -f "$RUN_DIR/collector-lock/pid"; rmdir "$RUN_DIR/collector-lock" 2>/dev/null || return 1
		mkdir "$RUN_DIR/collector-lock" || return 1
	fi
	log_self_pid >"$RUN_DIR/collector-lock/pid"
	rm -f "$RUN_DIR"/stream.*
	fifo="$RUN_DIR/stream.$$"; reader=''; writer=''; timer=''
	trap 'trap - EXIT; for child in "$reader" "$writer" "$timer"; do [ -z "$child" ] || kill "$child" 2>/dev/null || true; done; wait; rm -f "$fifo" "$RUN_DIR/collector-lock/pid"; rmdir "$RUN_DIR/collector-lock" 2>/dev/null || true; (log_flush force) || true' EXIT
	trap 'exit 0' INT TERM
	mkfifo "$fifo" || return 1
	logread -f -e 'owrt-remote\(\[[0-9][0-9]*\]\)\{0,1\}:' >"$fifo" 2>/dev/null & reader=$!
	while IFS= read -r line; do printf '%s\n' "$line" | log_append; done <"$fifo" & writer=$!
	while :; do
		# Lock-operation traps must not replace the collector's child/FIFO cleanup.
		(log_flush) || true
		sleep 5 & timer=$!
		wait "$timer" || return 0
		kill -0 "$reader" 2>/dev/null && kill -0 "$writer" 2>/dev/null || return 1
	done
}

case "${1:-}" in
	check) log_open && log_close ;;
	status) log_status ;;
	migrate) log_migrate ;;
	flush) log_flush "${2:-}" ;;
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
	*) printf 'Usage: logs.sh collect|read [live]|clear|append|event MESSAGE|status|flush [force]\n' >&2; exit 2 ;;
esac
