# -*- coding: utf-8 -*-
"""
ai_module.py — OpenAI API integration for ticket analysis.
Supports image attachments via vision API (gpt-4o-mini).
"""
import json
import re
import os
import base64
import time

from config import OPENAI_API_KEY, OPENAI_MODEL

_client = None


def get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


TICKET_TYPES = [
    "Жалоба",
    "Смена данных",
    "Консультация",
    "Претензия",
    "Неработоспособность приложения",
    "Мошеннические действия",
    "Спам",
]

FALLBACK = {
    "ticket_type": "Консультация",
    "sentiment": "Neutral",
    "priority_score": 5,
    "language": "RU",
    "summary": "Ошибка анализа — требуется ручная проверка.",
    "recommendation": "Обратитесь к клиенту для уточнения деталей.",
    "latitude": None,
    "longitude": None,
}

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def load_image_as_base64(filename, max_pixels=1568):
    """
    Load an image file from data/, resize if too large, return (base64_data, media_type) or None.
    """
    filepath = os.path.join(DATA_DIR, filename)
    if not os.path.exists(filepath):
        print(f"  [AI] Attachment file not found: {filepath}")
        return None

    try:
        from PIL import Image
        from io import BytesIO

        img = Image.open(filepath)
        if max(img.size) > max_pixels:
            ratio = max_pixels / max(img.size)
            new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        buf = BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")
        return b64, "image/png"
    except ImportError:
        print("  [AI] Pillow not installed, cannot process images")
        return None
    except Exception as e:
        print(f"  [AI] Error loading image {filename}: {e}")
        return None


# Known image descriptions for fallback when vision is unavailable
KNOWN_IMAGES = {
    "data_error.png": (
        "Screenshot of Freedom Broker mobile app showing MSFT.US (Microsoft) stock page. "
        "Price shown is 400.23 with -0.36 / -0.09% change. Period indicators show: "
        "Week +2.55%, Month +4.12%, Half-year +8.76%, Year -1.83%. "
        "The customer is complaining that these dynamics indicators are incorrect/inaccurate."
    ),
    "order_error.png": (
        "Screenshot of Freedom Finance trading platform (desktop). Shows a red error dialog: "
        "'Ошибка отправки приказа' (Order submission error) / 'Ошибка выставления приказа' "
        "(Order placement error). The customer could not place a trade order."
    ),
}


def postprocess_analysis(result, description_text="", attachment_name=""):
    """Apply rule-based corrections after AI analysis."""
    desc_lower = (description_text or "").lower()

    # Fraud tickets → high priority
    if result.get("ticket_type") == "Мошеннические действия":
        result["priority_score"] = max(result.get("priority_score", 5), 9)

    # Spam → lowest priority
    if result.get("ticket_type") == "Спам":
        result["priority_score"] = 1

    # Negative sentiment + lawsuit keywords → high priority
    lawsuit_keywords = ["суд", "заявление в правоохранительные", "прокуратур", "иск", "адвокат"]
    if result.get("sentiment") == "Negative" and any(kw in desc_lower for kw in lawsuit_keywords):
        result["priority_score"] = max(result.get("priority_score", 5), 8)

    # Attachment with "error" in name → priority bump
    if attachment_name and "error" in attachment_name.lower():
        result["priority_score"] = min(10, result.get("priority_score", 5) + 1)

    return result


