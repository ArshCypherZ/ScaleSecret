import os
import random
import logging
import asyncio
import json
import sqlite3
from dotenv import load_dotenv
from telethon import TelegramClient, events
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from google import genai
from pydantic import BaseModel

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

API_ID=int(os.getenv("API_ID", 0))
API_HASH=os.getenv("API_HASH")
BOT_TOKEN=os.getenv("BOT_TOKEN")
CHAT_ID=int(os.getenv("CHAT_ID", 0))
OWNER_ID=int(os.getenv("OWNER_ID", 0))
GEMINI_API_KEY=os.getenv("GEMINI_API_KEY")

if not all([API_ID, API_HASH, BOT_TOKEN, CHAT_ID, OWNER_ID, GEMINI_API_KEY]):
    logging.error("Missing one or more required environment variables. Exiting.")
    exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

class TechFact(BaseModel):
    app_name: str
    feature_title: str
    explanation: str

APPS = [
    # Social & Communication
    'Instagram', 'LinkedIn', 'X', 'WhatsApp', 'Telegram', 'Discord', 'Snapchat', 'Pinterest', 'Reddit', 'Facebook', 'TikTok',
    # Entertainment & Streaming
    'Netflix', 'YouTube', 'Spotify', 'Twitch', 'SoundCloud', 'Disney+ Hotstar', 'Amazon Prime Video', 'Crunchyroll',
    # Gaming
    'Clash of Clans', 'Pokemon GO', 'PUBG Mobile',
    # Lifestyle & Services
    'Uber', 'Airbnb', 'Zomato', 'Swiggy', 'Ola', 'MakeMyTrip',
    # E-commerce & Payments
    'Amazon', 'Flipkart', 'Myntra', 'Shopify', 'PayPal', 'Google Pay', 'Paytm', 'PhonePe',
    # Tools & Productivity
    'Google Maps', 'GitHub', 'Dropbox', 'Zoom', 'Quora', 'Medium',
    # Education
    'Duolingo',
    # Indian Specific
    'IRCTC', 'BookMyShow', 'Hotstar', 'JioCinema'
]

TOPICS_FILE = "discussed_topics.json"
DB_FILE = "topics.db"

def init_database():
    """Initialize the SQLite database and create the topics table if it doesn't exist."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS discussed_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    logging.info("Database initialized successfully.")

def migrate_json_to_db():
    """Migrate topics from the old JSON file to the new SQLite DB if it exists."""
    if os.path.exists(TOPICS_FILE):
        logging.info(f"Found {TOPICS_FILE}, migrating topics to SQLite database...")
        try:
            with open(TOPICS_FILE, 'r') as f:
                topics = json.load(f)
            
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            for topic in topics:
                # Use INSERT OR IGNORE to avoid duplicates if migration is run multiple times
                cursor.execute("INSERT OR IGNORE INTO discussed_topics (title) VALUES (?)", (topic,))
            conn.commit()
            conn.close()
            
            # Rename the old file to prevent re-migration
            backup_filename = f"{TOPICS_FILE}.bak"
            os.rename(TOPICS_FILE, backup_filename)
            logging.info(f"Migration successful. Renamed {TOPICS_FILE} to {backup_filename}")
        except Exception as e:
            logging.error(f"Error migrating JSON to DB: {e}")

def load_discussed_topics():
    """Load previously discussed topics from the SQLite database."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        # Fetch the most recent 50 topics to keep the similarity check prompt concise
        cursor.execute("SELECT title FROM discussed_topics ORDER BY id DESC LIMIT 50")
        topics = [row[0] for row in cursor.fetchall()]
        conn.close()
        return topics
    except Exception as e:
        logging.error(f"Error loading topics from database: {e}")
        return []

def add_discussed_topic(topic_title):
    """Save a new discussed topic to the database."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO discussed_topics (title) VALUES (?)", (topic_title,))
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        logging.warning(f"Attempted to add a duplicate topic, which was ignored: {topic_title}")
    except Exception as e:
        logging.error(f"Error saving topic to database: {e}")

def get_all_discussed_topics():
    """Load all discussed topics from the SQLite database for display."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT title FROM discussed_topics ORDER BY id ASC")
        topics = [row[0] for row in cursor.fetchall()]
        conn.close()
        return topics
    except Exception as e:
        logging.error(f"Error loading all topics from database: {e}")
        return []

