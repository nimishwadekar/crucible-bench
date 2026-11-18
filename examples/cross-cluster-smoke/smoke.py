import argparse
import base64
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode, urlsplit
from uuid import uuid4


KUBECONFIG = Path("/var/run/crucible-target/kubeconfig")
REGISTRY_AUTH = Path("/var/run/crucible-target/registry-auth.json")
JOB_FILE = Path(__file__).with_name("job.yaml")
MARKER = "crucible-cross-cluster-ok"


def read_secret(path, secret_name):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise RuntimeError(f"Bind the {secret_name} secret to the playbook") from None
    except json.JSONDecodeError:
        raise ValueError(f"The {secret_name} secret must be a JSON file") from None


def named_entry(entries, name, field):
    for entry in entries:
        if entry["name"] == name:
            return entry[field]
    raise ValueError(f"No matching {field} in the target kubeconfig")


def selected_context_name(config):
    if current := config.get("current-context"):
        return current
    contexts = config.get("contexts") or []
    if len(contexts) == 1:
        return contexts[0]["name"]
    raise ValueError("The target kubeconfig must identify a single default target")


def kubeconfig_token(credentials):
    if credentials.get("token"):
        return credentials["token"]
    plugin = credentials.get("exec")
    if not plugin:
        raise ValueError("The kubeconfig needs a bearer token or an exec credential plugin")

    environment = os.environ.copy()
    environment.update({item["name"]: item["value"] for item in (plugin.get("env") or [])})
    environment["KUBERNETES_EXEC_INFO"] = json.dumps({
        "apiVersion": plugin["apiVersion"],
        "kind": "ExecCredential",
        "spec": {"interactive": sys.stdin.isatty()},
    })
    try:
        result = subprocess.run(
            [plugin["command"], *(plugin.get("args") or [])],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
        )
        token = json.loads(result.stdout)["status"]["token"]
        if isinstance(token, str) and token:
            return token
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError):
        raise RuntimeError("The kubeconfig credential plugin could not provide a bearer token. Refresh its login and retry") from None
    raise RuntimeError("The kubeconfig credential plugin returned an empty bearer token")


def export_kubeconfig(path, namespace):
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", namespace):
        raise ValueError("Invalid target namespace")
    kubectl = ["kubectl", "--kubeconfig", path]
    config = json.loads(subprocess.check_output(kubectl + ["config", "view", "-o", "json"]))
    prepared = json.loads(subprocess.check_output(kubectl + [
        "--context", selected_context_name(config),
        "config", "view", "--minify", "--flatten", "--raw", "-o", "json",
    ]))
    prepared["contexts"][0]["context"]["namespace"] = namespace
    user = prepared["users"][0]
    user["user"] = {"token": kubeconfig_token(user["user"])}
    print(json.dumps(prepared))


class TargetCluster:
    def __init__(self, config):
        context = named_entry(config.get("contexts") or [], selected_context_name(config), "context")
        cluster = named_entry(config["clusters"], context["cluster"], "cluster")
        user = named_entry(config["users"], context["user"], "user")
        if any(cluster.get(key) for key in (
            "insecure-skip-tls-verify", "tls-server-name", "proxy-url"
        )):
            raise ValueError("The target kubeconfig must use direct HTTPS and verify the API hostname")
        if any(user.get(key) for key in ("exec", "auth-provider", "tokenFile")):
            raise ValueError("Use a self-contained kubeconfig with a token or embedded client certificates")

        self.server = urlsplit(cluster["server"])
        if (self.server.scheme != "https" or not self.server.hostname
                or self.server.username is not None or self.server.query or self.server.fragment):
            raise ValueError("The target kubeconfig must specify an HTTPS API URL")

        ca = cluster.get("certificate-authority-data")
        if cluster.get("certificate-authority") and not ca:
            raise ValueError("Embed the cluster CA in the target kubeconfig")
        self.tls = ssl.create_default_context(cadata=base64.b64decode(ca).decode("ascii") if ca else None)

        self.headers = {}
        token = user.get("token")
        if token:
            if not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token):
                raise ValueError("Invalid bearer token in the target kubeconfig")
            self.headers["Authorization"] = f"Bearer {token}"
        elif user.get("client-certificate-data") and user.get("client-key-data"):
            with tempfile.TemporaryDirectory() as directory:
                certificate = Path(directory) / "client.crt"
                key = Path(directory) / "client.key"
                certificate.write_bytes(base64.b64decode(user["client-certificate-data"]))
                key.write_bytes(base64.b64decode(user["client-key-data"]))
                self.tls.load_cert_chain(certificate, key, password="")
        else:
            raise ValueError("The target kubeconfig needs a token or embedded client certificates")

    def request(self, method, path, body=None, *, missing_ok=False, content_type="application/json"):
        connection = http.client.HTTPSConnection(
            self.server.hostname, self.server.port, context=self.tls, timeout=10
        )
        headers = self.headers.copy()
        if body is not None:
            body = json.dumps(body).encode()
            headers["Content-Type"] = content_type
        try:
            connection.request(method, self.server.path.rstrip("/") + path, body, headers)
            response = connection.getresponse()
            if response.status == 404 and missing_ok:
                return None
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Target API {method} {path.split('?')[0]} returned HTTP {response.status}")
            return response.read()
        finally:
            connection.close()


