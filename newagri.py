import streamlit as st
from pymongo import MongoClient
import certifi
from google import genai
from google.genai import types
from gtts import gTTS
import os
import time
import requests
from audio_recorder_streamlit import audio_recorder
import io

# --- 1. SETUP & CONFIGURATION ---
st.set_page_config(page_title="AgriBot 2.0", page_icon="🌱", layout="centered")

@st.cache_resource
def init_db():
    # REPLACE 'yourpassword' with your actual database password!
    MONGO_URI = "mongodb+srv://Joel123:Joshua1976@joelgeorge.trbt1u2.mongodb.net/?retryWrites=true&w=majority&appName=JoelGeorge"
    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    return client["agribot_pure_data_db"]

db = init_db()

# SECURE API KEY LOAD (Reads from Streamlit Secrets)
api_key = os.environ.get("GEMINI_API_KEY", "")
client_ai = genai.Client(api_key=api_key)

# --- 2. SESSION STATE MANAGEMENT ---
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.farmer_id = None

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "suggestions" not in st.session_state:
    st.session_state.suggestions = []
if "auto_submit_text" not in st.session_state:
    st.session_state.auto_submit_text = None

if "text_key_counter" not in st.session_state:
    st.session_state.text_key_counter = 0

def clear_chat():
    st.session_state.chat_history = []
    st.session_state.suggestions = []
    st.session_state.auto_submit_text = None
    st.session_state.text_key_counter += 1

# --- PAGE 1: THE LOGIN SCREEN ---
def login_page():
    st.title("🌱 Demetrix AgriBot")
    st.subheader("Farmer Login")
    farmer_id = st.text_input("Farmer ID (e.g., FARMER-001-1234):")

    if st.button("Login", type="primary"):
        if farmer_id:
            farmer = db.farmer_profiles.find_one({"farmer_id": farmer_id})
            if farmer:
                st.session_state.logged_in = True
                st.session_state.farmer_id = farmer_id
                clear_chat()
                st.rerun()
            else:
                st.error("Farmer ID not found in database. Please check the ID and try again.")
        else:
            st.warning("Please enter an ID.")

# --- MARKET PRICE WIDGET ---
def show_market_price_widget(profile, cycle):
    crop = cycle.get("crop", "")
    district = profile.get("district", "")

    agmarknet_api_key = os.environ.get(
        "AGMARKNET_API_KEY",
        "579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b"
    )

    url = "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070"
    params = {
        "api-key": agmarknet_api_key,
        "format": "json",
        "limit": 5,
        "filters[state]": "Karnataka",
        "filters[district]": district,
        "filters[commodity]": crop
    }

    try:
        response = requests.get(url, params=params, timeout=5)
        data = response.json()
        records = data.get("records", [])

        if records:
            st.subheader("📈 Today's Mandi Prices")
            cols = st.columns(len(records[:3]))
            for i, record in enumerate(records[:3]):
                with cols[i]:
                    market = record.get("market", "Unknown Market")
                    modal_price = record.get("modal_price", "N/A")
                    min_price = record.get("min_price", "N/A")
                    max_price = record.get("max_price", "N/A")
                    arrival_date = record.get("arrival_date", "")

                    st.metric(
                        label=f"🏪 {market}",
                        value=f"₹{modal_price}/q",
                        delta=f"Range: ₹{min_price}–₹{max_price}"
                    )
                    if arrival_date:
                        st.caption(f"📅 {arrival_date}")
            st.markdown("---")
        else:
            st.info(f"📊 No mandi price data found for {crop} in {district} today.")
            st.markdown("---")

    except Exception as e:
        st.warning("📡 Market prices unavailable right now.")
        st.markdown("---")

