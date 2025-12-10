# Gemini - Project Overview: dailyLitBits

This document provides essential context for the Gemini AI assistant to effectively understand and contribute to this project.

## 1. Project Goal

dailyLitBits is a Python-based web application that delivers daily literature excerpts to subscribers. It uses a **FastAPI** backend, a **MongoDB** database, and integrates with the **Google Gemini API** for content generation and analysis. The entire application is designed to be run via **Docker**.

## 2. Core Technologies

-   **Backend:** Python 3.11 with FastAPI
-   **Database:** MongoDB
-   **Containerization:** Docker (`docker-compose`)
-   **AI Integration:** Google Gemini API (for summaries, recommendations, and blurbs)
-   **Email Delivery:** SendGrid
-   **Frontend:** HTML with Jinja2 templating

## 3. Project Structure

-   `src/main.py`: The main FastAPI application entry point.
-   `src/ingest.py`: **Primary script for adding new books.** It now handles fetching text, chunking, downloading covers, and generating AI descriptions. This is the single source of truth for ingestion.
-   `src/dispatch.py`: Manages all email-related tasks, including sending daily excerpts and special notifications.
-   `src/tools/`: Contains various utility and maintenance scripts.
    -   `enhance_library.py`: Backfills metadata (covers, blurbs) for existing books.
    -   `audit_library.py`: Audits and corrects metadata mismatches in the library.
-   `docker/`: Contains the `Dockerfile` and `docker-compose.yml` for building and running the application.
-   `.env`: **Crucial file (not committed)** that holds all secrets and configuration (API keys, database URI).

## 4. How to Run the Application & Tools

All commands **MUST** be run within the Docker environment.

**A. Running the Web Server:**

1.  **Build the image:** `docker-compose build`
2.  **Start the services:** `docker-compose up -d`

The application will be accessible at `http://localhost:8002`.

**B. Running Utility Scripts:**

Use `docker exec` to run any script. The working directory inside the container is `/app`, which maps to the host's `src/` directory.

**Syntax:** `docker exec -it dailylitbits python <path_from_src>`

**Examples:**
```bash
# Ingest a new book
docker exec -it dailylitbits python ingest.py pg11

# Run the library audit
docker exec -it dailylitbits python tools/audit_library.py

# Send a file via the email tool
docker exec -it dailylitbits python tools/send_file_email.py static/some_file.html
```

## 5. How to Run Tests

The test framework and runner are not yet fully defined. Test files are located in `src/tests/`.

## 6. Coding Conventions & Style Guide

-   The project follows standard Python conventions (PEP 8).
-   All Python dependencies are managed in `docker/requirements.txt`.

## 7. Important Notes & "Don'ts"

-   **DON'T** run `python` commands directly on the host. **ALWAYS** use `docker exec`.
-   **DON'T** commit secrets. All API keys and connection strings must be in the `.env` file.
-   **DON'T** commit generated data. The `src/static/covers/` directory is in `.gitignore` because the `ingest.py` and `enhance_library.py` scripts can regenerate its contents.