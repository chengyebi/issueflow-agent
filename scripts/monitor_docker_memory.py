#!/usr/bin/env python3
import argparse
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def _bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9.]+)(B|KiB|MiB|GiB)", value.strip())
    if match is None:
        raise ValueError(f"无法解析 Docker 内存值: {value}")
    number, unit = match.groups()
    scales = {
        "B": 1,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
    }
    return int(float(number) * scales[unit])


def main() -> None:
    parser = argparse.ArgumentParser(description="记录单个 Docker 容器的内存峰值")
    parser.add_argument("container")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    samples = 0
    peak = 0
    started = datetime.now(timezone.utc)
    while True:
        state = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", args.container],
            capture_output=True,
            text=True,
            check=False,
        )
        if state.returncode != 0 or state.stdout.strip() != "true":
            break
        stats = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", args.container],
            capture_output=True,
            text=True,
            check=True,
        )
        record = json.loads(stats.stdout)
        usage = record["MemUsage"].split("/", 1)[0].strip()
        peak = max(peak, _bytes(usage))
        samples += 1
        time.sleep(args.interval)
    report = {
        "container": args.container,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "sample_interval_seconds": args.interval,
        "sample_count": samples,
        "peak_memory_bytes": peak,
        "peak_memory_mib": peak / 1024**2,
        "limit_exceeded_2gb": peak > 2 * 1024**3,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
