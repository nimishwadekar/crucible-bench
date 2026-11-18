# Cross-cluster smoke test

Crucible runs the playbook on `pirate`. A command task creates a Job and an image pull Secret in the target cluster. The Job starts one CPU-only pod once the Secret is in place. The pod pulls an external image and prints `crucible-cross-cluster-ok`. The task verifies the marker, saves `pod.log`, and confirms the Job, pod, and Secret are gone before reporting success. [Kubernetes image pull secrets](https://kubernetes.io/docs/concepts/containers/images/#specifying-imagepullsecrets-on-a-pod)

The target Job has a two-minute active deadline and a five-minute TTL after it finishes. The task attempts cleanup after a workload failure too. The Job owns the pod and the Secret, so the target cluster's TTL controller can clean up all three after a hard stop or lost connectivity. [Kubernetes Job cleanup](https://kubernetes.io/docs/concepts/workloads/controllers/ttlafterfinished/)

To change the pack's default image, edit the `image` field on the `smoke` container in [job.yaml](job.yaml), then save a new draft version. Keep the file in JSON syntax so Python can read it without installing a YAML parser. The task adds the generated Job name and pull Secret reference at runtime. The `smoke` container must print `crucible-cross-cluster-ok` on its own line.

## Inputs

| Input | Value |
| --- | --- |
| `target_namespace` | An existing namespace on that cluster. |
| `test_image` | Optional launch override for the image in `job.yaml`. Use an image pullable in the namespace that supports non-root execution and has `/bin/sh`. |
| `job_deadline_seconds` | Optional deadline override, up to 900 seconds, to allow time for large image pulls. Omit it to use the deadline in `job.yaml`. |
| `pod_hold_seconds` | Time to keep a successful pod visible before cleanup. Defaults to 60 seconds. Accepts 0 to 120 seconds. |
| `target-kubeconfig` | A Crucible secret of kind `kubeconfig` containing the target cluster's connection details and credentials. |
| `registry-auth` | A Crucible secret of kind `registry_authfile` with inline Docker `auths` for the selected registry. Use `{"auths":{}}` for a public image. |

Crucible mounts the kubeconfig from the draft's secret binding at `/var/run/crucible-target/kubeconfig` inside the loop pod. The setup commands below create that secret from a local kubeconfig and bind it to the draft.

The loop pod needs direct HTTPS access to the target Kubernetes API and the target nodes need access to the external registry. Crucible validates the kubeconfig when you register it. The exported file contains a bearer token and the target namespace. Include the API CA for clusters with a private CA. For Waldorf, [refresh the kubeconfig before every run](#refresh-the-waldorf-kubeconfig-before-every-run). [Kubeconfig reference](https://kubernetes.io/docs/reference/config-api/kubeconfig.v1/)

Give the target identity these permissions in the target namespace.

| API group | Resource | Verbs |
| --- | --- | --- |
| `batch` | `jobs` | `create`, `get`, `patch`, `delete` |
| core | `pods` | `list` |
| core | `pods/log` | `get` |
| core | `secrets` | `create`, `get`, `delete` |

## Refresh the Waldorf kubeconfig before every run

Run these commands from the repository root on macOS immediately before each Waldorf run. Set `SMOKE_NAMESPACE` to the same namespace you will pass as `target_namespace` when launching the draft.

The default Waldorf kubeconfig at `~/.kube/config.waldorf` uses the local `kubectl oidc-login` plugin. The permission check reuses a valid login or prompts you to sign in through your browser. After the check prints `yes`, the exporter embeds the plugin's current bearer token in a namespace-scoped kubeconfig and copies it to the clipboard.

```sh
export SMOKE_KUBECONFIG="$HOME/.kube/config.waldorf"
export SMOKE_NAMESPACE="rajjoshi-dev"
set -o pipefail

kubectl --kubeconfig "$SMOKE_KUBECONFIG" auth can-i create jobs.batch -n "$SMOKE_NAMESPACE" &&
  python3 examples/cross-cluster-smoke/smoke.py --export-kubeconfig "$SMOKE_KUBECONFIG" \
    --namespace "$SMOKE_NAMESPACE" | pbcopy
```

Before launching, open **Secrets** in Crucible, edit the existing `${SMOKE_DRAFT}-kubeconfig` secret, paste the clipboard contents, and save. For `raj-cross-cluster-smoke`, edit `raj-cross-cluster-smoke-kubeconfig`. Saving creates a new version under the same binding. Clear the clipboard with `pbcopy </dev/null`.

Clusters whose kubeconfigs already contain a valid bearer token can skip this Waldorf OIDC step.

## Set up a Crucible draft

From the repository root, export the draft ID, existing target namespace, and target kubeconfig path. These commands use the Waldorf kubeconfig. Set `SMOKE_IMAGE` for a one-run image override and `SMOKE_REGISTRY_AUTHFILE` when preparing private registry credentials.

```sh
export SMOKE_DRAFT="yourname-cross-cluster-smoke"
export SMOKE_NAMESPACE="your-test-namespace"

export SMOKE_KUBECONFIG="$HOME/.kube/config.waldorf"

# Optional. Uncomment and set these for an image in registry.redhat.io.
# export SMOKE_IMAGE="registry.redhat.io/team/image:tag"
# export SMOKE_REGISTRY_AUTHFILE="${REGISTRY_AUTH_FILE:-$HOME/.config/containers/auth.json}"
```

Use a kubeconfig scoped to the target namespace permissions above for shared runs. Run these commands with `crux` connected to Crucible.
For a first-time Waldorf setup, run the OIDC permission check above before creating the kubeconfig secret.

```sh
crux draft-create "$SMOKE_DRAFT" --description "Cross-cluster smoke test"
crux draft-push "$SMOKE_DRAFT" examples/cross-cluster-smoke --base-version 1
crux draft-preview "$SMOKE_DRAFT"

python3 examples/cross-cluster-smoke/smoke.py --export-kubeconfig "$SMOKE_KUBECONFIG" \
  --namespace "$SMOKE_NAMESPACE" \
  | crux secret-create "${SMOKE_DRAFT}-kubeconfig" --kind kubeconfig
```

For the public BusyBox image, register an empty registry auth file.

```sh
printf '%s' '{"auths":{}}' \
  | crux secret-create "${SMOKE_DRAFT}-registry" --kind registry-authfile
```

For an image in `registry.redhat.io`, Podman stores the login credentials in its auth file. On macOS the default is `$HOME/.config/containers/auth.json`; if you used `REGISTRY_AUTH_FILE` or `podman login --authfile`, point `SMOKE_REGISTRY_AUTHFILE` at that same file. `podman login --get-login` checks that the current auth file has a login without printing the credential. [Podman login and auth files](https://docs.podman.io/en/latest/markdown/podman-login.1.html)

```sh
export SMOKE_REGISTRY_AUTHFILE="${SMOKE_REGISTRY_AUTHFILE:-${REGISTRY_AUTH_FILE:-$HOME/.config/containers/auth.json}}"
podman login --authfile "$SMOKE_REGISTRY_AUTHFILE" --get-login registry.redhat.io

# Confirm the file contains this registry's entry, then copy only that entry
# to the clipboard for the Crucible UI. The credential stays out of terminal output.
jq -e --arg registry registry.redhat.io \
  '.auths[$registry] | select(type == "object")' \
  "$SMOKE_REGISTRY_AUTHFILE" >/dev/null
jq -c --arg registry registry.redhat.io \
  '{auths: {($registry): .auths[$registry]}}' \
  "$SMOKE_REGISTRY_AUTHFILE" \
  | pbcopy
```

In the Crucible UI, edit the existing `${SMOKE_DRAFT}-registry` secret and replace its value with the clipboard contents. The secret gets a new version while keeping its ID and playbook binding. Clear the clipboard afterward with `pbcopy </dev/null`. For a first-time setup with no registry secret yet, run `pbpaste | crux secret-create "${SMOKE_DRAFT}-registry" --kind registry-authfile` instead. `crux secret-create` cannot update an existing name. This is registry-pull authentication, not a Kubernetes API bearer token: the remote pod uses it only to pull the image. [Kubernetes private registry authentication](https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/)

Set the secret ID variables to the IDs returned by `crux secret-create` (or use the existing registry secret ID if you updated it in the UI). Bind each secret once; an existing binding remains in place after a UI value update.

```sh
export SMOKE_SECRET_ID="<target-kubeconfig-secret-id>"
export SMOKE_REGISTRY_SECRET_ID="<registry-auth-secret-id>"

# Bind each secret once; skip a bind command if that input is already bound.
crux secret-bind "$SMOKE_SECRET_ID" --playbook "$SMOKE_DRAFT" \
  --declared-name target-kubeconfig --file /var/run/crucible-target/kubeconfig
crux secret-bind "$SMOKE_REGISTRY_SECRET_ID" --playbook "$SMOKE_DRAFT" \
  --declared-name registry-auth --file /var/run/crucible-target/registry-auth.json
```

## Launch a run

For every Waldorf run, [export a fresh kubeconfig and save it to the existing Crucible secret](#refresh-the-waldorf-kubeconfig-before-every-run) before running either launch command below.

```sh
crux draft-launch "$SMOKE_DRAFT" --max-cost 0.10 --max-time 6m \
  --params "$(jq -cn --arg namespace "$SMOKE_NAMESPACE" \
    --arg image "${SMOKE_IMAGE:-}" \
    '{target_namespace: $namespace} +
     (if $image == "" then {} else {test_image: $image} end)')"
```

The two secret bindings are scoped to the draft. To keep several target kubeconfigs available at the same time, use a draft per target. Open the run from the launch response to see the `remote_smoke` task's Job and pod names and `pod.log`.

Start a pod watch before launching to see the pod start and finish. Crucible logs the pod name when it first sees it and keeps the pod in `Completed` for 60 seconds by default.

```sh
kubectl --kubeconfig "$SMOKE_KUBECONFIG" -n "$SMOKE_NAMESPACE" \
  get pods -l batch.kubernetes.io/job-name --watch
```

For a large image, supply `job_deadline_seconds` and give the draft time to run the Job and confirm cleanup.

```sh
crux draft-launch "$SMOKE_DRAFT" --max-cost 0.10 --max-time 20m \
  --params "$(jq -cn --arg namespace "$SMOKE_NAMESPACE" --arg image "$SMOKE_IMAGE" \
    '{target_namespace: $namespace, test_image: $image, job_deadline_seconds: "900"}')"
```
