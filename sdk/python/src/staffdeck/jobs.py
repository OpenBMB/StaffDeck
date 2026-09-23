from __future__ import annotations

from .models import APIResponse
from .resources import Resource, segment
from .runs import _wait_for_result


class Jobs(Resource):
    """Durable SOP generation/rewrite and knowledge ingestion jobs."""

    def get(self, job_id: str) -> APIResponse:
        return self._client.request("GET", f"jobs/{segment(job_id)}")

    def result(self, job_id: str) -> APIResponse:
        """Return the server's {job, result, error} envelope for a finished job."""
        return self._client.request("GET", f"jobs/{segment(job_id)}/result")

    def cancel(self, job_id: str) -> APIResponse:
        return self._client.request("POST", f"jobs/{segment(job_id)}:cancel")

    def wait(
        self, job_id: str, *, timeout: float = 300.0, poll_interval: float = 1.0,
    ) -> APIResponse:
        """Return the result envelope on success; raise RunFailedError otherwise.

        WaitTimeout and RunFailedError retain the job ID in their legacy run_id
        attribute. Local timeout never cancels the server's job.
        """
        return _wait_for_result(
            self._client, f"jobs/{segment(job_id)}", job_id,
            timeout=timeout, poll_interval=poll_interval,
        )
