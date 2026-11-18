# One image for the three Python programs: the meter, the controller (with the API)
# and the chargers. docker-compose.yml starts it three times with different commands.

# Start from a small official Python image (Debian Linux with Python 3.14 installed).
FROM python:3.14.6-slim

# Print log lines at once instead of keeping them in a buffer, so `docker compose logs`
# shows them as they happen.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install the Python packages first, in their own layer. Docker keeps finished layers,
# so changing the code later does not reinstall all packages.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then the project itself. .dockerignore keeps out .env (secrets), .venv and .git.
COPY settings.py .
COPY src ./src
COPY data ./data

# Without a command from docker-compose.yml, the container starts the controller.
CMD ["python", "-m", "src.controller.run_controller"]
