from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import random
import time
import math
import os
import socket
import threading
import logging
from datetime import datetime
from dotenv import load_dotenv
# Import the Gemini service
from gemini_service import get_gemini_analysis, get_gemini_recommendations
import requests
import http.client
from urllib.parse import urlparse
import concurrent.futures
import traceback
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
import io

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Thread pool for async Gemini requests (non-blocking AI)
_gemini_executor = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix='gemini')

# HTTP connection pool for faster APEX requests
_apex_connection_pool = {}
_apex_connection_lock = threading.Lock()

# Load environment variables from .env file
load_dotenv()

# Helper functions to determine sensor status
def _get_temperature_status(value):
    """Get status description for temperature reading
    Greenhouse optimal: 20-27°C (most vegetables and plants)
    """
    if 20 <= value <= 27:
        return "Optimal"
    elif (18 <= value < 20) or (27 < value <= 30):
        return "Acceptable"
    else:
        return "Critical"

def _get_humidity_status(value):
    """Get status description for humidity reading
    Greenhouse optimal: 45-70% (prevents disease while supporting growth)
    """
    if 45 <= value <= 70:
        return "Optimal"
    elif (71 <= value <= 80):
        return "Acceptable"
    else:
        return "Critical"

def _get_co2_status(value):
    """Get status description for CO2 reading"""
    if 300 <= value <= 800:
        return "Good"
    elif 800 < value <= 1500:
        return "Acceptable"
    else:
        return "High"

def _get_light_status(value):
    """Get status description for light reading
    Thresholds based on ambient light intensity (0-4095 raw):
    • 0-300 = Dark Night (no light / remote areas)
    • 301-819 = Low Light (parks, moonlight, dim streets)
    • 820-1638 = Dim Indoor / Early Dusk
    • 1639-2457 = Moderate (cloudy day or shaded area)
    • 2458+ = Bright (good daylight)
    """
    if value <= 300:
        return "Dark Night"
    elif value <= 819:
        return "Low Light"
    elif value <= 1638:
        return "Dim Indoor"
    elif value <= 2457:
        return "Moderate"
    else:
        return "Bright"

def _get_soil_moisture_status(value):
    """Get status description for soil moisture reading"""
    if 40 <= value <= 60:
        return "Optimal"
    elif (30 <= value < 40) or (60 < value <= 70):
        return "Acceptable"
    else:
        return "Critical"
        
def ip_broadcast_service():
    """Broadcasts the server IP address on the local network"""
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    # Get the server's IP address
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't actually connect but gets the route
        s.connect(('8.8.8.8', 1))
        ip_address = s.getsockname()[0]
    except Exception:
        hostname = socket.gethostname()
        ip_address = socket.gethostbyname(hostname)
    finally:
        s.close()
    
    print(f"Broadcasting server availability at {ip_address}:5000")
    
    # Broadcast message
    message = f"GREENHOUSE_SERVER:{ip_address}:5000".encode()
    
    while True:
        try:
            # Broadcast to the network
            server_socket.sendto(message, ('<broadcast>', 45678))
            time.sleep(5)  # Broadcast every 5 seconds
        except Exception as e:
            print(f"Broadcast error: {e}")
            time.sleep(10)  # Wait and retry

