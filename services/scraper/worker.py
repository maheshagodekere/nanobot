"""Background worker that processes pending scrape jobs."""

import asyncio
import json
import logging
import os

from scrapling import Fetcher

from models import get_job, list_jobs, update_job

logger = logging.getLogger("scraper.worker")

KNOWLEDGE_DIR = os.environ.get("KNOWLEDGE_DIR", "/knowledge")
POLL_INTERVAL = 5


async def run_worker() -> None:
    """Poll for pending jobs and process them."""
    logger.info("Worker started, polling every %ds", POLL_INTERVAL)
    while True:
        try:
            jobs = await list_jobs()
            pending = [j for j in jobs if j["status"] == "pending"]
            if pending:
                await process_job(pending[0])
        except Exception:
            logger.exception("Worker loop error")
        await asyncio.sleep(POLL_INTERVAL)


async def process_job(job: dict) -> None:
    """Process a single scrape job with checkpointing."""
    job_id = job["id"]
    logger.info("Processing job %s: %s", job_id, job["label"])
    await update_job(job_id, status="running", progress={"done": 0, "total": len(job["urls"]), "current_url": ""})

    urls = job["urls"]
    selectors = job["selectors"]
    output_schema = job.get("output_schema", {})
    delay = job.get("delay", 1.0)
    max_pages = job.get("max_pages", 50)
    output_path = os.path.join(KNOWLEDGE_DIR, job["output_file"])

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    fetcher = Fetcher(auto_match=False)
    done = 0
    errors = []

    for i, url in enumerate(urls[:max_pages]):
        # Check if job was cancelled
        current = await get_job(job_id)
        if current and current["status"] == "cancelled":
            logger.info("Job %s cancelled", job_id)
            return

        await update_job(job_id, progress={"done": done, "total": len(urls[:max_pages]), "current_url": url})

        try:
            page = await asyncio.to_thread(fetcher.get, url)
            row = extract_row(page, url, selectors, output_schema)

            # Checkpoint: append after each URL
            with open(output_path, "a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

            done += 1
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", url, e)
            errors.append({"url": url, "error": str(e)})
            # Write error row to results too
            with open(output_path, "a") as f:
                f.write(json.dumps({"url": url, "_error": str(e)}, ensure_ascii=False) + "\n")

        if i < len(urls[:max_pages]) - 1 and delay > 0:
            await asyncio.sleep(delay)

    status = "completed" if not errors else ("completed" if done > 0 else "failed")
    error_msg = f"{len(errors)} URL(s) failed" if errors else None
    await update_job(
        job_id,
        status=status,
        progress={"done": done, "total": len(urls[:max_pages]), "current_url": ""},
        error=error_msg,
    )
    logger.info("Job %s finished: %d/%d succeeded", job_id, done, len(urls[:max_pages]))


def extract_row(page, url: str, selectors: dict, output_schema: dict) -> dict:
    """Extract data from a page using CSS selectors."""
    if not selectors:
        # Default: extract title + full text content
        title = ""
        title_el = page.css_first("title")
        if title_el:
            title = title_el.text()
        return {"url": url, "title": title, "content": page.get_all_text(separator="\n")[:5000]}

    row = {"url": url}
    for field, selector in selectors.items():
        el = page.css_first(selector)
        if el:
            raw = el.text().strip()
            # Cast to schema type if provided
            if field in output_schema:
                raw = cast_value(raw, output_schema[field])
            row[field] = raw
        else:
            row[field] = None
    return row


def cast_value(value: str, dtype: str):
    """Best-effort cast to the declared schema type."""
    dtype = dtype.lower()
    if dtype == "int":
        try:
            return int(value.replace(",", ""))
        except ValueError:
            return value
    if dtype == "float":
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return value
    return value
