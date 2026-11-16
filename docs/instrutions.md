# Phase 0

## Ingesting data

- Download the dataset from 'data\source\data.txt'
- Create a GCP Compute Engine VM, then run SQL Server in Docker: `mcr.microsoft.com/mssql/server:2022-latest`
- Spin up the Legacy MSSQL Server
```
docker run -e "ACCEPT_EULA=Y" -e "MSSQL_SA_PASSWORD=FdeEnterprisePass123!" -p 1433:1433 --name legacy-mssql -d mcr.microsoft.com/mssql/server:2022-latest

# or multi-line in windows 
docker run -e "ACCEPT_EULA=Y" -e "MSSQL_SA_PASSWORD=FdeEnterprisePass123!" ^
   -p 1433:1433 --name legacy-mssql ^
   -d mcr.microsoft.com/mssql/server:2022-latest

# or multi-line unix
docker run -e "ACCEPT_EULA=Y" -e "MSSQL_SA_PASSWORD=FdeEnterprisePass123!" \
   -p 1433:1433 --name legacy-mssql \
   -d mcr.microsoft.com/mssql/server:2022-latest

# or multi-line with a volume on the GCP SQL VM
docker run -v mssql_data:/var/opt/mssql \
  -e "ACCEPT_EULA=Y" \
  -e "MSSQL_SA_PASSWORD=FdeEnterprisePass123!" \
  -p 1433:1433 \
  --name legacy-mssql \
  -d mcr.microsoft.com/mssql/server:2022-latest

```
- install the req > pip install -r requirements.txt

- python scripts\ingest_legacy_data.py

## Connecting to the data 

We need to query the database
- Download : https://github.com/microsoft/azuredatastudio
   - https://learn.microsoft.com/en-us/previous-versions/azure-data-studio/download-azure-data-studio?tabs=win-install%2Cwin-user-install%2Credhat-install%2Cwindows-uninstall%2Credhat-uninstall
- The recommendation is to use VS code extension : "SQL Server (mssql)" by microsoft
   - Click on icon that looks like server or refrigarator, not the one with cylinder 
   - Add connection
   - Fill the below :
   ```
   Profile Name: legacy-mssql
   Server name*: localhost
   Port: 1433
   Trust server certificate: 🟩 Check this box / turn it ON (Crucial for Docker)
   Authentication type*: SQL Login
   User name*: sa
   Password*: FdeEnterprisePass123!
   Save Password: 🟩 Check this box
   Database name: Type master (or leave it on "Select a database")
   Encrypt: ⚠️ Change this from Mandatory to Optional (or False)
   ```

- CTRL + N
- SQL
- SELECT COUNT(*) AS total_rows FROM dbo.TBL_SC_FLEET_HIST_RAW;

## instruction for the GCP SQL VM
The raw-data database is already on Compute Engine, not EC2.

- VM name: `instance-20261102-102739`
- Zone: `us-central1-c`
- Private IP: `10.128.0.2`
- Machine: Ubuntu, 30 GB disk
- Container name: `legacy-mssql`

SSH from your PC. There is no `.pem` file and no AWS security group.

```
gcloud compute ssh instance-20261102-102739 --zone=us-central1-c --project=project-a8f5b200-d874-4d72-b89
```

Install Docker on that VM, then start SQL Server with a volume:

```
docker run -v mssql_data:/var/opt/mssql \
  -e "ACCEPT_EULA=Y" \
  -e "MSSQL_SA_PASSWORD=FdeEnterprisePass123!" \
  -p 1433:1433 \
  --name legacy-mssql \
  -d mcr.microsoft.com/mssql/server:2022-latest
```

Use `localhost` on port 1433 only when the SQL client is on this same VM. From the Streamlit VM, set `SQL_SERVER_HOST=10.128.0.2`. Both VMs are on the default VPC, and the default internal firewall already allows that path. Do not point the app at a public IP.

# Phase 1

## SOP Ingestion

- https://app.pinecone.io/ > get api key
- Use it in .env
- run python scripts\ingest_sop_pinecone.py

# Phase 2

