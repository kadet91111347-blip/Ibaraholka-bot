import os
import asyncio
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"status": "ok", "version": "minimal-test"}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/listings")
async def listings():
    return []

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    logger.info(f"🌐 Starting minimal API on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
