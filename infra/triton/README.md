# Triton Inference Server for QueryMind RAG

Serves `BAAI/bge-m3` dense embeddings (1024-d, multilingual) over the
Triton V2 HTTP inference API. The backend's `services/rag/triton_client.py`
talks to whatever this service exposes.

## Layout

```
infra/triton/
├── Dockerfile                               # custom image with sentence-transformers
├── model_repository/
│   └── bge_m3/
│       ├── config.pbtxt                     # Triton model config
│       └── 1/
│           └── model.py                     # Python backend impl
└── README.md
```

## Architecture decision: Python backend, not ONNX

bge-m3 ships with a non-trivial pooling + L2-normalize forward.
Exporting to ONNX and rebuilding the tokenizer + pooler outside Triton
is ~half a day of work and brittle across HF versions. The Python
backend wraps `sentence-transformers.SentenceTransformer` directly:

  - Initialization (one cold load of bge-m3) takes ~30 s on CPU.
  - Inference is ~80–200 ms per query on Ryzen 7 5800X (CPU).
  - On a GPU host you can rebuild with CUDA torch and drop to ~10 ms.

The slowdown vs ONNX matters only at indexing time (RAG_INDEX_BATCH = 32),
where we already amortize the per-call overhead.

## First-time build

```bash
docker compose -f infra/docker-compose.dev.yml build triton
```

This builds `querymind/triton-bge-m3:local` (~3 GB — Triton base
+ torch CPU + sentence-transformers).

## Run

```bash
docker compose -f infra/docker-compose.dev.yml up -d triton
```

First start downloads bge-m3 into the `triton_cache` named volume
(~2 GB). Subsequent starts use the cache. The healthcheck has a
120 s grace period to cover the cold download.

Tail the container while it warms up:

```bash
docker logs -f qm_triton
```

You should see (last line):

```
[bge_m3] loaded model=BAAI/bge-m3 device=cpu dim=1024 max_seq_len=512
```

then Triton's own banner:

```
+-----------+---------+--------+
| Model     | Version | Status |
+-----------+---------+--------+
| bge_m3    | 1       | READY  |
+-----------+---------+--------+
```

## Verify with curl

Healthcheck:

```bash
curl http://localhost:8001/v2/health/ready
# (empty 200 OK)
```

Embed two texts:

```bash
curl -X POST http://localhost:8001/v2/models/bge_m3/infer \
  -H "Content-Type: application/json" \
  -d '{
    "inputs": [{
      "name": "TEXT",
      "shape": [2],
      "datatype": "BYTES",
      "data": ["salom dunyo", "hello world"]
    }]
  }'
```

Response shape: `outputs[0].shape == [2, 1024]`, `data` is the 2048
floats (2 × 1024).

## Wire into the backend

In `backend/.env`:

```
TRITON_URL=http://localhost:8001
TRITON_EMBED_MODEL=bge_m3
EMBEDDING_DIM=1024
```

Then trigger reindexing for an existing workspace:

```bash
curl -X POST http://localhost:8080/workspaces/<wid>/connections/<cid>/refresh \
  -H "Authorization: Bearer <jwt>"
```

The Celery `index_task` will now produce real embeddings into pgvector
instead of skipping with `TritonUnavailable`.

Confirm chunks landed:

```sql
SELECT kind, COUNT(*) FROM rag_chunks GROUP BY kind;
```

## GPU mode

Rebuild the Dockerfile with CUDA torch:

```dockerfile
# Replace the torch install line with:
RUN python3 -m pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cu121 \
    torch==2.3.1
```

Switch the base image to the CUDA-enabled one:

```dockerfile
FROM nvcr.io/nvidia/tritonserver:24.06-py3
```

Uncomment the `deploy.resources.reservations.devices` block in
`docker-compose.dev.yml`. Set `TRITON_FORCE_CPU=0` in the service
environment. `KIND_CPU` in `config.pbtxt` becomes `KIND_GPU`.

## Troubleshooting

**Healthcheck never goes green / `connection refused` on 8001.**
First start downloads ~2 GB. Watch `docker logs -f qm_triton`. If
the download is stuck, check the container has internet.

**`UNAVAILABLE: Internal: failed to load 'bge_m3'`.**
The Python backend couldn't import sentence-transformers — usually a
torch/CUDA mismatch from a custom rebuild. Run
`docker compose run --rm triton python3 -c "import sentence_transformers"`
to repro.

**Slow embeddings.**
Drop `max_seq_length` in `model.py` (defaults to 512). For schema
chunks 256 is usually plenty. Or rebuild for GPU.

**ES / vLLM share GPU.**
Don't run Triton on the GPU box if vLLM is using all VRAM. Keep
Triton on CPU; the latency is acceptable.
