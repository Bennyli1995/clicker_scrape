import time
import io
import re
import os
from PIL import Image
import pytesseract
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm
import concurrent.futures

def extract_image_urls_from_html(html_content):
    """Extract image URLs and timestamps from HTML"""
    soup = BeautifulSoup(html_content, 'html.parser')
    image_urls = []
    
    # Find all img tags with data-src attribute
    img_tags = soup.find_all('img', attrs={'data-src': True})
    
    for img in img_tags:
        # Extract the URL from data-src attribute
        url = img['data-src']
        # Get the timestamp from the parent li element if available
        timestamp = "unknown"
        if img.parent and img.parent.name == 'li':
            timestamp_div = img.parent.find('div', class_='thumbnail-timestamp')
            if timestamp_div:
                timestamp = timestamp_div.text.strip()
        
        image_urls.append((url, timestamp))
    
    return image_urls

def download_and_process_image(url_timestamp):
    """Download image and process it with OCR to find attendance codes"""
    url, timestamp = url_timestamp
    
    try:
        # Download the image
        response = requests.get(url)
        if response.status_code != 200:
            print(f"Failed to download image at {timestamp}: {response.status_code}")
            return None
        
        # Create a directory for saving images if it doesn't exist
        os.makedirs("thumbnails", exist_ok=True)
        
        # Save the image file for reference
        filename = f"thumbnails/thumbnail_{timestamp.replace(':', '_')}.jpg"
        with open(filename, 'wb') as f:
            f.write(response.content)
        
        # Convert to PIL Image
        image = Image.open(io.BytesIO(response.content))
        
        # Use OCR to extract text
        text = pytesseract.image_to_string(image)
        
        # Look for attendance slide indicators
        if re.search(r'attendance code|clicker question', text.lower()):
            # Pattern for words like "LUT DESERT" - uppercase words with spaces between
            # that aren't part of URLs or other non-code text
            code_pattern = r'(?<![a-zA-Z0-9:/.])[A-Z]{2,}(?: [A-Z]{2,})*(?![a-zA-Z0-9:/.])'
            matches = re.findall(code_pattern, text)
            
            # Filter out false positives
            filtered_matches = [
                match for match in matches 
                if not any(url_term in match.lower() for url_term in ['http', 'www', 'join', 'com'])
            ]
            
            if filtered_matches:
                # Save positive matches to a special directory
                os.makedirs("attendance_codes", exist_ok=True)
                for _, match in enumerate(filtered_matches):
                    code_filename = f"attendance_codes/code_{match.replace(' ', '_')}_{timestamp.replace(':', '_')}.jpg"
                    image.save(code_filename)
                
                return (timestamp, filtered_matches, filename)
            
            # Fallback for attendance slides without standard pattern
            if "Insert the following attendance code" in text:
                # Try to extract the code that appears after this phrase
                after_prompt = text.split("Insert the following attendance code")[1]
                lines = after_prompt.split('\n')
                for line in lines[:5]:  # Check the next few lines
                    if re.match(r'^[A-Z\s]+$', line.strip()) and len(line.strip()) > 3:
                        code = line.strip()
                        # Save the fallback match
                        os.makedirs("attendance_codes", exist_ok=True)
                        code_filename = f"attendance_codes/code_fallback_{code.replace(' ', '_')}_{timestamp.replace(':', '_')}.jpg"
                        image.save(code_filename)
                        return (timestamp, [code], filename)
        
        return None
    
    except Exception as e:
        print(f"Error processing image at {timestamp}: {str(e)}")
        return None

def extract_timestamps_from_thumbnails(html_content):
    """Extract timestamps from thumbnails HTML for navigating in the video"""
    soup = BeautifulSoup(html_content, 'html.parser')
    timestamps = []
    
    # Find all thumbnail li elements
    thumbnails = soup.find_all('li', class_='thumbnail')
    
    for thumbnail in thumbnails:
        # Get timestamp
        timestamp_div = thumbnail.find('div', class_='thumbnail-timestamp')
        if not timestamp_div:
            continue
            
        timestamp_text = timestamp_div.text.strip()
        
        # Convert timestamp to seconds
        try:
            minutes, seconds = map(int, timestamp_text.split(':'))
            total_seconds = minutes * 60 + seconds
            timestamps.append((total_seconds, timestamp_text))
        except:
            continue
    
    # Sort and remove duplicates
    timestamps = sorted(list(set(timestamps)), key=lambda x: x[0])
    return timestamps

