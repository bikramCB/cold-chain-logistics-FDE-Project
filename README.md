# Cold-chain dispatch console

Streamlit console for cold-chain dispatch. A push to `main` deploys it to one GCP VM. The SQL Server VM and the nightly judge job are not part of this pipeline.

## Pipeline

Workflow: [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)

| | |
| --- | --- |
| Name | Deploy Streamlit to GCP |
| Runs on | Every push to `main`, or **Actions → Deploy Streamlit to GCP → Run workflow** |
| Target | Compute Engine VM `fde-ai-cold-chain-logistics`, zone `asia-south1-c` |
| Project | `project-a8f5b200-d874-4d72-b89` |
| App path on the VM | `/home/bikimj03/cold-chain-logistics-FDE-Project` |
| Service | systemd unit `streamlit`, port 8501 |

GitHub authenticates to Google with Workload Identity Federation. No SSH private key is stored in GitHub. The job then opens an Identity-Aware Proxy SSH session as `bikimj03` and does four things:

1. Refuses to continue if `.env` or `venv/bin/streamlit` is missing.
2. Fast-forwards `main` from this repository. Local commits on the VM make the job fail.
3. Runs `pip install -r requirements.txt` only when that file changed.
4. Restarts `streamlit` and waits until `http://127.0.0.1:8501/_stcore/health` returns ok.

A second deploy waits until the one already running has finished.

## Repository secrets

Settings → Secrets and variables → Actions → Repository secrets. Environment secrets are not read by this workflow.

| Secret | Purpose |
| --- | --- |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | Workload Identity provider that accepts tokens from this repo on `main` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Service account the job impersonates, `github-deploy@project-a8f5b200-d874-4d72-b89.iam.gserviceaccount.com` |

The provider only accepts tokens whose repository is `bikramCB/cold-chain-logistics-FDE-Project` and whose ref is `refs/heads/main`.

## What the VM must already have

- The repo cloned on `main`, with a clean working tree.
- A virtualenv at `venv/` and the ODBC Driver 18 for SQL Server.
- `.env` in the app directory. It is gitignored. The pipeline never writes it. `SQL_SERVER_HOST` must be an address this VM can reach. The SQL VM's private address is `10.128.0.2`.
- Passwordless `sudo` for `systemctl`, so the job can restart the service.
- The `streamlit` network tag and firewall rule `allow-ssh-iap-streamlit`, which allows TCP 22 only from Google's Identity-Aware Proxy range `35.235.240.0/20`.

## When a tool fails

The console limits a bad SQL, weather, or rule-book call instead of waiting on it. The full list is in [docs/instrutions.md](docs/instrutions.md) under Phase 3.

- Each tool runs at most twice per question.
- SQL and the rule book time out at 8 seconds. Weather times out at 6 seconds and is tried once more.
- After three connection failures in a minute, that service is skipped for 30 seconds.
- The agent stops after 8 steps instead of calling tools forever.
- If shipment records cannot be read, the screen does not guess a cargo temperature. It tells the dispatcher to contact the driver and to escalate a High Risk shipment with delay probability above 0.65.

## What this pipeline does not deploy

- The SQL Server VM `instance-20261102-102739` in `us-central1-c`.
- The Cloud Run job `eval-judge`, Cloud Scheduler, or BigQuery.
- Secrets, `.env`, or the Pinecone index.

## More detail

- Setup of the two VMs, the database, and the nightly judge: [docs/instrutions.md](docs/instrutions.md)
- How to scale past this single VM, and when not to: [docs/scaling.md](docs/scaling.md)
