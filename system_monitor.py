import paho.mqtt.client as mqtt
import json
import time
import psutil
import platform
import subprocess
import threading
import sys
import os
import shutil
import requests
from pathlib import Path
import configparser
import logging
from logging.handlers import RotatingFileHandler

# ==========================================
# 0. LOGGING & HARDWARE CONFIGURATION
# ==========================================
logging.basicConfig(
    handlers=[
        RotatingFileHandler('/opt/node_agent.log', maxBytes=2000000, backupCount=3),
        logging.StreamHandler()
    ],
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("NodeAgent")

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    logger.warning("RPi.GPIO library not found. Hardware reset disabled.")

# ==========================================
# 1. UNIFIED CONFIGURATION LOADING
# ==========================================
CONFIG_PATH = Path("/opt/node_config.json")

def load_config():
    try:
        if not CONFIG_PATH.exists():
            logger.error(f"ERROR: File {CONFIG_PATH} not found!")
            sys.exit(1)
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"CRITICAL JSON ERROR: {e}")
        sys.exit(1)

cfg = load_config()

# Identifiers and Topics
CLIENT_ID = cfg.get('client_id', 'iv3jdv').lower()
BASE_TOPIC = cfg.get('mqtt', {}).get('base_topic', f"servizi/{CLIENT_ID}")

TOPIC_CMD = f"{BASE_TOPIC}/cmnd"
TOPIC_STAT = f"{BASE_TOPIC}/stat"

# Global Status Variables
boot_recovered = False
current_status = "ONLINE"
auto_healing_counter = {}

# ==========================================
# 2. TELEGRAM NOTIFICATION FUNCTION
# ==========================================
def send_telegram_message(message):
    t_cfg = cfg.get('telegram', {})
    if not t_cfg.get('enabled', False): return
        
    current_hour = int(time.strftime("%H"))
    if current_hour >= 23 or current_hour < 7:
        logger.info(f"🌙 Late night ({current_hour}:00): Telegram notification skipped.")
        return

    token = t_cfg.get('token')
    chat_id = t_cfg.get('chat_id')
    if not token or not chat_id or token == "TOKEN ID": return

    try:
        clean_msg = message.replace("<b>", "").replace("</b>", "")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": f"[{CLIENT_ID.upper()}]\n{clean_msg}"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Telegram sending error: {e}")

# ==========================================
# 3. MULTIPLE PROFILE SWITCH LOGIC
# ==========================================
def get_actual_config_from_disk():
    try:
        path = "/opt/last_profile.txt"
        if os.path.exists(path):
            with open(path, "r") as f:
                label = f.read().strip()
                if label:
                    return f"ONLINE - {label}"
    except Exception as e:
        logger.error(f"Errore lettura memoria profilo: {e}")
    
    return "ONLINE" # Default se il file non esiste o è vuoto

def switch_config(config_type):
    profile = cfg.get('profiles', {}).get(config_type)
    
    if not profile:
        return f"ERROR: Profile {config_type} not found in JSON"
        
    label = profile.get('label', f"Profile {config_type}")
    services = profile.get('services', [])
    
    if not services:
        return f"ERROR: No services configured for {config_type}"
    
    try:
        for s in services:
            subprocess.run(["sudo", "systemctl", "stop", s['name']], check=False)
            
        for s in services:
            if not os.path.exists(s['source']):
                return f"ERROR: Missing source file {s['source']}"
            shutil.copy(s['source'], s['target'])
            
        for s in services:
            subprocess.run(["sudo", "systemctl", "start", s['name']], check=False)
            
        # Save the current profile to disk to remember it on reboot
        with open("/opt/last_profile.txt", "w") as f:
            f.write(label)
            
        send_telegram_message(f"✅ Multiple switch completed: {label}")
        return f"ONLINE - {label}"
        
    except Exception as e:
        return f"ERROR: {str(e)}"

def force_online_if_needed(client):
    global boot_recovered, current_status
    if not boot_recovered:
        logger.info("⚠️ Memory recovery skipped. Setting status from disk...")
        current_status = get_actual_config_from_disk()
        client.publish(TOPIC_STAT, current_status, retain=True)
        boot_recovered = True