# --- CORE LOGIC: THE AI CALL ---
def process_agribot_query(text_query=None, uploaded_image=None, raw_audio_data=None):
    profile = db.farmer_profiles.find_one({"farmer_id": st.session_state.farmer_id})
    cycle = db.active_crop_cycles.find_one({"farmer_id": st.session_state.farmer_id})

    weather_cursor = db.weather_telemetry.find(
        {"location.district": profile.get("district")}
    ).sort("timestamp", -1).limit(1)
    weather = next(weather_cursor, None)

    user_display = text_query if text_query else ""
    if uploaded_image: user_display += " [📷 Image Uploaded]"
    if raw_audio_data: user_display += " [🎤 Audio Recorded]"

    st.session_state.chat_history.append({"role": "user", "content": user_display.strip()})

    weather_text = f"{weather['metrics']['temperature_c']}°C, {weather['metrics']['humidity_pct']}% Humidity, Rain (1h): {weather['metrics'].get('rainfall_1h_mm', 0)}mm" if weather else "Unknown"
    phenology = cycle.get("phenology", {})
    tasks = cycle.get("tasks", {})

    context = f"""
    Name: {profile.get('name')}, Location: {profile.get('district')}, Karnataka.
    Crop: {cycle.get('crop')}, Stage: {phenology.get('current_stage')} ({phenology.get('days_after_sowing')} Days After Sowing).
    Completed Tasks: {', '.join(tasks.get('completed', []))}
    Pending/Current Tasks: {', '.join(tasks.get('pending', []))}
    Current Local Weather: {weather_text}
    """

    conversation_history = ""
    for msg in st.session_state.chat_history:
        role_name = "Farmer" if msg["role"] == "user" else "AgriBot"
        msg_content = msg["content"] if msg["role"] == "user" else msg.get("kannada", "")
        if msg_content:
            conversation_history += f"{role_name}: {msg_content}\n"

    prompt_instructions = f"""
    You are AgriBot 2.0, an AI agricultural assistant designed to help farmers. You are an AI, NOT a human.
    Farmer Data & Current Status: {context.strip()}

    Recent Conversation Context:
    {conversation_history}

    Instructions:
    1. Answer agricultural questions using their specific profile data, their CURRENT pending tasks, their local weather, and the conversation history.
    2. WEATHER PERMISSION: You HAVE access to their current local weather in the data above. If the farmer asks about the weather, YOU MUST answer using that exact data and explain how it affects their crops or tasks.
    3. GUARDRAIL: Only decline if the question is completely unrelated to farming, weather, or crops (for example: politics, movies, or coding).
    4. Keep the main answer to 2-3 short sentences.

    CRITICAL FORMATTING RULE:
    You MUST provide your answer first in Kannada, followed by the exact English translation, and then the suggestions.
    For the suggestions, you MUST provide the Kannada question, followed by a pipe character "|", followed by the English translation.
    Format EXACTLY like this:

    [Your main Kannada answer here]
    ---ENGLISH---
    [Your exact English translation here]
    ---SUGGESTIONS---
    [Follow-up question 1 in Kannada] | [Follow-up question 1 in English]
    [Follow-up question 2 in Kannada] | [Follow-up question 2 in English]
    """

    contents = []

    if raw_audio_data:
        contents.append(types.Part.from_bytes(data=raw_audio_data, mime_type='audio/wav'))

    # --- THE HEIC IMAGE FIX ---
    if uploaded_image:
        image_mime_type = 'image/jpeg' # Default
        filename = uploaded_image.name.lower()
        if filename.endswith('.png'):
            image_mime_type = 'image/png'
        elif filename.endswith(('.heic', '.heif')):
            image_mime_type = 'image/heic'
        elif filename.endswith('.webp'):
            image_mime_type = 'image/webp'
            
        contents.append(types.Part.from_bytes(data=uploaded_image.read(), mime_type=image_mime_type))

    contents.append(prompt_instructions)

    max_retries = 3
    response = None

    for attempt in range(max_retries):
        try:
            response = client_ai.models.generate_content(
                model='gemini-2.5-flash-lite',
                contents=contents
            )
            break
        except Exception as e:
            error_msg = str(e)
            if "503" in error_msg or "UNAVAILABLE" in error_msg:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
            st.error(f"📡 AI Error: {error_msg}")
            if st.session_state.chat_history:
                st.session_state.chat_history.pop()
            return False

    if response:
        raw_text = response.text.strip()
        kannada_part = "Error parsing Kannada response."
        english_part = "Error parsing English translation."
        suggestions = []

        if "---ENGLISH---" in raw_text and "---SUGGESTIONS---" in raw_text:
            parts_1 = raw_text.split("---ENGLISH---")
            kannada_part = parts_1[0].strip()

            parts_2 = parts_1[1].split("---SUGGESTIONS---")
            english_part = parts_2[0].strip()

            raw_suggestions = [s.strip() for s in parts_2[1].strip().split('\n') if s.strip()]
            for sug in raw_suggestions:
                if "|" in sug:
                    kn_text, en_text = sug.split("|", 1)
                    suggestions.append({"kn": kn_text.strip(), "en": en_text.strip()})
                else:
                    suggestions.append({"kn": sug.strip(), "en": ""})

        st.session_state.suggestions = suggestions[:3]

        reply_audio_bytes = None
        try:
            tts = gTTS(text=kannada_part, lang='kn')
            audio_fp = io.BytesIO()
            tts.write_to_fp(audio_fp)
            audio_fp.seek(0)
            reply_audio_bytes = audio_fp.read()
        except Exception as e:
            st.error(f"Audio generation failed: {e}")

        st.session_state.chat_history.append({
            "role": "assistant",
            "kannada": kannada_part,
            "english": english_part,
            "audio_bytes": reply_audio_bytes
        })

        return True