## Data Security

- Click on file : scripts\setup_security_and_view.sql
- VS-code will show you Start button directly on top else run like we were running the commands previously.
- Once done, create a new connection now with Agent-Profile
```
* Profile Name: agent-fde-ro
* Connection Group: Leave it on <Default>
* Input type: Select Parameters (Do not click "Load from Connection String", "Browse Azure", or "Browse Fabric")
* Server name*: localhost
* Port: 1433
* Trust server certificate: 🟩 Check this box / Turn it ON
* Authentication type*: SQL Login
* User name*: USR_FDE_RO
* Password*: AgentPassword2026!
* Save Password: 🟩 Check this box / Turn it ON
* Database name: Type master (or click "Select a database" and select master)
* Encrypt: Change this from Mandatory to Optional (or False)
```

Connect and test below commands :
```
-- TEST 1: This SHOULD work perfectly (Access to clean view)
SELECT TOP 5 * FROM FDE_VIEWS.VW_ACTIVE_FLEET;

-- TEST 2: This SHOULD fail instantly (Access to raw legacy table is DENIED)
SELECT TOP 5 * FROM dbo.TBL_SC_FLEET_HIST_RAW;
```

## Phase 3

run src\agent_tools.py
run src\orchestrator.py

Step-1 : Pass below in the dispatcher >
- Question-1 (The "Domino Effect" Test)
```
Find any active shipments near Los Angeles (Latitude ~33.8, Longitude ~-118.1). Check the local weather there, and tell me if the current cargo temperature violates the SOP for fresh perishables.
```

- Question-2 (The "Restraint" Test (No-Tool Routing))
```
I'm a new dispatcher on the night shift. Can you quickly explain the difference between a Tier 1 and Tier 2 escalation?
```

### When a tool fails

`src/agent_tools.py` limits each lookup. A failed call returns an `error:` string, the chat continues with the tools that worked, and the failure is written to `FDE_VIEWS.AgentAuditLog` with `tool_meta.status` set to `error`.

| Lookup | Timeout | Retry | Stop calling it |
| --- | --- | --- | --- |
| `query_telemetry_db` | 8 seconds to connect and 8 seconds to run the query | None. A bad `SELECT` is not tried again. | Twice per question. After 3 connection failures in 60 seconds, skip it for 30 seconds. |
| `fetch_corridor_conditions` | 6 seconds | Once, after 0.4 seconds, only for a timeout, a dropped connection, or HTTP 500. | Twice per question. Same 3-failure skip. |
| `search_compliance_sop` | 8 seconds | None | Twice per question. Same 3-failure skip. |

Other limits:

- SQL must be one `SELECT`. A second statement, or `INSERT`, `UPDATE`, `DELETE`, `DROP`, or `INTO`, is refused. Latitude must be between -90 and 90, and longitude between -180 and 180. An empty or 500-character rule-book question is refused.
- The graph in `src/orchestrator.py` stops after 8 steps (`RECURSION_LIMIT`). The screen says the lookup was stopped and asks for one shipment or one location.
- If shipment records were not read and the draft still states a cargo temperature, the draft is not shown. The screen tells the dispatcher not to treat the cargo as safe, to contact the driver if a breach is possible, and to escalate a High Risk shipment with delay probability above 0.65 to the Tier 2 Logistics Manager.
- Chat memory is `MemorySaver` in the Streamlit process. It keeps the conversation for that process. It does not resume a call that died halfway through.

## Phase 4
Go to VS-code > CTRL + N > 'click on plain-text' and use 'sql'

Select the su user and write below :

CREATE TABLE FDE_VIEWS.AgentAuditLog (
    LogID INT IDENTITY(1,1) PRIMARY KEY,
    Timestamp DATETIME DEFAULT GETDATE(),
    SessionID VARCHAR(50),
    NodeExecuted VARCHAR(50),
    ToolName VARCHAR(100),
    Content NVARCHAR(MAX) -- NVARCHAR to safely handle JSON strings and large LLM outputs
);

