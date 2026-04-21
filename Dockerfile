# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Ensure static and media directories exist
RUN mkdir -p staticfiles media

# Collect static files
# Note: SECRET_KEY is required for collectstatic. Using a dummy if not provided.
RUN DJANGO_SECRET_KEY=collectstatic-dummy python manage.py collectstatic --noinput

# Expose the port Daphne will run on (using 9142 as a non-standard port)
EXPOSE 9142

# Start Daphne
CMD ["daphne", "-b", "0.0.0.0", "-p", "9142", "chatbox_vi_pham.asgi:application"]
