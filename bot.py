import os
import random
import logging
import asyncio
import json
import sqlite3
import re
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

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", 0))
OWNER_ID = int(os.getenv("OWNER_ID", 0))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

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

LENSES = [
    'History and evolution',
    'Core principles and algorithms',
    'Design choices and architecture',
    'Trade-offs and limitations',
    'Privacy and security',
    'Performance vs. cost',
    'On-device vs. cloud computation',
    'Edge cases and failure modes',
    'User experience and accessibility',
    'Energy efficiency and sustainability',
    'Data governance and ethics',
    'Open-source vs. proprietary approaches',
    'Future trends and emerging research',
    'Comparisons and analogies',
    'Real-world applications and case studies'
]

WRITING_PRINCIPLES = """
Core Principles:

1. Assume smart, not technical. Reader has never studied CS. If you use a technical term, explain it immediately in parentheses.

2. Titles must be curiosity-driven. No colons. No jargon. Would someone tap on this?

3. Start with an app or experience they use daily.

4. Every jargon word needs instant translation in parentheses.

5. Max 15 words per sentence. Break long sentences.

6. Mom test - would a non-technical person understand paragraph one?

7. Be concrete. Describe what happens, not abstract concepts.

8. End with a simple memorable insight.

Banned:
- Titles with colons
- Unexplained jargon
- Academic phrasing
- Sentences over 20 words
"""

TOPICS_FILE = "discussed_topics.json"
DB_FILE = "topics.db"


def init_database():
    """Initialize the SQLite database and create the topics table if it doesn't exist."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS discussed_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            subcategory TEXT NOT NULL,
            title TEXT NOT NULL UNIQUE,
            explanation TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    logging.info("Database initialized successfully.")


def add_discussed_topic(category, subcategory, title, explanation):
    """Save a new discussed topic to the database with hierarchy."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO discussed_topics (category, subcategory, title, explanation) VALUES (?, ?, ?, ?)",
            (category, subcategory, title, explanation)
        )
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        logging.warning(f"Attempted to add a duplicate topic, which was ignored: {title}")
    except Exception as e:
        logging.error(f"Error saving topic to database: {e}")


def get_last_n_topics(n=10):
    """Fetch the last N discussed topics with their category and subcategory for cooldown enforcement."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT category, subcategory, title FROM discussed_topics ORDER BY id DESC LIMIT ?",
            (n,)
        )
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logging.error(f"Error fetching last N topics: {e}")
        return []


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


def get_recent_titles_for_dedup(n=30):
    """Fetch the last N discussed topic titles to pass to LLM for deduplication."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT title FROM discussed_topics ORDER BY id DESC LIMIT ?",
            (n,)
        )
        titles = [row[0] for row in cursor.fetchall()]
        conn.close()
        return titles
    except Exception as e:
        logging.error(f"Error fetching recent titles for dedup: {e}")
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


def _looks_like_json(s: str) -> bool:
    if not s:
        return False
    s_strip = s.strip()
    if s_strip.startswith("{") or s_strip.startswith("["):
        return True
    if '"category"' in s or '"explanation"' in s or '"title"' in s:
        return True
    if s.count('"') > 6 and s.count(':') > 3:
        return True
    return False


def _clean_explanation(text: str) -> str:
    """Remove accidental JSON fragments or metadata headers while preserving article content."""
    if not text:
        return ""
    if _looks_like_json(text):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and "explanation" in parsed:
                text = parsed["explanation"]
            else:
                if isinstance(parsed, dict):
                    for k, v in parsed.items():
                        if isinstance(v, str) and len(v) > 80:
                            text = v
                            break
        except Exception:
            pass

    cleaned_lines = []
    for line in text.splitlines():
        if re.match(r'^\s*(Category|Subcategory|Title)\s*[:\-–—]', line, re.IGNORECASE):
            continue
        if re.search(r'"\w+"\s*:', line):
            continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip()
    cleaned = re.sub(r'\n\s*\n+', '\n\n', cleaned)
    return cleaned


def _clean_title(title: str) -> str | None:
    if not title:
        return None
    t = title.strip()
    if _looks_like_json(t):
        return None
    t = " ".join(t.split())
    if len(t) > 180:
        return None
    t = t.strip('\'"')
    return t


