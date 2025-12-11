# Gemini - Project Overview: dailyLitBits

This document provides essential context for the Gemini AI assistant to effectively understand and contribute to this project.

## 1. Project Goal

dailyLitBits is a Python-based web application that delivers daily literature excerpts to subscribers. It uses a **FastAPI** backend, a **MongoDB** database, and integrates with the **Google Gemini API** for content generation and analysis. The entire application is designed to be run via **Docker**.

## 2. Core Technologies

-   **Backend:** Python 3.11 with FastAPI
-   **Database:** MongoDB
-   **Containerization:** Docker (`docker-compose`)
-   **Testing:** `pytest` with `httpx`
-   **AI Integration:** Google Gemini API (for summaries, recommendations, and blurbs)
-   **Email Delivery:** SendGrid
-   **Frontend:** HTML with Jinja2 templating

## 3. Project Structure

-   `src/main.py`: The main FastAPI application entry point. **Uses dependency injection (`Depends(get_db)`) for database access.**
-   `src/ingest.py`: **Primary script for adding new books.** It now handles fetching text, chunking, downloading covers, and generating AI descriptions.
-   `src/dispatch.py`: Manages all email-related tasks.
-   `src/tools/`: Contains various utility and maintenance scripts.
-   `src/tests/`: Contains all `pytest` tests.
-   `docker/`: Contains the `Dockerfile` and `docker-compose.yml`.
-   `.env`: **Crucial file (not committed)** that holds all secrets and configuration.

## 4. How to Run the Application & Tools

All commands **MUST** be run within the Docker environment.

**A. Running the Web Server:**

1.  **Build/Rebuild:** `docker compose -f docker/docker-compose.yml build`
2.  **Start Services:** `docker compose -f docker/docker-compose.yml up -d`

    The default is to have the dailylitbits container up, so do not leave it shut down normally.
3.  **Restart Services:** `docker compose -f docker/docker-compose.yml restart web`

The application is accessible at `http://localhost:8002`.

**B. Running Utility Scripts:**

Use `docker exec` to run any script. The working directory inside the container is `/app`, which maps to the host's `src/` directory.

**Syntax:** `docker exec -it dailylitbits python <path_from_src>`

**Examples:**
```bash
# Ingest a new book
docker exec -it dailylitbits python ingest.py pg11

# Run the library audit
docker exec -it dailylitbits python tools/audit_library.py
```

## 5. How to Run Tests

Tests are run using `pytest` from within the Docker container. The test suite uses a **separate, temporary database** for each run to ensure isolation.

**Command:**
```bash
docker exec -it dailylitbits pytest tests/
```

## 6. Coding Conventions & Style Guide

-   The project follows standard Python conventions (PEP 8).
-   All Python dependencies, including for testing, are managed in `docker/requirements.txt`.

## 7. Important Notes & "Don'ts"

-   **DON'T** run `python` commands directly on the host. **ALWAYS** use `docker exec`.
-   **REBUILD** the Docker image (`docker compose build`) after changing `docker/requirements.txt`.
-   **RESTART** the Docker container (`docker compose restart`) after changing backend Python code (`.py` files) to ensure the server picks up the changes.
-   **DON'T** commit secrets or generated data (`src/static/covers/`).
