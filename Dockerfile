FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source and assets
COPY app.py .
COPY providers/ providers/
COPY resolvers/ resolvers/
COPY templates/ templates/
COPY assets/ assets/

EXPOSE 5099

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5099", "--access-logfile", "-", "app:app"]
