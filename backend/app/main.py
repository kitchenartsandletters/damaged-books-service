# backend/app/main.py

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routes import router as app_router

# Import routers
from api import system
# Future imports: webhook, products, redirects, status, etc.

app = FastAPI(
    title="Used Books Automation API",
    description="API for managing used book inventory and redirects",
    version="1.0.0"
)

# CORS (adjust origins as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Update to specific frontend domains in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(system.router)
app.include_router(app_router)

# Optional root route
@app.get("/")
def root():
    return {"message": "Used Books Automation API"}