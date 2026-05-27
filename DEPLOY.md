# Deploying QueryMind AI

Production deployment story for the QueryMind stack: bare Kubernetes
manifests for inspection (`infra/k8s/`) and a parameterized Helm chart
for actual rollouts (`infra/helm/querymind/`).

---

## 1. Overview

You will need:

| Requirement              | Notes                                                                                   |
| ------------------------ | --------------------------------------------------------------------------------------- |
| Kubernetes cluster       | v1.25+ tested. Any flavor (EKS / GKE / AKS / k3s) with a default StorageClass.          |
| vLLM endpoint            | `google/gemma-3-4b-it` with xgrammar guided decoding. Needs a GPU; usually out-of-cluster. |
| Triton (optional, for RAG) | bge-m3 embeddings on the v2 HTTP inference API. Build from `infra/triton/`. RAG silently falls back to BM25 if missing. |
| Ingress controller       | nginx-ingress assumed. The chart sets nginx-specific annotations for SSE and `/api` rewrite. |
| cert-manager (optional)  | Used for the `letsencrypt-prod` ClusterIssuer referenced in the Ingress.                |
| Container images         | `querymind/backend:0.1.0` and `querymind/frontend:0.1.0`. Build and push before deploy. |

The architecture deployed is identical between bare manifests and Helm:
backend × 2 + celery-worker × 1 + celery-beat × 1 + frontend × 2 + Postgres
(pgvector) StatefulSet + Redis Deployment, fronted by a single Ingress.

---

## 2. Quickstart — bare manifests

Apply in order. Files are numerically prefixed so a sorted `kubectl apply`
already does the right thing, but the Secret and the migrations Job need
human attention.

```bash
# 1. Namespace.
kubectl apply -f infra/k8s/00-namespace.yaml

# 2. Secrets — copy the template, fill in real values, apply.
cp infra/k8s/10-secret.yaml.example infra/k8s/10-secret.yaml
# Edit infra/k8s/10-secret.yaml: replace every <replace-me>.
#   QM_MASTER_KEY:  python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
#   JWT_SECRET:     openssl rand -base64 48
kubectl apply -f infra/k8s/10-secret.yaml

# 3. Config + data plane.
kubectl apply -f infra/k8s/20-configmap.yaml
kubectl apply -f infra/k8s/30-postgres.yaml
kubectl apply -f infra/k8s/31-redis.yaml

# 4. Wait for Postgres to be Ready before migrations.
kubectl -n querymind rollout status sts/postgres

# 5. Run migrations once. The Job has ttlSecondsAfterFinished: 600.
kubectl apply -f infra/k8s/60-migrations-job.yaml
kubectl -n querymind wait --for=condition=complete job/alembic-upgrade --timeout=300s

# 6. App pods.
kubectl apply -f infra/k8s/40-backend.yaml
kubectl apply -f infra/k8s/41-celery-worker.yaml
kubectl apply -f infra/k8s/42-celery-beat.yaml
kubectl apply -f infra/k8s/50-frontend.yaml

# 7. Ingress.
kubectl apply -f infra/k8s/70-ingress.yaml

# 8. (Optional) ServiceMonitor — only if you run kube-prometheus-stack.
kubectl apply -f infra/k8s/80-servicemonitor.yaml
```

Update `querymind.example.com` in `70-ingress.yaml` to your real host and
your `CORS_ORIGINS` in `20-configmap.yaml` to match before applying.

---

## 3. Quickstart — Helm

```bash
helm install querymind infra/helm/querymind \
  --namespace querymind --create-namespace \
  --set secrets.jwtSecret="$(openssl rand -base64 48)" \
  --set secrets.qmMasterKey="$(openssl rand -base64 32)" \
  --set ingress.host=querymind.example.com \
  --set backend.env.CORS_ORIGINS="https://querymind.example.com" \
  --set frontend.publicApiBaseUrl="https://querymind.example.com/api"
```

The chart wraps the migrations Job as a `pre-install` / `pre-upgrade`
Helm hook, so a fresh install or version bump always runs `alembic
upgrade head` before the backend Deployment rolls.

