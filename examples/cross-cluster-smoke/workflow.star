params = {
    "target_namespace": {"type": "string", "required": True, "pattern": "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", "doc": "Existing namespace on the target cluster"},
    "test_image": {"type": "string", "default": "", "doc": "Optional override for the image in job.yaml. Must support non-root execution and /bin/sh."},
    "job_deadline_seconds": {"type": "string", "default": "", "pattern": "^[0-9]{0,3}$", "doc": "Override the Job deadline for a large image pull, up to 900 seconds. Zero or empty uses the deadline in job.yaml."},
    "pod_hold_seconds": {"type": "string", "default": "60", "pattern": "^[0-9]{1,3}$", "doc": "Keep the completed pod visible before cleanup, up to 120 seconds. Zero cleans up immediately."},
}

smoke = command(
    name = "remote_smoke",
    run = "python3 smoke.py",
    timeout = "19m",
    emits = ["job", "pod"],
    emits_files = ["pod.log"],
)

workflow(type = "playbook", tasks = [smoke], result = smoke)