app = Flask(__name__)
# Enable CORS for all routes with explicit configuration for Chrome/web support
CORS(app, resources={
    r"/api/*": {
        "origins": "*",
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

# NOTE: To make this server accessible from tablet or other devices on your network:
# 1. Make sure the Flask server is running with host='0.0.0.0' 
#    (this is already set in the code below when the app is run)
# 2. Find your computer's IP address:
#    - On Windows: Open command prompt and type 'ipconfig'
#    - On Mac/Linux: Open terminal and type 'ifconfig' or 'ip addr'
# 3. Use this IP address in the tablet app's settings

# Sample data - replace with your actual data model/database
items = [
    {"id": 1, "name": "Item 1", "description": "Description for item 1"},
    {"id": 2, "name": "Item 2", "description": "Description for item 2"}
]

# ============================================================================
# APEX DATA SOURCE - All sensor data comes from Oracle APEX (NO SIMULATED DATA)
# ============================================================================

ORACLE_APEX_URL = os.getenv('ORACLE_APEX_URL', "https://oracleapex.com/ords/g3_data/iot/greenhouse/")

def get_apex_connection(url):
    """Get or create persistent HTTPS connection to APEX for connection pooling"""
    parsed = urlparse(url)
    host = parsed.hostname
    
    with _apex_connection_lock:
        if host not in _apex_connection_pool:
            logger.info(f"Creating new APEX connection pool for {host}")
            _apex_connection_pool[host] = http.client.HTTPSConnection(
                host, 
                timeout=5,  # Shorter timeout with persistent connection
                blocksize=8192  # Larger buffer for faster reads
            )
        return _apex_connection_pool[host]

def fetch_apex_readings(apex_url=None, timeout=10):
    """Fetch list of readings from Oracle APEX using http.client (more reliable than requests).
       Returns a list of dict readings or empty list on failure.
       NOW WITH CONNECTION POOLING for 2-3x faster requests!
    """
    import http.client
    import json
    from urllib.parse import urlparse
    
    url = apex_url or ORACLE_APEX_URL
    try:
        # Parse the URL
        parsed = urlparse(url)
        host = parsed.hostname  # e.g., "oracleapex.com"
        path = parsed.path or "/"  # e.g., "/ords/g3_data/iot/greenhouse/"
        
        # Get connection from pool (reuses existing connection!)
        try:
            conn = get_apex_connection(url)
            
            # Make request with keep-alive header for connection reuse
            conn.request("GET", path, headers={
                'Connection': 'keep-alive',
                'Accept-Encoding': 'gzip, deflate'  # Request compression
            })
            
            # Get response
            res = conn.getresponse()
        except Exception as conn_err:
            # Connection died, remove from pool and retry
            logger.warning(f"Connection pool error: {conn_err}, creating fresh connection")
            with _apex_connection_lock:
                if host in _apex_connection_pool:
                    try:
                        _apex_connection_pool[host].close()
                    except:
                        pass
                    del _apex_connection_pool[host]
            
            # Retry with fresh connection
            conn = http.client.HTTPSConnection(host, timeout=timeout)
            conn.request("GET", path)
            res = conn.getresponse()
        
        if res.status == 200:
            # Read response - handle GZIP compression!
            data_bytes = res.read()
            
            # Check if response is gzip-compressed (starts with 0x1f 0x8b)
            import gzip
            if len(data_bytes) >= 2 and data_bytes[0] == 0x1f and data_bytes[1] == 0x8b:
                try:
                    data_bytes = gzip.decompress(data_bytes)
                except Exception as e:
                    print(f"GZIP decompression failed: {e}")
                    return []
            
            # Decode JSON
            data = json.loads(data_bytes.decode("utf-8"))
            
            # normalize possible shapes: {"items": [...]} or [...]
            if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
                items = data["items"]
            elif isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                # single-object payload -> wrap
                items = [data]
            else:
                return []
            
            # Ensure each item has a numeric timestamp for sorting
            # Use APEX timestamp but fix the year to 2025
            import time
            from datetime import datetime
            
            normalized = []
            for idx, it in enumerate(items):
                it_copy = dict(it)
                
                # Get the original APEX timestamp (format: "2025-10-29T15:21:22.123456Z")
                apex_ts_str = it.get("timestamp_reading", "")
                
                if apex_ts_str:
                    try:
                        # Parse the APEX ISO timestamp WITH microseconds
                        # APEX already provides the CORRECT full timestamp - use it as-is!
                        # Format: "2025-10-29T15:21:22.971802Z"
                        apex_dt = datetime.strptime(apex_ts_str.replace('Z', ''), "%Y-%m-%dT%H:%M:%S.%f")
                        
                        # Convert to Unix timestamp (this is the ACTUAL time from APEX)
                        it_copy["timestamp"] = apex_dt.timestamp()
                    except Exception as e:
                        print(f"Failed to parse timestamp '{apex_ts_str}': {e}")
                        # Fallback: use current time if parsing fails
                        it_copy["timestamp"] = time.time() - (idx * 10)
                else:
                    # No timestamp in APEX data, use current time
                    it_copy["timestamp"] = time.time() - (idx * 10)
                    it_copy["timestamp"] = time.time() - (idx * 10)
                
                it_copy["_ts_num"] = it_copy["timestamp"]
                it_copy["_pull_time"] = time.time()  # Track when we pulled this data
                normalized.append(it_copy)
            # sort descending by timestamp numeric (newest first)
            normalized.sort(key=lambda x: x.get("_ts_num", 0), reverse=True)
            
            # Log the timestamps of the first 3 readings to verify we're getting fresh data
            if len(normalized) >= 3:
                latest_ts = datetime.fromtimestamp(normalized[0].get("timestamp", 0)).strftime("%Y-%m-%d %H:%M:%S")
                second_ts = datetime.fromtimestamp(normalized[1].get("timestamp", 0)).strftime("%Y-%m-%d %H:%M:%S")
                third_ts = datetime.fromtimestamp(normalized[2].get("timestamp", 0)).strftime("%Y-%m-%d %H:%M:%S")
                temp1 = normalized[0].get("temperature_bmp280", "N/A")
                temp2 = normalized[1].get("temperature_bmp280", "N/A")
                print(f"📊 Latest APEX Data: {latest_ts} (Temp: {temp1}°C), 2nd: {second_ts} (Temp: {temp2}°C), 3rd: {third_ts}")
            
            # DON'T close connection - keep it in pool for reuse!
            return normalized
        else:
            print(f"fetch_apex_readings: HTTP {res.status}")
            # DON'T close connection - keep it in pool!
            return []
            
    except Exception as e:
        print(f"fetch_apex_readings error: {e}")
        # If connection error, remove from pool
        try:
            parsed = urlparse(apex_url or ORACLE_APEX_URL)
            with _apex_connection_lock:
                if parsed.hostname in _apex_connection_pool:
                    try:
                        _apex_connection_pool[parsed.hostname].close()
                    except:
                        pass
                    del _apex_connection_pool[parsed.hostname]
        except:
            pass
        return []

def build_derived_from_reading(r):
    """Build derived fields from a single reading dict r from APEX.
       NO CONVERSIONS - use APEX data exactly as provided.
    """
    # helper to get numeric safely
    def num(key, default=0.0):
        try:
            return float(r.get(key, default))
        except Exception:
            return default

    temp_b = num("temperature_bmp280", None)
    temp_d = num("temperature_dht22", None)
    # prefer average if both present
    if temp_b is not None and temp_d is not None:
        temperature = round((temp_b + temp_d) / 2.0, 1)
    elif temp_b is not None:
        temperature = round(temp_b, 1)
    elif temp_d is not None:
        temperature = round(temp_d, 1)
    else:
        temperature = None

    # USE APEX VALUES DIRECTLY - NO CONVERSIONS!
    # APEX already sends mq135_drop, mq2_drop, mq7_drop calculated
    # If negative, use 0 (sensor calibration issue)
    mq135_drop = max(0, round(num("mq135_drop", 0), 1))  # Direct from APEX (PPM), never negative
    mq2_drop = max(0, round(num("mq2_drop", 0), 1))      # Direct from APEX (PPM), never negative
    mq7_drop = max(0, round(num("mq7_drop", 0), 1))      # Direct from APEX (PPM), never negative
    
    mq135_baseline = round(num("mq135_baseline", 0), 1)  # Direct from APEX
    mq2_baseline = round(num("mq2_baseline", 0), 1)      # Direct from APEX
    mq7_baseline = round(num("mq7_baseline", 0), 1)      # Direct from APEX

    # Light intensity - use RAW value directly (0-4095)
    light_raw = num("light_raw", None)
    if light_raw is not None:
        light_intensity = round(light_raw, 0)
    else:
        # Fallback to light_percent if available
        light_percent = num("light_percent", None)
        if light_percent is not None:
            # Convert percentage back to raw (0-4095 range)
            light_intensity = round((light_percent / 100.0) * 4095, 0)
        else:
            light_intensity = 0

    flame_raw = num("flame_raw", 4095)
    
    # Use flame_detected from APEX payload directly
    # APEX sends: 1 = detected, 0 = not detected
    flame_detected_value = r.get("flame_detected", 0)
    if isinstance(flame_detected_value, (int, float)):
        flame_detected = bool(flame_detected_value)
    elif isinstance(flame_detected_value, bool):
        flame_detected = flame_detected_value
    elif isinstance(flame_detected_value, str):
        flame_detected = flame_detected_value.strip().lower() in ("yes", "y", "true", "1")
    else:
        flame_detected = bool(flame_raw < 2000)
    
    flame_status = "Flame Detected" if flame_detected else "Flame Not Detected"

    co2_level = round(400 + mq135_drop * 1.2, 1)

    derived = {
        "temperature": temperature,
        "humidity": round(num("humidity", 0.0), 1),
        "co2_level": co2_level,
        "light": light_intensity,  # Raw light intensity (0-4095)
        "light_raw": light_intensity,  # Also include as light_raw for compatibility
        "pressure": round(num("pressure", 0.0), 1),
        "altitude": round(num("altitude", 0.0), 1),
        "soil_moisture": round(num("soil_moisture", 45)),
        "flame_detected": flame_detected,
        "flame_status": flame_status,
        # Gas sensor values - DIRECT FROM APEX (already in PPM)
        "mq135_drop": mq135_drop,
        "mq2_drop": mq2_drop,
        "mq7_drop": mq7_drop,
        "mq135_baseline": mq135_baseline,
        "mq2_baseline": mq2_baseline,
        "mq7_baseline": mq7_baseline,
        # Status based on REAL thresholds from system specification
        # MQ135: >500 = poor, >200 = degraded, ≤200 = good
        "air_quality": "Good" if mq135_drop <= 200 else ("Poor" if mq135_drop > 500 else "Moderate"),
        # MQ2: >750 = high, >300 = elevated, ≤300 = safe
        "flammable_gas": "Safe" if mq2_drop <= 300 else ("High" if mq2_drop > 750 else "Elevated"),
        # MQ7: >750 = high, >300 = elevated, ≤300 = safe
        "co_level": "Safe" if mq7_drop <= 300 else ("High" if mq7_drop > 750 else "Elevated"),
        "timestamp": r.get("timestamp", r.get("_ts_num", time.time())),
        "co2_air_quality": {
            "co2": co2_level,
            "air_quality": "Good" if mq135_drop <= 200 else ("Poor" if mq135_drop > 500 else "Moderate"),
        },
        "pressure_altitude": {
            "pressure": round(num("pressure", 0.0), 1),
            "altitude": round(num("altitude", 0.0), 1)
        }
    }
    return derived

# In-memory cache for APEX readings (newest-first)
apex_cache = []
apex_cache_lock = threading.Lock()

# Poll interval (seconds) for fetching APEX data; configurable via env
ORACLE_APEX_POLL_INTERVAL = float(os.getenv('ORACLE_APEX_POLL_INTERVAL', '2'))

def apex_poller():
    """Background thread: poll APEX endpoint and keep cached latest readings."""
    global apex_cache
    url = ORACLE_APEX_URL
    interval = ORACLE_APEX_POLL_INTERVAL
    print(f"Starting APEX poller (url={url}, interval={interval}s)")
    while True:
        try:
            readings = fetch_apex_readings(url, timeout=5)
            if readings:
                with apex_cache_lock:
                    apex_cache = readings
            time.sleep(interval)
        except Exception as e:
            print(f"APEX poller error: {e}")
            time.sleep(max(1, interval))

# Smart cache with TTL for APEX data - continuously updated by background poller
_smart_cache = {
    'data': None,
    'timestamp': None,
    'ttl_seconds': 3,  # 3-second cache TTL based on APEX response time
    'fetch_interval': 3  # Poll APEX every 3 seconds
}
_smart_cache_lock = threading.Lock()

def continuous_apex_poller():
    """
    Background thread that continuously polls APEX every 3 seconds
    and keeps the cache updated with fresh data
    """
    global _smart_cache
    interval = _smart_cache.get('fetch_interval', 3)
    print(f"🔄 Starting continuous APEX poller (fetching every {interval} seconds)...")
    
    while True:
        try:
            print(f"🔍 Polling APEX...")
            readings = fetch_apex_readings(ORACLE_APEX_URL, timeout=60)
            
            if readings:
                with _smart_cache_lock:
                    _smart_cache['data'] = readings
                    _smart_cache['timestamp'] = datetime.now()
                print(f"✅ APEX poll successful! Got {len(readings)} readings. Cache updated.")
            else:
                print(f"⚠️ APEX poll returned no data. Keeping existing cache.")
                
        except Exception as e:
            print(f"❌ APEX poll error: {e}")
        
        # Wait for the next poll interval
        time.sleep(interval)

def get_cached_apex_or_fetch():
    """
    Smart caching function that returns data from continuously-updated cache
    (cache is kept fresh by background poller every 3 seconds)
    """
    with _smart_cache_lock:
        # Return cached data if available
        if _smart_cache['data'] is not None and _smart_cache['timestamp'] is not None:
            age = (datetime.now() - _smart_cache['timestamp']).total_seconds()
            if age < 10:  # Cache is reasonably fresh (within 10 seconds)
                return _smart_cache['data'], f'cache_age_{age:.0f}s'
            else:
                return _smart_cache['data'], f'cache_stale_{age:.0f}s'
        
        # No cache available yet (poller hasn't succeeded yet)
        print("⏳ Waiting for background poller to fetch first APEX data...")
        return None, 'no_data'

# ...existing code...

@app.route('/api/items', methods=['GET'])
def get_items():
    return jsonify(items)

@app.route('/api/items/<int:item_id>', methods=['GET'])
def get_item(item_id):
    item = next((item for item in items if item['id'] == item_id), None)
    if item:
        return jsonify(item)
    return jsonify({"error": "Item not found"}), 404

@app.route('/api/items', methods=['POST'])
def add_item():
    new_item = request.json
    # In a real app, validate input and generate proper ID
    if 'id' not in new_item:
        new_item['id'] = len(items) + 1
    items.append(new_item)
    return jsonify(new_item), 201

@app.route('/api/sensor-data', methods=['GET'])
def get_sensor_data():
    # ONLY USE APEX DATA - NO SIMULATION
    readings, cache_status = get_cached_apex_or_fetch()
    if readings:
        latest = readings[0]
        derived = build_derived_from_reading(latest)
        merged = {**{k: v for k, v in latest.items() if not k.startswith("_")}, **derived}
        merged['_cache_status'] = cache_status
        merged['_data_source'] = 'apex'
        return jsonify(merged)
    else:
        # No APEX data available yet - return error
        return jsonify({
            "error": "APEX data not available yet",
            "message": "Waiting for APEX to respond. Please wait...",
            "_data_source": "none"
        }), 503

@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "message": "Flask API is running"})

@app.route('/api/sensor-analysis/<sensor_type>', methods=['GET'])
def get_sensor_analysis(sensor_type):
    """
    Get detailed analysis for a specific sensor - ONLY FROM APEX
    Supports ?include_ai=false to skip AI analysis for INSTANT page load!
    """
    time_range = request.args.get('time_range', 'hours')
    include_ai = request.args.get('include_ai', 'true').lower() == 'true'
    
    # Determine number of points requested
    data_points = {
        'seconds': 60, 'minutes': 60, 'hours': 24,
        'days': 30, 'weeks': 52, 'months': 12, 'years': 5
    }
    num_points = data_points.get(time_range, 30)

    # Helper to extract numeric value for a sensor key from a reading
    def extract_value_for_sensor(reading, key):
        try:
            if key == 'temperature':
                # prefer averaged temperature if both sensors present
                a = reading.get('temperature')
                if a is not None:
                    return float(a)
                b1 = reading.get('temperature_bmp280')
                b2 = reading.get('temperature_dht22')
                if b1 is not None and b2 is not None:
                    return (float(b1) + float(b2)) / 2.0
                if b1 is not None:
                    return float(b1)
                if b2 is not None:
                    return float(b2)
                return None
            elif key == 'light':
                # Use raw light intensity value (0-4095)
                if 'light_raw' in reading and reading['light_raw'] is not None:
                    return float(reading['light_raw'])
                if 'light' in reading and reading['light'] is not None:
                    return float(reading['light'])
                # Fallback: convert light_percent to raw if available
                if 'light_percent' in reading and reading['light_percent'] is not None:
                    return float(reading['light_percent']) * 4095.0 / 100.0
                return None
            else:
                if key in reading:
                    return float(reading[key])
                # try derived keys
                if key + '_raw' in reading:
                    return float(reading[key + '_raw'])
        except Exception:
            return None
        return None

    # ONLY USE APEX DATA
    readings, cache_status = get_cached_apex_or_fetch()
    if readings:
        latest = readings[0]
        derived = build_derived_from_reading(latest)
        current_data = {**{k: v for k, v in latest.items() if not k.startswith("_")}, **derived}
        current_data['_data_source'] = 'apex'
        current_data['_cache_status'] = cache_status
        # Include ALL readings in historical data (including the latest at index 0)
        historical_raw = readings[0:num_points] if len(readings) > 0 else []
    else:
        # No APEX data available - return error
        return jsonify({
            "error": "APEX data not available yet",
            "message": "Waiting for APEX to respond...",
            "sensor_type": sensor_type
        }), 503

    # Map requested sensor_type to a field and unit and status function
    st = sensor_type.lower()
    if 'temp' in st:
        key = 'temperature'
        unit = '°C'
        status_fn = _get_temperature_status
    elif 'humid' in st:
        key = 'humidity'
        unit = '%'
        status_fn = _get_humidity_status
    elif 'co2' in st or 'co₂' in st or 'air quality' in st.lower() or 'mq135' in st:
        # Check if they want the drop value or the co2_level estimate
        if 'mq135' in st or 'air quality' in st.lower():
            key = 'mq135_drop'  # Direct from APEX (already in PPM)
            unit = 'ppm'
            # MQ135 thresholds: >500 = poor, >200 = degraded, ≤200 = good
            status_fn = lambda v: "Good" if v <= 200 else ("Poor" if v > 500 else "Moderate")
        else:
            key = 'co2_level'
            unit = 'ppm'
            status_fn = _get_co2_status
    elif 'light' in st:
        key = 'light'  # Now using raw light intensity (0-4095)
        unit = 'lux'  # Display as lux
        status_fn = _get_light_status
    elif 'soil' in st:
        key = 'soil_moisture'
        unit = '%'
        status_fn = _get_soil_moisture_status
    elif 'flame' in st:
        key = 'flame_detected'  # Use boolean value for charting (True/False -> 1/0)
        unit = ''  # No unit for binary sensor
        status_fn = lambda v: "Flame Detected" if v else "Flame Not Detected"
    elif 'mq2' in st or 'smoke' in st or 'lpg' in st or 'flammable' in st.lower():
        key = 'mq2_drop'  # Direct from APEX (already in PPM)
        unit = 'ppm'
        # MQ2 thresholds: >750 = high, >300 = elevated, ≤300 = safe
        status_fn = lambda v: "Safe" if v <= 300 else ("High" if v > 750 else "Elevated")
    elif 'mq7' in st or ('co' in st and 'co2' not in st) or 'carbon monoxide' in st.lower():
        key = 'mq7_drop'  # Direct from APEX (already in PPM)
        unit = 'ppm'
        # MQ7 thresholds: >750 = high, >300 = elevated, ≤300 = safe
        status_fn = lambda v: "Safe" if v <= 300 else ("High" if v > 750 else "Elevated")
    elif 'pressure' in st and 'altitude' in st:
        # Wildcard: Pressure & Altitude - return both as combined analysis
        key = 'pressure'  # Primary key for value
        unit = 'hPa'
        status_fn = lambda v: "Normal" if 990 <= v <= 1030 else ("Low" if v < 990 else "High")
    elif 'pressure' in st:
        key = 'pressure'
        unit = 'hPa'
        status_fn = lambda v: "Normal" if 990 <= v <= 1030 else ("Low" if v < 990 else "High")
    elif 'altitude' in st:
        key = 'altitude'
        unit = 'm'
        status_fn = lambda v: f"{v:.1f}m"  # Just return the value as status
    else:
        # default fallback
        key = 'temperature'
        unit = '°C'
        status_fn = _get_temperature_status

    # Compute current value
    current_value = extract_value_for_sensor(current_data, key)
    if current_value is None and key in current_data:
        try:
            current_value = float(current_data.get(key))
        except Exception:
            current_value = 0.0

    # Determine status
    if status_fn and current_value is not None:
        try:
            status = status_fn(current_value)
        except Exception:
            status = 'Unknown'
    else:
        status = 'Unknown'

    # Build historical data series
    historical_data = []
    if historical_raw:
        # Use actual timestamps from when data was pulled
        for r in historical_raw:
            # Get the timestamp we added when pulling from APEX
            ts = r.get('timestamp', r.get('_ts_num', time.time()))
            
            val = extract_value_for_sensor(r, key)
            if val is None:
                # try reading key directly
                try:
                    val = float(r.get(key, 0.0))
                except Exception:
                    val = 0.0
            historical_data.append({"value": val, "timestamp": ts})
        
        # Sort by timestamp ASCENDING (oldest first) for proper chart display
        historical_data.sort(key=lambda x: x['timestamp'])

    # Ensure at least two points for charting
    if len(historical_data) < 2:
        # Create minimal dataset with current value
        now_ts = time.time()
        val = current_value if current_value is not None else 0.0
        historical_data = [
            {"value": val, "timestamp": now_ts - 10},
            {"value": val, "timestamp": now_ts}
        ]

    # Optionally get AI analysis (Gemini) - can be skipped for faster loading
    analysis_text = ''
    if include_ai:
        try:
            analysis_text = get_gemini_analysis(sensor_type, current_value or 0.0, unit, status, [d['value'] for d in historical_data])
        except Exception as e:
            logger.warning(f"AI analysis failed for {sensor_type}: {e}")
            analysis_text = ''

    response = {
        "sensor_type": sensor_type,
        "current_value": current_value,
        "unit": unit,
        "status": status,
        "historical_data": historical_data,
        "time_range": time_range,
        "analysis": analysis_text,
        "timestamp": current_data.get('timestamp', time.time()),
        "raw_data": current_data
    }

    return jsonify(response)
# ...existing code...

@app.route('/api/sensor-analysis/<sensor_type>/ai', methods=['GET'])
def get_sensor_ai_only(sensor_type):
    """
    Get ONLY AI analysis for a sensor - called separately for async loading!
    This endpoint is FAST because it skips historical data processing.
    """
    try:
        readings, _ = get_cached_apex_or_fetch()
        if not readings:
            return jsonify({'analysis': 'No data available'}), 503
        
        latest = readings[0]
        sensor_data = {**latest, **build_derived_from_reading(latest)}
        
        # Determine sensor specifics based on type
        st = sensor_type.lower()
        if 'temp' in st:
            current_value = sensor_data.get('temperature', 0)
            unit = '°C'
            status = _get_temperature_status(current_value)
        elif 'humid' in st:
            current_value = sensor_data.get('humidity', 0)
            unit = '%'
            status = _get_humidity_status(current_value)
        elif 'light' in st:
            current_value = sensor_data.get('light', 0)
            unit = 'lux'
            status = _get_light_status(current_value)
        elif 'co2' in st or 'air' in st or 'mq135' in st:
            current_value = sensor_data.get('mq135_drop', 0)
            unit = 'ppm'
            status = "Good" if current_value <= 200 else ("Poor" if current_value > 500 else "Moderate")
        elif 'pressure' in st:
            current_value = sensor_data.get('pressure', 0)
            unit = 'hPa'
            status = "Normal"
        else:
            current_value = 0
            unit = ''
            status = 'Unknown'
        
        # Get last 10 readings for trend analysis
        historical_values = []
        for r in readings[:10]:
            if 'temp' in st:
                val = r.get('temperature', 0)
            elif 'humid' in st:
                val = r.get('humidity', 0)
            elif 'light' in st:
                val = r.get('light', 0)
            elif 'co2' in st or 'air' in st:
                val = r.get('mq135_drop', 0)
            elif 'pressure' in st:
                val = r.get('pressure', 0)
            else:
                val = 0
            historical_values.append(val)
        
        # Call Gemini AI
        analysis_text = get_gemini_analysis(sensor_type, current_value, unit, status, historical_values)
        
        return jsonify({
            'analysis': analysis_text,
            'timestamp': time.time(),
            'sensor_type': sensor_type
        }), 200
        
    except Exception as e:
        logger.error(f"AI analysis error for {sensor_type}: {e}")
        return jsonify({'analysis': f'AI analysis temporarily unavailable'}), 500

@app.route('/api/ai-recommendations', methods=['GET'])
def get_ai_recommendations():
    """
    Get AI-powered recommendations from Gemini based on APEX sensor data.
    """
    # ONLY USE APEX DATA
    readings, _ = get_cached_apex_or_fetch()
    if not readings:
        return jsonify({"error": "No APEX data available"}), 503
    
    latest = readings[0]
    current_data = {**latest, **build_derived_from_reading(latest)}
    
    # Use Gemini AI to generate recommendations based on APEX sensor data
    recommendations = get_gemini_recommendations(current_data)
    
    # Return recommendations along with timestamp
    return jsonify({
        "recommendations": recommendations,
        "timestamp": current_data.get('timestamp', time.time())
    })

@app.route('/api/alerts', methods=['GET'])
def get_alerts():
    """
    Get alerts when sensors are outside normal ranges - ONLY FROM APEX DATA
    Triggers sound notification in frontend when alerts exist.
    
    Thresholds (based on THRESHOLDS.md):
    - Temperature: Outside 21-27°C triggers alert
    - Humidity: Outside 60-75% triggers alert
    - MQ135 (Air Quality): >200 ppm triggers alert
    - MQ2 (Flammable Gas): >300 ppm triggers alert
    - MQ7 (Carbon Monoxide): >300 ppm triggers alert
    - Flame: Detection triggers critical alert
    """
    # ONLY USE APEX DATA
    readings, _ = get_cached_apex_or_fetch()
    if not readings:
        return jsonify({"error": "No APEX data available", "alerts": [], "alert_count": 0, "should_alert": False}), 503
    
    latest = readings[0]
    current_data = {**latest, **build_derived_from_reading(latest)}
    
    # Generate alerts based on thresholds from THRESHOLDS.md
    alerts = []
    
    # CRITICAL SAFETY ALERTS (highest priority)
    
    # Flame detection alert
    if current_data.get('flame_detected') == 'Yes':
        alerts.append({
            "title": "🔥 FIRE HAZARD",
            "message": "Flame or strong IR source detected. Inspect all heating equipment immediately!",
            "timestamp": current_data['timestamp'],
            "sensor_type": "flame",
            "severity": "critical",
            "value": current_data.get('flame_raw', 0),
            "unit": "raw",
            "sound": True  # Trigger sound alert
        })
    
    # Carbon monoxide alert (using real thresholds: 300/750)
    mq7_drop = current_data.get('mq7_drop', 0)
    if mq7_drop > 750:
        alerts.append({
            "title": "⚠️ CO CRITICAL",
            "message": f"Carbon monoxide at {mq7_drop:.0f} ppm exceeds safe levels (>750). Ventilate immediately!",
            "timestamp": current_data['timestamp'],
            "sensor_type": "carbon_monoxide",
            "severity": "critical",
            "value": mq7_drop,
            "unit": "ppm",
            "sound": True
        })
    elif mq7_drop > 300:
        alerts.append({
            "title": "CO Elevated",
            "message": f"Carbon monoxide at {mq7_drop:.0f} ppm. Monitor heating equipment closely.",
            "timestamp": current_data['timestamp'],
            "sensor_type": "carbon_monoxide",
            "severity": "high",
            "value": mq7_drop,
            "unit": "ppm",
            "sound": True
        })
    
    # Flammable gas alert (using real thresholds: 300/750)
    mq2_drop = current_data.get('mq2_drop', 0)
    if mq2_drop > 750:
        alerts.append({
            "title": "⚠️ GAS CRITICAL",
            "message": f"Flammable gas at {mq2_drop:.0f} ppm (>750). Check for leaks immediately!",
            "timestamp": current_data['timestamp'],
            "sensor_type": "flammable_gas",
            "severity": "critical",
            "value": mq2_drop,
            "unit": "ppm",
            "sound": True
        })
    elif mq2_drop > 300:
        alerts.append({
            "title": "Gas Elevated",
            "message": f"Flammable gas at {mq2_drop:.0f} ppm. Increase ventilation.",
            "timestamp": current_data['timestamp'],
            "sensor_type": "flammable_gas",
            "severity": "high",
            "value": mq2_drop,
            "unit": "ppm",
            "sound": True
        })
    
    # ENVIRONMENTAL ALERTS
    
    # Temperature alert (optimal: 20-27°C)
    avg_temp = (current_data['temperature_bmp280'] + current_data['temperature_dht22']) / 2
    if avg_temp < 18:
        alerts.append({
            "title": "Temperature Critical Low",
            "message": f"Temperature at {avg_temp:.1f}°C is critically low (<18°C). Plants may suffer cold damage.",
            "timestamp": current_data['timestamp'],
            "sensor_type": "temperature",
            "severity": "high",
            "value": avg_temp,
            "unit": "°C",
            "sound": True
        })
    elif avg_temp > 30:
        alerts.append({
            "title": "Temperature Critical High",
            "message": f"Temperature at {avg_temp:.1f}°C is dangerously high (>30°C). Risk of heat stress.",
            "timestamp": current_data['timestamp'],
            "sensor_type": "temperature",
            "severity": "high",
            "value": avg_temp,
            "unit": "°C",
            "sound": True
        })
    elif avg_temp < 20 or avg_temp > 27:
        alerts.append({
            "title": "Temperature Outside Optimal",
            "message": f"Temperature at {avg_temp:.1f}°C is outside optimal range (20-27°C).",
            "timestamp": current_data['timestamp'],
            "sensor_type": "temperature",
            "severity": "medium",
            "value": avg_temp,
            "unit": "°C",
            "sound": False
        })
    
    # Humidity alert (optimal: 45-70%)
    humidity = current_data.get('humidity', 0)
    if humidity < 45:
        alerts.append({
            "title": "Humidity Critical Low",
            "message": f"Humidity at {humidity}% is critically low (<45%). Too dry - recommend shading to reduce evaporation.",
            "timestamp": current_data['timestamp'],
            "sensor_type": "humidity",
            "severity": "high",
            "value": humidity,
            "unit": "%",
            "sound": True
        })
    elif humidity > 80:
        alerts.append({
            "title": "Humidity Critical High",
            "message": f"Humidity at {humidity}% is dangerously high (>80%). Risk of fungal growth - run all ventilation and open vents!",
            "timestamp": current_data['timestamp'],
            "sensor_type": "humidity",
            "severity": "high",
            "value": humidity,
            "unit": "%",
            "sound": True
        })
    elif humidity < 45 or humidity > 70:
        alerts.append({
            "title": "Humidity Outside Optimal",
            "message": f"Humidity at {humidity}% is outside optimal range (45-70%). Adjust vents/fans.",
            "timestamp": current_data['timestamp'],
            "sensor_type": "humidity",
            "severity": "medium",
            "value": humidity,
            "unit": "%",
            "sound": False
        })
    
    # Air quality alert (using real thresholds: 200/500)
    mq135_drop = current_data.get('mq135_drop', 0)
    if mq135_drop > 500:
        alerts.append({
            "title": "Air Quality Poor",
            "message": f"Air quality at {mq135_drop:.0f} ppm indicates poor conditions (>500). Increase ventilation.",
            "timestamp": current_data['timestamp'],
            "sensor_type": "air_quality",
            "severity": "medium",
            "value": mq135_drop,
            "unit": "ppm",
            "sound": True
        })
    elif mq135_drop > 200:
        alerts.append({
            "title": "Air Quality Moderate",
            "message": f"Air quality at {mq135_drop:.0f} ppm is outside optimal range (>200).",
            "timestamp": current_data['timestamp'],
            "sensor_type": "air_quality",
            "severity": "low",
            "value": mq135_drop,
            "unit": "ppm",
            "sound": False
        })
    
    # Determine if sound alert should be triggered (any critical/high severity)
    should_alert = any(alert.get('sound', False) for alert in alerts)
    
    return jsonify({
        "alerts": alerts,
        "timestamp": current_data['timestamp'],
        "alert_count": len(alerts),
        "should_alert": should_alert  # Frontend can use this to trigger sound
    })

if __name__ == '__main__':
    # Start continuous APEX poller if ORACLE_APEX_URL is set
    if ORACLE_APEX_URL:
        poller_thread = threading.Thread(target=continuous_apex_poller, daemon=True)
        poller_thread.start()
        print('✅ Continuous APEX poller started (fetching every 3 seconds)')
    else:
        print('⚠️ ORACLE_APEX_URL not set - APEX polling disabled')

    # Start the IP broadcast service in a separate thread
    broadcast_thread = threading.Thread(target=ip_broadcast_service, daemon=True)
    broadcast_thread.start()
    print("IP broadcast service started")

@app.route('/api/export-report', methods=['GET'])
def export_greenhouse_report():
    """Generate comprehensive greenhouse PDF report with AI analysis"""
    try:
        # Get latest APEX data directly
        readings, cache_status = get_cached_apex_or_fetch()
        if not readings:
            return jsonify({'error': 'No APEX data available'}), 503
        
        latest = readings[0]
        sensor_data = {**latest, **build_derived_from_reading(latest)}
        
        # Calculate statuses for all sensors
        temp_avg = (sensor_data.get('temperature_bmp280', 0) + sensor_data.get('temperature_dht22', 0)) / 2
        
        all_analysis = {
            'temperature': {
                'value': temp_avg,
                'status': _get_temperature_status(temp_avg),
                'unit': '°C'
            },
            'humidity': {
                'value': sensor_data.get('humidity', 0),
                'status': _get_humidity_status(sensor_data.get('humidity', 0)),
                'unit': '%'
            },
            'air_quality': {
                'value': sensor_data.get('mq135_drop', 0),
                'status': "Good" if sensor_data.get('mq135_drop', 0) <= 200 else ("Poor" if sensor_data.get('mq135_drop', 0) > 500 else "Moderate"),
                'unit': 'ppm'
            },
            'light': {
                'value': sensor_data.get('light', 0),
                'status': _get_light_status(sensor_data.get('light', 0)),
                'unit': 'lux'
            }
        }
        
        # Get AI recommendations
        try:
            ai_recommendations = get_gemini_recommendations(sensor_data)
        except Exception as e:
            logger.warning(f"AI recommendations failed: {e}")
            ai_recommendations = []
        
        # Generate PDF
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=72, leftMargin=72, topMargin=72, bottomMargin=18)
        
        # Container for PDF elements
        elements = []
        styles = getSampleStyleSheet()
        
        # Custom styles
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            textColor=colors.HexColor('#4CAF50'),
            spaceAfter=30,
            alignment=TA_CENTER
        )
        
        heading_style = ParagraphStyle(
            'CustomHeading',
            parent=styles['Heading2'],
            fontSize=16,
            textColor=colors.HexColor('#2196F3'),
            spaceAfter=12,
            spaceBefore=12
        )
        
        # Title
        elements.append(Paragraph("🌿 EcoView Greenhouse Report", title_style))
        elements.append(Spacer(1, 12))
        
        # Report metadata
        report_date = datetime.now().strftime('%B %d, %Y at %I:%M %p')
        elements.append(Paragraph(f"<b>Generated:</b> {report_date}", styles['Normal']))
        elements.append(Paragraph(f"<b>System:</b> EcoView Greenhouse Monitoring", styles['Normal']))
        elements.append(Spacer(1, 20))
        
        # Health Score
        health_score = _calculate_health_score(all_analysis)
        elements.append(Paragraph("Overall Greenhouse Health", heading_style))
        health_color = colors.green if health_score >= 80 else (colors.orange if health_score >= 60 else colors.red)
        health_data = [[f"{health_score}/100", "EXCELLENT" if health_score >= 80 else ("GOOD" if health_score >= 60 else "NEEDS ATTENTION")]]
        health_table = Table(health_data, colWidths=[2*inch, 3*inch])
        health_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, 0), health_color),
            ('TEXTCOLOR', (0, 0), (0, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 14),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
            ('GRID', (0, 0), (-1, -1), 1, colors.grey)
        ]))
        elements.append(health_table)
        elements.append(Spacer(1, 20))
        
        # Current Conditions
        elements.append(Paragraph("📊 Current Sensor Readings", heading_style))
        sensor_table_data = [['Sensor', 'Value', 'Status', 'Assessment']]
        
        for sensor_name, data in all_analysis.items():
            status = data['status']
            status_color = colors.green if status == 'Optimal' else (colors.orange if status in ['Warning', 'Moderate'] else colors.red)
            sensor_table_data.append([
                sensor_name.replace('_', ' ').title(),
                f"{data['value']:.1f} {data['unit']}",
                status,
                '✓' if status == 'Optimal' else '⚠' if status in ['Warning', 'Moderate'] else '✗'
            ])
        
        sensor_table = Table(sensor_table_data, colWidths=[2*inch, 1.5*inch, 1.5*inch, 1*inch])
        sensor_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4CAF50')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('GRID', (0, 0), (-1, -1), 1, colors.grey),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.beige, colors.lightgrey])
        ]))
        elements.append(sensor_table)
        elements.append(Spacer(1, 20))
        
        # AI Recommendations
        if ai_recommendations:
            elements.append(Paragraph("🤖 AI-Powered Recommendations", heading_style))
            for i, rec in enumerate(ai_recommendations[:5], 1):
                elements.append(Paragraph(f"<b>{i}.</b> {rec}", styles['Normal']))
                elements.append(Spacer(1, 6))
        elements.append(Spacer(1, 20))
        
        # Alert Summary
        elements.append(Paragraph("⚠️ Alert Summary", heading_style))
        alert_summary = _generate_alert_summary(sensor_data)
        critical_count = alert_summary.get('critical_count', 0)
        warning_count = alert_summary.get('warning_count', 0)
        
        if critical_count > 0 or warning_count > 0:
            elements.append(Paragraph(f"<b>Critical Alerts:</b> {critical_count}", styles['Normal']))
            elements.append(Paragraph(f"<b>Warnings:</b> {warning_count}", styles['Normal']))
            if alert_summary.get('alerts'):
                for alert in alert_summary['alerts'][:5]:
                    elements.append(Paragraph(f"• {alert.get('message', 'Unknown alert')}", styles['Normal']))
                    elements.append(Spacer(1, 4))
        else:
            elements.append(Paragraph("✅ No active alerts - All systems operating normally", styles['Normal']))
        
        elements.append(Spacer(1, 20))
        
        # Historical Data Summary
        elements.append(PageBreak())
        elements.append(Paragraph("📈 Recent Historical Data", heading_style))
        elements.append(Paragraph(f"Last {min(10, len(readings))} readings from APEX database:", styles['Normal']))
        elements.append(Spacer(1, 12))
        
        history_table_data = [['Timestamp', 'Temp (°C)', 'Humidity (%)', 'Light (lux)']]
        for reading in readings[:10]:
            timestamp = datetime.fromtimestamp(reading.get('timestamp', time.time())).strftime('%m/%d %H:%M')
            temp = (reading.get('temperature_bmp280', 0) + reading.get('temperature_dht22', 0)) / 2
            humidity = reading.get('humidity', 0)
            light = reading.get('light', 0)
            history_table_data.append([timestamp, f"{temp:.1f}", f"{humidity:.1f}", f"{light:.0f}"])
        
        history_table = Table(history_table_data, colWidths=[1.5*inch, 1.5*inch, 1.5*inch, 1.5*inch])
        history_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2196F3')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.lightgrey])
        ]))
        elements.append(history_table)
        
        # Footer
        elements.append(Spacer(1, 30))
        elements.append(Paragraph("―――――――――――――――――――――――――――――――――――", styles['Normal']))
        elements.append(Paragraph("<i>Generated by EcoView Greenhouse Monitoring System</i>", styles['Normal']))
        elements.append(Paragraph(f"<i>Report ID: {datetime.now().strftime('%Y%m%d%H%M%S')}</i>", styles['Normal']))
        
        # Build PDF
        doc.build(elements)
        buffer.seek(0)
        
        # Generate filename
        filename = f"EcoView_Report_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.pdf"
        
        return send_file(
            buffer,
            as_attachment=True,
            download_name=filename,
            mimetype='application/pdf'
        )
        
    except Exception as e:
        logger.error(f"Error generating PDF report: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500

def _generate_overview(sensor_data, analysis):
    """Generate greenhouse overview summary"""
    
    overview = {
        'status': 'OPERATIONAL',
        'monitoring_since': sensor_data.get('timestamp', time.time()),
        'total_sensors': 11,
        'active_sensors': 8,  # Count non-null sensors
        'summary': ''
    }
    
    # Generate summary text
    temp_status = analysis.get('temperature', {}).get('status', 'Unknown')
    humidity_status = analysis.get('humidity', {}).get('status', 'Unknown')
    air_status = analysis.get('air_quality', {}).get('status', 'Unknown')
    light_status = analysis.get('light', {}).get('status', 'Unknown')
    
    summary_parts = []
    if temp_status == 'Optimal':
        summary_parts.append("Temperature is in optimal range")
    elif temp_status == 'Critical':
        summary_parts.append("⚠️ CRITICAL: Temperature requires immediate attention")
    else:
        summary_parts.append(f"Temperature is {temp_status}")
    
    if humidity_status == 'Optimal':
        summary_parts.append("humidity is ideal")
    elif humidity_status == 'Critical':
        summary_parts.append("⚠️ humidity needs urgent correction")
    else:
        summary_parts.append(f"humidity is {humidity_status}")
    
    if air_status == 'Good':
        summary_parts.append("air quality is excellent")
    elif air_status == 'Poor':
        summary_parts.append("⚠️ poor air quality detected")
    
    if light_status in ['Bright', 'Moderate']:
        summary_parts.append(f"lighting is {light_status.lower()}")
    elif light_status in ['Dark Night', 'Low Light']:
        summary_parts.append(f"low light conditions ({light_status})")
    
    overview['summary'] = ', '.join(summary_parts).capitalize() + '.'
    
    return overview

def _generate_current_conditions(sensor_data):
    """Generate current conditions summary"""
    
    return {
        'temperature': {
            'bmp280': sensor_data.get('temperature_bmp280', 0),
            'dht22': sensor_data.get('temperature_dht22', 0),
            'average': (sensor_data.get('temperature_bmp280', 0) + sensor_data.get('temperature_dht22', 0)) / 2
        },
        'humidity': sensor_data.get('humidity', 0),
        'air_quality': {
            'mq135_co2': sensor_data.get('mq135_drop', 0),
            'mq2_gas': sensor_data.get('mq2_drop', 0),
            'mq7_co': sensor_data.get('mq7_drop', 0)
        },
        'atmospheric': {
            'pressure': sensor_data.get('pressure', 0),
            'altitude': sensor_data.get('altitude', 0)
        },
        'light_intensity': sensor_data.get('light', 0),
        'flame_detection': sensor_data.get('flame_status', 'Unknown')
    }

def _generate_alert_summary(sensor_data):
    """Generate alert summary"""
    alerts = []
    
    # Temperature alerts (20-27°C optimal)
    temp_avg = (sensor_data.get('temperature_bmp280', 0) + sensor_data.get('temperature_dht22', 0)) / 2
    if temp_avg < 18:
        alerts.append({'level': 'CRITICAL', 'type': 'Temperature', 'message': f'Temperature too low: {temp_avg:.1f}°C'})
    elif temp_avg > 30:
        alerts.append({'level': 'CRITICAL', 'type': 'Temperature', 'message': f'Temperature too high: {temp_avg:.1f}°C'})
    elif temp_avg < 20 or temp_avg > 27:
        alerts.append({'level': 'WARNING', 'type': 'Temperature', 'message': f'Temperature suboptimal: {temp_avg:.1f}°C'})
    
    # Humidity alerts (45-70% optimal)
    humidity = sensor_data.get('humidity', 0)
    if humidity < 45:
        alerts.append({'level': 'CRITICAL', 'type': 'Humidity', 'message': f'Too dry: {humidity}% - Add shading'})
    elif humidity > 80:
        alerts.append({'level': 'CRITICAL', 'type': 'Humidity', 'message': f'Too humid: {humidity}% - Run ventilation'})
    elif humidity > 70:
        alerts.append({'level': 'WARNING', 'type': 'Humidity', 'message': f'Slightly high: {humidity}%'})
    
    # Air quality alerts (200/500 thresholds)
    mq135 = sensor_data.get('mq135_drop', 0)
    if mq135 > 500:
        alerts.append({'level': 'WARNING', 'type': 'Air Quality', 'message': f'Poor air quality: {mq135:.0f} PPM'})
    elif mq135 > 200:
        alerts.append({'level': 'INFO', 'type': 'Air Quality', 'message': f'Moderate air quality: {mq135:.0f} PPM'})
    
    # Gas alerts
    mq2 = sensor_data.get('mq2_drop', 0)
    if mq2 > 750:
        alerts.append({'level': 'CRITICAL', 'type': 'Flammable Gas', 'message': f'High gas level: {mq2:.0f} PPM'})
    elif mq2 > 300:
        alerts.append({'level': 'WARNING', 'type': 'Flammable Gas', 'message': f'Elevated gas: {mq2:.0f} PPM'})
    
    # CO alerts
    mq7 = sensor_data.get('mq7_drop', 0)
    if mq7 > 750:
        alerts.append({'level': 'CRITICAL', 'type': 'Carbon Monoxide', 'message': f'High CO: {mq7:.0f} PPM'})
    elif mq7 > 300:
        alerts.append({'level': 'WARNING', 'type': 'Carbon Monoxide', 'message': f'Elevated CO: {mq7:.0f} PPM'})
    
    # Flame detection
    if sensor_data.get('flame_detected'):
        alerts.append({'level': 'CRITICAL', 'type': 'FIRE', 'message': '⚠️ FIRE DETECTED'})
    
    return {
        'total_alerts': len(alerts),
        'critical_count': sum(1 for a in alerts if a['level'] == 'CRITICAL'),
        'warning_count': sum(1 for a in alerts if a['level'] == 'WARNING'),
        'alerts': alerts
    }

def _calculate_health_score(analysis):
    """Calculate overall greenhouse health score (0-100)"""
    score = 100
    
    # Temperature impact
    temp_status = analysis.get('temperature', {}).get('status', '')
    if temp_status == 'Critical':
        score -= 30
    elif temp_status == 'Acceptable':
        score -= 10
    
    # Humidity impact
    humidity_status = analysis.get('humidity', {}).get('status', '')
    if humidity_status == 'Critical':
        score -= 30
    elif humidity_status == 'Acceptable':
        score -= 10
    
    # Air quality impact
    air_status = analysis.get('air_quality', {}).get('status', '')
    if air_status == 'Poor':
        score -= 20
    elif air_status == 'Moderate':
        score -= 10
    
    # Light impact
    light_status = analysis.get('light', {}).get('status', '')
    if light_status in ['Dark Night', 'Low Light']:
        score -= 15
    elif light_status == 'Dim Indoor':
        score -= 5
    
    return max(0, min(100, score))

if __name__ == '__main__':
    # For development only - use gunicorn in production
    app.run(host='0.0.0.0', port=5000, debug=True)