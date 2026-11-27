# Automatic calibration impact tracing

Use this recipe if your facility opens an impact assessment whenever an engineer
sets an instrument's **Calibration** state to **Failed**. Each new case links one
signed out-of-tolerance check to a measured result's first use in its suspect
window: a result has one case for each failure, whether traced or opened by
Equipment QA for a result no use was recorded for. The service history, use
records and QA dispositions all belong to Equipment.
Engineers can also run **Trace failed calibrations** directly from its Service
schedule tab.

## Adapt the policy

Copy this directory into your lab's plugin repository. Set `name` in
`plugin.yaml` to a name in your lab's namespace, for example
`my_lab/calibration_tracing`. Keep the qualified `iki/equipment` types.
The trigger runs an action declared in the same plugin. Its worker and tests
are included here so you can review and change the policy with your team.

The trigger watches the instrument flag. The engineer should record a failure
as soon as it is discovered. If the signed service report or a use arrives
after the flag was set, run a manual trace once that evidence is available.
The project administrator who enables the trigger must be able to read the
project's instruments, services, uses and prior impact cases. Inspect the
project's trigger history and runs for missing evidence or input limits.
Reconcile measurements outside the project separately.

Review the event and its three downstream reference searches in
`triggers/trace_failed_instrument.yaml`. Review the suspect-window calculation
in `scripts/trace.py` with the facility's quality team. The window starts at
the earliest suspected time and ends just before the next signed passing
calibration after the failure. The failure/use key is shared with Equipment's
own trace so the two routes agree about which case to review, and a result
that already has a case among the prior impacts gets no second. Keep the
recovery-boundary and missing-signature cases in `tests/trace.yaml` and add
examples from the procedure that your facility approves.

## Test and deploy

Install the Iki CLI and worker matching your deployment. From the copied
directory, set `IKI_PLUGINS` to the first-party `plugins/` directory at the
commit your deployment uses and run these checks in a shell:

```sh
set -- --with "$IKI_PLUGINS/lab_roles" --with "$IKI_PLUGINS/equipment"
iki plugin validate . "$@"
iki plugin makemigrations . "$@" --check --no-input
iki plugin test . "$@"
```

Commit the copied plugin and its tests in your repository. Add its name,
repository URL, path and reviewed commit SHA to `plugins.lock.yaml` alongside
Equipment and its dependencies. Bind the renamed plugin's environment in
`compute.yaml` to a backend template available at your site, for example:

```yaml
apiVersion: iki/v1
kind: EnvironmentBinding
environment: my_lab/calibration_tracing.calibration-tools
templates: [{ template: backend-small }]
```

Build and roll out the deployment. A project administrator can now enable
**Trace uses when an instrument fails calibration** in that project's triggers.
Use a test project to confirm the run sees the intended service history and
creates the expected cases before enabling it for routine work.