def clear_all_topics():
    """Clear all topics from the discussed_topics table."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM discussed_topics")
        conn.commit()
        conn.close()
        logging.info("All discussed topics have been cleared from the database.")
    except Exception as e:
        logging.error(f"Error clearing topics from database: {e}")

async def is_topic_already_discussed(new_title, previous_topics):
    """Use Gemini to determine if the new topic is similar to previously discussed ones"""
    if not previous_topics:
        return False
    
    previous_topics_text = "\n".join([f"- {topic}" for topic in previous_topics])
    
    prompt = f"""
You are an expert AI analyzing tech topics for similarity. Your goal is to avoid posting repetitive content, but allow for nuanced variations.

New topic: "{new_title}"

Previously discussed topics:
{previous_topics_text}

Analyze if the new topic is substantially similar to any of the previously discussed topics.

Here's how to judge similarity:
1.  **Core Concept is Identical**: If the underlying technical challenge is the same (e.g., both are about real-time messaging sync, or both are about payment fraud detection), they are likely similar.
2.  **Domain Matters**: If the core concept is similar (e.g., recommendation engines) but the application domain is very different (e.g., music recommendations vs. e-commerce product recommendations), consider them **DIFFERENT**. The unique challenges of each domain make them interesting.
3.  **Be Lenient**: When in doubt, lean towards "DIFFERENT" to allow for a wider variety of content.

Examples:
- "How Netflix streams videos" and "YouTube's video compression" are SIMILAR (both about video streaming).
- "Spotify's music recommendations" and "Myntra's product recommendations" are DIFFERENT (different domains: music vs. e-commerce).
- "Instagram's feed ranking" and "TikTok's 'For You' page algorithm" are SIMILAR (both are about content ranking algorithms in social media).
- "How Google Maps calculates ETAs" and "Uber's driver dispatch algorithm" are DIFFERENT (related to location, but solve different core problems).