# ==========================================
# 4. TELEMETRY AND AUTO-HEALING
# ==========================================
def get_cpu_temperature():
    temp = 0.0
    try:
        temps = psutil.sensors_temperatures()
        if 'cpu_thermal' in temps: temp = temps['cpu_thermal'][0].current
        elif 'coretemp' in temps: temp = temps['coretemp'][0].current
        elif platform.system() == "Linux":
            res = os.popen('vcgencmd measure_temp').readline()
            if res: temp = float(res.replace("temp=","").replace("'C\n",""))
    except: pass
    return round(temp, 1)

def get_system_status():
    status = {
        "cpu_usage_percent": psutil.cpu_percent(interval=0.5),
        "cpu_temp": get_cpu_temperature(),
        "memory_usage_percent": psutil.virtual_memory().percent,
        "disk_usage_percent": psutil.disk_usage('/').percent,
        "processes": {},
        "timestamp": time.strftime("%H:%M:%S"),
        "profiles": {
            "A": cfg.get('profiles', {}).get('A', {}).get('label', 'PROFILE A'),
            "B": cfg.get('profiles', {}).get('B', {}).get('label', 'PROFILE B')
        }
    }
    proc_path = Path(cfg['paths'].get('process_list', ''))
    if proc_path.exists():
        try:
            target_processes = proc_path.read_text(encoding="utf-8").splitlines()
            running_names = {p.info['name'].lower() for p in psutil.process_iter(['name'])}
            for name in target_processes:
                name = name.strip().lower()
                if name: status["processes"][name] = "online" if name in running_names else "offline"
        except Exception as e: logger.error(f"Process check error: {e}")
    return status

def check_auto_healing(client, status):
    if not cfg['settings'].get('auto_healing', False): return
    for proc_name, state in status["processes"].items():
        if state == "offline":
            attempts = auto_healing_counter.get(proc_name, 0)
            if attempts < 3:
                auto_healing_counter[proc_name] = attempts + 1
                msg = f"🛠 Auto-healing: {proc_name} offline. Restarting {attempts+1}/3..."
                client.publish(f"devices/{CLIENT_ID}/logs", msg)
                send_telegram_message(msg)
                
                # --- START MODIFICATION: SPECIFIC HARDWARE RESET FOR MMDVMHOST ---
                if proc_name.lower() == "mmdvmhost" and GPIO_AVAILABLE:
                    logger.info("Executing automatic HAT RESET before restarting MMDVMHost...")
                    try:
                        RESET_PIN = 21 # Ensure the PIN is correct for your nodes
                        GPIO.setwarnings(False)
                        GPIO.setmode(GPIO.BCM)
                        GPIO.setup(RESET_PIN, GPIO.OUT)
                        # LOW pulse to reset
                        GPIO.output(RESET_PIN, GPIO.LOW)
                        time.sleep(0.5)
                        GPIO.output(RESET_PIN, GPIO.HIGH)
                        GPIO.cleanup(RESET_PIN)
                        # Give the microcontroller time to restart
                        time.sleep(1.5)
                        client.publish(f"devices/{CLIENT_ID}/logs", "🔌 GPIO Pulse (MMDVM Reset) sent!")
                    except Exception as e:
                        logger.error(f"GPIO error in auto-healing: {e}")
                # --- END MODIFICATION ---

                subprocess.run(["sudo", "systemctl", "restart", proc_name])
            elif attempts == 3:
                msg = f"🚨 CRITICAL: {proc_name} failed!"
                client.publish(f"devices/{CLIENT_ID}/logs", msg)
                send_telegram_message(msg)
                auto_healing_counter[proc_name] = 4 
        else:
            auto_healing_counter[proc_name] = 0

def publish_all(client):
    status = get_system_status()
    file_list_path = Path(cfg['paths'].get('file_list', ''))
    status["config_files"] = []
    status["files"] = []
    
    if file_list_path.exists():
        try:
            files = file_list_path.read_text(encoding="utf-8").splitlines()
            extracted_names = [Path(f.strip()).stem for f in files if f.strip()]
            status["config_files"] = extracted_names
            status["files"] = extracted_names
        except: pass

    client.publish(f"devices/{CLIENT_ID}/services", json.dumps(status), qos=1)

    if file_list_path.exists():
        try:
            files = file_list_path.read_text(encoding="utf-8").splitlines()
            for f in files:
                p = Path(f.strip())
                if p.exists():
                    client.publish(f"data/{CLIENT_ID}/{p.stem}/full_config", json.dumps({"raw_text": p.read_text(encoding="utf-8")}), qos=1, retain=True)
        except: pass
    return status

