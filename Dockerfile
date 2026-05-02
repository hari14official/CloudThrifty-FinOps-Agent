# Use a lightweight Python base image
FROM python:3.9-slim

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.local/bin:${PATH}"

# Set the working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    unzip \
    wget \
    gpg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install AWS CLI v2
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" \
    && unzip awscliv2.zip \
    && ./aws/install \
    && rm -rf aws awscliv2.zip

# Install Terraform (latest via official HashiCorp repository)
RUN wget -O- https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(awk -F= '/^VERSION_CODENAME=/{print$2}' /etc/os-release) main" > /etc/apt/sources.list.d/hashicorp.list \
    && apt-get update && apt-get install -y --no-install-recommends terraform \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user and group for security
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser

# Ensure the appuser owns the working directory
RUN chown -R appuser:appuser /app

# Switch to the non-root user
USER appuser

# Install Python dependencies (boto3)
# We install with --user since we are running as a non-root user
RUN pip install --no-cache-dir --user boto3

# Copy the project files into the container, ensuring correct permissions
COPY --chown=appuser:appuser . /app/

# Default command placeholder (can be overridden at runtime)
CMD ["python", "src/waste_hunter.py"]
