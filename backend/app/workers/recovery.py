import argparse
import json
import time

from app.services.outbox import dispatch_pending


def run_once(limit: int = 50) -> list[dict]:
    return [result.__dict__ for result in dispatch_pending(limit)]


def main() -> None:
    parser = argparse.ArgumentParser(description="恢复未投递的 IssueFlow Outbox 任务")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    while True:
        print(json.dumps(run_once(args.limit), ensure_ascii=False, default=str))
        if not args.loop:
            break
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()
