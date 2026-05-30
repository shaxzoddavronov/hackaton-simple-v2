# Infra cheatsheet

## Start the dev stack (Postgres + Redis)
```bash
docker compose -f infra/docker-compose.dev.yml up -d
```

## ffmpeg (required for audio/video harvest sources)

Phase 17.3 added Whisper-based transcription for `.mp3`, `.mp4`,
`.m4a`, `.wav`, `.webm`, `.ogg`, `.opus`, `.mpeg`, and `.mpga` files
landed by the harvester. `faster-whisper` shells out to `ffmpeg` for
non-WAV decoding, so the binary must be on PATH wherever the Celery
worker runs.

```bash
# Linux
apt-get install -y ffmpeg

# macOS
brew install ffmpeg

# Windows (PowerShell, admin)
winget install Gyan.FFmpeg
```

Restart the Celery worker after installing — Python only resolves
`ffmpeg` against the PATH that was set at process start.

Tuning knobs (env vars, all optional):
- `WHISPER_MODEL_SIZE` — `tiny | base | small | medium | large-v3`
  (default `base`, ~140 MB multilingual).
- `WHISPER_DEVICE` — `cpu | cuda` (default `cpu`).
- `WHISPER_COMPUTE_TYPE` — `int8 | float16 | float32`
  (default `int8`; `float16` is the GPU sweet spot).

## Tesseract (required for OCR of scanned PDFs + image attachments)

Phase 20 added Tesseract OCR for `.png`, `.jpg`, `.tiff`, `.bmp`,
`.webp` files and a fallback OCR pass for `.pdf` files when pypdf
returns empty text (typical for scanned documents). The Python
binding `pytesseract` shells out to the `tesseract` binary; install
it on the host running the Celery worker.

Linux:
```bash
apt-get install -y tesseract-ocr tesseract-ocr-uzb tesseract-ocr-rus
```

macOS:
```bash
brew install tesseract tesseract-lang
```

Windows: install the [UB-Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki),
pick the "Additional language data" → Uzbek + Russian during the
installer, and add the install folder (default
`C:\Program Files\Tesseract-OCR`) to PATH. Restart the Celery
worker afterwards — Python only resolves `tesseract` against the
PATH that was set at process start.

Tuning knobs (env vars, all optional):
- `OCR_LANGS` — Tesseract `-l` argument (default `uzb+rus+eng`).
  Add more language codes joined with `+` if you have the matching
  tessdata packs installed.
- `OCR_PDF_DPI` — render resolution for the scanned-PDF fallback
  (default `200`; raise to `300` for fine print, lower to `150`
  for speed).

Without Tesseract installed the harvester still runs — image files
are skipped with a warning and PDFs degrade to pypdf-only
extraction (so scanned PDFs produce no text instead of crashing
the run).

## Run migrations against the dev Postgres
```bash
cd backend && alembic upgrade head
```

## Start vLLM on the host (recommended — needs your GPU)
```bash
vllm serve google/gemma-3-4b-it \
  --guided-decoding-backend xgrammar \
  --max-model-len 8192 \
  --port 8000
```
The OpenAI-compatible endpoint will then be reachable at `http://localhost:8000/v1`.

## Run the ephemeral test Postgres (port 55432)
```bash
docker compose -f infra/docker-compose.test.yml up -d
```

## Wipe everything (containers + named volume)
```bash
docker compose -f infra/docker-compose.dev.yml down -v
docker compose -f infra/docker-compose.test.yml down -v
```

## Per-dialect dev databases

The dev compose file ships **commented-out** service blocks for MySQL,
ClickHouse, MongoDB, and Oracle XE so the default `up -d` only starts
the metadata Postgres + Redis. To smoke-test QueryMind against a real
instance of a non-Postgres dialect:

1. Open `infra/docker-compose.dev.yml`.
2. Uncomment the service block you want.
3. Uncomment the matching named volume at the bottom of the file
   (`mysql_data`, `clickhouse_data`, `mongodb_data`, `oracle_data`).
4. `docker compose -f infra/docker-compose.dev.yml up -d <service>`.

Each container auto-loads its seed file from `infra/seed/` on first
start (empty volume), giving you the same `customers` + `sales` dataset
across dialects. To re-seed, `docker compose down -v` then `up -d`.

### MySQL 8.4
```bash
docker compose -f infra/docker-compose.dev.yml up -d mysql
# Re-seed manually (optional):
docker exec -i qm_mysql mysql -u querymind -pquerymind sales_demo \
  < infra/seed/mysql_demo.sql
```
Add-Connection form:
```
Dialect:  mysql
Host:     localhost
Port:     3306
Database: sales_demo
User:     querymind
Password: querymind
```

### ClickHouse 24.8
```bash
docker compose -f infra/docker-compose.dev.yml up -d clickhouse
# Re-seed manually (optional):
docker exec -i qm_clickhouse clickhouse-client \
  --user querymind --password querymind --multiquery \
  < infra/seed/clickhouse_demo.sql
```
Add-Connection form (HTTP interface):
```
Dialect:  clickhouse
Host:     localhost
Port:     8123
Database: sales_demo
User:     querymind
Password: querymind
```
Port 9000 is exposed as well for the native protocol if you prefer it.

### MongoDB 7
```bash
docker compose -f infra/docker-compose.dev.yml up -d mongodb
# Re-seed manually (optional):
docker exec -i qm_mongodb mongosh \
  "mongodb://querymind:querymind@localhost:27017/?authSource=admin" \
  < infra/seed/mongo_demo.js
```
Add-Connection form:
```
Dialect:  mongodb
Host:     localhost
Port:     27017
Database: sales_demo
User:     querymind
Password: querymind
Auth DB:  admin
```

### Oracle XE 21c
First start takes **~2 minutes** while Oracle initializes the database.
Tail the logs until you see `DATABASE IS READY TO USE!`:
```bash
docker compose -f infra/docker-compose.dev.yml up -d oracle
docker logs -f qm_oracle
# Re-seed manually (optional, once ready):
docker exec -i qm_oracle sqlplus -S querymind/querymind@//localhost:1521/sales_demo \
  < infra/seed/oracle_demo.sql
```
Add-Connection form:
```
Dialect:  oracle
Host:     localhost
Port:     1521
Service:  sales_demo
User:     querymind
Password: querymind
```