def job_manifest(name, image_override):
    try:
        manifest = json.loads(JOB_FILE.read_text())
    except json.JSONDecodeError as error:
        raise ValueError(f"{JOB_FILE.name} must use JSON syntax: {error}") from None
    manifest["metadata"]["name"] = name
    spec = manifest["spec"]
    # Hold the Job while creating the pull Secret. Its active deadline still runs.
    spec["parallelism"] = 0
    pod_spec = spec["template"]["spec"]
    pod_spec.setdefault("imagePullSecrets", []).append({"name": name})
    smoke = next((container for container in pod_spec["containers"] if container["name"] == "smoke"), None)
    if smoke is None:
        raise ValueError(f"{JOB_FILE.name} must define a container named smoke")
    if image_override:
        smoke["image"] = image_override
    return manifest


def pull_secret_manifest(job_name, job_uid, auths):
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": job_name,
            "ownerReferences": [{
                "apiVersion": "batch/v1",
                "kind": "Job",
                "name": job_name,
                "uid": job_uid,
            }],
        },
        "type": "kubernetes.io/dockerconfigjson",
        "stringData": {".dockerconfigjson": json.dumps({"auths": auths})},
    }


def wait_for_job(cluster, job_path, pod_list_path, active_deadline_seconds):
    deadline = time.monotonic() + active_deadline_seconds + 25
    pod_seen = False
    while time.monotonic() < deadline:
        if not pod_seen:
            pods = json.loads(cluster.request("GET", pod_list_path))["items"]
            if pods:
                for pod in pods:
                    print(f"Observed pod {pod['metadata']['name']}", flush=True)
                pod_seen = True
        job = json.loads(cluster.request("GET", job_path))
        for condition in job.get("status", {}).get("conditions", []):
            if condition["status"] == "True" and condition["type"] in ("Complete", "Failed"):
                return condition
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {job_path.rsplit('/', 1)[-1]} to finish")


def delete_resource(cluster, path, uid):
    cluster.request("DELETE", path, {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "propagationPolicy": "Foreground",
        "preconditions": {"uid": uid},
    }, missing_ok=True)


