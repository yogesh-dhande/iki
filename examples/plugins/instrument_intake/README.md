# Bring qPCR runs into Iki from the instrument PC

A qPCR core runs a QuantStudio 7 Flex all day. After each run, the operator
exports the results as text to a folder on the PC beside the instrument. This
example sends each export to the core's project without anyone opening a
browser. It then reads the run's wells, Cq values and standard curves into a
record that says which file they came from.

1. A small uploader runs on the instrument PC from its task scheduler. It sends
   each new export to Iki as the core's own service account.
2. Iki checks the file's bytes and scans the file for malware. The uploader
   makes the run's record, a **qPCR run** (`QPCR-000123`), only after the scan
   finds the file clean. The record holds the file and its SHA-256.
3. When the record is made, the project's trigger, **Read each new qPCR run's
   results**, runs the import. The import reads the export and writes the
   run's facts, one row per well, and each target's standard curve onto the
   record.

Sending the same export again never makes a second record. That includes
running the uploader twice, losing its journal, renaming the file or sending it
from a second PC.

## What it reads

The import reads the text results export of QuantStudio Real-Time PCR
Software and ViiA 7 Software (**File → Export**, with **Results** selected, as
**.txt**). The export starts with `* Name = value` lines about the experiment and
instrument, followed by tab-separated sections such as `[Results]`. The import
finds each column by its heading, because the columns depend on the experiment
type and export settings. It reads standard curve, relative standard curve and
comparative Cт (ΔΔCт) runs. It takes Cq from `CT`, or from `CRT` for the
relative threshold method. A well reported as `Undetermined` has no Cq and is
marked undetermined. Each well keeps its sample, target, task, reporter,
quencher, omit flag, threshold and quantity. Each target with a standard curve
keeps its slope, y-intercept, R² and efficiency. The record also keeps the
endogenous control and reference sample from a ΔΔCт run, and any calibrations
the export reports as expired. An expired calibration also adds a warning to the
run.

The import refuses a file that isn't a results export. It also refuses an
export with a well outside A1 to P24, two results for one well and target, or a
number it can't read, and names the line. It never shortens a value to fit a
field. Before reading, the import checks that the file's SHA-256 matches the one
the record holds.

## Try the example

From an Iki checkout with its Python environment active and a matching
`iki-worker` on your path, run:

```sh
iki plugin validate examples/plugins/instrument_intake
iki plugin test examples/plugins/instrument_intake
uv run pytest -c pyproject.toml examples/plugins/instrument_intake/tests
```

