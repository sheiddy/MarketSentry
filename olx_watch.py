#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import time
import threading
from dataclasses import dataclass, asdict
from typing import List, Optional
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

BASE = "https://www.olx.ro"


@dataclass
class Listing:
    id: str
    title: str
    price: str
    location_time: str
    url: str
    image_url: Optional[str] = None


def load_seen(path: str) -> set:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen", []))
    except Exception:
        return set()


def save_seen(path: str, seen: set) -> None:
    tmp = {"seen": sorted(seen)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tmp, f, ensure_ascii=False, indent=2)


def build_search_url(query: str) -> str:
    # OLX commonly supports /oferte/q-<query>/
    q = quote_plus(query.strip())
    return f"{BASE}/oferte/q-{q}/"


def session_with_headers() -> requests.Session:
    s = requests.Session()
    # Normal, non-evasive headers; keep it simple.
    s.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ro-RO,ro;q=0.9,en-US;q=0.8,en;q=0.7",
        }
    )
    return s


def extract_listing_id(url: str) -> str:
    # Try to derive a stable ID from the URL if possible
    # Example patterns may vary; we keep a robust fallback.
    m = re.search(r"ID([A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    # fallback: use the URL itself
    return url


def fetch_image_from_listing(s: requests.Session, listing_url: str) -> Optional[str]:
    """Fetch detailed listing page to extract image URL when card thumbnail is missing."""
    try:
        r = s.get(listing_url, timeout=10)
        if r.status_code != 200:
            return None
        
        soup = BeautifulSoup(r.text, "html.parser")
        
        # Try multiple selectors for main listing image
        img_selectors = [
            'img[data-testid="image-gallery-image"]',
            'img[data-cy="listing-photo"]',
            'picture img[src]',
            'div[data-testid="image-gallery"] img[src]',
            'img.image-gallery-image',
            'img[alt*="oferta"]',
            'img[alt*="listing"]',
            'div[role="region"] img[src]',
            'img[data-src]',
            'img[src*="cdn"]',
            'img[src*="olx"]',
            'img.gallery-image',
        ]
        
        for selector in img_selectors:
            img_el = soup.select_one(selector)
            if img_el:
                img_src = img_el.get("src", "").strip() or img_el.get("data-src", "").strip()
                if img_src and "placeholder" not in img_src and "no_thumbnail" not in img_src and not img_src.endswith(".svg"):
                    full_url = img_src if img_src.startswith("http") else urljoin(BASE, img_src)
                    if full_url.startswith("http"):
                        print(f"Found image from selector {selector}", file=sys.stderr)
                        return full_url
        
        # Try to extract from JSON-LD or embedded JSON (common in modern web apps)
        try:
            json_scripts = soup.find_all('script', type='application/json')
            for script in json_scripts:
                try:
                    data = json.loads(script.string)
                    # Look for image URLs in the JSON structure
                    if isinstance(data, dict):
                        img_url = find_image_in_json(data)
                        if img_url:
                            print(f"Found image in JSON", file=sys.stderr)
                            return img_url
                except:
                    pass
        except:
            pass
        
        # Try to find any img with decent src (not placeholder/svg)
        all_imgs = soup.find_all('img')
        for img_el in all_imgs:
            img_src = img_el.get("src", "").strip() or img_el.get("data-src", "").strip()
            if (img_src and 
                "placeholder" not in img_src and 
                "no_thumbnail" not in img_src and 
                not img_src.endswith(".svg") and
                ("cdn" in img_src or "olx" in img_src or "image" in img_src)):
                full_url = img_src if img_src.startswith("http") else urljoin(BASE, img_src)
                if full_url.startswith("http"):
                    print(f"Found image from generic search", file=sys.stderr)
                    return full_url
        
        return None
    except Exception as e:
        print(f"Error fetching image from listing: {e}", file=sys.stderr)
        return None


def find_image_in_json(obj, depth=0) -> Optional[str]:
    """Recursively search for image URLs in JSON data."""
    if depth > 5:  # Prevent infinite recursion
        return None
    
    if isinstance(obj, dict):
        # Check common image field names
        for key in ['image', 'imageUrl', 'img', 'photo', 'thumbnail', 'src', 'url']:
            if key in obj:
                val = obj[key]
                if isinstance(val, str) and val.startswith("http") and not val.endswith(".svg"):
                    return val
        
        # Recurse into dict values
        for value in obj.values():
            result = find_image_in_json(value, depth + 1)
            if result:
                return result
    
    elif isinstance(obj, list):
        # Recurse into lists
        for item in obj:
            result = find_image_in_json(item, depth + 1)
            if result:
                return result
    
    return None


def parse_listings(html: str) -> List[Listing]:
    # Prefer "lxml" when available for speed/robustness; fall back to the
    # built-in parser if it's not installed in the environment.
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    listings: List[Listing] = []

    # Heuristic 1: OLX cards often have data-cy="l-card"
    cards = soup.select('[data-cy="l-card"]')
    if not cards:
        # Heuristic 2: fallback — anchors that look like offer pages
        cards = soup.select('a[href*="/d/oferta/"]')

    seen_urls = set()

    for card in cards:
        # If card is an <a> already (fallback path), use it; else find inner <a>
        a = card if getattr(card, "name", None) == "a" else card.select_one('a[href]')
        if not a:
            continue

        href = a.get("href", "").strip()
        if not href:
            continue

        url = href if href.startswith("http") else urljoin(BASE, href)

        # Avoid duplicates from repeated anchors
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Title / price / location-time are highly dependent on OLX markup,
        # so we try multiple selectors and fall back to text where possible.
        title_el = (
            card.select_one('[data-cy="ad-card-title"]')
            or card.select_one("h6")
            or card.select_one("h4")
        )
        title = title_el.get_text(" ", strip=True) if title_el else ""

        price_el = (
            card.select_one('[data-testid="ad-price"]')
            or card.select_one('[data-cy="ad-card-price"]')
            or card.select_one("p")
        )
        price = price_el.get_text(" ", strip=True) if price_el else ""

        lt_el = (
            card.select_one('[data-testid="location-date"]')
            or card.select_one('[data-cy="ad-card-location"]')
        )
        location_time = lt_el.get_text(" ", strip=True) if lt_el else ""

        listing_id = extract_listing_id(url)
        
        # Extract image URL - try multiple selectors for lazy-loaded and regular images
        image_url = None
        img_el = (
            card.select_one('img[src]:not([src*="placeholder"]):not([src*="no_thumbnail"])')
            or card.select_one('img[data-src]:not([data-src*="placeholder"]):not([data-src*="no_thumbnail"])')
            or card.select_one('picture img[src]')
            or card.select_one('img')
        )
        if img_el:
            # Try src first, then data-src (lazy loading)
            img_src = img_el.get("src", "").strip() or img_el.get("data-src", "").strip()
            if img_src and "placeholder" not in img_src and "no_thumbnail" not in img_src:
                image_url = img_src if img_src.startswith("http") else urljoin(BASE, img_src)
                if not image_url.startswith("http"):
                    image_url = None  # Invalid URL, skip

        # Basic sanity: skip empty titles if we're clearly not parsing correctly
        if not title and "/d/oferta/" not in url:
            continue

        listings.append(
            Listing(
                id=listing_id,
                title=title or "(no title parsed)",
                price=price or "(no price parsed)",
                location_time=location_time or "",
                url=url,
                image_url=image_url,
            )
        )

    return listings


def fetch_listings(s: requests.Session, query: str, timeout: int = 20) -> List[Listing]:
    url = build_search_url(query)
    r = s.get(url, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} fetching {url}")
    return parse_listings(r.text)


def format_listings_for_telegram(listings: List[Listing], title: str = None) -> List[str]:
    """Format listings into Telegram messages, splitting if too long (4096 char limit)."""
    if not listings:
        msg = "No listings found." if not title else f"{title}\n\nNo listings found."
        return [msg]
    
    messages = []
    current_message = (title or "Current Active Listings") + f" ({len(listings)}):\n\n"
    MAX_LENGTH = 4096
    
    for idx, listing in enumerate(listings, 1):
        entry = f"<b>{idx}. {listing.title}</b>\n"
        entry += f"💰 {listing.price}\n"
        entry += f"📍 {listing.location_time}\n"
        entry += f"🔗 <a href=\"{listing.url}\">View</a>\n\n"
        
        # If adding this entry would exceed limit, save current message and start new one
        if len(current_message) + len(entry) > MAX_LENGTH:
            messages.append(current_message.rstrip())
            current_message = f"<b>Continued ({idx}/{len(listings)}):</b>\n\n" + entry
        else:
            current_message += entry
    
    # Add remaining message
    if current_message.strip():
        messages.append(current_message.rstrip())
    
    return messages


def telegram_bot_poller(bot_token: str, chat_id: str, query: str, seen_file: str, interval: int = 600):
    """Poll Telegram for commands and OLX for new listings."""
    s = session_with_headers()
    seen = load_seen(seen_file)
    last_update_id = 0
    last_poll_time = 0
    
    print(f"[Telegram Bot] Started polling for commands (press Ctrl+C to stop)", file=sys.stderr)
    print(f"[Telegram Bot] Also polling OLX every {interval}s for new listings", file=sys.stderr)
    
    def send_new_listings_to_telegram(listings):
        """Send new listings to Telegram."""
        new_items = [x for x in listings if x.id not in seen]
        if new_items:
            print(f"[Telegram Bot] Found {len(new_items)} NEW listing(s).", file=sys.stderr)
            send_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
            
            for item in new_items:
                # Try to send photo with caption
                caption = f"<b>{item.title}</b>\n{item.price} | {item.location_time}\n<a href=\"{item.url}\">View Listing</a>"
                image_url = item.image_url
                
                if not image_url or "no_thumbnail" in image_url:
                    image_url = fetch_image_from_listing(s, item.url)
                
                if image_url:
                    try:
                        response = requests.post(send_url, data={
                            "chat_id": chat_id,
                            "photo": image_url,
                            "caption": caption,
                            "parse_mode": "HTML"
                        }, timeout=10)
                        
                        # If 400 error, try downloading and uploading
                        if response.status_code == 400:
                            try:
                                img_response = requests.get(image_url, headers={
                                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                                }, timeout=10)
                                if img_response.status_code == 200:
                                    files = {"photo": img_response.content}
                                    data = {
                                        "chat_id": chat_id,
                                        "caption": caption,
                                        "parse_mode": "HTML"
                                    }
                                    response = requests.post(send_url, data=data, files=files, timeout=10)
                            except Exception as e:
                                print(f"[Telegram Bot] Failed to download image: {e}", file=sys.stderr)
                    except Exception as e:
                        print(f"[Telegram Bot] Error sending photo: {e}", file=sys.stderr)
                else:
                    # Send text-only message
                    try:
                        send_text_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                        requests.post(send_text_url, json={
                            "chat_id": chat_id,
                            "text": caption,
                            "parse_mode": "HTML"
                        }, timeout=10)
                    except Exception as e:
                        print(f"[Telegram Bot] Error sending message: {e}", file=sys.stderr)
            
            # Update seen listings
            seen.update(x.id for x in new_items)
            save_seen(seen_file, seen)
    
    try:
        while True:
            try:
                # Check if it's time to poll for new listings
                current_time = time.time()
                if current_time - last_poll_time >= interval:
                    try:
                        print(f"[Telegram Bot] Polling OLX for new listings...", file=sys.stderr)
                        listings = fetch_listings(s, query)
                        send_new_listings_to_telegram(listings)
                        last_poll_time = current_time
                    except Exception as e:
                        print(f"[Telegram Bot] Error polling OLX: {e}", file=sys.stderr)
                
                # Get updates with shorter timeout for better responsiveness to Ctrl+C
                url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
                params = {
                    "offset": last_update_id + 1,
                    "timeout": 5,  # Shorter timeout for responsiveness to Ctrl+C
                    "allowed_updates": ["message"]
                }
                try:
                    response = requests.get(url, params=params, timeout=15)
                except requests.exceptions.RequestException:
                    # Connection timeout or error - just continue
                    time.sleep(1)
                    continue
                
                if response.status_code != 200:
                    print(f"[Telegram Bot] Error getting updates: {response.status_code}", file=sys.stderr)
                    time.sleep(5)
                    continue
                
                data = response.json()
                if not data.get("ok"):
                    print(f"[Telegram Bot] API error: {data.get('description')}", file=sys.stderr)
                    time.sleep(5)
                    continue
                
                updates = data.get("result", [])
                if not updates:
                    continue
                
                for update in updates:
                    last_update_id = update.get("update_id")
                    message = update.get("message", {})
                    text = message.get("text", "").strip()
                    msg_chat_id = message.get("chat", {}).get("id")
                    
                    # Only respond to messages from the configured chat
                    if msg_chat_id != int(chat_id):
                        continue
                    
                    print(f"[Telegram Bot] Received: {text}", file=sys.stderr)
                    
                    if text == "/status":
                        try:
                            # Fetch current listings
                            listings = fetch_listings(s, query)
                            print(f"[Telegram Bot] Fetched {len(listings)} total listings from OLX", file=sys.stderr)
                            
                            # Show ONLY listings that were already seen AND are still on the site
                            # This filters out expired listings
                            active_listings = [x for x in listings if x.id in seen]
                            print(f"[Telegram Bot] Found {len(active_listings)} still-active listings from seen.json", file=sys.stderr)
                            
                            if active_listings:
                                # Sort by title for consistent ordering
                                active_listings.sort(key=lambda x: x.title)
                                message_texts = format_listings_for_telegram(
                                    active_listings,
                                    f"🔍 Active OLX Listings for: <b>{query}</b>"
                                )
                            else:
                                all_in_seen = len(seen)
                                message_texts = [f"🔍 Status for: <b>{query}</b>\n\nNo active listings remaining.\nTotal tracked: {all_in_seen}\nCurrently on OLX: {len(listings)}"]
                            
                            # Send response (may be multiple messages)
                            send_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                            for msg_text in message_texts:
                                try:
                                    requests.post(send_url, json={
                                        "chat_id": chat_id,
                                        "text": msg_text,
                                        "parse_mode": "HTML"
                                    }, timeout=10)
                                except Exception as e:
                                    print(f"[Telegram Bot] Error sending message: {e}", file=sys.stderr)
                            
                            print(f"[Telegram Bot] Status report sent ({len(message_texts)} message(s))", file=sys.stderr)
                        except Exception as e:
                            send_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                            try:
                                requests.post(send_url, json={
                                    "chat_id": chat_id,
                                    "text": f"❌ Error fetching listings: {str(e)}",
                                    "parse_mode": "HTML"
                                }, timeout=10)
                            except:
                                pass
                            print(f"[Telegram Bot] Error handling /status: {e}", file=sys.stderr)
                    
                    elif text == "/help":
                        help_text = (
                            "Available commands:\n"
                            "/status - Show all still-active OLX listings (from previously tracked)\n"
                            "/help - Show this message\n\n"
                            "The bot automatically sends new listings as they appear."
                        )
                        send_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                        try:
                            requests.post(send_url, json={
                                "chat_id": chat_id,
                                "text": help_text
                            }, timeout=10)
                        except:
                            pass
                    
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"[Telegram Bot] Error in polling loop: {e}", file=sys.stderr)
                time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Telegram Bot] Stopped.", file=sys.stderr)
        save_seen(seen_file, seen)