def publish_all_ini_files(client):
    file_list_path = cfg.get('paths', {}).get('file_list', '/opt/file_list.txt')
    if not os.path.exists(file_list_path): return

    try:
        with open(file_list_path, 'r') as f:
            files_to_parse = [line.strip() for line in f if line.strip()]
    except Exception as e:
        logger.error(f"Error reading {file_list_path}: {e}")
        return

    for file_path in files_to_parse:
        if not os.path.exists(file_path): continue
        try:
            base_name = os.path.splitext(os.path.basename(file_path))[0]
            ini_data = {}
            current_section = None
            
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith(('#', ';')): continue
                    if line.startswith('[') and line.endswith(']'):
                        current_section = line[1:-1].strip()
                        ini_data[current_section] = {}
                    elif '=' in line and current_section is not None:
                        k, v = line.split('=', 1)
                        k, v = k.strip(), v.strip()
                        if k in ini_data[current_section]:
                            ini_data[current_section][k] = str(ini_data[current_section][k]) + "," + v
                        else:
                            ini_data[current_section][k] = v
                            
            for section, payload in ini_data.items():
                topic = f"data/{CLIENT_ID}/{base_name}/{section}"
                client.publish(topic, json.dumps(payload), retain=True)
        except Exception as e:
            logger.error(f"Error parsing INI for {file_path}: {e}")

def write_config_from_json(slug, json_payload):
    file_list_path = Path(cfg['paths'].get('file_list', ''))
    if not file_list_path.exists(): return
    try:
        files = file_list_path.read_text(encoding="utf-8").splitlines()
        for f in files:
            p = Path(f.strip())
            if p.stem.lower() == slug.lower():
                new_data = json.loads(json_payload)
                shutil.copy(p, str(p) + ".bak")
                with open(p, 'w', encoding="utf-8") as file: file.write(new_data.get("raw_text", ""))
                os.system(f"sudo systemctl restart {slug}")
                send_telegram_message(f"📝 Config {slug.upper()} updated via Web.")
                logger.info(f"Configuration {slug} updated successfully.")
                break
    except Exception as e: logger.error(f"Config writing error: {e}")

# ==========================================
# 5. MQTT CALLBACKS
# ==========================================
def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        logger.info(f"✅ Connected to MQTT broker: {CLIENT_ID.upper()}")
        client.subscribe([(TOPIC_CMD, 0), (TOPIC_STAT, 0)])
        client.subscribe([
            ("devices/control/request", 0), 
            (f"devices/{CLIENT_ID}/control", 0), 
            (f"devices/{CLIENT_ID}/config_set/#", 0)
        ])
        threading.Timer(5.0, force_online_if_needed, [client]).start()
        publish_all(client)
        publish_all_ini_files(client)
    else:
        logger.error(f"❌ MQTT connection error. Code: {reason_code}")

def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    logger.warning(f"⚠️ Disconnected from MQTT broker! Code: {reason_code}")
    logger.info("Waiting for network return. Paho-MQTT will attempt automatic reconnection...")

