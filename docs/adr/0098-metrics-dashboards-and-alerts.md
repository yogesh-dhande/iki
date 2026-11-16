# 0098. Iki's metrics, dashboards and alerts

- Status: Accepted
- Date: 2026-11-16

## Context

Iki sends traces and metrics over OTLP to the operator's collector
(`otel.endpoint`). An operator needs to see whether runs are waiting, on
which tiers, whether workers are there to take them, what the machines Iki
starts cost, and whether the server and its background tasks are healthy,
and to be told when something's wrong without watching a screen. Compute
→ Overview answers the first questions for a person; dashboards and alerts
answer them for the people on call.

## Decision

**Iki's own metrics** are a contract: their names, units and attributes
change only with a note in the changelog. Prometheus names them by its
default OTLP translation (dots become underscores, a unit and `_total` are
added), as listed here.

| Metric                                   | Kind      | Attributes                        | From                   |
| ---------------------------------------- | --------- | --------------------------------- | ---------------------- |
| `iki_runs_attempts`                      | gauge     | tier, environment, state          | task runners           |
| `iki_runs_oldest_queued_seconds`         | gauge     | tier, environment                 | task runners           |
| `iki_workers`                            | gauge     | tier, state (busy, idle, offline) | task runners           |
| `iki_machines`                           | gauge     | template, provisioner, state      | task runners           |
| `iki_runs_queue_wait_seconds`            | histogram | tier, template                    | whatever leases        |
| `iki_compute_machine_time_seconds_total` | counter   | template, tier, kind (run, idle)  | whatever records costs |
| `iki_compute_cost_total`                 | counter   | template, tier, kind, currency    | whatever records costs |
| `iki_tasks_attempts_total`               | counter   | iki_task, iki_task_outcome        | task runners           |
| `iki_tasks_duration_seconds`             | histogram | iki_task, iki_task_outcome        | task runners           |

The server also exports OpenTelemetry's standard HTTP and database client
metrics (`http_server_request_duration_seconds`,
`db_client_operation_duration_seconds`).

- **The gauges are read from the database** as metrics are exported, by
  every task runner, so each reports the same numbers. Dashboards and alerts
  take their `max`, never their sum. A runner that can't read the database
  reports nothing rather than zeros, so a gap means "unknown", not "empty".
- **Counters and histograms are recorded once the change commits**, by
  whichever process made it, and are summed across pods.
- **No attribute names a project, team, person or run.** Those are
  unbounded, and cost per project already lives in Compute → Costs and its
  CSV. Templates, tiers and environments are few: an operator defines them.

**Dashboards and alerts ship as files in the chart**,
`files/dashboards/iki.json` and `files/alerts.yaml`, and the chart renders
them when asked: the dashboard as a ConfigMap labelled for a Grafana
sidecar, the alerts as a PrometheusRule for the Prometheus Operator. Both
are off by default, as the operator may run neither. Without them, the
files load as they are: Grafana imports the dashboard, and Prometheus or
anything that reads its rules files loads the alerts. The development
cluster's Grafana loads the same dashboard.

The alerts, each saying what to look at:

| Alert                       | When                                                         | Severity |
| --------------------------- | ------------------------------------------------------------ | -------- |
| `IkiRunsWaitingLong`        | a tier and environment's oldest queued run waits over 30 min | warning  |
| `IkiNoWorkersForQueuedRuns` | runs are queued on a tier with no worker online, for 15 min  | critical |
| `IkiWorkersOffline`         | a non-client tier has offline workers for 30 min             | warning  |
| `IkiServerErrors`           | over 5% of requests get a 5xx, for 10 min                    | critical |
| `IkiTasksFailing`           | a background task failed with no retries left in 30 min      | warning  |
| `IkiTaskRunnersSilent`      | no task runner has reported for 15 min                       | critical |

Client-tier workers are people's own machines, which are often off, so
they don't alert. Each alert has unit tests (`promtool test rules`, run by
`make check-chart`) that feed it series and check when it fires and what it
says.

## Consequences

- A deployment with kube-prometheus-stack turns on both with two values;
  others import two files.
- A collector or Prometheus set to keep OTLP names as they are (Prometheus'
  `NoTranslation`) needs the queries adjusted.
- Thresholds suit a typical deployment. An operator who needs others copies
  `files/alerts.yaml` and changes them, rather than templating every one.
- Alerts about the platform under Iki (nodes, Postgres, the bucket) are the
  operator's, from their own monitoring.
