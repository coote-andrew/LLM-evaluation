# Cicada

**Clinical Informatics Centre AI-Driven Analysis**

A Django-based web tool for systematically evaluating Large Language Model (LLM) and agent outputs against structured clinical test data. Built for the Clinical Informatics Centre at Royal Melbourne Hospital.

## Features (Phase 1 MVP)

- **Test Cases**: Upload CSV/Excel with `input_` and `output_` column convention
- **Prompt Templates**: Reusable templates with `{column_name}` placeholders
- **Model Configuration**: Configure LLM endpoints (OpenAI, Azure, Anthropic, Local)
- **Test Runs**: Execute prompts against models asynchronously via Celery
- **Results**: View per-row results (prompt, response, latency, tokens)

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run migrations
make migrate

# Create superuser (for admin/login)
python manage.py createsuperuser

# Run server
make run
```

For async test runs, start Redis and Celery:

```bash
# Terminal 1: Redis
redis-server

# Terminal 2: Celery worker
celery -A config worker -l info

# Terminal 3: Django
make run
```

## CSV Format

Columns must be prefixed with:
- `input_` — fed into the prompt (e.g. `input_text`, `input_diagnosis`)
- `output_` — used for scoring (e.g. `output_label`, `output_expected`)

## Running Tests

```bash
make test
```

## Project Structure

```
config/          # Django project settings
core/            # Main app
  models.py      # TestCase, Version, Row, PromptTemplate, ModelConfig, TestRun, TestRunResult
  services/      # csv_parser, prompt_builder, llm_client, rate_limiter
  tasks.py       # Celery task for run execution
  views/         # Dashboard, test cases, prompts, models, runs
  templates/     # Django templates
```

## Docker

```bash
docker-compose up
```

Access the app at http://localhost:8000