def extract_attendance_codes(panopto_url, wait_for_login=False):
    """Main function to extract attendance codes from Panopto"""
    # Set up Chrome options
    chrome_options = Options()
    # chrome_options.add_argument("--headless")  # Uncomment for headless mode
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    
    # Initialize the driver
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    
    try:
        # Navigate to the URL
        print(f"Navigating to {panopto_url}")
        driver.get(panopto_url)
        
        # If login is required, give time for the user to log in
        if wait_for_login:
            print("Waiting for 5 seconds...")
            time.sleep(5)  # Adjust time as needed for login
        
        # Wait for the page to load
        try:
            # Wait for the thumbnails container to be present
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".thumbnail-strip"))
            )
            print("Thumbnail strip loaded successfully")
        except:
            print("Could not find thumbnail strip. Taking a screenshot to diagnose...")
            driver.save_screenshot("page_load.png")
            print("Screenshot saved as page_load.png")
        
        # Wait a bit longer to ensure all thumbnails are loaded
        time.sleep(5)
        
        # Get the HTML content with thumbnails
        html_content = driver.page_source
        
        # Save the HTML for debugging
        with open("thumbnails_html.html", "w", encoding="utf-8") as f:
            f.write(html_content)
            print("Saved HTML to thumbnails_html.html")
        
        # Extract image URLs from HTML
        print("Extracting thumbnail image URLs...")
        image_urls = extract_image_urls_from_html(html_content)
        print(f"Found {len(image_urls)} thumbnail images")
        
        # Download and process thumbnails
        if image_urls:
            print("Processing thumbnails for attendance codes...")
            results = []
            
            # Use a thread pool to process images concurrently
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_to_url = {executor.submit(download_and_process_image, url_timestamp): url_timestamp for url_timestamp in image_urls}
                
                # Use tqdm for a progress bar
                for future in tqdm(concurrent.futures.as_completed(future_to_url), total=len(image_urls)):
                    url_timestamp = future_to_url[future]
                    try:
                        result = future.result()
                        if result:
                            results.append(result)
                    except Exception as e:
                        print(f"Error processing {url_timestamp[1]}: {str(e)}")
            
            # Extract codes from results
            found_codes = set()
            for timestamp, codes, image_file in results:
                for code in codes:
                    found_codes.add(code)
            
            # If codes found, return them
            if found_codes:
                print("\nFound attendance codes from thumbnails:")
                for code in found_codes:
                    print(f"- {code}")
                return list(found_codes)
        
        # If no codes found from thumbnails, try navigating through the video
        print("\nNo attendance codes found in thumbnails. Attempting to scan through the video...")
        
        # Find the video player
        try:
            video_player = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".panopto-player"))
            )
            print("Video player found")
        except:
            print("Could not find video player element. Taking a screenshot...")
            driver.save_screenshot("video_player_not_found.png")
            print("Screenshot saved as video_player_not_found.png")
            # Try alternative selectors
            try:
                video_player = driver.find_element(By.TAG_NAME, "video")
                print("Found video element directly")
            except:
                print("No video element found. Cannot navigate through video.")
                return []
        
        # Extract timestamps for navigation
        timestamps = extract_timestamps_from_thumbnails(html_content)
        print(f"Extracted {len(timestamps)} timestamps for navigation")
        
        # Navigate through timestamps and capture frames
        video_scan_results = []
        
        for seconds, time_str in timestamps:
            print(f"Checking timestamp {time_str}")
            
            # Navigate to timestamp
            driver.execute_script(f"document.querySelector('video').currentTime = {seconds};")
            time.sleep(1.5)  # Wait for frame to load
            
            # Take screenshot
            screenshot = driver.find_element(By.TAG_NAME, "video").screenshot_as_png
            
            # Save screenshot
            screenshot_file = f"video_frames/frame_{time_str.replace(':', '_')}.png"
            os.makedirs("video_frames", exist_ok=True)
            with open(screenshot_file, "wb") as f:
                f.write(screenshot)
            
            # Process with OCR
            image = Image.open(io.BytesIO(screenshot))
            text = pytesseract.image_to_string(image)
            
            # Check for attendance codes
            if re.search(r'attendance code|clicker question', text.lower()):
                code_pattern = r'(?<![a-zA-Z0-9:/.])[A-Z]{2,}(?: [A-Z]{2,})+(?![a-zA-Z0-9:/.])'
                matches = re.findall(code_pattern, text)
                
                filtered_matches = [
                    match for match in matches 
                    if not any(url_term in match.lower() for url_term in ['http', 'www', 'join', 'com'])
                ]
                
                if filtered_matches:
                    os.makedirs("attendance_codes", exist_ok=True)
                    for match in filtered_matches:
                        code_file = f"attendance_codes/video_code_{match.replace(' ', '_')}_{time_str.replace(':', '_')}.png"
                        image.save(code_file)
                    
                    video_scan_results.append((time_str, filtered_matches, screenshot_file))
        
        # Process results from video scanning
        for timestamp, codes, image_file in video_scan_results:
            for code in codes:
                if code not in found_codes:
                    found_codes.add(code)
                    print(f"Found code from video: {code} at {timestamp}")
        
        return list(found_codes)
    
    finally:
        # Close the browser
        driver.quit()

if __name__ == "__main__":
    # Replace with your actual Panopto URL
    panopto_url = "https://ubc.ca.panopto.com/Panopto/Pages/Viewer.aspx?id=ec0195ab-a288-459c-90b3-b25a010b1d0d"
    
    # Set to True if the page requires login
    wait_for_login = False
    
    # Extract attendance codes
    codes = extract_attendance_codes(panopto_url, wait_for_login)
    
    # Final output
    if codes:
        print("\nAll extracted attendance codes:")
        for code in codes:
            print(f"- {code}")
    else:
        print("\nNo attendance codes could be extracted.")