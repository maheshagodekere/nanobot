"""FastAPI app for the scraper service."""

import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from models import create_job, delete_job, get_job, init_db, list_jobs, update_job
from worker import run_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("scraper.app")

KNOWLEDGE_DIR = os.environ.get("KNOWLEDGE_DIR", "/knowledge")

app = FastAPI(title="nanobot-scraper", version="0.1.0")


class JobRequest(BaseModel):
    label: str = ""
    urls: list[str]
    selectors: dict = {}
    output_schema: dict = {}
    follow_links: bool = False
    max_pages: int = 50
    delay: float = 1.0
    output_file: str = ""


@app.on_event("startup")
async def startup():
    await init_db()
    asyncio.create_task(run_worker())
    logger.info("Scraper service started")


@app.post("/jobs")
async def submit_job(req: JobRequest):
    if not req.urls:
        raise HTTPException(status_code=400, detail="urls list cannot be empty")
    result = await create_job(
        label=req.label,
        urls=req.urls,
        selectors=req.selectors,
        output_schema=req.output_schema,
        follow_links=req.follow_links,
        max_pages=req.max_pages,
        delay=req.delay,
        output_file=req.output_file,
    )
    return result


@app.get("/jobs")
async def get_jobs():
    jobs = await list_jobs()
    # Return summary view
    return [
        {
            "id": j["id"],
            "label": j["label"],
            "status": j["status"],
            "progress": j["progress"],
            "created_at": j["created_at"],
            "output_file": j["output_file"],
            "error": j["error"],
        }
        for j in jobs
    ]


@app.get("/jobs/{job_id}")
async def get_job_detail(job_id: str):
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/jobs/{job_id}/results")
async def get_job_results(job_id: str):
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    path = os.path.join(KNOWLEDGE_DIR, job["output_file"])
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Results file not yet created")
    return FileResponse(path, media_type="application/x-ndjson", filename=job["output_file"])


@app.delete("/jobs/{job_id}")
async def cancel_or_delete_job(job_id: str):
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] == "running":
        await update_job(job_id, status="cancelled")
        return {"id": job_id, "status": "cancelled"}
    deleted = await delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"id": job_id, "status": "deleted"}
