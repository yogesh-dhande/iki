# Independent construct review

Use this plugin when your team records who built an assembly and asks a second person
to verify the comparison before locking the record. Create a **Reviewed assembly** in
the project for each plan that needs this review. It inherits Plasmid’s Assembly
fields, comparison action and recheck trigger.

A project Admin grants the **Cloning scientist** and **Construct reviewer** lab roles
to the people doing that work. A scientist or reviewer marks a matching plan
**Checked**, then **Built**. A reviewer can mark it **Verified** when the latest
comparison matches, someone else recorded the build, and the reviewer has made no
value edits since the build. Verification locks the record. Either role can abandon
a plan with a reason. The creator and watchers receive notifications for a mismatch
and for verification, subject to their access and delivery settings.

## Adapt the review for your lab

1. Copy this directory into your own repository. Set the namespace and plugin name in
   `plugin.yaml` before your first deployment. Choose a record prefix unique to your
   deployment in `types/reviewed_assembly.yaml`.
2. Match the roles, transitions, guards, and notification recipients to your SOP.
   Keep the check on `matches_product` wherever a transition attests to the current
   comparison. Editing the plan invalidates the result even when the project Admin has
   disabled the automatic recheck.
3. Install the Iki CLI and testing tools for your deployment. Set `IKI_CHECKOUT` to
   a checkout of that Iki version, including its Plasmid and DNA plugins. In a source
   checkout, `make test-wasm` builds the rule evaluator. After renaming a fresh copy,
   discard its example `migrations/` and generate the initial migration under your
   plugin’s own name. Run these commands from the copied directory.

   ```sh
   iki plugin makemigrations . --with "$IKI_CHECKOUT/plugins/dna" --with "$IKI_CHECKOUT/plugins/plasmid"
   iki plugin validate . --with "$IKI_CHECKOUT/plugins/dna" --with "$IKI_CHECKOUT/plugins/plasmid"
   iki plugin makemigrations . --check --no-input --with "$IKI_CHECKOUT/plugins/dna" --with "$IKI_CHECKOUT/plugins/plasmid"
   python -m pytest -q tests/test_review_guards.py
   ```

   The tests use `iki-spec` and pytest. Keep them in your plugin’s CI and update the
   expected decisions when your SOP changes.

4. Commit the manifests, reviewed migration and tests. Pin your repository, commit
   SHA, plugin name and directory in the deployment’s `plugins.lock.yaml` alongside
   `iki/dna` and `iki/plasmid`. Build and roll out the deployment, then grant the lab
   roles in each project that uses reviewed assemblies.
