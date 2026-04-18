FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project
COPY . .

# Ensure runtime directories exist
RUN mkdir -p memory reports

# Make the package importable
ENV PYTHONPATH=/app

# Run as non-root for security
RUN useradd -m -u 1000 kubeagent && chown -R kubeagent:kubeagent /app
USER kubeagent

CMD ["python", "-m", "kubeagent.agent.core"]
