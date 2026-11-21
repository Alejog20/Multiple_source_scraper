#!/usr/bin/env bash
# scripts/backup_db.sh — consistent SQLite backup, uploaded to S3.
#
# Uses `sqlite3 .backup` (not `cp`) so the copy is transactionally
# consistent even if the bot is mid-write at the moment this runs — a
# plain file copy of a live SQLite db can capture a half-written page.
#
# Usage (set via systemd timer or cron, see docs/aws-setup.md):
#   S3_BUCKET=my-backups-bucket scripts/backup_db.sh
#
# Required env:
#   S3_BUCKET   — target bucket (object key: scraper/scraper_jobs_<ts>.db)
# Optional env:
#   DB_PATH     — default /opt/scraper-bot/data/scraper_jobs.db

set -euo pipefail

DB_PATH="${DB_PATH:-/opt/scraper-bot/data/scraper_jobs.db}"

if [[ -z "${S3_BUCKET:-}" ]]; then
  echo "backup_db.sh: S3_BUCKET is not set" >&2
  exit 1
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "backup_db.sh: no database found at $DB_PATH" >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
tmp_backup="$(mktemp /tmp/scraper_jobs_backup.XXXXXX.db)"
trap 'rm -f "$tmp_backup"' EXIT

sqlite3 "$DB_PATH" ".backup '$tmp_backup'"

aws s3 cp "$tmp_backup" "s3://${S3_BUCKET}/scraper/scraper_jobs_${timestamp}.db"

echo "backup_db.sh: uploaded scraper_jobs_${timestamp}.db to s3://${S3_BUCKET}/scraper/"