-- Grant the agent user permission to write only to this specific table
```
GRANT INSERT ON FDE_VIEWS.AgentAuditLog TO USR_FDE_RO;
```

## Phase 5
streamlit run src\ui.py

## Phase 6
The Streamlit app runs on the GCP VM `fde-ai-cold-chain-logistics` in `asia-south1-c`. The Linux user is `bikimj03`. The app directory is `/home/bikimj03/cold-chain-logistics-FDE-Project`. The public page is `http://8.234.82.34:8501`.

A push to `main` runs `.github/workflows/deploy.yml`. That workflow SSHs to this VM through Identity-Aware Proxy, pulls `main`, and restarts the `streamlit` service. It does not deploy the SQL VM and it does not copy `.env`.

One-time setup on a new VM, as `bikimj03`:

```
sudo apt update && sudo apt install -y python3-pip python3-venv git curl
cd /home/bikimj03
git clone https://github.com/bikramCB/cold-chain-logistics-FDE-Project.git
cd cold-chain-logistics-FDE-Project
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create `.env` in that directory on the VM. Set `SQL_SERVER_HOST=10.128.0.2`. Do not commit `.env`, and do not set `HF_HOME` to a Windows path.

1. Add the Microsoft GPG repository key
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | sudo gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg

2. Add the Microsoft SQL Server Ubuntu repository
curl -fsSL https://packages.microsoft.com/config/ubuntu/$(lsb_release -rs)/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list

3. Update apt and install the driver (Accepting the EULA)
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18

4. Install the unixODBC development headers
sudo apt-get install -y unixodbc-dev

5. Create the systemd service file
```
sudo nano /etc/systemd/system/streamlit.service
```

6. Paste the configuration
```
[Unit]
Description=Streamlit Cold-Chain Dispatch Console
After=network.target

[Service]
User=bikimj03
WorkingDirectory=/home/bikimj03/cold-chain-logistics-FDE-Project
ExecStart=/home/bikimj03/cold-chain-logistics-FDE-Project/venv/bin/streamlit run src/ui.py --server.port=8501 --server.address=0.0.0.0
Restart=always

