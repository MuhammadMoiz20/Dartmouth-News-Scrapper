# Dartmouth News Scraper

A Python-based tool for scraping articles from Dartmouth News with date range filtering, PDF generation, and data preservation. The scraper intelligently processes articles, creating PDFs only for substantial content (50+ words) while maintaining high-quality image integration.

## Features
- Date range filtering (YYYY-MM-DD format)
- Smart PDF generation with minimum word count threshold (50+ words)
- Automatic image integration with captions
- JSON data preservation for raw article data
- Duplicate image detection and prevention
- Robust error handling and retry mechanisms
- Progress tracking with tqdm
- Rate limiting to respect server resources

## Installation
```bash
pip install -r requirements.txt
```

## Usage
```bash
python news_scraper.py --start-date 2024-01-01 --end-date 2024-12-31
```

## Output Structure
```
project_root/
├── pdfs/          # Generated PDF articles
├── images/        # Downloaded article images
├── json/         # Raw JSON article data
├── docs/          # Documentation files
├── news_scraper.py # Main application
└── requirements.txt
```

## Documentation
Full documentation available in the [docs directory](docs/).
