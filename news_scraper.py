import os
import json
import requests
from bs4 import BeautifulSoup
from fpdf import FPDF
from PIL import Image
from datetime import datetime, timezone
from dateutil import parser as dateparser
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
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed

# Suppress InsecureRequestWarning
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[logging.FileHandler("scraper.log"), logging.StreamHandler()]
        )
        self.logger = logging.getLogger(__name__)

        # Create directories if they don't exist
        os.makedirs("pdfs", exist_ok=True)
        os.makedirs("images", exist_ok=True)
        os.makedirs("json", exist_ok=True)
        os.makedirs("fonts", exist_ok=True)

    def fetch_articles(self):
        """Fetch all articles within the specified date range using efficient server-side filtering."""
        print("Fetching articles from Dartmouth News...")

        if self.start_date and self.end_date:
            print(f"Date range: {self.start_date.strftime('%Y-%m-%d')} to {self.end_date.strftime('%Y-%m-%d')}")

        all_articles = []
        all_included = []
        page = 0
        articles_per_page = 50
        has_more = True

        start_timestamp = int(self.start_date.timestamp()) if self.start_date else None
        end_timestamp = int(self.end_date.timestamp()) if self.end_date else None

        while has_more:
            params = {
                "page[limit]": articles_per_page,
                "page[offset]": page * articles_per_page,
                "sort": "-created",  # Sort newest first
                "include": "article_image",
            }

            if start_timestamp and end_timestamp:
                params.update({
                    "filter[created][condition][path]": "created",
                    "filter[created][condition][operator]": "BETWEEN",
                    "filter[created][condition][value][0]": start_timestamp,
                    "filter[created][condition][value][1]": end_timestamp,
                })

            params["filter[status][value]"] = 1

            print(f"\nFetching articles from: {self.api_url}")
            print(f"With parameters: {params}")

            try:
                response = self.session.get(self.api_url, params=params, verify=False)
                response.raise_for_status()
                data = response.json()

                articles = data.get("data", [])
                included = data.get("included", [])

                if not articles:
                    has_more = False
                    break

                all_articles.extend(articles)
                all_included.extend(included)

                if len(articles) < articles_per_page:
                    has_more = False

                page += 1
                print(f"Fetched {len(articles)} articles in current page")
                print(f"Total articles collected: {len(all_articles)}")

                time.sleep(self.rate_limit_delay)
            except requests.exceptions.RequestException as e:
                print(f"Error fetching articles: {str(e)}")
                break

        print(f"\nTotal articles found within date range: {len(all_articles)}")
        return all_articles, all_included

    def get_image_hash(self, image_path):
        """Generate a simple hash for an image based on its size and first few bytes."""
        try:
            with open(image_path, "rb") as f:
                content = f.read(1024)
                file_size = os.path.getsize(image_path)
                return hash((file_size, content))
        except Exception as e:
            print(f"Error generating image hash: {str(e)}")
            return None

    def is_duplicate_image(self, image_path):
        """Check if an image is a duplicate based on its hash."""
        image_hash = self.get_image_hash(image_path)
        if image_hash is None:
            return False
        if image_hash in self.image_hashes:
            return True
        self.image_hashes.add(image_hash)
        return False

    def get_image_resolution(self, image_path):
        """Get the resolution (width x height) of an image."""
        try:
            with Image.open(image_path) as img:
                width, height = img.size
                resolution = width * height
                return resolution
        except Exception as e:
            self.logger.warning(f"Error getting image resolution: {str(e)}")
            return 0
            
    def download_image(self, url, filename):
        """Download an image and save it to the images directory."""
        try:
            self.logger.info(f"Attempting to download image from: {url}")
            url = url.replace(" ", "%20")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://home.dartmouth.edu/"
            }

            # Check if the URL is valid
            if not url.startswith("http"):
                self.logger.warning(f"Invalid URL: {url}")
                return None

            response = self.session.get(url, verify=False, headers=headers, timeout=15)
            print(f"Response status code: {response.status_code}")
            print(f"Response content type: {response.headers.get('content-type', 'unknown')}")

            if response.status_code == 200 and "image" in response.headers.get("content-type", "").lower():
                self.logger.info(f"Successfully downloaded image, saving to: {filename}")
                temp_filename = f"{filename}.temp"
                try:
                    image_data = io.BytesIO(response.content)
                    with Image.open(image_data) as img:
                        if img.size[0] < 10 or img.size[1] < 10:
                            raise ValueError("Image dimensions too small")
                        if img.format.lower() not in ["jpeg", "jpg", "png", "gif", "webp"]:
                            raise ValueError(f"Unsupported image format: {img.format}")

                        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                            rgb_img = Image.new("RGB", img.size, (255, 255, 255))
                            if img.mode == "P":
                                img = img.convert("RGBA")
                            rgb_img.paste(img, mask=img.split()[3] if img.mode == "RGBA" else None)
                            img = rgb_img

                        max_width = 1200  # Maximum width for PDF
                        if img.size[0] > max_width:
                            ratio = max_width / img.size[0]
                            new_size = (max_width, int(img.size[1] * ratio))
                            img = img.resize(new_size, Image.Resampling.LANCZOS)

                        img = img.convert("RGB")
                        img.save(temp_filename, "JPEG", quality=95)

                        if self.is_duplicate_image(temp_filename):
                            self.logger.info(f"Skipping duplicate image: {url}")
                            os.remove(temp_filename)
                            return None

                        os.rename(temp_filename, filename)
                        self.logger.info(f"Successfully saved image to: {filename}")
                        return filename

                except Exception as img_error:
                    self.logger.warning(f"Error processing image from {url}: {str(img_error)}")
                    if os.path.exists(temp_filename):
                        os.remove(temp_filename)
                    return None
            else:
                self.logger.debug(f"Skipping non-image response: {response.headers.get('content-type', 'unknown')}")
                return None

        except Exception as e:
            self.logger.error(f"Error in download_image: {str(e)}")
            if os.path.exists(f"{filename}.temp"):
                os.remove(f"{filename}.temp")
            return None

    def extract_image_urls(self, article_data):
        """
        Extract image URLs only from the article's meta tags.
        This method looks for meta tags with the property "og:image".
        """
        unique_images = []
        metatag = article_data.get("metatag")
        if metatag:
            # Ensure we work with a list
            if isinstance(metatag, dict):
                metatag = [metatag]
            for tag in metatag:
                if tag.get("tag") == "meta":
                    attributes = tag.get("attributes", {})
                    if attributes.get("property") == "og:image":
                        content = attributes.get("content")
                        if content:
                            unique_images.append(content)
        print(f"\nFound {len(unique_images)} image(s) via meta tags")
        return unique_images
        
    def extract_images_from_html(self, article_data):
        """
        Extract image URLs from the article body HTML content.
        This handles both absolute and relative image URLs.
        """
        unique_images = []
        if not article_data.get("article_body") or not article_data["article_body"].get("value"):
            return unique_images
            
        html_content = article_data["article_body"]["value"]
        # Also check processed content if available
        if article_data["article_body"].get("processed"):
            html_content += article_data["article_body"]["processed"]
            
        soup = BeautifulSoup(html_content, "html.parser")
        
        # Find all img tags
        for img in soup.find_all("img"):
            src = img.get("src")
            if src:
                # Handle relative URLs
                if src.startswith("/"):
                    src = urljoin(self.base_url, src)
                unique_images.append(src)
        
        # Find all srcset attributes and extract URLs
        for tag in soup.find_all(lambda tag: tag.has_attr("srcset")):
            srcset = tag.get("srcset")
            if srcset:
                # Extract URLs from srcset (format: "url1 1x, url2 2x, ...") 
                for src_item in srcset.split(","):
                    src = src_item.strip().split(" ")[0]
                    if src:
                        if src.startswith("/"):
                            src = urljoin(self.base_url, src)
                        unique_images.append(src)
        
        # Find all data-entity-uuid attributes in drupal-media tags
        for media_tag in soup.find_all("drupal-media"):
            uuid = media_tag.get("data-entity-uuid")
            jsonapi_url = media_tag.get("data-entity-jsonapi-url")
            
            if jsonapi_url:
                # If the JSON API URL is provided, use it directly
                try:
                    response = self.session.get(jsonapi_url, verify=False)
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("field_media_image") and data["field_media_image"].get("uri", {}).get("url"):
                            img_url = data["field_media_image"]["uri"]["url"]
                            if img_url.startswith("/"):
                                img_url = urljoin(self.base_url, img_url)
                            unique_images.append(img_url)
                except Exception as e:
                    self.logger.warning(f"Error fetching media JSON: {str(e)}")
        
        # Also look for images in div tags with style attributes containing background-image
        for div in soup.find_all("div", style=True):
            style = div.get("style", "")
            if "background-image" in style:
                # Extract URL from background-image: url('...')
                match = re.search(r"background-image:\s*url\(['\"]{0,1}([^'\"\)]+)['\"]{0,1}\)", style)
                if match:
                    src = match.group(1)
                    if src.startswith("/"):
                        src = urljoin(self.base_url, src)
                    unique_images.append(src)
        
        # Filter out common non-image URLs and icon placeholders
        filtered_images = []
        for img_url in unique_images:
            # Skip icon placeholders and non-image URLs
            if any(x in img_url.lower() for x in [
                "image-x-generic.png", 
                "default/image-", 
                "icon-", 
                "placeholder", 
                "transparent.gif",
                "blank.gif"
            ]):
                continue
            filtered_images.append(img_url)
        
        print(f"Found {len(filtered_images)} image(s) in article body HTML")
        return filtered_images

    def clean_text(self, text):
        """Clean text by replacing problematic characters with their closest ASCII equivalents."""
        if text is None:
            return ""
        
        replacements = {
            "’": "'",
            "‘": "'",
            "“": '"',
            "”": '"',
            "—": "-",
            "–": "-",
            "…": "...",
            "ā": "a",
            "ē": "e",
            "ī": "i",
            "ō": "o",
            "ū": "u",
            "•": "*",
            "©": "(c)",
            "®": "(R)",
            "™": "(TM)",
            "\u200b": "",
            "\xa0": " ",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return text

    def create_pdf(self, article_data, image_files):
        """Create a PDF document for the article if it has at least 50 words."""
        if article_data.get("article_body"):
            body_text = BeautifulSoup(article_data["article_body"]["value"], "html.parser").get_text()
            word_count = len(body_text.split())
            if word_count < 50:
                print(f"Skipping PDF creation - article has only {word_count} words (minimum 50 required)")
                return None

        class PDFWithHeaderFooter(FPDF):
            def footer(self):
                self.set_y(-25)
                self.set_font("Helvetica", "", 8)
                self.cell(0, 10, f"Page {self.page_no()}", 0, 1, "C")
                footnote = (
                    "Rauner Special Collections. Dartmouth College, Office of Communications records (DA-29). "
                    "Copyright Trustees of Dartmouth College"
                )
                self.multi_cell(0, 5, footnote, 0, "C")

        print("\nCreating PDF document...")
        pdf = PDFWithHeaderFooter()
        pdf.set_font("Helvetica", "", 14)
        pdf.add_page()

        pdf.set_font("Helvetica", "", 20)
        pdf.cell(0, 10, "DARTMOUTH NEWS", 0, 1, "C")
        pdf.ln(10)

        title = self.clean_text(article_data["title"])
        pdf.set_font("Helvetica", "", 16)
        pdf.multi_cell(0, 10, title, 0, "C")
        pdf.ln(5)

        pdf.set_font("Helvetica", "", 12)
        author = self.clean_text(article_data.get("news_author", "Unknown Author"))

        news_date = article_data.get("created", "") or article_data.get("news_date", "")
        if news_date:
            try:
                date_obj = dateparser.parse(news_date)
                formatted_date = date_obj.strftime("%B %d, %Y")
            except (ValueError, TypeError):
                formatted_date = "Unknown Date"
        else:
            formatted_date = "Unknown Date"

        pdf.cell(0, 10, f"By {author}", 0, 1, "C")
        pdf.cell(0, 10, formatted_date, 0, 1, "C")
        pdf.ln(10)

        if article_data.get("news_subtitle"):
            subtitle = self.clean_text(
                BeautifulSoup(article_data["news_subtitle"]["value"], "html.parser").get_text()
            )
            pdf.set_font("Helvetica", "I", 12)
            pdf.multi_cell(0, 10, subtitle, 0, "C")
            pdf.ln(10)

        # Use only the highest resolution image for the PDF
        print(f"\nProcessing image for PDF...")
        if image_files:
            # Find the image with the highest resolution
            highest_res_image = None
            highest_resolution = 0
            
            for image_path in image_files:
                if not os.path.exists(image_path):
                    print(f"Image file not found: {image_path}")
                    continue
                    
                resolution = self.get_image_resolution(image_path)
                if resolution > highest_resolution:
                    highest_resolution = resolution
                    highest_res_image = image_path
            
            if highest_res_image:
                try:
                    print(f"Using highest resolution image: {highest_res_image}")
                    img = Image.open(highest_res_image)
                    aspect = img.width / img.height
                    max_width = 190
                    max_height = 120

                    if aspect > max_width / max_height:
                        width = max_width
                        height = width / aspect
                    else:
                        height = max_height
                        width = height * aspect

                    x = (210 - width) / 2
                    pdf.image(highest_res_image, x=x, w=width)
                    pdf.ln(5)

                    # Try to find a caption for the image
                    caption = None
                    if "media_image_caption" in article_data:
                        caption = self.clean_text(
                            BeautifulSoup(article_data["media_image_caption"], "html.parser").get_text()
                        )
                    elif "image_captions" in article_data and len(article_data["image_captions"]) > 0:
                        caption = self.clean_text(
                            BeautifulSoup(article_data["image_captions"][0], "html.parser").get_text()
                        )

                    if caption:
                        pdf.set_font("Helvetica", "I", 10)
                        pdf.multi_cell(0, 5, caption, 0, "C")
                        pdf.ln(5)

                except Exception as e:
                    print(f"Error processing image {highest_res_image}: {str(e)}")
            pdf.ln(10)

        if article_data.get("article_body"):
            body_text = self.clean_text(
                BeautifulSoup(article_data["article_body"]["value"], "html.parser").get_text()
            )
            pdf.set_font("Helvetica", "", 12)
            pdf.multi_cell(0, 10, body_text)

        if news_date:
            try:
                date_prefix = dateparser.parse(news_date).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                date_prefix = "no-date"
        else:
            date_prefix = "no-date"

        safe_title = "".join(x for x in title if x.isalnum() or x in (" ", "-", "_")).rstrip()
        filename = f"pdfs/{date_prefix}_{safe_title[:50]}.pdf"

        try:
            pdf.output(filename)
            print(f"Successfully created PDF: {filename}")
            return filename
        except Exception as e:
            print(f"Error creating PDF: {str(e)}")
            return None

    def process_article(self, article, included_data):
        """Process a single article: extract meta image and create PDF."""
        try:
            article_data = {
                "title": article["attributes"]["title"],
                "created": article["attributes"]["created"],
                "news_date": article["attributes"].get("news_date", ""),
                "news_author": article["attributes"].get("news_author", "Unknown Author"),
                "article_body": article["attributes"]["article_body"],
                "html_dcrs_repo": article["attributes"].get("html_dcrs_repo", ""),
                "image_captions": []
            }

            if "metatag" in article["attributes"]:
                article_data["metatag"] = article["attributes"]["metatag"]

            if "article_main_image_url" in article["attributes"]:
                img_url = article["attributes"]["article_main_image_url"]
                if img_url:
                    img_url = urljoin("https://home.dartmouth.edu", img_url)
                    article_data["article_main_image_url"] = img_url

            print(f"\nProcessing article: {article_data['title']}")

            # Extract image URLs from meta tags and article body HTML
            meta_image_urls = self.extract_image_urls(article_data)
            html_image_urls = self.extract_images_from_html(article_data)
            
            # Combine and deduplicate image URLs
            # Prioritize HTML images over meta images (which are often just default images)
            if html_image_urls:
                image_urls = html_image_urls
                # Only add meta images if we don't have any HTML images
                if not image_urls and meta_image_urls:
                    image_urls = meta_image_urls
            else:
                image_urls = meta_image_urls
            
            print(f"Found {len(image_urls)} total unique image(s) in article")

            image_files = []
            skipped_count = 0

            for i, url in enumerate(image_urls):
                try:
                    # Create a more reliable filename
                    base_name = os.path.basename(url.split('?')[0])
                    # If the base_name is empty or just a file extension, use a generic name
                    if not base_name or base_name.startswith(".") or len(base_name) < 3:
                        base_name = f"image_{i}.jpg"
                    
                    filename = os.path.join("images", f"{i}_{base_name}")
                    downloaded_file = self.download_image(url, filename)
                    if downloaded_file:
                        image_files.append(downloaded_file)
                    else:
                        skipped_count += 1
                        print(f"Skipped image {url} (duplicate or download failed)")
                except Exception as e:
                    skipped_count += 1
                    print(f"Error processing image URL {url}: {str(e)}")

            # Even if we download multiple images, we'll only use the highest resolution one in the PDF
            print(f"Successfully downloaded {len(image_files)} image(s), skipped {skipped_count} duplicate/failures")
            if len(image_files) > 1:
                print("Note: Only the highest resolution image will be used in the PDF")
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
                    created_date = article["attributes"].get("created", "")
                    if created_date:
                        date_obj = dateparser.parse(created_date)
                        date_str = date_obj.strftime("%Y-%m-%d")
                    else:
                        date_str = "unknown_date"

                    title = article["attributes"].get("title", "").replace("/", "-")[:50]
                    json_filename = f"{date_str}_{title}.json"
                    json_path = os.path.join("json", json_filename)

                    with open(json_path, "w", encoding="utf-8") as f:
                        json.dump(article, f, indent=2, ensure_ascii=False)

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
    parser = argparse.ArgumentParser(description="Scrape Dartmouth News articles within a date range")
    parser.add_argument("--start-date", type=str, help="Start date (inclusive) in YYYY-MM-DD format")
    parser.add_argument("--end-date", type=str, help="End date (inclusive) in YYYY-MM-DD format")
    parser.add_argument("--verify-ssl", action="store_true", help="Verify SSL certificates (default: disabled)")

    args = parser.parse_args()

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
    if args.verify_ssl:
        scraper.session.verify = True

    scraper.run()