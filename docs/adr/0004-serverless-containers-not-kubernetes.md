# 0004. Serverless containers instead of Kubernetes

- **Status:** Accepted
- **Date:** 2026-10-31
- **Milestone:** M1 (GCP), M7 (Azure), M8 (AWS)

## Context

The API is one stateless container that streams responses. It must cost $0 when idle and
handle a 50k-conversations-a-day design target. Kubernetes is the default for many platform
teams, but every managed offering bills for nodes, and EKS also bills for the control plane,
whether or not traffic arrives.

## Decision

Run the same container image (one slim multi-stage image per cloud, models baked in) on each
cloud's serverless container service:

| Cloud | Service | Why |
| --- | --- | --- |
| GCP | Cloud Run, us-east4 | Scales to zero, streams HTTP, revisions for rollback |
| Azure | Container Apps, East US | Same model, revisions for rollback |
| AWS | Lambda from a container image + Web Adapter, us-east-1 | Models exceed the zip limit; the Web Adapter streams |

Deploys come from CI only; rollback shifts traffic to the last good revision or alias.

## Alternatives considered

- **GKE / AKS / EKS:** the most portable runtime and familiar to platform teams, but nodes (and
  EKS's control plane) cost money around the clock, and cluster upgrades are operational work
  this project does not need.
- **VMs:** cheap at small scale, but no scale to zero, and patching becomes the job.

## Consequences

- $0 when idle, and scaling is the provider's problem.
- Cold starts land on the first request; M5 addresses them (models loaded at startup,
  quantized e5, startup CPU boost, warm-up pings).
- Each runtime differs in limits (Lambda timeouts and response streaming, Container Apps
  revisions), which is exactly what the per-cloud adapters and ADRs in M7 and M8 document.
- No Kubernetes in the demo. The design doc explains when Kubernetes would win: many services,
  steady load, or a platform team already running clusters.