def main():
    ap = argparse.ArgumentParser(description="Watch OLX.ro search results and print new listings.")
    ap.add_argument("--query", required=True, help='Search query, e.g. "m4 mac mini 24gb"')
    ap.add_argument("--interval", type=int, default=600, help="Seconds between checks (default: 600)")
    ap.add_argument("--seen-file", default="seen.json", help="File to store seen listing IDs/URLs")
    ap.add_argument("--once", action="store_true", help="Run once and exit")
    ap.add_argument("--bot-mode", action="store_true", help="Enable Telegram bot mode (listen for commands instead of polling)")
    args = ap.parse_args()

    # Initialize Telegram bot if credentials are available
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if args.bot_mode:
        # Bot command-listening mode
        if not bot_token or not chat_id:
            print("Error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set for bot mode", file=sys.stderr)
            sys.exit(1)
        
        print(f"Starting in bot mode (listening for commands)", file=sys.stderr)
        telegram_bot_poller(bot_token, chat_id, args.query, args.seen_file, interval=args.interval)
        return
    
    # Original polling mode
    seen = load_seen(args.seen_file)
    s = session_with_headers()
    
    if bot_token and chat_id:
        print(f"Telegram notifications enabled (chat: {chat_id})", file=sys.stderr)
    else:
        print(f"Telegram notifications disabled (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable)", file=sys.stderr)

    def send_telegram(text: str):
        if not bot_token or not chat_id:
            return
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            print(f"Sending Telegram message...", file=sys.stderr)
            response = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
            print(f"Telegram response: {response.status_code}", file=sys.stderr)
            if response.status_code != 200:
                print(f"Telegram error: {response.text}", file=sys.stderr)
        except Exception as e:
            print(f"Error sending Telegram message: {e}", file=sys.stderr)

    def send_telegram_photo(listing: Listing):
        if not bot_token or not chat_id:
            return
        try:
            caption = f"<b>{listing.title}</b>\n{listing.price} | {listing.location_time}\n<a href=\"{listing.url}\">View Listing</a>"
            
            # If no image or placeholder, try fetching from listing page
            image_url = listing.image_url
            if not image_url or "no_thumbnail" in image_url:
                print(f"No valid thumbnail, fetching from listing page...", file=sys.stderr)
                image_url = fetch_image_from_listing(s, listing.url)
            
            if image_url:
                url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
                print(f"Sending photo to Telegram...", file=sys.stderr)
                
                # First attempt: send URL directly with headers
                response = requests.post(url, data={
                    "chat_id": chat_id,
                    "photo": image_url,
                    "caption": caption,
                    "parse_mode": "HTML"
                }, timeout=10)
                
                # If 400 error, try downloading the image and uploading it
                if response.status_code == 400:
                    print(f"Photo URL failed (400), trying to download image...", file=sys.stderr)
                    try:
                        img_response = requests.get(image_url, headers={
                            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                        }, timeout=10)
                        if img_response.status_code == 200:
                            print(f"Downloaded image, uploading to Telegram...", file=sys.stderr)
                            files = {"photo": img_response.content}
                            data = {
                                "chat_id": chat_id,
                                "caption": caption,
                                "parse_mode": "HTML"
                            }
                            response = requests.post(url, data=data, files=files, timeout=10)
                    except Exception as e:
                        print(f"Failed to download image: {e}", file=sys.stderr)
                
                # If photo still fails, fall back to text-only message
                if response.status_code != 200:
                    print(f"Photo failed ({response.status_code}), falling back to text...", file=sys.stderr)
                    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    response = requests.post(url, json={
                        "chat_id": chat_id,
                        "text": caption,
                        "parse_mode": "HTML"
                    }, timeout=10)
            else:
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                print(f"Sending text message to Telegram (no image found)...", file=sys.stderr)
                response = requests.post(url, json={
                    "chat_id": chat_id,
                    "text": caption,
                    "parse_mode": "HTML"
                }, timeout=10)
            
            if response.status_code != 200:
                print(f"Telegram error: {response.text}", file=sys.stderr)
        except Exception as e:
            print(f"Error sending Telegram message: {e}", file=sys.stderr)

    def poll_once():
        try:
            listings = fetch_listings(s, args.query)
            new_items = [x for x in listings if x.id not in seen]
            if new_items:
                print(f"Found {len(new_items)} NEW listing(s).", file=sys.stderr)
                for item in new_items:
                    message = f"<b>{item.title}</b>\n{item.price} | {item.location_time}\n<a href=\"{item.url}\">Link</a>"
                    print(f"\n{message}", file=sys.stdout)
                    if item.image_url:
                        print(f"  Image: {item.image_url[:80]}...", file=sys.stderr)
                    else:
                        print(f"  No image found", file=sys.stderr)
                    send_telegram_photo(item)
                
                seen.update(x.id for x in new_items)
                save_seen(args.seen_file, seen)
            else:
                print("No new listings.", file=sys.stderr)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)

    if args.once:
        poll_once()
    else:
        print(f"Polling every {args.interval}s. Press Ctrl+C to stop.", file=sys.stderr)
        try:
            while True:
                poll_once()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("Stopped.", file=sys.stderr)
            save_seen(args.seen_file, seen)

if __name__ == "__main__":
    main()