async def generate_tech_fact() -> tuple[str, str, str, str]:
    """Generate a tech fact using principle-first reasoning and single-pass structured output."""
    try:
        COOLDOWN_N = 15
        DEDUP_HISTORY = 30

        HIERARCHY = {
            "Computer Science Foundations": [
                "Data Structures & Algorithms", "Operating Systems", "Databases (DBMS)", "Computer Networks"
            ],
            "System Design & Development": [
                "Architecture patterns", "APIs", "Microservices", "Distributed systems"
            ],
            "Emerging Technologies": [
                "AI/ML", "IoT", "Blockchain", "AR/VR", "Edge computing", "Cloud platforms"
            ],
            "Software Engineering Practices": [
                "Version control", "CI/CD", "Testing strategies", "Productivity tools"
            ],
            "Security & Privacy": [
                "Encryption", "Authentication", "Secure protocols"
            ]
        }

        # Category/subcategory cooldown
        last_n = get_last_n_topics(COOLDOWN_N)
        recent_subcat = set((cat, subcat) for cat, subcat, _ in last_n)
        eligible_pairs = []
        for cat, subcats in HIERARCHY.items():
            for subcat in subcats:
                if (cat, subcat) not in recent_subcat:
                    eligible_pairs.append((cat, subcat))

        if not eligible_pairs:
            eligible_pairs = [(cat, subcat) for cat, subcats in HIERARCHY.items() for subcat in subcats]

        category, subcategory = random.choice(eligible_pairs)
        
        # Select a lens for variety
        lens = random.choice(LENSES)
        
        # Sample apps for real-world context
        sample_apps = random.sample(APPS, min(5, len(APPS)))
        
        # Get recent titles for explicit deduplication
        recent_titles = get_recent_titles_for_dedup(DEDUP_HISTORY)
        recent_titles_str = "\n".join([f"- {t}" for t in recent_titles]) if recent_titles else "(No previous topics yet)"

        try:
            from google.genai import types
            
            # Cannot use tools (google_search) with response_mime_type="application/json"
            config = types.GenerateContentConfig(
                response_mime_type="application/json"
            )

            prompt = f"""
You are a tech writer for curious people who are not CS graduates.

{WRITING_PRINCIPLES}

Write about:
- Category: {category}
- Subcategory: {subcategory}
- Angle: {lens}

You can reference apps like: {', '.join(sample_apps)}

Avoid these recent topics:
{recent_titles_str}

Output JSON:
{{
  "category": "{category}",
  "subcategory": "{subcategory}",
  "title": "curiosity-driven, no colons, no jargon",
  "content": "150-200 words, short sentences, explain jargon in parentheses"
}}

Rules:
1. IMPORTANT: Title and content must be about the SAME app or topic. Do not mix.
2. No unexplained jargon - always add explanation in parentheses
3. Max 15 words per sentence
4. Start with something relatable
5. Title has no colon, sounds like a YouTube video someone would click
6. Last sentence under 10 words
"""

            response = client.models.generate_content(
                model="gemini-flash-latest",
                contents=prompt,
                config=config
            )
            resp_text = response.text if hasattr(response, "text") else str(response)
            logging.info(f"Gemini response preview: {resp_text[:300]}...")
            
            # Parse JSON response
            try:
                topic_json = json.loads(resp_text)
                for field in ["category", "subcategory", "title", "content"]:
                    if field not in topic_json:
                        raise ValueError(f"Missing field: {field}")
            except Exception as e:
                logging.error(f"Error parsing Gemini JSON: {e}")
                return (category, subcategory, "API Parsing Failed", resp_text if resp_text else "No response text")
            
            raw_title = topic_json.get("title", "")
            raw_content = topic_json.get("content", "")

            title = _clean_title(raw_title)
            explanation = _clean_explanation(raw_content)

            if not title or not explanation or _looks_like_json(title) or _looks_like_json(explanation):
                logging.warning("Parsed content looks invalid or JSON-like after sanitization.")
                return (category, subcategory, "API Parsing Failed", resp_text)

            # Save to database
            add_discussed_topic(
                topic_json.get("category", category),
                topic_json.get("subcategory", subcategory),
                title,
                explanation
            )

            return (
                topic_json.get("category", category),
                topic_json.get("subcategory", subcategory),
                title,
                explanation
            )
            
        except Exception as e:
            logging.error(f"Gemini generation error: {e}")
            return (category, subcategory, "Generation Failed", f"Could not generate content: {str(e)}")
            
    except Exception as e:
        logging.error(f"generate_tech_fact: Unhandled exception: {e}", exc_info=True)
        raise


telethn = TelegramClient('botto_session', API_ID, API_HASH)
post_lock = asyncio.Lock()


async def post_message():
    category, subcategory, title, explanation = await generate_tech_fact()
    if title in ("API Parsing Failed", "Generation Failed"):
        try:
            await telethn.send_message(OWNER_ID, (
                f"Topic generation failed.\n"
                f"Category: {category}\nSubcategory: {subcategory}\nTitle: {title}\nExplanation: {explanation}"
            ))
            logging.warning("Generation failed or fallback used. Not posting to channel.")
        except Exception as e:
            logging.error(f"Failed to notify owner: {e}")
        return

    if _looks_like_json(title) or _looks_like_json(explanation):
        logging.error("Final content looks JSON-like. Aborting post and notifying owner.")
        try:
            await telethn.send_message(OWNER_ID, (
                "Aborted posting because generated content looked like JSON after final checks.\n"
                f"Category: {category}\nSubcategory: {subcategory}\nTitle (preview): {title}\nExplanation (preview): {explanation[:800]}"
            ))
        except Exception as e:
            logging.error(f"Failed to notify owner after JSON-suspect content: {e}")
        return

    if len(explanation) > 3800:
        explanation = explanation[:3800].rsplit("\n", 1)[0] + "..."

    def format_explanation(text):
        lines = [line.rstrip() for line in text.split('\n')]
        formatted = []
        prev_blank = False
        for line in lines:
            if not line.strip():
                if not prev_blank:
                    formatted.append("")
                prev_blank = True
            else:
                formatted.append(line.strip())
                prev_blank = False
        return '\n'.join([l for l in formatted if l is not None])

    clean_explanation = format_explanation(explanation)
    safe_title = re.sub(r'[\n\r]+', ' ', title).strip()
    post_text = f"<b>{safe_title}</b>\n\n{clean_explanation}"

    try:
        await telethn.send_message(
            CHAT_ID,
            post_text,
            parse_mode="html"
        )
        logging.info(f"Posted: {category} - {subcategory} - {safe_title}")
    except Exception as e:
        logging.error(f"Failed to send message: {e}")
        try:
            await telethn.send_message(OWNER_ID, f"Failed to post topic: {safe_title}\nError: {e}")
        except Exception as e2:
            logging.error(f"Failed to notify owner after post failure: {e2}")


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
    init_database()
    await telethn.start(bot_token=BOT_TOKEN)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(post_message, "interval", hours=12)
    scheduler.start()
    logging.info("Botto started nyan nyan :3")
    await telethn.run_until_disconnected()


asyncio.run(main())
