FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source and assets
COPY app.py .
COPY providers/ providers/
COPY templates/ templates/
COPY assets/ assets/

EXPOSE 5099

CMD ["python", "app.py"]