def analyze_ticket(ticket) -> dict:
    """
    Call OpenAI to analyze a Ticket object and return a dict with:
      ticket_type, sentiment, priority_score, language,
      summary, recommendation, latitude, longitude
    """
    address_parts = [
        ticket.region or "",
        ticket.city or "",
        ticket.street or "",
        ticket.building or "",
    ]
    address = ", ".join(p for p in address_parts if p).strip(", ")

    description_text = ticket.description or ""
    attachment_name = (ticket.attachments or "").strip()

    # Build image context
    image_context = ""
    image_b64 = None

    if attachment_name:
        image_b64 = load_image_as_base64(attachment_name)

        # Add text description from known images as supplement
        if attachment_name in KNOWN_IMAGES:
            image_context = (
                f"\n\nATTACHED IMAGE ({attachment_name}): {KNOWN_IMAGES[attachment_name]}"
            )
        elif not image_b64:
            image_context = f"\n\nNote: Customer attached a file named '{attachment_name}' but it could not be loaded."

    if not description_text and attachment_name:
        description_text = (
            f"(No text provided — customer submitted only an image: {attachment_name}. "
            "Analyze the image to understand their issue.)"
        )

    image_instruction = ""
    if image_b64 or image_context:
        image_instruction = """
If image information is provided, incorporate it into your analysis:
- Identify what the image shows (UI elements, error messages, data displayed)
- If it shows an error, identify the error type and what went wrong
- If it shows data/charts, identify any inaccuracies the customer might be referring to
- Incorporate your image analysis into the summary and recommendation
"""

    prompt = f"""You are an AI assistant for a Kazakh brokerage firm's customer support system.

Analyze the customer support ticket below and return ONLY a valid JSON object.
{image_instruction}
--- TICKET ---
Description: {description_text or "(empty)"}
Customer segment: {ticket.segment or "Mass"}
Address: {address or "(unknown)"}
Country: {ticket.country or "Kazakhstan"}{image_context}
--- END TICKET ---

Return this exact JSON structure:
{{
  "ticket_type": "<one of: Жалоба | Смена данных | Консультация | Претензия | Неработоспособность приложения | Мошеннические действия | Спам>",
  "sentiment": "<Positive | Neutral | Negative>",
  "priority_score": <integer 1-10>,
  "language": "<KZ | ENG | RU>",
  "summary": "<1-2 sentence summary of the issue in Russian>",
  "recommendation": "<actionable advice for the manager in Russian, 1-2 sentences>",
  "latitude": <float or null>,
  "longitude": <float or null>
}}

Rules:
- ticket_type: pick the best matching category from the list above.
- sentiment: how the customer feels (Positive/Neutral/Negative).
- priority_score: 1=least urgent (general question), 10=most urgent (fraud, account blocked, money lost).
- language: detect the primary language of the ticket text. Default to RU if unclear or mixed.
- summary: concise description of the problem in Russian.
- recommendation: what the manager should do first.
- latitude/longitude: approximate GPS coordinates for the city/region in Kazakhstan.
  If the city is recognizable (e.g. Алматы, Астана, Шымкент), return its coordinates.
  If the address is empty, unknown, or outside Kazakhstan, return null for both."""

    # Build message content blocks
    content_blocks = []
    if image_b64:
        b64_data, media_type = image_b64
        content_blocks.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{media_type};base64,{b64_data}",
            }
        })
    content_blocks.append({"type": "text", "text": prompt})

    def _call_with_retry(messages, max_retries=5):
        """Call OpenAI API with exponential backoff for rate limits."""
        for attempt in range(max_retries):
            try:
                response = get_client().chat.completions.create(
                    model=OPENAI_MODEL,
                    max_tokens=1024,
                    response_format={"type": "json_object"},
                    messages=[{"role": "user", "content": messages}],
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "rate_limit" in err_str:
                    wait = 2 ** attempt + 1  # 2, 3, 5, 9, 17 seconds
                    print(f"  [AI] Rate limited (attempt {attempt+1}/{max_retries}), waiting {wait}s...")
                    time.sleep(wait)
                    continue
                raise  # re-raise non-rate-limit errors
        raise Exception(f"Rate limit exceeded after {max_retries} retries")

    try:
        raw = _call_with_retry(content_blocks)
    except Exception as e:
        if image_b64:
            print(f"  [AI] Vision call failed, retrying text-only: {e}")
            try:
                raw = _call_with_retry(prompt)
            except Exception as e2:
                print(f"  [AI] LLM error for ticket {ticket.id}: {e2}")
                return FALLBACK.copy()
        else:
            print(f"  [AI] LLM error for ticket {ticket.id}: {e}")
            return FALLBACK.copy()

    try:
        result = json.loads(raw)

        # Validate ticket_type
        if result.get("ticket_type") not in TICKET_TYPES:
            result["ticket_type"] = "Консультация"

        # Clamp priority score
        try:
            result["priority_score"] = max(1, min(10, int(result["priority_score"])))
        except (TypeError, ValueError):
            result["priority_score"] = 5

        # Post-processing corrections
        result = postprocess_analysis(result, description_text, attachment_name)

        return result

    except json.JSONDecodeError as e:
        print(f"  [AI] JSON parse error for ticket {ticket.id}: {e}")
        print(f"  [AI] Raw response: {raw[:300]}")
        return FALLBACK.copy()