# --- PAGE 2: THE PERSONALIZED DASHBOARD ---
def dashboard_page():
    profile = db.farmer_profiles.find_one({"farmer_id": st.session_state.farmer_id})
    cycle = db.active_crop_cycles.find_one({"farmer_id": st.session_state.farmer_id})

    col1, col2 = st.columns([0.8, 0.2])
    with col1:
        st.title(f"👋 Namaskara, {profile.get('name')}!")
        st.caption(f"📍 {profile.get('district')} | 📱 {profile.get('contact_number')} | 🌱 {cycle.get('crop')} ({cycle.get('phenology', {}).get('current_stage')})")
    with col2:
        if st.button("Log Out"):
            st.session_state.clear()
            st.rerun()
    st.markdown("---")

    show_market_price_widget(profile, cycle)

    if not st.session_state.chat_history:
        st.write("How can I help you today? You can type, record a voice note, or upload a picture.")
    else:
        for msg in st.session_state.chat_history:
            if msg["role"] == "user":
                with st.chat_message("user"):
                    st.write(msg["content"])
            else:
                with st.chat_message("assistant", avatar="🌱"):
                    st.write(f"**{msg['kannada']}**")
                    
                    if msg.get("audio_bytes"):
                        st.audio(msg["audio_bytes"], format="audio/mp3")
                        
                    st.success(f"🌐 Translation: *{msg['english']}*")

    if st.session_state.suggestions:
        st.write("")
        st.caption("**Suggested Questions:**")
        cols = st.columns(len(st.session_state.suggestions))
        for idx, sug in enumerate(st.session_state.suggestions):
            with cols[idx]:
                btn_label = f"{sug['kn']}\n\n*{sug['en']}*" if sug['en'] else sug['kn']
                if st.button(btn_label, key=f"sug_{idx}_{len(st.session_state.chat_history)}", use_container_width=True):
                    st.session_state.auto_submit_text = sug['kn']
                    st.rerun()
    st.markdown("---")

    default_text = st.session_state.auto_submit_text if st.session_state.auto_submit_text else ""
    text_query = st.text_area("✍️ Type your question:", value=default_text, key=f"user_text_input_{st.session_state.text_key_counter}")

    col3, col4, col5 = st.columns([1, 1, 1])
    
    with col3:
        st.write("🎤 Live Voice Note")
        audio_bytes = audio_recorder(text="", icon_size="2x", icon_name="microphone")
        if audio_bytes:
            st.success("✅ Audio recorded! Ready to analyze.")

    with col4:
        # --- THE HEIC UPLOADER FIX ---
        uploaded_image = st.file_uploader("📷 Upload Image", type=["jpg", "jpeg", "png", "heic", "heif", "webp"])
        
    with col5:
        st.write("")
        st.write("")
        if st.button("🔄 Clear Chat", type="secondary", use_container_width=True):
            clear_chat()
            st.rerun()

    is_suggestion = st.session_state.auto_submit_text is not None
    final_query = st.session_state.auto_submit_text if is_suggestion else text_query

    if st.button("Analyze & Diagnose", type="primary", use_container_width=True) or is_suggestion:
        st.session_state.auto_submit_text = None

        if not (final_query or uploaded_image or audio_bytes):
            st.warning("Please type a question, record audio, or upload an image.")
            return

        with st.spinner("AgriBot is listening and analyzing..."):
            is_success = process_agribot_query(final_query, uploaded_image, raw_audio_data=audio_bytes)
            if is_success:
                st.session_state.text_key_counter += 1
                st.rerun()

if st.session_state.logged_in:
    dashboard_page()
else:
    login_page()
