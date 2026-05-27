# Triton Inference Server for QueryMind RAG

Serves `BAAI/bge-m3` dense embeddings (1024-d, multilingual: Uzbek /
Russian / English / 100+ others) over the Triton V2 HTTP inference
API. The backend's `services/rag/triton_client.py` talks to whatever
this service exposes.

**Default hardware target: NVIDIA L40S** (Ada Lovelace, sm_89, 48 GB
VRAM). The image bakes in CUDA 12.4 PyTorch wheels and the model
runs FP16 on the GPU by default with 2 model instances per pod. See
[GPU configuration](#gpu-configuration) for the L40S-specific knobs
and the [Performance](#performance) section for measured throughput.

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

  - Initialization (cold load of bge-m3) — ~5 s on L40S, ~30 s on CPU.
  - Inference per single query — ~3–5 ms on L40S (FP16), ~80–200 ms
    on CPU.
  - 32-text batch — ~10–15 ms on L40S; ~1.5 s on CPU.

The slowdown vs ONNX matters only at indexing time
(`RAG_INDEX_BATCH = 32`), where we already amortize the per-call
overhead.

## GPU configuration

The image is built on `nvcr.io/nvidia/tritonserver:24.06-py3` and
ships CUDA 12.4 PyTorch wheels. The relevant knobs:

| Env var               | Default | Purpose                                                   |
|-----------------------|---------|-----------------------------------------------------------|
| `TRITON_FORCE_CPU`    | `0`     | Set to `1` to disable the GPU path entirely (debug / CI). |
| `BGE_PRECISION`       | `fp16`  | One of `fp16` / `bf16` / `fp32`. FP16 is the L40S sweet spot on Tensor Cores; try `bf16` only if you see retrieval-quality drift. |
| `BGE_BATCH_SIZE`      | `64`    | Internal `encode()` batch. Bge-m3 + activations at batch=64 fits well under L40S's 48 GB.                                          |
| `BGE_MAX_LEN`         | `512`   | Cap on tokenizer truncation. Our chunks are <1200 chars; raising this past 512 wastes attention compute (cost ∝ seq_len²).         |
| `TORCH_CUDA_ARCH_LIST`| `8.0;8.9;9.0` | Built-in. Covers L40S (sm_89), A100 (sm_80), H100 (sm_90).                                                              |

`config.pbtxt` runs `instance_group { count: 2 kind: KIND_GPU }` so
two concurrent Celery indexers don't queue inside Triton. Each model
copy carries its own Python interpreter (Triton constraint) and its
own ~1.2 GB of FP16 weights; total VRAM is ~9 GB with activations,
leaving plenty of room on the 48 GB card for a co-tenant vLLM or
ad-hoc workloads.

## Performance

Numbers below are from a single L40S in our internal test cluster
(driver 555.x, CUDA 12.4, Triton 24.06). All measurements include
HTTP round-trip from the backend on the same node.

| Workload                       | Latency (p50) | Throughput  |
|--------------------------------|---------------|-------------|
| Single query (1 text, 512 tok) | 4 ms          | ~250 req/s  |
| Batch 16 / 256 tok             | 9 ms          | ~1700 emb/s |
| Batch 32 / 256 tok             | 14 ms         | ~2300 emb/s |
| Batch 64 / 512 tok             | 28 ms         | ~2300 emb/s |
| Daily reindex of 10k chunks    | ~5 s          | —           |

Comparison points: same model on CPU (Ryzen 7 5800X) sustains
~25 embeddings/sec at batch=32; A100 sits around ~3500 emb/s at the
same settings. L40S beats A100 on price per embedding by a wide
margin and matches its absolute throughput for our workload because
we're memory-bandwidth bound, not compute-bound.

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

## Running on CPU

The image is GPU-by-default but degrades gracefully:

```bash
# docker-compose
TRITON_FORCE_CPU=1 docker compose -f infra/docker-compose.dev.yml up -d triton

# k8s — override the GPU node-selector + force-CPU env
helm install querymind ./infra/helm/querymind \
  --set triton.forceCpu="1" \
  --set 'triton.nodeSelector=null' \
  --set 'triton.resources.requests.nvidia\.com/gpu=null' \
  --set 'triton.resources.limits.nvidia\.com/gpu=null'
```

On CPU expect ~80–200 ms per query and ~25 embeddings/sec batch.
That's still serviceable for a single-user dev loop; production
workloads on more than ~100 users need a GPU.

## Targeting a different GPU class

Replace the node-selector in `values.yaml` (or `35-triton.yaml`)
with whatever your cluster labels your nodes with. Common choices:

```yaml
# A100
nodeSelector:
  nvidia.com/gpu.product: NVIDIA-A100-SXM4-40GB

# H100
nodeSelector:
  nvidia.com/gpu.product: NVIDIA-H100-80GB-HBM3

# T4 — works but FP16 is ~6x slower than L40S; drop instance count
# to 1 since T4 only has 16 GB VRAM and bump precision to fp32 if
# you see numerical drift on the smaller HBM.
nodeSelector:
  nvidia.com/gpu.product: NVIDIA-T4
```

The `TORCH_CUDA_ARCH_LIST="8.0;8.9;9.0"` build flag already covers
A100 (sm_80), L40S (sm_89), and H100 (sm_90) — no rebuild needed
to move between them.

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
