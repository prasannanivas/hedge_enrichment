FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source and data
COPY agent.py prepare_input.py entrypoint.sh ./
COPY all_managers.csv ./

# Output dir — mount a volume here to persist results outside the container
RUN mkdir -p /data && chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
