FROM python:3.11

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# --with-deps installs all required system libraries automatically
RUN playwright install --with-deps chromium

COPY MD6_Scraper.py .

# Port the scraper listens on
EXPOSE 8765

CMD ["python", "MD6_Scraper.py"]
