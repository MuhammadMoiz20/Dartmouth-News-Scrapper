import os
import json
import requests
from bs4 import BeautifulSoup
import html2text
from fpdf import FPDF
from PIL import Image
from datetime import datetime
from dateutil import parser as dateparser
from datetime import timezone
import argparse
import sys
from tqdm import tqdm
import re
from urllib.parse import urljoin
import time
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
import io
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

class DartmouthNewsScraper:
    def __init__(self, start_date=None, end_date=None):
        """Initialize the scraper with optional date range.
        
        Args:
            start_date (str): Start date in YYYY-MM-DD format
            end_date (str): End date in YYYY-MM-DD format
        """
        self.base_url = "https://home.dartmouth.edu"
        self.api_url = f"{self.base_url}/jsonapi/node/article"
        
        # Convert input dates to UTC timezone-aware datetime objects
        if start_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            self.start_date = start_dt.replace(tzinfo=timezone.utc)
        else:
            self.start_date = None
            
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            # Set time to end of day for inclusive end date
            end_dt = end_dt.replace(hour=23, minute=59, second=59)
            self.end_date = end_dt.replace(tzinfo=timezone.utc)
        else:
            self.end_date = None
        
        # Set up robust session with retries
        self.session = requests.Session()
        retry_strategy = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        self.image_hashes = set()  # Store image hashes to prevent duplicates
        self.rate_limit_delay = 0.5  # Delay between requests in seconds
        
        # Set up logging
        logging.basicConfig(level=logging.INFO,
                          format='%(asctime)s - %(levelname)s - %(message)s',
                          handlers=[logging.FileHandler('scraper.log'),
                                   logging.StreamHandler()])
        self.logger = logging.getLogger(__name__)
        
        # Create directories if they don't exist
        os.makedirs('pdfs', exist_ok=True)
        os.makedirs('images', exist_ok=True)
        os.makedirs('json', exist_ok=True)  # New directory for JSON data

    def fetch_articles(self):
        """Fetch all articles within the specified date range."""
        print("Fetching articles from Dartmouth News...")
        if self.start_date and self.end_date:
            print(f"Date range: {self.start_date.strftime('%Y-%m-%d')} to {self.end_date.strftime('%Y-%m-%d')}")
        
        all_articles = []
        all_included = []
        page = 0
        articles_per_page = 50
        has_more = True
        
        while has_more:
            params = {
                'page[limit]': articles_per_page,
                'page[offset]': page * articles_per_page,
                'sort': '-created',
                'include': 'article_image'
            }
            
            print(f"\nFetching articles from: {self.api_url}")
            print(f"With parameters: {params}")
            
            try:
                response = self.session.get(self.api_url, params=params, verify=False)
                response.raise_for_status()
                data = response.json()
                
                articles = data.get('data', [])
                included = data.get('included', [])
                
                if not articles:
                    has_more = False
                    break
                
                # Filter articles by date range
                filtered_articles = []
                for article in articles:
                    created_date = article['attributes'].get('created', '')
                    if created_date:
                        try:
                            article_date = dateparser.parse(created_date)
                            if not article_date.tzinfo:
                                article_date = article_date.replace(tzinfo=timezone.utc)
                            
                            # Check if article is within date range
                            if self.start_date and article_date < self.start_date:
                                has_more = False
                                break
                            if self.end_date and article_date > self.end_date:
                                continue
                            if (not self.start_date or article_date >= self.start_date) and \
                               (not self.end_date or article_date <= self.end_date):
                                filtered_articles.append(article)
                        except (ValueError, TypeError) as e:
                            print(f"Error parsing date {created_date}: {str(e)}")
                            continue
                
                all_articles.extend(filtered_articles)
                all_included.extend(included)
                
                if len(articles) < articles_per_page:
                    has_more = False
                
                page += 1
                print(f"Found {len(filtered_articles)} articles in current page")
                print(f"Total articles collected: {len(all_articles)}")
                
            except requests.exceptions.RequestException as e:
                print(f"Error fetching articles: {str(e)}")
                break
        
        print(f"\nTotal articles found within date range: {len(all_articles)}")
        return all_articles, all_included

    def get_image_hash(self, image_path):
        """Generate a simple hash for an image based on its size and first few bytes"""
        try:
            with open(image_path, 'rb') as f:
                # Read first 1024 bytes for a quick content sample
                content = f.read(1024)
                file_size = os.path.getsize(image_path)
                return hash((file_size, content))
        except Exception as e:
            print(f"Error generating image hash: {str(e)}")
            return None

    def is_duplicate_image(self, image_path):
        """Check if an image is a duplicate based on its hash"""
        image_hash = self.get_image_hash(image_path)
        if image_hash is None:
            return False
        if image_hash in self.image_hashes:
            return True
        self.image_hashes.add(image_hash)
        return False

    def download_image(self, url, filename):
        """Download an image and save it to the images directory"""
        try:
            self.logger.info(f"Attempting to download image from: {url}")
            
            # Handle URLs with spaces
            url = url.replace(' ', '%20')
            
            # Add headers to mimic a browser request
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://home.dartmouth.edu/'
            }
            
            # Build list of URLs to try
            urls_to_try = set()
            
            # Add original URL with and without query parameters
            urls_to_try.add(url)
            base_url = url.split('?')[0] if '?' in url else url
            urls_to_try.add(base_url)
            
            # If URL contains /styles/, try different style variations
            if '/styles/' in url:
                # Extract the base path and filename
                base_path = url.split('/styles/')[0]
                filename_with_ext = url.split('/')[-1].split('?')[0]
                
                # Try different style variations
                styles = [
                    'max_width_2880px', 'max_width_1440px', 'max_width_1110px',
                    'max_width_720px', 'max_width_560px', '16_9_xl', '16_9_lg'
                ]
                
                for style in styles:
                    # Try with public/date path
                    if '/public/' in url:
                        style_url = f"{base_path}/styles/{style}/public/{url.split('/public/')[-1].split('?')[0]}"
                        urls_to_try.add(style_url)
                    
                    # Try without public/date path
                    style_url = f"{base_path}/styles/{style}/{filename_with_ext}"
                    urls_to_try.add(style_url)
            
            # Convert set to list and sort by resolution (highest first)
            urls_to_try = sorted(list(urls_to_try), 
                                key=lambda x: int(re.search(r'max_width_(\d+)px', x).group(1)) if re.search(r'max_width_(\d+)px', x) else 0,
                                reverse=True)
            
            last_error = None
            for try_url in urls_to_try:
                try:
                    self.logger.debug(f"Trying URL: {try_url}")
                    
                    # Apply rate limiting
                    time.sleep(self.rate_limit_delay)
                    
                    response = self.session.get(try_url, verify=False, headers=headers, timeout=15)
                    print(f"Response status code: {response.status_code}")
                    print(f"Response content type: {response.headers.get('content-type', 'unknown')}")
                    
                    if response.status_code == 200 and 'image' in response.headers.get('content-type', '').lower():
                        self.logger.info(f"Successfully downloaded image, saving to: {filename}")
                        
                        # Save image to a temporary file first
                        temp_filename = f"{filename}.temp"
                        try:
                            # Load image into memory first to validate it
                            image_data = io.BytesIO(response.content)
                            with Image.open(image_data) as img:
                                # Validate image dimensions
                                if img.size[0] < 10 or img.size[1] < 10:
                                    raise ValueError("Image dimensions too small")
                                
                                # Check image format
                                if img.format.lower() not in ['jpeg', 'jpg', 'png', 'gif', 'webp']:
                                    raise ValueError(f"Unsupported image format: {img.format}")
                                
                                self.logger.debug(f"Image validation passed: format={img.format}, size={img.size}, mode={img.mode}")
                                
                                # Convert to RGB if image is in RGBA mode
                                if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                                    self.logger.debug(f"Converting {img.mode} image to RGB")
                                    rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                                    if img.mode == 'P':
                                        img = img.convert('RGBA')
                                    rgb_img.paste(img, mask=img.split()[3] if img.mode == 'RGBA' else None)
                                    img = rgb_img
                                
                                # Resize image if needed
                                max_width = 1200  # Maximum width for PDF
                                if img.size[0] > max_width:
                                    ratio = max_width / img.size[0]
                                    new_size = (max_width, int(img.size[1] * ratio))
                                    self.logger.debug(f"Resizing image from {img.size} to {new_size}")
                                    img = img.resize(new_size, Image.Resampling.LANCZOS)
                                
                                # Convert to RGB and save as JPEG
                                img = img.convert('RGB')
                                img.save(temp_filename, 'JPEG', quality=95)
                                
                                # Check if it's a duplicate
                                if self.is_duplicate_image(temp_filename):
                                    self.logger.info(f"Skipping duplicate image: {try_url}")
                                    os.remove(temp_filename)
                                    return None
                                
                                # If not a duplicate, move to final location
                                os.rename(temp_filename, filename)
                                self.logger.info(f"Successfully saved image to: {filename}")
                                return filename
                                
                        except Exception as img_error:
                            self.logger.warning(f"Error processing image from {try_url}: {str(img_error)}")
                            if os.path.exists(temp_filename):
                                os.remove(temp_filename)
                            last_error = img_error
                            continue
                    else:
                        self.logger.debug(f"Skipping non-image response: {response.headers.get('content-type', 'unknown')}")
                        
                except requests.exceptions.RequestException as e:
                    self.logger.warning(f"Request error for {try_url}: {str(e)}")
                    last_error = e
                    continue
                except Exception as e:
                    self.logger.error(f"Unexpected error with {try_url}: {str(e)}")
                    last_error = e
                    continue
            
            self.logger.error(f"All URL variations failed. Last error: {str(last_error)}")
            return None
            
        except Exception as e:
            self.logger.error(f"Error in download_image: {str(e)}")
            if os.path.exists(f"{filename}.temp"):
                os.remove(f"{filename}.temp")
            return None

    def get_drupal_media_url(self, media_uuid, included_data):
        """Extract image URL from Drupal media data"""
        for item in included_data:
            if item.get('id') == media_uuid:
                if item.get('type') == 'media--image':
                    # Get the image URL from the media item
                    image_url = item.get('attributes', {}).get('field_media_image', {}).get('uri', {}).get('url')
                    if image_url:
                        return urljoin('https://home.dartmouth.edu', image_url)
        return None

    def extract_image_urls(self, article_data):
        """Extract image URLs from article content."""
        unique_images = []
        seen_base_urls = set()  # Track base URLs to avoid duplicates
        
        def get_base_url(url):
            # Remove style parameters and query strings to get base URL
            base = url.split('/styles/')[0] if '/styles/' in url else url.split('?')[0]
            return base.rstrip('/')
        
        def add_image_url(url):
            if not url:
                return
            if not isinstance(url, str):
                return
            
            # Make sure URL is absolute
            if url.startswith('/'):
                url = f"https://home.dartmouth.edu{url}"
            elif not url.startswith('http'):
                return
            
            base_url = get_base_url(url)
            if base_url not in seen_base_urls:
                seen_base_urls.add(base_url)
                # Prefer high-res version if available
                if '/styles/' in url:
                    high_res_url = f"{base_url}/styles/max_width_2880px/{url.split('/')[-1]}"
                    if '?' in high_res_url:
                        high_res_url = high_res_url.split('?')[0]
                    unique_images.append(high_res_url)
                else:
                    unique_images.append(url)
        
        def extract_urls_from_html(html_content):
            if not html_content:
                return
            
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Find all img tags
            for img in soup.find_all('img'):
                # Try all URLs from srcset
                srcset = img.get('srcset', '')
                if srcset:
                    try:
                        # Split srcset into individual sources
                        sources = [s.strip() for s in srcset.split(',')]
                        for source in sources:
                            # Split each source into URL and width
                            parts = source.split()
                            if len(parts) >= 1:
                                url = parts[0].strip('"')
                                # Clean up the URL
                                if url.startswith('https://') or url.startswith('http://'):
                                    add_image_url(url)
                                elif url.startswith('//'):
                                    add_image_url('https:' + url)
                                elif url.startswith('/'):
                                    add_image_url('https://home.dartmouth.edu' + url)
                                else:
                                    add_image_url('https://home.dartmouth.edu/' + url)
                    except Exception as e:
                        print(f"Error processing srcset: {e}")
                
                # Also try src attribute as fallback
                src = img.get('src')
                if src:
                    if src.startswith('https://') or src.startswith('http://'):
                        add_image_url(src)
                    elif src.startswith('//'):
                        add_image_url('https:' + src)
                    elif src.startswith('/'):
                        add_image_url('https://home.dartmouth.edu' + src)
                    else:
                        add_image_url('https://home.dartmouth.edu/' + src)
            
            # Find all meta tags with image properties
            meta_tags = soup.find_all('meta', {'property': ['og:image', 'twitter:image']})
            for tag in meta_tags:
                content = tag.get('content')
                if content:
                    add_image_url(content)
        
        def extract_urls_from_json(data):
            if isinstance(data, dict):
                for key, value in data.items():
                    # Check if the key suggests it might contain an image URL
                    if any(img_key in key.lower() for img_key in ['image', 'img', 'photo', 'thumbnail', 'icon', 'avatar']):
                        if isinstance(value, str):
                            if any(ext in value.lower() for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                                add_image_url(value)
                        elif isinstance(value, dict):
                            # Check for common Drupal image URL patterns
                            uri = value.get('uri', {}).get('url') if isinstance(value.get('uri'), dict) else value.get('url')
                            if uri:
                                add_image_url(uri)
                    
                    # Check if value contains HTML content
                    if isinstance(value, str) and ('<img' in value.lower() or '<article' in value.lower()):
                        extract_urls_from_html(value)
                    
                    # Recursively check nested structures
                    extract_urls_from_json(value)
            
            elif isinstance(data, list):
                for item in data:
                    extract_urls_from_json(item)
        
        print("\nPerforming deep scan for images in article data...")
        extract_urls_from_json(article_data)
        
        print("\nExtracting images from HTML content...")
        html_content = article_data.get('html_dcrs_repo', '')
        extract_urls_from_html(html_content)
        
        print(f"\nFound {len(unique_images)} unique images")
        return unique_images

    def clean_text(self, text):
        """Clean text by replacing problematic characters with their closest ASCII equivalents."""
        replacements = {
            ''': "'",
            ''': "'",
            '"': '"',
            '"': '"',
            '—': '-',
            '–': '-',
            '…': '...',
            'ā': 'a',
            'ē': 'e',
            'ī': 'i',
            'ō': 'o',
            'ū': 'u',
            '•': '*',
            '©': '(c)',
            '®': '(R)',
            '™': '(TM)',
            '\u200b': '',  # Zero-width space
            '\xa0': ' ',   # Non-breaking space
        }
        
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

    def create_pdf(self, article_data, image_files):
        """Create a PDF document for the article if it has at least 50 words."""
        # Check word count in article body
        if article_data.get('article_body'):
            body_text = BeautifulSoup(article_data['article_body']['value'], 'html.parser').get_text()
            word_count = len(body_text.split())
            if word_count < 50:
                print(f"Skipping PDF creation - article has only {word_count} words (minimum 50 required)")
                return None
        class PDFWithHeaderFooter(FPDF):
            def footer(self):
                # Position cursor at 15mm from bottom
                self.set_y(-25)
                
                # Set font for footer
                self.set_font('DejaVu', '', 8)
                
                # Add page number
                self.cell(0, 10, f'Page {self.page_no()}', 0, 1, 'C')
                
                # Add footnote
                footnote = 'Rauner Special Collections. Dartmouth College, Office of Communications records (DA-29). Copyright © Trustees of Dartmouth College'
                self.multi_cell(0, 5, footnote, 0, 'C')
        
        print("\nCreating PDF document...")
        pdf = PDFWithHeaderFooter()
        # Add a Unicode font
        pdf.add_font('DejaVu', '', os.path.join('fonts', 'Arial.ttf'), uni=True)
        pdf.add_page()
        
        # Set up fonts - use Unicode-compatible font
        pdf.set_font('DejaVu', '', 20)
        
        # Add header
        pdf.cell(0, 10, 'DARTMOUTH NEWS', 0, 1, 'C')
        pdf.ln(10)
        
        # Add title
        title = self.clean_text(article_data['title'])
        pdf.set_font('DejaVu', '', 16)
        pdf.multi_cell(0, 10, title, 0, 'C')
        pdf.ln(5)
        
        # Add author and date
        pdf.set_font('DejaVu', '', 12)
        author = self.clean_text(article_data.get('news_author', 'Unknown Author'))
        
        # Parse and format the date
        news_date = article_data.get('created', '')  # Try created date first
        if not news_date:
            news_date = article_data.get('news_date', '')  # Fall back to news_date
        
        if news_date:
            try:
                # Handle both ISO format and other common formats
                date_obj = dateparser.parse(news_date)
                formatted_date = date_obj.strftime('%B %d, %Y')
            except (ValueError, TypeError) as e:
                print(f"Error parsing date {news_date}: {str(e)}")
                formatted_date = 'Unknown Date'
        else:
            formatted_date = 'Unknown Date'
        
        pdf.cell(0, 10, f"By {author}", 0, 1, 'C')
        pdf.cell(0, 10, formatted_date, 0, 1, 'C')
        pdf.ln(10)
        
        # Add subtitle if available
        if article_data.get('news_subtitle'):
            subtitle = self.clean_text(BeautifulSoup(article_data['news_subtitle']['value'], 'html.parser').get_text())
            pdf.set_font('DejaVu', 'I', 12)
            pdf.multi_cell(0, 10, subtitle, 0, 'C')
            pdf.ln(10)
        
        # Add images
        print(f"\nProcessing {len(image_files)} images for PDF...")
        if image_files:
            for i, image_path in enumerate(image_files):
                try:
                    print(f"\nProcessing image {i+1}/{len(image_files)}: {image_path}")
                    if not os.path.exists(image_path):
                        print(f"Image file not found: {image_path}")
                        continue
                        
                    img = Image.open(image_path)
                    print(f"Image opened successfully: format={img.format}, size={img.size}, mode={img.mode}")
                    
                    # Calculate aspect ratio
                    aspect = img.width / img.height
                    
                    # Set maximum width and height
                    max_width = 190
                    max_height = 120
                    
                    # Calculate dimensions while maintaining aspect ratio
                    if aspect > max_width/max_height:  # Width is the limiting factor
                        width = max_width
                        height = width / aspect
                    else:  # Height is the limiting factor
                        height = max_height
                        width = height * aspect
                    
                    print(f"Calculated dimensions: width={width:.2f}, height={height:.2f}")
                    
                    # Center the image
                    x = (210 - width) / 2
                    
                    # Add image to PDF
                    print("Adding image to PDF...")
                    pdf.image(image_path, x=x, w=width)
                    pdf.ln(5)
                    print("Image added successfully")
                    
                    # Add image caption if available
                    caption = None
                    if i == 0 and 'media_image_caption' in article_data:
                        caption = self.clean_text(BeautifulSoup(article_data['media_image_caption'], 'html.parser').get_text())
                    elif 'image_captions' in article_data and i < len(article_data['image_captions']):
                        caption = self.clean_text(BeautifulSoup(article_data['image_captions'][i], 'html.parser').get_text())
                    
                    if caption:
                        print(f"Adding caption: {caption[:50]}...")
                        pdf.set_font('DejaVu', 'I', 10)
                        pdf.multi_cell(0, 5, caption, 0, 'C')
                        pdf.ln(5)
                    
                except Exception as e:
                    print(f"Error processing image {image_path}: {str(e)}")
                    import traceback
                    traceback.print_exc()
            pdf.ln(10)
        
        # Add body content
        if article_data.get('article_body'):
            body_text = self.clean_text(BeautifulSoup(article_data['article_body']['value'], 'html.parser').get_text())
            pdf.set_font('DejaVu', '', 12)
            pdf.multi_cell(0, 10, body_text)
        
        # Generate PDF filename with date prefix
        if news_date:
            try:
                date_prefix = dateparser.parse(news_date).strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                date_prefix = "no-date"
        else:
            date_prefix = "no-date"
            
        safe_title = "".join(x for x in title if x.isalnum() or x in (' ', '-', '_')).rstrip()
        filename = f"pdfs/{date_prefix}_{safe_title[:50]}.pdf"
        
        try:
            pdf.output(filename)
            print(f"Successfully created PDF: {filename}")
            return filename
        except Exception as e:
            print(f"Error creating PDF: {str(e)}")
            return None

    def wrap_text(self, text, pdf, max_width):
        """Wrap text to fit within a specified width
        
        Args:
            text (str): Text to wrap
            pdf (FPDF): PDF object to use for string width calculation
            max_width (float): Maximum width in mm
            
        Returns:
            list: List of wrapped lines
        """
        lines = []
        words = text.split()
        current_line = []
        
        for word in words:
            current_line.append(word)
            line = ' '.join(current_line)
            width = pdf.get_string_width(line)
            
            if width > max_width:
                if len(current_line) == 1:
                    lines.append(line)
                    current_line = []
                else:
                    current_line.pop()
                    lines.append(' '.join(current_line))
                    current_line = [word]
        
        if current_line:
            lines.append(' '.join(current_line))
        
        return lines

    def process_article(self, article, included_data):
        """Process a single article: extract images and create PDF"""
        try:
            article_data = {
                'title': article['attributes']['title'],
                'created': article['attributes']['created'],
                'news_date': article['attributes'].get('news_date', ''),
                'news_author': article['attributes'].get('news_author', 'Unknown Author'),
                'article_body': article['attributes']['article_body'],
                'html_dcrs_repo': article['attributes']['html_dcrs_repo'],
                'image_captions': []
            }

            # Extract article_main_image_url if present
            if 'article_main_image_url' in article['attributes']:
                img_url = article['attributes']['article_main_image_url']
                if img_url:
                    img_url = urljoin('https://home.dartmouth.edu', img_url)
                    print(f"Found main image URL from attributes: {img_url}")
                    article_data['article_main_image_url'] = img_url

            print(f"\nProcessing article: {article_data['title']}")

            # Extract image URLs and captions from HTML content and included data
            image_urls = self.extract_image_urls(article_data)
            
            # Extract captions from figure elements
            soup = BeautifulSoup(article_data['html_dcrs_repo'], 'html.parser')
            figures = soup.find_all('figure')
            for figure in figures:
                figcaption = figure.find('figcaption')
                if figcaption:
                    article_data['image_captions'].append(figcaption.get_text())
                else:
                    article_data['image_captions'].append('')
            
            # Add article_main_image_url if present and not already in image_urls
            if 'article_main_image_url' in article_data:
                if article_data['article_main_image_url'] not in image_urls:
                    image_urls.insert(0, article_data['article_main_image_url'])

            # Download images
            print(f"Found {len(image_urls)} images in article")
            image_files = []
            skipped_count = 0
            
            for i, url in enumerate(image_urls):
                filename = os.path.join('images', f"{i}_{os.path.basename(url)}")
                downloaded_file = self.download_image(url, filename)
                
                if downloaded_file:
                    image_files.append(downloaded_file)
                else:
                    skipped_count += 1
                    print(f"Skipped image {url} (duplicate or download failed)")

            print(f"Successfully downloaded {len(image_files)} images, skipped {skipped_count} duplicates/failures")

            # Create PDF
            return self.create_pdf(article_data, image_files)
        except Exception as e:
            print(f"Error processing article: {str(e)}")
            return None

    def run(self):
        """Run the scraper to fetch articles and create PDFs."""
        articles, included = self.fetch_articles()
        
        if not articles:
            print("No articles found within the specified date range.")
            return
        
        processed_count = 0
        total_articles = len(articles)
        
        with tqdm(total=total_articles, desc="Processing articles") as pbar:
            for article in articles:
                try:
                    # Save JSON data
                    created_date = article['attributes'].get('created', '')
                    if created_date:
                        date_obj = dateparser.parse(created_date)
                        date_str = date_obj.strftime('%Y-%m-%d')
                    else:
                        date_str = 'unknown_date'
                    
                    title = article['attributes'].get('title', '').replace('/', '-')[:50]  # Truncate long titles
                    json_filename = f"{date_str}_{title}.json"
                    json_path = os.path.join('json', json_filename)
                    
                    with open(json_path, 'w', encoding='utf-8') as f:
                        json.dump(article, f, indent=2, ensure_ascii=False)
                    
                    # Create PDF as before
                    self.process_article(article, included)
                    processed_count += 1
                    pbar.update(1)
                except Exception as e:
                    print(f"Error processing article: {str(e)}")
                    pbar.update(1)
                    continue
        
        print(f"\nProcessed {processed_count} articles successfully!")
        print("PDFs are saved in the 'pdfs' directory")
        print("Images are saved in the 'images' directory")
        print("JSON data is saved in the 'json' directory")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Scrape Dartmouth News articles within a date range')
    parser.add_argument('--start-date', type=str, help='Start date (inclusive) in YYYY-MM-DD format')
    parser.add_argument('--end-date', type=str, help='End date (inclusive) in YYYY-MM-DD format')
    
    args = parser.parse_args()
    
    # Validate date formats
    if args.start_date or args.end_date:
        date_format = "%Y-%m-%d"
        try:
            if args.start_date:
                datetime.strptime(args.start_date, date_format)
            if args.end_date:
                datetime.strptime(args.end_date, date_format)
                
            if args.start_date and args.end_date:
                start = datetime.strptime(args.start_date, date_format)
                end = datetime.strptime(args.end_date, date_format)
                if start > end:
                    print("Error: Start date must be before end date")
                    sys.exit(1)
                
        except ValueError:
            print("Error: Dates must be in YYYY-MM-DD format (e.g., 2024-12-31)")
            sys.exit(1)
    
    scraper = DartmouthNewsScraper(args.start_date, args.end_date)
    scraper.run()
