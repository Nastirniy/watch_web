#!/usr/bin/env bash
# ============================================================================
# media-watcher.sh
# ============================================================================
# Watches the uploads directory for new media files using inotifywait and
# triggers systemd instantiated thumbnail generation services.
#
# Each new media file spawns an independent thumbnailgen@.service instance,
# giving true multiprocessed thumbnail generation that never blocks FastAPI.
#
# Requirements:
#   apt install inotify-tools
#
# Environment variables:
#   MEDIA_UPLOADS_DIR  - Absolute path to the uploads directory (required)
# ============================================================================
set -euo pipefail

UPLOADS_DIR="${MEDIA_UPLOADS_DIR:?MEDIA_UPLOADS_DIR must be set to an absolute path}"
LOG_PREFIX="[media-watcher]"

# Ensure the uploads directory path is absolute
if [[ "$UPLOADS_DIR" != /* ]]; then
    echo "ERROR: MEDIA_UPLOADS_DIR must be an absolute path, got: $UPLOADS_DIR" >&2
    exit 1
fi

# Image and video extensions that support thumbnails
THUMB_EXTENSIONS="jpg|jpeg|png|gif|webp|bmp|tiff|mp4|avi|mkv|mov|wmv|flv|webm"

log_info()  { echo "$(date '+%Y-%m-%d %H:%M:%S') ${LOG_PREFIX} INFO:  $*"; }
log_warn()  { echo "$(date '+%Y-%m-%d %H:%M:%S') ${LOG_PREFIX} WARN:  $*" >&2; }
log_error() { echo "$(date '+%Y-%m-%d %H:%M:%S') ${LOG_PREFIX} ERROR: $*" >&2; }

# -------------------------------------------------------------------
# Pre-flight checks
# -------------------------------------------------------------------
if ! command -v inotifywait &>/dev/null; then
    log_error "inotifywait not found. Install: apt install inotify-tools"
    exit 1
fi

if ! command -v systemd-escape &>/dev/null; then
    log_error "systemd-escape not found. Is systemd installed?"
    exit 1
fi

if [[ ! -d "$UPLOADS_DIR" ]]; then
    log_warn "Uploads directory not found, creating: $UPLOADS_DIR"
    mkdir -p "$UPLOADS_DIR"
fi

log_info "Starting media file watcher"
log_info "Watching directory: $(realpath "$UPLOADS_DIR")"
log_info "Thumbnail extensions: $THUMB_EXTENSIONS"

# -------------------------------------------------------------------
# Main watch loop
# -------------------------------------------------------------------
# Events:
#   moved_to    -- triggered by os.rename() (same filesystem move)
#   close_write -- triggered by shutil.copy2() (cross-filesystem copy)
#
# Format: full path to the file (%w = watched dir, %f = filename)
# -------------------------------------------------------------------
inotifywait \
    --monitor \
    --recursive \
    --format '%w%f' \
    --event moved_to \
    --event close_write \
    "$UPLOADS_DIR" | while IFS= read -r filepath; do

    # Extract filename
    filename=$(basename "$filepath")

    # Skip hidden files
    [[ "$filename" == .* ]] && continue

    # Skip thumbnail files (they contain _thumb_ in the name)
    [[ "$filename" == *_thumb_* ]] && continue

    # Skip temp files (created briefly during upload before rename)
    [[ "$filename" == temp_* ]] && continue

    # Extract extension (lowercase)
    ext="${filename##*.}"
    ext="${ext,,}"

    # Only process files with supported thumbnail extensions
    if [[ ! "$ext" =~ ^($THUMB_EXTENSIONS)$ ]]; then
        continue
    fi

    log_info "New media file detected: $filepath"

    # Escape the file path for use as a systemd instance name
    escaped=$(systemd-escape -- "$filepath")

    # Start thumbnail generation service (non-blocking)
    # --no-block: returns immediately, does not wait for completion
    # Each instance runs in its own process via thumbnailgen@.service
    service_name="thumbnailgen@${escaped}.service"
    if output=$(systemctl start --no-block "$service_name" 2>&1); then
        log_info "Triggered $service_name"
    else
        log_error "Failed to trigger thumbnail generation for: $filename"
        log_error "systemctl error: $output"
    fi

done