Upgrade later with:

```bash
helm upgrade querymind infra/helm/querymind \
  --namespace querymind \
  --reuse-values \
  --set image.backend.tag=0.2.0 \
  --set image.frontend.tag=0.2.0
```

Useful overrides:

| Override                                  | Default                                                  | Effect                                                |
| ----------------------------------------- | -------------------------------------------------------- | ----------------------------------------------------- |
| `postgres.enabled=false`                  | `true`                                                   | BYO database. Must also set `secrets.databaseUrl`.    |
| `redis.enabled=false`                     | `true`                                                   | BYO Redis. Must also set `secrets.redisUrl`.          |
| `ingress.enabled=false`                   | `true`                                                   | No Ingress. Use `kubectl port-forward` to reach pods. |
| `monitoring.serviceMonitor.enabled=true`  | `false`                                                  | Render a ServiceMonitor for Prometheus Operator.      |
| `triton.url=""`                           | `http://triton.querymind.svc.cluster.local:8001`         | Disable RAG; retriever falls back to BM25.            |
| `worker.concurrency=8`                    | `4`                                                      | Bump celery worker concurrency for heavy RAG loads.   |

---

## 4. Secrets

Three values gate the backend's `_check_secrets` lifespan hook (see
`backend/app/main.py`). With `QM_ENVIRONMENT=production` the API refuses
to boot if any of these are the dev defaults:

* **`JWT_SECRET`** — must be ≥16 chars (we recommend ≥32 chars). Sign JWTs
  symmetrically (HS256). Generate with `openssl rand -base64 48`.
* **`QM_MASTER_KEY`** — base64-encoded 32 bytes. AES-GCM key for the
  encrypted `connection.credentials_enc` column. Generate with
  `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`.
* **`VLLM_API_KEY`** — bearer token sent to vLLM. Set to `not-needed` for
  an unauthenticated local vLLM; required when behind a gateway / NIM.

Rotation: every secret is reloaded on pod restart. `kubectl rollout
restart deploy/backend deploy/celery-worker deploy/celery-beat` after
editing the Secret. `QM_MASTER_KEY` rotation requires re-encrypting the
`connection` table — `key_version` is reserved for that path.

For Helm, the `secret.yaml` template calls `required` on `jwtSecret`
and `qmMasterKey`; the chart fails to render without them, on purpose.

---

## 5. Scaling

| Component       | Default                                | Scale how                                                         |
| --------------- | -------------------------------------- | ----------------------------------------------------------------- |
| `backend`       | 2 replicas                             | `kubectl -n querymind scale deploy backend --replicas=N`          |
| `frontend`      | 2 replicas                             | Same.                                                             |
| `celery-worker` | 1 replica, concurrency 4               | Bump `worker.concurrency` first; add replicas only for RAG indexing fan-out. |
| `celery-beat`   | 1 replica (strategy: Recreate)         | **Never run more than one.** Beat owns the daily diff schedule.   |
| `postgres`      | 1 replica (StatefulSet, 20Gi)          | v1 is single-instance. Switch to managed Postgres (or Crunchy/Cloudnative-PG) for HA. |
| `redis`         | 1 replica, ephemeral                   | v1 doesn't persist queue state — restarts retry tasks per Celery defaults. |

The backend is stateless. The worker queue lives in Redis. Adding backend
replicas costs nothing beyond CPU. SSE streams are long-lived; tune the
`proxy-read-timeout` annotation upward (already 3600s) if you see
mid-stream resets.

---

## 6. Ingress

The Ingress routes:

* `/api/<anything>` → `backend` Service `:8080`, with `/api` stripped via
  the `rewrite-target` annotation. The backend mounts its routers at
  root (`/auth`, `/chat`, `/workspaces`); the ingress rewrites so the
  frontend can keep `NEXT_PUBLIC_API_BASE_URL=https://host/api`.
* Everything else → `frontend` Service `:3000`.

SSE-critical annotations (already set):

```yaml
nginx.ingress.kubernetes.io/proxy-buffering: "off"     # required for SSE
nginx.ingress.kubernetes.io/proxy-read-timeout: "3600" # long agent runs
nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
```