Answer with only "YES" if the new topic is too similar and should be rejected, or "NO" if it's different enough to be interesting.
"""
    
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        
        result = response.text.strip().upper()
        is_similar = result == "YES"
        
        if is_similar:
            logging.info(f"Gemini detected similar topic: '{new_title}' is similar to previous topics")
        else:
            logging.info(f"Gemini confirmed unique topic: '{new_title}' is different from previous topics")
            
        return is_similar
        
    except Exception as e:
        logging.error(f"Error checking topic similarity with Gemini: {e}")
        return False

async def generate_tech_fact() -> tuple[str, str, str]:
    previous_topics = load_discussed_topics()
    
    # Identify apps that have already been discussed to encourage variety
    discussed_apps = set()
    for topic in previous_topics:
        for app_name in APPS:
            # Use word boundaries to avoid partial matches (e.g., 'Ola' in 'Clash of Clans')
            if f"\\b{app_name.lower()}\\b" in topic.lower():
                discussed_apps.add(app_name)

    # Create a list of apps that haven't been discussed yet
    available_apps = [app for app in APPS if app not in discussed_apps]
    
    # If we've run through all apps, just use the full list as a fallback
    if not available_apps:
        logging.info("All apps have been discussed at least once. Resetting to full list for selection.")
        available_apps = APPS

    max_attempts = 10 
    attempts = 0
    
    while attempts < max_attempts:
        app = random.choice(available_apps)
        prompt = (
            f"Your task is to explain a fascinating technical feature of a popular app. Pick an app like {app} or another well-known app that people use daily. "
            "Avoid niche, B2B, or enterprise software (like Salesforce or obscure developer tools). Focus on apps with a massive user base. "
            "1.  **Title**: Create an engaging question about a specific feature. Example: 'How does Shazam identify a song in seconds?' "
            "2.  **Explanation**: Write a clear, story-like explanation for a curious developer. "
            "   - Start with the user experience. "
            "   - Explain the technical challenge (e.g., scale, speed, data). "
            "   - Detail the solution: mention specific technologies, architecture (e.g., CDNs, microservices, caching layers), and clever algorithms. "
            "   - Keep the explanation concise and to the point. **Strictly limit the explanation to under 2000 characters.** "
            "The goal is to reveal the 'magic' behind a feature everyone uses. You can also answer in a coversational tone, like you're explaining it to a friend or an interviewer asking you questions."
            "Return the output as a JSON object matching the TechFact schema, with fields for 'app_name', 'feature_title', and 'explanation'."
        )

        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": TechFact,
                }
            )

            logging.info(f"Gemini response text: {response.text[:200]}...")
            
            if response.parsed:
                fact: TechFact = response.parsed
                is_similar = await is_topic_already_discussed(fact.feature_title, previous_topics)
                
                if is_similar:
                    logging.info(f"Topic is similar to previous topics, generating new one. Attempt {attempts + 1}")
                    attempts += 1
                    continue
                
                # Add the new unique topic to our database
                add_discussed_topic(fact.feature_title)
                
                return fact.app_name, fact.feature_title, fact.explanation
            else:
                logging.error("Response parsed is None, using text response")
                return (app, "API Parsing Failed", response.text if response.text else "No response text")

        except Exception as e:
            logging.error(f"Gemini generation error: {e}")
            attempts += 1
            continue
    
    logging.warning(f"Could not generate unique topic after {max_attempts} attempts")
    return ("ErrorApp", "Generation Failed", "Could not generate unique content after multiple attempts")

telethn = TelegramClient('botto_session', API_ID, API_HASH)

# Create a lock to prevent concurrent post generation
post_lock = asyncio.Lock()

async def post_message():
    app, title, desc = await generate_tech_fact()
    # If fallback or error, notify owner and don't post
    if app == "ErrorApp" or title == "API Parsing Failed":
        try:
            await telethn.send_message(OWNER_ID, f"Tech fact generation failed:\nApp: {app}\nTitle: {title}\nDesc: {desc}")
            logging.warning("Generation failed or fallback used. Not posting to channel.")
        except Exception as e:
            logging.error(f"Failed to notify owner: {e}")
        return

    # Truncate description if it's too long (safeguard)
    if len(desc) > 3800: # Telegram's limit is 4096, this provides a buffer
        desc = desc[:3800] + "..."
        
    text = (
        f"**{app}**\n\n"
        f"**{title}**\n\n\n"
        f"**Explanation**: {desc}"
    )
    try:
        await telethn.send_message(CHAT_ID, text)
        logging.info(f"Posted: {app} - {title}")
    except Exception as e:
        logging.error(f"Failed to send message: {e}")

@telethn.on(events.NewMessage(pattern="/postnow"))
async def postnow(event):
    if event.is_private and event.sender_id == OWNER_ID:
        if post_lock.locked():
            await event.reply("A post is already being generated. Please wait a moment.")
            return

        async with post_lock:
            await event.reply("On it! Generating a new post for you...")
            await post_message()
            await event.reply("Posted new stuff.")
    elif not event.is_private:
        await event.reply("Let us talk in private ;p")
    else:
        await event.reply("Sata Andagi. Bahhhhhhh!!!")

@telethn.on(events.NewMessage(pattern="/id"))
async def _idd(event):
    await event.reply(f"**Chat ID**: `{event.chat_id}`")

@telethn.on(events.NewMessage(pattern="/topics"))
async def show_topics(event):
    if event.is_private and event.sender_id == OWNER_ID:
        topics = get_all_discussed_topics()
        if topics:
            # To avoid hitting message length limits, send topics in chunks
            message_parts = []
            current_part = f"**Discussed Topics ({len(topics)}):**\n\n"
            for i, topic in enumerate(topics):
                topic_line = f"{i+1}. {topic}\n"
                if len(current_part) + len(topic_line) > 4000:
                    message_parts.append(current_part)
                    current_part = ""
                current_part += topic_line
            message_parts.append(current_part)

            for part in message_parts:
                await event.reply(part)
        else:
            await event.reply("No topics discussed yet!")
    else:
        await event.reply("Owner only command!")

@telethn.on(events.NewMessage(pattern="/cleartopics"))
async def clear_topics(event):
    if event.is_private and event.sender_id == OWNER_ID:
        clear_all_topics()
        await event.reply("All discussed topics cleared!")
    else:
        await event.reply("Owner only command!")

async def main():
    logging.info("Starting botto...please waito..")
    
    # Initialize and migrate database
    init_database()
    migrate_json_to_db()

    await telethn.start(bot_token=BOT_TOKEN)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(post_message, "interval", hours=23)
    scheduler.start()
    
    logging.info("Botto started nyan nyan :3")
    await telethn.run_until_disconnected()


asyncio.run(main())
