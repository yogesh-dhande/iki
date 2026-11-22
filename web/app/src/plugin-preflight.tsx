/** Checks a proposed build against this deployment's records before promotion (ADR 0181). */
import { unwrap, type Schemas } from "@ikisci/sdk";
import { useState } from "react";

import { iki, problemOf } from "./api";
import { saveFile } from "./download";

type Report = Schemas["PluginPreflightOut"];

function records(count: number, adjective = ""): string {
  return `${count.toLocaleString()} ${adjective}${count === 1 ? "record" : "records"}`;
}

function reportStatus(report: Report): string {
  if (report.types.some((t) => t.findings.some((f) => f.blocking))) {
    return "Blocking findings require review before deployment.";
  }
  if (report.types.some((t) => t.sampled)) {
    return "Check incomplete. Review the sampled types before deployment.";
  }
  return report.blocking ? "Deployment requires review." : "No blocking findings.";
}

function ReportDetails({ report }: { report: Report }) {
  function download() {
    saveFile(
      new Blob([JSON.stringify(report, null, 2) + "\n"], { type: "application/json" }),
      `${report.plugin.replace("/", "-")}-preflight-${report.candidate_revision.slice(0, 12)}.json`,
    );
  }
  return (
    <>
      <dl className="summary">
        <dt>Plugin</dt>
        <dd>
          <code>{report.plugin}</code>
        </dd>
        <dt>Proposed version</dt>
        <dd>{report.version}</dd>
        <dt>Configuration revision checked</dt>
        <dd>
          <code>{report.configuration_revision}</code>
        </dd>
        <dt>Proposed manifest revision</dt>
        <dd>
          <code>{report.candidate_revision}</code>
        </dd>
      </dl>
      {report.blocking ? (
        <p>
          Resolve the findings or acknowledge the proposed manifest revision in the deployment’s
          <code> migrate.acknowledgePreflight</code> Helm value after review.
        </p>
      ) : null}
      {report.types.map((item) => (
        <div key={item.type}>
          <h3>
            <code>{item.type}</code>
          </h3>
          <p>
            {item.sampled
              ? `Sampled ${records(item.checked)}. Affected counts are lower bounds.`
              : `Checked ${records(item.checked)}.`}
            {item.findings.length === 0 ? " No findings among the records checked." : ""}
          </p>
          {item.findings.length > 0 ? (
            <ul>
              {item.findings.map((finding) => (
                <li key={JSON.stringify([finding.kind, finding.subject, finding.message])}>
                  <strong>{finding.blocking ? "Blocking" : "Informational"}</strong>
                  {" · "}
                  <code>{finding.subject}</code>
                  {" · "}
                  {finding.kind.replaceAll("_", " ")}
                  <p>{finding.message}</p>
                  <p>
                    {item.sampled ? "At least " : ""}
                    {records(finding.count, "affected ")}
                    {finding.examples.length > 0 ? (
                      <>
                        {" "}
                        · Examples{" "}
                        {finding.examples.map((ref, index) => (
                          <span key={`${ref}-${String(index)}`}>
                            {index > 0 ? ", " : ""}
                            <code>{ref}</code>
                          </span>
                        ))}
                      </>
                    ) : null}
                  </p>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ))}
      <button type="button" className="button" onClick={download}>
        Download report (JSON)
      </button>
    </>
  );
}

export function PluginPreflight({ plugin = "" }: { plugin?: string }) {
  const [file, setFile] = useState<File>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [report, setReport] = useState<Report>();

  async function run(candidate: File) {
    setReport(undefined);
    setError("");
    setBusy(true);
    try {
      const manifest: unknown = JSON.parse(await candidate.text());
      if (
        !manifest ||
        typeof manifest !== "object" ||
        !("apiVersion" in manifest) ||
        !("plugin" in manifest) ||
        !manifest.plugin ||
        typeof manifest.plugin !== "object" ||
        !("name" in manifest.plugin) ||
        typeof manifest.plugin.name !== "string"
      ) {
        setError("Choose the compiled plugin’s manifest.json.");
        return;
      }
      const name = plugin || manifest.plugin.name;
      const result = await iki.POST("/api/v1/plugins/{name}/preflight", {
        params: { path: { name } },
        body: manifest as Schemas["PluginCandidateIn"],
      });
      setReport(unwrap(result));
    } catch (err) {
      if (err instanceof SyntaxError) {
        setError("Choose a manifest.json file containing valid JSON.");
      } else {
        const problem = problemOf(err);
        setError([problem.detail, ...(problem.errors ?? []).map((e) => e.detail)].join(" "));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (file && !busy) void run(file);
        }}
      >
        <div className="field">
          <label htmlFor="preflight-manifest">Proposed plugin manifest</label>
          <input
            id="preflight-manifest"
            type="file"
            accept=".json,application/json"
            aria-describedby="preflight-help"
            disabled={busy}
            onChange={(event) => {
              setFile(event.currentTarget.files?.[0]);
              setReport(undefined);
              setError("");
            }}
          />
          <p className="field-help" id="preflight-help">
            Choose <code>manifest.json</code> from the proposed plugin’s build to check it against
            existing records. This check is read-only.
          </p>
        </div>
        <button className="button button-primary" type="submit" disabled={!file || busy}>
          Run preflight
        </button>
      </form>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <div role="status">
        {busy ? <p>Checking existing records…</p> : null}
        {report ? (
          <p>
            <strong>{reportStatus(report)}</strong>
          </p>
        ) : null}
      </div>
      {report ? <ReportDetails report={report} /> : null}
    </>
  );
}
