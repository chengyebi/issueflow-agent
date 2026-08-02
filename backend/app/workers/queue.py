from functools import lru_cache

from redis import Redis
from rq import Queue, Retry

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_queue() -> Queue:
    settings = get_settings()
    return Queue(
        settings.rq_queue_name,
        connection=Redis.from_url(settings.redis_url),
    )


def enqueue_issue_agent_run(agent_run_id: int) -> str:
    from app.workers.tasks import process_issue_agent_run

    job = get_queue().enqueue(
        process_issue_agent_run,
        agent_run_id,
        job_timeout=get_settings().agent_job_timeout_seconds,
        retry=Retry(
            max=get_settings().rq_max_retries,
            interval=get_settings().retry_intervals,
        ),
    )
    return job.id


def enqueue_review_commands(review_task_id: int) -> str:
    from app.workers.tasks import process_review_commands

    job = get_queue().enqueue(
        process_review_commands,
        review_task_id,
        job_timeout=get_settings().command_job_timeout_seconds,
        retry=Retry(
            max=get_settings().rq_max_retries,
            interval=get_settings().retry_intervals,
        ),
    )
    return job.id
