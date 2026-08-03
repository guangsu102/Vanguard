import base64
import collections
import json
import sys

import redis


def main() -> int:
    client = redis.Redis(host="redis", port=6379, db=1)
    queue = sys.argv[1] if len(sys.argv) > 1 else "automation"
    total = client.llen(queue)
    counts: collections.Counter[str] = collections.Counter()
    dry_run: collections.Counter[str] = collections.Counter()
    recent = []

    for raw in client.lrange(queue, 0, -1):
        message = json.loads(raw)
        headers = message.get("headers") or {}
        task = headers.get("task", "unknown")
        counts[task] += 1
        try:
            body = json.loads(base64.b64decode(message.get("body", "")))
            kwargs = body[1] if len(body) > 1 and isinstance(body[1], dict) else {}
        except Exception:
            kwargs = {}
        if kwargs.get("dry_run") is True:
            dry_run[task] += 1
        if len(recent) < 10:
            recent.append(
                {
                    "id": headers.get("id"),
                    "task": task,
                    "kwargs": kwargs,
                }
            )

    print(json.dumps({"queue": queue, "total": total, "counts": counts, "dry_run": dry_run, "head": recent}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
