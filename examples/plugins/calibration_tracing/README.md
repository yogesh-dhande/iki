# Automatic calibration impact tracing

Use this recipe if your facility opens an impact assessment whenever an engineer
sets an instrument's **Calibration** state to **Failed**. Each new case links one
signed out-of-tolerance check to a measured result's first use in its suspect
window: a result has one case for each failure, whether traced or opened by
Equipment QA for a result no use was recorded for. The service history, use
records and QA dispositions all belong to Equipment.
Engineers can also run **Trace failed calibrations** directly from its Service
schedule tab.

The plugin is one trigger. It runs Equipment's own action,
`iki/equipment.trace_failed_calibrations`, named in full. The automatic and
manual routes use the same calculation, the same failure/use key and the same
tests. A result that already has a case among the prior impacts gets no
second one. Each run is Equipment's: it runs in Equipment's environment, at
the Equipment version your deployment installed, and it can create only the
impact cases Equipment's action declares. The run page names this trigger,
and the plugin it belongs to, beside the action.

## Adapt the policy

Copy this directory into your lab's plugin repository. Set `name` in
`plugin.yaml` to a name in your lab's namespace, for example
`my_lab/calibration_tracing`. Keep `iki/equipment` under `dependencies`. A
trigger can run only the actions of a plugin listed there.

The trigger watches the instrument flag. The engineer should record a failure
as soon as it is discovered. If the signed service report or a use arrives
after the flag was set, run a manual trace once that evidence is available.
The project administrator who enables the trigger must be able to read the
project's instruments, services, uses and prior impact cases. Inspect the
project's trigger history and runs for missing evidence or input limits.
Reconcile measurements outside the project separately.

Review the event and its three downstream reference searches in
`triggers/trace_failed_instrument.yaml` with the facility's quality team.
Equipment's README describes the suspect window. It starts at the earliest
suspected time and ends just before the next signed passing calibration after
the failure. Equipment's action tests cover the recovery boundary and a
failure still awaiting its signature.

## Test and deploy

Install the Iki CLI matching your deployment. From the copied directory, set
`IKI_PLUGINS` to the first-party `plugins/` directory at the commit your
deployment uses and run these checks in a shell:

```sh
set -- --with "$IKI_PLUGINS/lab_roles" --with "$IKI_PLUGINS/equipment"
iki plugin validate . "$@"
iki plugin makemigrations . "$@" --check --no-input
IKI_CHECKOUT="$IKI_PLUGINS/.." python -m pytest tests
```

The validator checks the trigger against Equipment's action: that its input
takes an instrument, and that each related search feeds an input that takes
many. `tests/test_trigger.py` checks the same wiring against the Equipment
release you pin, and that the trigger stays off until a project's Admin turns
it on. Commit the copied plugin in your repository. Add its name, repository
URL, path and reviewed commit SHA to `plugins.lock.yaml` alongside Equipment
and its dependencies. The trigger's runs use Equipment's `equipment-tools`
environment, which the deployment already binds for Equipment. The build
checks the trigger against the Equipment release in the lock.

Build and roll out the deployment. A project administrator can now enable
**Trace uses when an instrument fails calibration** in that project's
triggers. The confirmation says it runs an Equipment action. Use a test
project to confirm the run sees the intended service history and creates the
expected cases before enabling it for routine work.