The `/chat` route streams `event: session | node | final | error`
frames. Token-level streaming is explicitly out of v1 — the frontend
renders the full `UISpec` once the `final` event arrives.

---

## 7. Metrics

The backend exposes Prometheus metrics at `/metrics` via
`prometheus-fastapi-instrumentator`. `/healthz` and `/metrics` themselves
are excluded from the histograms.

Scrape target (in-cluster):

```
backend.querymind.svc.cluster.local:8080/metrics
```

If you run `kube-prometheus-stack`, apply the ServiceMonitor:

```bash
# Bare manifests:
kubectl apply -f infra/k8s/80-servicemonitor.yaml
# Helm:
helm upgrade querymind infra/helm/querymind \
  --namespace querymind --reuse-values \
  --set monitoring.serviceMonitor.enabled=true \
  --set monitoring.serviceMonitor.additionalLabels.release=prom
```

The `additionalLabels` map must include whatever label your Prometheus
Operator uses to discover ServiceMonitors (commonly `release: prom`).

---

## 8. Troubleshooting

| Symptom                                       | Diagnosis                                                                                                          |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `alembic-upgrade` Job stuck in `Pending`      | Postgres not Ready yet, or PVC binding failed. `kubectl describe pod -l job-name=alembic-upgrade`.                 |
| `alembic-upgrade` runs but Pod fails          | `DATABASE_URL` mismatch. Bare manifests: check the Secret value points at the in-cluster Service DNS, NOT `localhost:5432` (that's the docker-compose mapping). Helm synthesizes the URL automatically. |
| Backend never Ready                           | vLLM unreachable. `kubectl exec deploy/backend -- curl -fsS $VLLM_ENDPOINT/models`. The lifespan hook *warns* but doesn't crash on vLLM failure, so the pod stays Ready — until `/healthz` itself fails. |
| Backend CrashLoops with "Refusing to start"   | `_check_secrets` caught a dev-default secret with `QM_ENVIRONMENT=production`. Rotate `JWT_SECRET` / `QM_MASTER_KEY`. |
| `404` on `/api/...` from the frontend         | Ingress isn't rewriting `/api`. Verify both `nginx.ingress.kubernetes.io/rewrite-target` and `use-regex` annotations are present (`kubectl describe ingress querymind`). The backend mounts routers at root, **not** under `/api`, which mirrors the docker-compose dev setup that talks to `:8080` directly. |
| RAG silently returning weak results           | Triton offline. Retriever falls back to BM25 by design — see `services/rag/retriever.py`. Check Triton logs and `EMBEDDING_DIM` vs. the model's actual output. |
| `429 Too Many Requests` on `/chat`            | slowapi rate limit. Inspect `RATE_LIMIT_STORAGE_URL` — it must point at Redis db=1 (Celery uses db=0), or `memory://` for a single-replica setup. |
| Daily diff didn't fire                        | `celery-beat` not running (Recreate strategy + 1 replica). `kubectl logs deploy/celery-beat`. Verify `RAG_DIFF_CHECK_HOUR_UTC` is what you expect, **in UTC**. |
| SSE stream cuts off after ~60s                | nginx default timeouts. The annotations bump it to 3600s, but a cloud LB in front of nginx might re-cap. Check your LB idle timeout. |

### Anti-pattern called out by the deploy story

The dev `docker-compose.dev.yml` exposes the backend at `localhost:8080`
and the frontend's `NEXT_PUBLIC_API_BASE_URL` defaults to `http://localhost:8080`
— **no `/api` prefix**. The backend's FastAPI routers are likewise
mounted at root (`/auth`, `/chat`, `/workspaces`), not `/api/...`. The
production Ingress here adds an `/api` prefix so the frontend's
`API_BASE_URL` can be `https://host/api`, and uses the
`nginx.ingress.kubernetes.io/rewrite-target` annotation to strip it
before the request hits the backend. Drop the `use-regex` /
`rewrite-target` annotations and every API call becomes a 404 — see the
404 row above.
