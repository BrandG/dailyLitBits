# Gemini - Project Overview: dailyLitBits

This document provides essential context for the Gemini AI assistant to effectively understand and contribute to this project.

## 1. Project Goal

dailyLitBits is a Python-based web application that delivers daily literature excerpts to subscribers. It appears to use a Flask backend, manages users, ingests books, and uses AI to generate summaries.

## 2. Core Technologies

- **Backend:** Python (likely Flask, based on file structure)
- **Frontend:** HTML, likely using a templating engine like Jinja2 (in `src/templates/`)
- **Containerization:** Docker (`docker/docker-compose.yml`, `docker/Dockerfile`)
- **AI Integration:** Custom AI logic is likely in `src/ai.py`.

## 3. Project Structure

- `src/main.py`: The main application entry point. Contains web server setup and routing.
- `src/templates/`: HTML files for the web interface.
- `src/static/`: Static assets like images. `covers/` holds book cover images.
- `src/ingest.py`: Handles the process of adding new books to the system.
- `src/summarize.py` & `summarize_threaded.py`: Logic for creating book summaries.
- `src/user_manager.py`: Manages user accounts, profiles, and authentication.
- `src/dispatch.py`: Likely handles sending the daily excerpts to users.
- `docker/`: Contains files for building and running the application in a Docker container.
- `src/tools/`: Utility scripts for project maintenance.

## 4. How to Run the Application

```bash
docker exec -it dailylitbits python main.py
```

## 5. How to Run Tests

```bash
docker exec -it dailylitbits tests/python setup_victory_test.py
```

## 6. Coding Conventions & Style Guide

- This project appears to follow standard Python conventions.
## - *If you use a linter like Black or Flake8, please specify it here.*

## 7. Important Notes & "Don'ts"

- Do not commit large files to the `src/static/covers` directory.
- Before committing, ensure all tests are passing.
