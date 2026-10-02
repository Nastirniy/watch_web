"""Media subsystem services.

- imgproxy: signs and builds imgproxy paths (thumbnails are rendered on demand)
- urls: client-facing thumbnail URLs and readiness derived from a MediaFile row
- jobs: poster job store on media_files (claim, complete, fail, reconcile, heartbeat)
- posters: ffmpeg poster extraction, pure functions without database access
- manager: process-wide MediaManager instance built from settings
"""
