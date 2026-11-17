import json
import os
from pathlib import Path

inputs = json.loads(Path(os.environ["CRUCIBLE_INPUTS_FILE"]).read_text())
title = inputs["write"]["title"]
haiku = Path("inputs/write/HAIKU.md").read_text()
body = [line for line in haiku.splitlines() if line and not line.startswith("#")]
words = sum(len(line.split()) for line in body)
Path("REPORT.md").write_text(f"# {title}\n\n{len(body)} lines, {words} words.\n\n{haiku}")
print(json.dumps({"words": str(words)}))
