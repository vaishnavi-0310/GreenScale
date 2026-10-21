# GreenScale — GCP/GKE Feasibility Verification

**Course:** DAT515 Cloud Computing  
**Team:** Group-24  
**Responsible:** vachi1522 
**Verification date:** 21 October 2026

## 1. Google Cloud Project

- Project name: GreenScale-Group24
- Project ID: `greenscale-group24`
- Cloud platform: Google Cloud Platform (GCP)
- Planned Kubernetes platform: Google Kubernetes Engine (GKE)

A dedicated Google Cloud project has been created for the GreenScale application.

## 2. Billing and Budget

- Billing account: Billing Account for Education
- Initial education credits: $50 USD
- Monthly budget: $40 USD
- Configured alerts: 50%, 75%, and 90%
- Billing account successfully linked to the GreenScale project

The three group members have separate $50 education credit allocations. The team plans to switch the project's billing account when appropriate, subject to permissions and credit eligibility.

Budget alerts provide spending notifications but do not automatically stop cloud resources.

## 3. Kubernetes Engine API

The Kubernetes Engine API was enabled and verified using Google Cloud Shell.

Command:

```bash
gcloud services list --enabled --filter="name:container.googleapis.com"
```

Result:

```text
NAME: container.googleapis.com
TITLE: Kubernetes Engine API
```

**Status:** Successfully enabled.

## 4. GKE Cluster Availability

Command:

```bash
gcloud container clusters list --project=greenscale-group24
```

The command completed successfully without listing any clusters.

**Status:** The project has no GKE clusters currently available.

No Kubernetes clusters or paid GKE workloads were created during Phase 1–2.

## 5. IAM Access

The following project-level permissions have been configured:

| Member | IAM Role |
|---|---|
|vachi1522 | Owner |
| jaani9720 | Viewer |
| mesab2118 | Viewer |

A Compute Engine default service account also exists with the Editor role.

Teammate access grants have been verified in the IAM console. Independent login verification by both teammates is pending.

Viewer access does not grant deployment permissions. Specific GKE and Kubernetes RBAC permissions will be configured during the deployment phase.

## 6. Current Limitations

- No GKE cluster has been deployed.
- Kubernetes Job creation permissions have not yet been tested.
- Kubernetes namespace and RBAC configuration are pending.
- Cloud application deployment and actual job execution are not included in this verification.

## 7. Conclusion

GreenScale's initial GCP feasibility checks are complete for project creation, education billing configuration, budget alerts, GKE API enablement, and project IAM grants.

The remaining cloud deployment work will be carried out in later phases, after GKE costs and permissions have been reviewed.