def main():
    params = json.loads(Path(os.environ["CRUCIBLE_INPUTS_FILE"]).read_text())["params"]
    namespace = params["target_namespace"]
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", namespace):
        raise ValueError("Invalid target namespace")
    cluster = TargetCluster(read_secret(KUBECONFIG, "target-kubeconfig"))
    registry_auth = read_secret(REGISTRY_AUTH, "registry-auth")
    if (not isinstance(registry_auth, dict) or not isinstance(registry_auth.get("auths"), dict)
            or registry_auth.get("credsStore") or registry_auth.get("credHelpers")):
        raise ValueError("The registry-auth secret needs inline Docker auths or an empty auths object")

    job_name = f"crucible-smoke-{uuid4().hex[:16]}"
    jobs = f"/apis/batch/v1/namespaces/{namespace}/jobs"
    job_path = f"{jobs}/{job_name}"
    namespace_api = f"/api/v1/namespaces/{namespace}"
    pods = f"{namespace_api}/pods"
    secrets = f"{namespace_api}/secrets"
    secret_path = f"{secrets}/{job_name}"
    manifest = job_manifest(job_name, params.get("test_image"))
    job_deadline_override = int(params.get("job_deadline_seconds") or 0)
    if not 0 <= job_deadline_override <= 900:
        raise ValueError("job_deadline_seconds must be between 0 and 900")
    if job_deadline_override:
        manifest["spec"]["activeDeadlineSeconds"] = job_deadline_override
    active_deadline_seconds = manifest["spec"]["activeDeadlineSeconds"]
    pod_hold_seconds = int(params.get("pod_hold_seconds", "60"))
    if not 0 <= pod_hold_seconds <= 120:
        raise ValueError("pod_hold_seconds must be between 0 and 120")
    print(f"Creating {job_name} in namespace {namespace} on {cluster.server.hostname}", flush=True)
    job = json.loads(cluster.request("POST", jobs, manifest))
    job_uid = job["metadata"]["uid"]
    selector = urlencode({"labelSelector": f"batch.kubernetes.io/controller-uid={job_uid}"})
    pod_list_path = f"{pods}?{selector}"
    secret_uid = None
    try:
        secret = pull_secret_manifest(job_name, job_uid, registry_auth["auths"])
        secret_uid = json.loads(cluster.request("POST", secrets, secret))["metadata"]["uid"]
        cluster.request("PATCH", job_path, [
            {"op": "test", "path": "/metadata/uid", "value": job_uid},
            {"op": "replace", "path": "/spec/parallelism", "value": 1},
        ], content_type="application/json-patch+json")

        condition = wait_for_job(cluster, job_path, pod_list_path, active_deadline_seconds)
        if condition["type"] == "Failed":
            raise RuntimeError(f"Target job failed ({condition.get('reason', 'unknown reason')})")

        pod_list = json.loads(cluster.request("GET", pod_list_path))["items"]
        succeeded = [pod["metadata"]["name"] for pod in pod_list if pod["status"]["phase"] == "Succeeded"]
        if len(succeeded) != 1:
            raise RuntimeError(f"Expected one successful smoke pod, found {len(succeeded)}")
        pod_name = succeeded[0]
        logs = cluster.request("GET", f"{pods}/{pod_name}/log?container=smoke&limitBytes=16384")
        Path("pod.log").write_bytes(logs)
        if MARKER.encode() not in logs.splitlines():
            raise RuntimeError("The smoke pod output did not contain the expected marker")
        if pod_hold_seconds:
            print(f"Keeping completed pod {pod_name} for {pod_hold_seconds}s before cleanup", flush=True)
            time.sleep(pod_hold_seconds)
    finally:
        try:
            delete_resource(cluster, job_path, job_uid)
        finally:
            if secret_uid is not None:
                delete_resource(cluster, secret_path, secret_uid)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if (cluster.request("GET", job_path, missing_ok=True) is None
                    and not json.loads(cluster.request("GET", pod_list_path))["items"]
                    and cluster.request("GET", secret_path, missing_ok=True) is None):
                break
            time.sleep(2)
        else:
            raise RuntimeError(f"Timed out confirming cleanup of {job_name}")
        print(f"Deleted {job_name}, its pod, and its image pull secret", flush=True)

    print(json.dumps({"job": job_name, "pod": pod_name}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-kubeconfig", metavar="PATH", help="Prepare a local kubeconfig for the Crucible secret")
    parser.add_argument("--namespace", help="Namespace to set in the prepared kubeconfig")
    args = parser.parse_args()
    try:
        if args.export_kubeconfig:
            if not args.namespace:
                parser.error("--namespace is required with --export-kubeconfig")
            export_kubeconfig(args.export_kubeconfig, args.namespace)
        else:
            main()
    except (RuntimeError, ValueError, OSError, http.client.HTTPException, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