[Install]
WantedBy=multi-user.target
```

7. Register and start the service
```
sudo systemctl daemon-reload
sudo systemctl enable streamlit
sudo systemctl start streamlit
```

# Phase 7

## Nightly grading, where to look, and the safety email

The Streamlit chat does not grade itself. Each chat turn writes rows into `FDE_VIEWS.AgentAuditLog` immediately. Once a night, Cloud Scheduler starts the Cloud Run job `eval-judge`. That job runs `evals/run_judge_job.py`, reads the last 24 hours of those rows, asks Gemini to score each final answer, and inserts one row per answer into BigQuery `eval.judge_scores`.

If a safety score is below 3, the job prints `alert=safety_below_3`. A log-based metric counts that line, and the alerting policy **Safety score below 3** emails `bikimj03@gmail.com`.

Project `project-a8f5b200-d874-4d72-b89`, region `asia-south1`. Run the `gcloud` commands from PowerShell on your PC. In PowerShell a command is one physical line. Quote any flag whose value contains a comma. The GitHub Action in `.github/workflows/deploy.yml` only pulls code onto the Streamlit VM `fde-ai-cold-chain-logistics` and restarts Streamlit. It does not build or run this job, and it does not change the SQL VM `instance-20261102-102739`. The tradeoffs for the guardrails, the SOP cache, this nightly job, and the audit cost are in [scaling.md](scaling.md) under "Current design tradeoffs".

### What each chat turn already writes

Same table and same columns as Phase 4 (`LogID`, `Timestamp`, `SessionID`, `NodeExecuted`, `ToolName`, `Content`). Extra detail is extra rows:

| NodeExecuted | What Content holds |
| --- | --- |
| `user_question` | The dispatcher’s question, stored before the answer. |
| `reasoner_final` | The dispatcher-facing answer. The night job grades this. |
| `tools` | Tool name plus the tool result, including SOP text. |
| `tool_meta` | JSON `{duration_ms, status}` with status `ok` or `error`. Tool time only. |
| `response_meta` | JSON `{response_ms}`: milliseconds from the accepted question until the answer is ready to show. |
| `usage` | JSON input and output token counts. |
| `reasoner_trace` | Model text from the step that chose a tool. |
| `sop_cache` | The cached SOP answer. |
| `cache_meta` | JSON `{age_seconds}` for that cache hit. |
| `input_guard` / `output_guard` | A guardrail blocked the turn. |

A turn ends at `reasoner_final`. Rows earlier in that `SessionID` belong to that answer.

### 1. APIs and the two service accounts

```
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com bigquery.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com aiplatform.googleapis.com logging.googleapis.com --project=project-a8f5b200-d874-4d72-b89
```

```
gcloud iam service-accounts create eval-judge --project=project-a8f5b200-d874-4d72-b89
gcloud iam service-accounts create eval-scheduler --project=project-a8f5b200-d874-4d72-b89
```

`eval-judge` runs the container. `eval-scheduler` is only allowed to start that job.

```
gcloud projects add-iam-policy-binding project-a8f5b200-d874-4d72-b89 --member=serviceAccount:eval-judge@project-a8f5b200-d874-4d72-b89.iam.gserviceaccount.com --role=roles/aiplatform.user
gcloud projects add-iam-policy-binding project-a8f5b200-d874-4d72-b89 --member=serviceAccount:eval-judge@project-a8f5b200-d874-4d72-b89.iam.gserviceaccount.com --role=roles/logging.logWriter
gcloud projects add-iam-policy-binding project-a8f5b200-d874-4d72-b89 --member=serviceAccount:eval-judge@project-a8f5b200-d874-4d72-b89.iam.gserviceaccount.com --role=roles/bigquery.jobUser
gcloud projects add-iam-policy-binding project-a8f5b200-d874-4d72-b89 --member=serviceAccount:eval-judge@project-a8f5b200-d874-4d72-b89.iam.gserviceaccount.com --role=roles/bigquery.dataEditor
```

Cloud Build uses the default compute account. It needs the builder role or the image upload fails with 403:

```
gcloud projects add-iam-policy-binding project-a8f5b200-d874-4d72-b89 --member=serviceAccount:62474257474-compute@developer.gserviceaccount.com --role=roles/cloudbuild.builds.builder
```

### 2. A read-only SQL login, stored as secrets

The job reaches SQL at the private address `10.128.0.2:1433` through the default VPC. Create login `USR_FDE_AUDIT` on the SQL VM (`instance-20261102-102739`, zone `us-central1-c`, container `legacy-mssql`) with `SELECT` on `FDE_VIEWS.AgentAuditLog` only. Put the username, password, and host in Secret Manager. Do not commit them.

```
gcloud secrets create sql-eval-user --replication-policy=automatic --project=project-a8f5b200-d874-4d72-b89
gcloud secrets create sql-eval-password --replication-policy=automatic --project=project-a8f5b200-d874-4d72-b89
gcloud secrets create sql-server-host --replication-policy=automatic --project=project-a8f5b200-d874-4d72-b89
```

Add a version to each secret (`USR_FDE_AUDIT`, the audit password, and `10.128.0.2`), then:

```
gcloud secrets add-iam-policy-binding sql-eval-user --member=serviceAccount:eval-judge@project-a8f5b200-d874-4d72-b89.iam.gserviceaccount.com --role=roles/secretmanager.secretAccessor --project=project-a8f5b200-d874-4d72-b89
gcloud secrets add-iam-policy-binding sql-eval-password --member=serviceAccount:eval-judge@project-a8f5b200-d874-4d72-b89.iam.gserviceaccount.com --role=roles/secretmanager.secretAccessor --project=project-a8f5b200-d874-4d72-b89
gcloud secrets add-iam-policy-binding sql-server-host --member=serviceAccount:eval-judge@project-a8f5b200-d874-4d72-b89.iam.gserviceaccount.com --role=roles/secretmanager.secretAccessor --project=project-a8f5b200-d874-4d72-b89
```

### 3. BigQuery table

```
bq mk --dataset --location=asia-south1 project-a8f5b200-d874-4d72-b89:eval
```

```sql
CREATE TABLE IF NOT EXISTS `project-a8f5b200-d874-4d72-b89.eval.judge_scores` (
  session_id STRING,
  audited_at TIMESTAMP,
  graded_at TIMESTAMP,
  tone INT64,
  actionability INT64,
  safety INT64,
  groundedness INT64,
  comments STRING,
  user_question STRING,
  response_text STRING,
  input_blocked BOOL,
  output_blocked BOOL,
  repeated_tool_failures INT64,
  tool_error_count INT64,
  tool_duration_ms INT64,
  response_ms INT64,
  tool_trace STRING,
  input_tokens INT64,
  output_tokens INT64,
  cost_usd FLOAT64,
  cache_hit BOOL,
  cache_age_seconds INT64,
  cache_stale BOOL,
  reasoning_trace STRING
);
```

`bq query` prints `Skipped` for this DDL. That means the statement ran. Check the shape with `bq show --schema --format=prettyjson project-a8f5b200-d874-4d72-b89:eval.judge_scores`.

Scores are 1 to 5:

- **tone**: plain language for a night-shift dispatcher.
- **actionability**: the next step gives clear SOP guidance.
- **safety**: a cargo temperature above 4.0°C is identified when the tool results show one.
- **groundedness**: a rule in the answer is supported by the retrieved SOP text.

`cost_usd` is `(input_tokens / 1e6) * 1.65 + (output_tokens / 1e6) * 9.90`. Those are the non-global Gemini 3.5 Flash prices for `asia-south1`. Leaving the two variables unset stores `0`.

`response_ms` is the customer wait on the server: from the moment the question is accepted until the answer text is ready to show, including a guardrail retry. `tool_duration_ms` is only the SQL, weather, and SOP calls inside that wait. A cached SOP reply stores `response_ms` in the audit log and does not create a new score row.

### 4. Build the image and create the job

From the project folder on your PC. `.gcloudignore` keeps `.env` out of the upload.

```
gcloud artifacts repositories create eval --repository-format=docker --location=asia-south1 --project=project-a8f5b200-d874-4d72-b89
gcloud builds submit --tag asia-south1-docker.pkg.dev/project-a8f5b200-d874-4d72-b89/eval/judge:latest --project=project-a8f5b200-d874-4d72-b89
```

```
gcloud run jobs create eval-judge --image=asia-south1-docker.pkg.dev/project-a8f5b200-d874-4d72-b89/eval/judge:latest --region=asia-south1 --service-account=eval-judge@project-a8f5b200-d874-4d72-b89.iam.gserviceaccount.com --network=default --subnet=default --vpc-egress=private-ranges-only --set-env-vars="GOOGLE_CLOUD_PROJECT=project-a8f5b200-d874-4d72-b89,GOOGLE_CLOUD_LOCATION=asia-south1,GEMINI_MODEL=gemini-3.5-flash,SQL_SERVER_PORT=1433,GEMINI_INPUT_USD_PER_MILLION=1.65,GEMINI_OUTPUT_USD_PER_MILLION=9.90" --set-secrets="SQL_EVAL_USER=sql-eval-user:latest,SQL_EVAL_PASSWORD=sql-eval-password:latest,SQL_SERVER_HOST=sql-server-host:latest" --project=project-a8f5b200-d874-4d72-b89
```

After a code change in `evals/run_judge_job.py`, submit the build again and then:

```
gcloud run jobs update eval-judge --region=asia-south1 --image=asia-south1-docker.pkg.dev/project-a8f5b200-d874-4d72-b89/eval/judge:latest --project=project-a8f5b200-d874-4d72-b89
```

### 5. Run it every night at 02:00 IST

`eval-scheduler` needs permission to start the job. The Cloud Scheduler service agent needs permission to mint a token for `eval-scheduler`.

```
gcloud run jobs add-iam-policy-binding eval-judge --region=asia-south1 --member=serviceAccount:eval-scheduler@project-a8f5b200-d874-4d72-b89.iam.gserviceaccount.com --role=roles/run.invoker --project=project-a8f5b200-d874-4d72-b89
gcloud iam service-accounts add-iam-policy-binding eval-scheduler@project-a8f5b200-d874-4d72-b89.iam.gserviceaccount.com --member=serviceAccount:service-62474257474@gcp-sa-cloudscheduler.iam.gserviceaccount.com --role=roles/iam.serviceAccountTokenCreator --project=project-a8f5b200-d874-4d72-b89
```

```
gcloud scheduler jobs create http eval-judge-nightly --location=asia-south1 --schedule="0 2 * * *" --time-zone=Asia/Kolkata --uri="https://run.googleapis.com/v2/projects/project-a8f5b200-d874-4d72-b89/locations/asia-south1/jobs/eval-judge:run" --http-method=POST --oauth-service-account-email=eval-scheduler@project-a8f5b200-d874-4d72-b89.iam.gserviceaccount.com --project=project-a8f5b200-d874-4d72-b89
```

To grade today's chats without waiting until 02:00:

```
gcloud run jobs execute eval-judge --region=asia-south1 --wait --project=project-a8f5b200-d874-4d72-b89
```

A run with no `reasoner_final` row in the last 24 hours succeeds and prints `No reasoner_final rows in the last 24 hours.` BigQuery stays unchanged. Ask one real question in the Streamlit app first, then execute again.

### 6. The email when safety is below 3

This is done in the Google Cloud console.

1. Open Logs Explorer. Set the time range to **Last 7 days**. Paste this and click **Run query**:

```text
resource.type="cloud_run_job"
resource.labels.job_name="eval-judge"
textPayload:"alert=safety_below_3"
```

An empty result is expected until some answer scores below 3. Create the metric from the filter anyway.

2. **Actions** → **Create metric**. Name it `safety_below_3`. Type **Counter**.
3. Monitoring → Alerting → **Create policy**.
4. Select metric `Cloud Run Job - logging/user/safety_below_3`. If the list is empty, turn off **Active**. Add no filter. Rolling window `5 min`, rolling window function `sum`. Leave secondary transformation off.
5. **Configure trigger**: Threshold, any time series violates, above threshold, value `0`, retest `0 sec`.
6. Notification channel: email `bikimj03@gmail.com`. Policy name: `Safety score below 3`.

The metric only counts log lines written after it was created. A score of 5 writes a BigQuery row and does not send mail. Mail goes out on the next job run whose safety score is 1 or 2.

### When to look, and where

| When | Where | What you should see |
| --- | --- | --- |
| Right after a chat | SQL `FDE_VIEWS.AgentAuditLog` for that `SessionID` | `reasoner_final`, and when the new `src/ui.py` is deployed, `usage`, `tool_meta`, `cache_meta` |
| After 02:00 IST, or after `gcloud run jobs execute` | Cloud Run → Jobs → `eval-judge` → Executions | A succeeded execution |
| Same moment | Logs Explorer, filter below | `Grading N answers` and `Wrote N rows to ...judge_scores` |
| Same moment | BigQuery `eval.judge_scores` | One new row per final answer, with `cost_usd` filled in |
| Only if safety is below 3 | Email, and the alerting policy page | Policy **Safety score below 3** opens an incident |

Job logs, any time range that covers the run:

```text
resource.type="cloud_run_job"
resource.labels.job_name="eval-judge"
```

Latest scores:

```sql
SELECT session_id, user_question, graded_at, tone, actionability, safety, groundedness,
       response_ms, input_tokens, output_tokens, cost_usd, tool_duration_ms, tool_error_count,
       cache_hit, cache_stale, comments
FROM `project-a8f5b200-d874-4d72-b89.eval.judge_scores`
ORDER BY graded_at DESC
LIMIT 20;
```

A later run inserts another row. It does not update the row already stored, so an older row can keep `cost_usd = 0` from the run that happened before the price variables were set. The policy page shows an empty chart until the first `alert=safety_below_3` line. That empty chart means no answer has scored below 3 since the metric was created.