`tests/import_quantstudio_results.yaml` imports three real QuantStudio 7 Flex
exports: a standard curve run, Thermo Fisher's RNase P relative standard curve
example, and a ΔΔCт run exported on Windows. It also covers a file that doesn't
match its record and a text file that isn't an export. `tests/test_quantstudio.py`
checks the values against each export's own curve: each efficiency matches its
slope (MIQE: E = 10^(−1/slope) − 1), and each unknown's quantity matches its Cq
on the curve. `tests/test_uploader.py` checks the uploader against fake
deployments. Iki's own tests (`server/tests/test_instrument_intake.py`) run the
whole path on a real deployment: upload, scan, record, trigger, worker and
import, including held files and the service account's reach. The
[data's sources](tests/data/README.md) are listed with their licence.

## Set it up

You need an Org Admin, the core project's Admin, and a person who can install a
scheduled task on the instrument PC.

1. **Install the plugin.** Copy this directory to a repository your lab owns and
   rename `acme/instrument_intake` to your lab's namespace. Pin the plugin in
   the deployment's `plugins.lock.yaml` and bind
   `your_namespace/instrument_intake.qpcr-tools` to a backend worker in
   `compute.yaml`. The import needs only Python's standard library.
2. **Make the uploader's identity.** Under **People → Service accounts**, an Org
   Admin makes a service account for the PC, such as _QuantStudio bay 3
   uploader_. On its page, they make a credential with the **read** and
   **write** scopes. Its secret is shown once.
3. **Give it one project.** The core project's Admin adds the service account as
   an **Editor** of that project and of no other. That limits the credential to
   adding files and records in that project and reading what's there. If the PC
   is stolen, revoke the credential on the service account's page.
4. **Turn the trigger on.** The project's Admin turns on **Read each new qPCR
   run's results** under the project's **Automation** tab. Each import runs as
   that Admin and is recorded as started by the trigger.
5. **Install the uploader on the PC.** Install Python 3.12 or later and
   `pip install ikisci`. Add `boto3` if you send files to an S3 bucket, or
   `google-cloud-storage` for a Cloud Storage bucket. Copy `uploader/` to the
   PC. Copy `uploader.example.toml` to `uploader.toml` and fill in the
   deployment's URL, the project and the credential's ID. Keep the secret out
   of the file. Either set it in the scheduled task's environment as
   `IKI_UPLOADER_SECRET`, or put it in a file that only the uploader's Windows
   account can read, named by `client_secret_file`.
6. **Schedule it.** In Windows Task Scheduler, run
   `python iki_uploader.py --config uploader.toml` every five minutes as that
   account, whether or not anyone is signed in. When a run is still going, the
   next one finds its lock and leaves the files to it.

The uploader writes what it did to its log. It exits with 0 when every export has
a record or is still being scanned, 1 when an export failed or was held, and 2
when the configuration is wrong. Point the task's failure alert at the core's
inbox.

## Upload or register

Every export reaches Iki in one of two ways. The record, scan and import are the
same either way.

- **Upload** (`mode = "upload"`) sends the file to Iki's own storage. Use it for
  files up to about a gigabyte, such as qPCR, plate reader and most
  chromatography exports, when the facility has no bucket of its own. This is
  the default without a `[bucket]`, and the right choice for QuantStudio
  exports, which are a few hundred kilobytes.
- **Register** (`mode = "register"`) writes the file to the facility's own
  bucket and registers it there. Iki pins the object's version, then reads it
  once to hash and scan it. The file stays in the bucket and is never copied
  into Iki. Use it for large files, such as sequencer runs, imaging stacks and
  mass spectrometry raw files. Also use it when the facility must keep its
  data in its own bucket under its own retention and access rules. Before
  using it, have an administrator approve the bucket's prefix for registering
  in place (`IKI_FILES_REGISTER_LOCATIONS`), and make sure the bucket keeps
  object versions. The PC writes with its own cloud credentials: an AWS
  profile, or Application Default Credentials.
- **Auto** (`mode = "auto"`, the default when a `[bucket]` is set) registers
  files larger than `register_over` (1 GiB by default) and uploads the rest.

A registered export is written under its SHA-256
(`<prefix><sha256>/<name>`), so the same export always goes to the same
object, and nothing else is ever written over it. If the facility deletes the
object or its version from the bucket, the record keeps its SHA-256, but nobody
can read the file in Iki any more.

## When the scan holds a file

The uploader makes no record for a file that Iki is still scanning. If the scan
takes longer than `scan_timeout`, the uploader notes which upload it was
waiting for. On its next run it waits for that upload again and doesn't send
the file a second time. If the scanner holds a file, the uploader logs the
finding, makes no record and exits with 1. A Quarantine Admin reviews the
finding through `GET /api/v1/quarantine` (see Iki's server README, "Reviewing
held files"). If they release a false positive, the uploader's next run makes
the record, and the trigger imports the file. A held file never reaches a
worker, and a run's worker gets the file only after it's been scanned.

## Adapt it for another instrument

- **Another export.** Change the record type in `types/qpcr_run.yaml` and the
  reader in `scripts/quantstudio.py`. Keep the file field, the unique
  `source_sha256` field and the SHA-256 check in `run_output`. Add real exports
  from your instrument to `tests/data`, with their provenance and licence, and
  an action test for each.
- **Another record.** `[record]` in the uploader's configuration names the type,
  its file field and its unique key field. `[record.values]` gives fixed
  values, such as the instrument's name.
- **Acting for a person.** If records must name the person who ran the
  instrument, have the person delegate to the uploader as an agent and give
  the delegation's grant ID as `delegation`. Records then name the person and
  the agent.
