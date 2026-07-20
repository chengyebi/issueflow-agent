import os

from redis import Redis
from rq import Queue

from app.tasks import process_issue_agent_run


REDIS_URL = os.environ["REDIS_URL"]

redis_connection = Redis.from_url(REDIS_URL)
issue_queue = Queue("issueflow", connection=redis_connection)


def enqueue_issue_agent_run(agent_run_id: int) -> str:
    job = issue_queue.enqueue(
        process_issue_agent_run,
        agent_run_id,
        job_timeout=180,
    )

    return job.id
