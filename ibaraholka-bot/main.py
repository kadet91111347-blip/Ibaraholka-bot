"""Minimal FastAPI to test Railway startup."""
import os
from fastapi import FastAPI

app = FastAPI()


@app.get("/")
async def root():
    return {"status": "ok", "version": "minimal-v3"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/listings")
async def listings():
    return []


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    print(f"Starting on port {port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