def on_message(client, userdata, msg):
    global boot_recovered, current_status, cfg
    payload = msg.payload.decode().strip()
    topic = msg.topic
    
    if topic == TOPIC_STAT and not boot_recovered:
        if not any(x in payload.upper() for x in ["OFFLINE", "ERROR", "REBOOT"]):
            current_status = payload
            boot_recovered = True
            client.publish(TOPIC_STAT, current_status, retain=True)

    elif topic == TOPIC_CMD:
        cmd = payload.upper()
        if cmd in ["A", "B"]:
            current_status = switch_config(cmd)
            client.publish(TOPIC_STAT, current_status, retain=True)
            boot_recovered = True
            publish_all(client)
        elif cmd == "REBOOT":
            client.publish(TOPIC_STAT, f"OFFLINE - Rebooting {CLIENT_ID.upper()}...", retain=False)
            logger.info("REBOOT command received. Rebooting system...")
            time.sleep(1)
            subprocess.run(["sudo", "reboot"], check=True)
        elif cmd == 'RESET_HAT':
            RESET_PIN = 21
            if GPIO_AVAILABLE:
                try:
                    GPIO.setwarnings(False)
                    GPIO.setmode(GPIO.BCM)
                    GPIO.setup(RESET_PIN, GPIO.OUT)
                    GPIO.output(RESET_PIN, GPIO.LOW)
                    time.sleep(0.5)
                    GPIO.output(RESET_PIN, GPIO.HIGH)
                    GPIO.cleanup(RESET_PIN)
                    logger.info(f"RESET pulse sent to GPIO {RESET_PIN}")
                    time.sleep(1.5)
                    logger.info("Restarting MMDVMHost...")
                    subprocess.run(["sudo", "systemctl", "restart", "mmdvmhost"], check=False)
                    client.publish(f"fleet/{CLIENT_ID}/status", "HAT RESET + MMDVM RESTART OK")
                    client.publish(f"devices/{CLIENT_ID}/logs", "🔌 HAT Reset + MMDVMHost Restarted")
                except Exception as e:
                    logger.error(f"Error during GPIO/MMDVMHost reset: {e}")
                    client.publish(f"fleet/{CLIENT_ID}/status", f"RESET ERROR: {e}")

        elif cmd in ["TG:OFF", "TG:ON"]:
            new_state = (cmd == "TG:ON")
            cfg['telegram']['enabled'] = new_state
            try:
                with open(CONFIG_PATH, 'w') as f: json.dump(cfg, f, indent=4)
                client.publish(f"devices/{CLIENT_ID}/logs", f"{'🔔' if new_state else '🔇'} Notifications {'ON' if new_state else 'OFF'}")
                if new_state: send_telegram_message("Notifications reactivated!")
            except Exception as e: logger.error(f"Error saving Telegram status: {e}")

    elif topic == "devices/control/request" and payload.lower() in ["status", "update"]:
        logger.info("📥 Received global update command (REQ CONFIG)")
        publish_all(client)
        publish_all_ini_files(client)
        # Force the visual update of the card on the dashboard!
        client.publish(TOPIC_STAT, current_status, retain=True)
    
    elif topic == f"devices/{CLIENT_ID}/control":
        if ":" in payload:
            action, service = payload.split(":")
            if action.lower() in ["restart", "stop", "start"]:
                try:
                    subprocess.run(["sudo", "systemctl", action.lower(), service.lower()], check=True)
                    client.publish(f"devices/{CLIENT_ID}/logs", f"✅ {action.upper()}: {service}")
                    logger.info(f"Service command executed: {action.upper()} {service}")
                    publish_all(client)
                except Exception as e: 
                    client.publish(f"devices/{CLIENT_ID}/logs", f"❌ ERROR: {str(e)}")
                    logger.error(f"Error executing service command: {e}")
                    
    elif topic.startswith(f"devices/{CLIENT_ID}/config_set/"):
        slug = topic.split("/")[-1]
        write_config_from_json(slug, payload)
        time.sleep(1)
        publish_all(client)
        publish_all_ini_files(client)

def auto_publish_task(client):
    while True:
        status = publish_all(client)
        publish_all_ini_files(client)
        check_auto_healing(client, status)
        time.sleep(cfg['settings'].get('update_interval', 30))

def start_service():
    global current_status
    current_status = get_actual_config_from_disk()
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID.upper())
    client.will_set(TOPIC_STAT, payload=f"OFFLINE - {CLIENT_ID.upper()}", qos=1, retain=False)
    client.username_pw_set(cfg['mqtt']['user'], cfg['mqtt']['password'])
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    
    # 1. Start the telemetry "engine" ONLY ONCE
    threading.Thread(target=auto_publish_task, args=(client,), daemon=True).start()
    
    while True:
        try:
            logger.info("Attempting connection to MQTT broker...")
            client.connect(cfg['mqtt']['broker'], cfg['mqtt']['port'], 60)
            
            # 2. Start network manager in background (handles reconnections automatically!)
            client.loop_start() 
            
            # 3. Pause main thread indefinitely
            while True: 
                time.sleep(1)
                
        except Exception as e:
            # This triggers ONLY if the broker is down when the node boots
            logger.error(f"Broker unreachable at boot ({e}). Retrying in 10 seconds...")
            time.sleep(10)

if __name__ == "__main__":
    start_service()
