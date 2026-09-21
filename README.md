# Varuna Netra — Oil-Spill Detection & Dark Vessel Correlation Platform

Varuna Netra is an automated maritime surveillance and investigation system that correlates satellite-derived oil spill observations (Sentinel-1 SAR imagery) with live and historical Automatic Identification System (AIS) vessel transponder tracking to identify and attribute potential spill sources.

---

## Deployment on Render

This repository includes full support for deploying on [Render](https://render.com) using Infrastructure-as-Code:

- **Blueprint Specification**: [`render.yaml`](./render.yaml) defines both the backend FastAPI service and the frontend React SPA.
- **Detailed Instructions**: See the [Render Deployment Guide](./RENDER_DEPLOYMENT.md) for step-by-step setup, database configuration, and environment variables.

---

## Local Development

### 1. Backend Setup
```bash
cd backend
python -m venv venv
source venv/bin/activate  # Or `venv\Scripts\activate` on Windows
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
```

### 2. Frontend Setup
```bash
cd frontend
yarn install
yarn start
```
