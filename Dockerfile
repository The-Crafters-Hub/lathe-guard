# LatheGuard Physical AI — Containerized Linux Runtime & Test Suite
# Evaluated on Debian 12 (Bookworm) / Python 3.11-slim

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CUDA_VISIBLE_DEVICES="-1"

WORKDIR /app

# Install minimal OS dependencies for OpenCV execution
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Default: Run full test suite across safety engine, failsafe matrix, and CraftTwin math
# Override example (headless demo):
#   docker run --rm lathe-guard python services/lathe_guard/run_lathe_guard.py --mock --no-nebius --duration 8
CMD ["python", "-m", "pytest", "services/"]

