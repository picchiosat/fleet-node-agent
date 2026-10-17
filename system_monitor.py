#!/usr/bin/env python3
"""
Fleet Control - Node Agent  (v2.0)

Compatibile con node_config.json esistenti: tutte le chiavi nuove sono OPZIONALI.
Vedi AGENT_CHANGELOG.md per l'elenco completo delle modifiche e dei nuovi parametri.
"""
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
import re
import queue
import random
import signal
import requests
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler

AGENT_VERSION = "2.0"

# --- CALCOLO PERCORSI DINAMICI ---
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(AGENT_DIR, 'node_agent.log')
CONFIG_FILE = os.environ.get('FLEET_AGENT_CONFIG', os.path.join(AGENT_DIR, 'node_config.json'))

# ==========================================
# 0. LOGGING & HARDWARE CONFIGURATION
# ==========================================
logging.basicConfig(
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=2000000, backupCount=3),
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
except RuntimeError as e:
    # La libreria c'è ma non funziona su questa scheda (l'import stesso solleva RuntimeError): non deve bloccare l'agent
    GPIO_AVAILABLE = False
    logger.warning(f"RPi.GPIO not usable on this board ({e}). GPIO hardware reset disabled.")

# ==========================================
# 1. CONFIGURATION LOADING (con validazione)
# ==========================================
CONFIG_PATH = Path(CONFIG_FILE)


def load_config():
    try:
        if not CONFIG_PATH.exists():
            logger.error(f"ERROR: File {CONFIG_PATH} not found!")
            sys.exit(1)
        with open(CONFIG_PATH, 'r') as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"CRITICAL JSON ERROR: {e}")
        sys.exit(1)

    if not isinstance(data, dict):
        logger.error("CRITICAL: node_config.json must contain a JSON object")
        sys.exit(1)

    missing = []
    if not str(data.get('client_id', '')).strip():
        missing.append('client_id')
    mqtt_cfg = data.get('mqtt') if isinstance(data.get('mqtt'), dict) else {}
    for k in ('broker', 'port', 'user', 'password'):
        if k not in mqtt_cfg:
            missing.append(f'mqtt.{k}')
    if missing:
        logger.error(f"CRITICAL: missing required settings in node_config.json: {', '.join(missing)}")
        sys.exit(1)

    # sezioni opzionali: sempre presenti come dict (niente KeyError in giro per il codice)
    for section in ('paths', 'settings', 'telegram', 'profiles'):
        if not isinstance(data.get(section), dict):
            data[section] = {}
    return data


cfg = load_config()

# Identifiers and Topics
CLIENT_ID = str(cfg['client_id']).strip().lower()
MQTT_CFG = cfg['mqtt']
BASE_TOPIC = MQTT_CFG.get('base_topic', f"servizi/{CLIENT_ID}")
if BASE_TOPIC != f"servizi/{CLIENT_ID}":
    logger.warning(f"mqtt.base_topic='{BASE_TOPIC}' diverso da 'servizi/{CLIENT_ID}': "
                   f"il server di controllo usa SEMPRE servizi/<id> e non ti vedrebbe.")

TOPIC_CMD = f"{BASE_TOPIC}/cmnd"
TOPIC_STAT = f"{BASE_TOPIC}/stat"
TOPIC_REQUEST = "devices/control/request"
TOPIC_CONTROL = f"devices/{CLIENT_ID}/control"
TOPIC_CONFIG_SET = f"devices/{CLIENT_ID}/config_set/"
TOPIC_LOGS = f"devices/{CLIENT_ID}/logs"
TOPIC_SERVICES = f"devices/{CLIENT_ID}/services"

# Percorsi (tutti configurabili in "paths")
FILE_LIST_PATH = cfg['paths'].get('file_list') or '/opt/file_list.txt'
PROCESS_LIST_PATH = cfg['paths'].get('process_list') or '/opt/process_list.txt'
LAST_PROFILE_PATH = cfg['paths'].get('last_profile') or '/opt/last_profile.txt'
STATE_PATH = cfg['paths'].get('state_file') or '/opt/agent_state.json'

# Global Status Variables
mqtt_client = None
current_status = "ONLINE"
auto_healing_counter = {}
stopped_by_admin = set()          # servizi fermati volontariamente dalla dashboard (l'auto-healing li ignora)
ops_lock = threading.RLock()      # una sola operazione "pesante" alla volta (profilo, config, reset, update...)
heal_pause_until = 0.0
disconnected_since = None
published_sig = {}                # percorso file -> (mtime_ns, size) dell'ultima versione pubblicata
_warned_units = set()

cmd_queue = queue.Queue(maxsize=20)
tg_queue = queue.Queue(maxsize=50)

UNIT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.@\-]{0,63}$')
USB_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9:/_.\- ]{0,63}$')
SLUG_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,64}$')
SENSITIVE_KEY_RE = re.compile(r'^(.*password|passwd|pass|secret|.*token|authkey|apikey)$', re.I)
MAX_CONFIG_BYTES = 65536


def int_setting(name, default, minimum=0):
    try:
        value = int(cfg['settings'].get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def get_update_interval():
    return int_setting('update_interval', 30, 10)


# ==========================================
# 2. HELPERS: SICUREZZA, FILE, COMANDI
# ==========================================
def redact(text):
    """Toglie dai testi (log, messaggi) token Telegram e credenziali negli URL."""
    text = str(text)
    token = str(cfg['telegram'].get('token') or '')
    if token and token != "TOKEN ID":
        text = text.replace(token, '***')
    text = re.sub(r'bot\d+:[A-Za-z0-9_\-]+', 'bot***', text)
    text = re.sub(r'(https?://)[^/@\s]+@', r'\1***@', text)
    return text


def safe_write_bytes(path, data):
    """Scrittura atomica (file temporaneo + fsync + replace) mantenendo permessi e proprietario.
    Se la directory non è scrivibile (es. /etc con file concesso all'utente) ripiega sulla scrittura in-place."""
    path = str(path)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        st = None
        try:
            st = os.stat(path)
        except FileNotFoundError:
            pass
        with open(tmp, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if st is not None:
            os.chmod(tmp, st.st_mode & 0o777)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except (PermissionError, OSError):
                pass
        os.replace(tmp, path)
        return
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        logger.warning(f"Scrittura atomica di {path} non possibile ({e}): scrivo in-place.")
    with open(path, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def safe_write_text(path, text):
    safe_write_bytes(path, text.encode('utf-8'))


def run_cmd(args, timeout=30, cwd=None):
    """Esegue un comando SENZA shell e con timeout. rc=124 timeout, rc=127 comando mancante."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout ({timeout}s) eseguendo: {' '.join(map(str, args))}")
        return subprocess.CompletedProcess(args, 124, '', 'timeout')
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, '', 'command not found')
    except Exception as e:
        return subprocess.CompletedProcess(args, 1, '', str(e))


def read_lines(path):
    """Righe non vuote e non commentate di un file di testo ([] se manca)."""
    try:
        p = Path(str(path))
        if not str(path) or not p.is_file():
            return []
        return [l.strip() for l in p.read_text(encoding='utf-8', errors='ignore').splitlines()
                if l.strip() and not l.strip().startswith('#')]
    except Exception as e:
        logger.error(f"Errore lettura {path}: {e}")
        return []


def connected():
    return mqtt_client is not None and mqtt_client.is_connected()


def log_to_broker(msg):
    try:
        if mqtt_client is not None:
            mqtt_client.publish(TOPIC_LOGS, msg)
    except Exception as e:
        logger.error(f"Publish log error: {e}")


def publish_status():
    """Stato del nodo (retained). Va ripubblicato a ogni connessione: dopo un blip il LWT l'ha messo OFFLINE."""
    try:
        if mqtt_client is not None:
            mqtt_client.publish(TOPIC_STAT, current_status, qos=1, retain=True)
    except Exception as e:
        logger.error(f"Publish status error: {e}")


def note_ops_done():
    """Dopo un'operazione l'auto-healing resta in pausa qualche secondo (i servizi stanno ripartendo)."""
    global heal_pause_until
    heal_pause_until = time.time() + int_setting('heal_grace_seconds', 20, 0)


# ==========================================
# 3. TELEGRAM (invio in un thread dedicato)
# ==========================================
def in_quiet_hours(hour):
    q = cfg['telegram'].get('quiet_hours', {"start": 23, "end": 7})
    if q is False or q is None or not isinstance(q, dict):
        return False
    try:
        start, end = int(q.get('start', 23)), int(q.get('end', 7))
    except (TypeError, ValueError):
        return False
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def send_telegram_message(message, urgent=False):
    t_cfg = cfg['telegram']
    if not t_cfg.get('enabled', False):
        return

    current_hour = int(time.strftime("%H"))
    if not urgent and in_quiet_hours(current_hour):
        logger.info(f"🌙 Quiet hours ({current_hour}:00): Telegram notification skipped.")
        return

    token = t_cfg.get('token')
    chat_id = t_cfg.get('chat_id')
    if not token or not chat_id or token == "TOKEN ID":
        return

    clean_msg = str(message).replace("<b>", "").replace("</b>", "")
    try:
        tg_queue.put_nowait((token, chat_id, f"[{CLIENT_ID.upper()}]\n{clean_msg}"))
    except queue.Full:
        logger.warning("Telegram queue full: message dropped")


def telegram_worker():
    while True:
        token, chat_id, text = tg_queue.get()
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat_id, "text": text}, timeout=10)
            if not r.ok:
                logger.error(f"Telegram HTTP {r.status_code}")
        except Exception as e:
            # l'eccezione di requests contiene l'URL con il token: va ripulita
            logger.error(f"Telegram sending error: {type(e).__name__}: {redact(e)}")


# ==========================================
# 4. STATO PERSISTENTE (profilo attivo, servizi fermati a mano)
# ==========================================
def norm_name(name):
    n = str(name).strip().lower()
    return n[:-8] if n.endswith('.service') else n


def load_state():
    global stopped_by_admin
    try:
        with open(STATE_PATH, 'r') as f:
            data = json.load(f)
        stopped_by_admin = {norm_name(x) for x in data.get('stopped', [])}
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Impossibile leggere {STATE_PATH}: {e}")


def save_state():
    try:
        safe_write_text(STATE_PATH, json.dumps({"stopped": sorted(stopped_by_admin)}))
    except Exception as e:
        logger.warning(f"Impossibile salvare {STATE_PATH}: {e}")


def get_actual_config_from_disk():
    try:
        if os.path.exists(LAST_PROFILE_PATH):
            with open(LAST_PROFILE_PATH, "r") as f:
                label = f.read().strip()
                if label:
                    return f"ONLINE - {label}"
    except Exception as e:
        logger.error(f"Errore lettura memoria profilo: {e}")
    return "ONLINE"  # Default se il file non esiste o è vuoto


def save_last_profile(label):
    try:
        safe_write_text(LAST_PROFILE_PATH, label)
    except Exception as e:
        logger.error(f"Impossibile salvare il profilo attivo in {LAST_PROFILE_PATH}: {e}")


# ==========================================
# 5. SYSTEMD: NOMI UNITÀ (whitelist) E CONTROLLI
# ==========================================
def build_unit_map():
    """nome normalizzato -> nome unità. Solo ciò che il nodo conosce: process_list, profili, settings.service_map."""
    m = {}
    for name in read_lines(PROCESS_LIST_PATH):
        n = norm_name(name)
        if UNIT_RE.match(n):
            m[n] = n
    for profile in cfg['profiles'].values():
        for s in (profile.get('services', []) if isinstance(profile, dict) else []):
            name = str(s.get('name', ''))
            if UNIT_RE.match(name):
                m[norm_name(name)] = name
    for k, v in (cfg['settings'].get('service_map') or {}).items():
        if UNIT_RE.match(str(v)):
            m[norm_name(k)] = str(v)
    return m


def resolve_unit(name):
    return build_unit_map().get(norm_name(name))


def systemctl(action, unit, timeout=60):
    return run_cmd(["sudo", "systemctl", action, unit], timeout=timeout)


def unit_is_active(unit):
    r = run_cmd(["systemctl", "is-active", unit], timeout=10)
    return r.returncode == 0 and r.stdout.strip() == 'active'


def unit_exists(unit):
    return run_cmd(["systemctl", "cat", unit], timeout=10).returncode == 0


def unit_is_disabled(unit):
    r = run_cmd(["systemctl", "is-enabled", unit], timeout=10)
    return r.stdout.strip() in ('disabled', 'masked')


def wait_units_active(units, seconds):
    """Attende fino a `seconds` che tutte le unità siano attive. Ritorna la lista di quelle NON attive."""
    deadline = time.monotonic() + seconds
    pending = list(units)
    while True:
        pending = [u for u in pending if not unit_is_active(u)]
        if not pending or time.monotonic() >= deadline:
            return pending
        time.sleep(1)


# ==========================================
# 6. RESET HARDWARE (USB / GPIO)
# ==========================================
def gpio_pin_config():
    pin = cfg['settings'].get('gpio_reset_pin')
    if pin in (None, '', 0, False):
        return None            # nessun default implicito: il GPIO si usa solo se configurato
    try:
        return int(pin)
    except (TypeError, ValueError):
        return None


def hardware_reset(auto=False):
    """Reset del modem. Ritorna {"attempted", "ok", "method", "msg"}."""
    usb_id = str(cfg['settings'].get('usb_reset_id') or '').strip()
    if usb_id:
        if not USB_ID_RE.match(usb_id):
            return {"attempted": False, "ok": False, "method": "USB", "msg": f"⚠️ usb_reset_id non valido: {usb_id!r}"}
        logger.info(f"Esecuzione reset USB per il dispositivo {usb_id}...")
        r = run_cmd(["sudo", "usbreset", usb_id], timeout=15)
        time.sleep(1.5)
        if r.returncode == 0:
            return {"attempted": True, "ok": True, "method": "USB", "msg": f"🔌 USB Reset ({usb_id}) inviato!"}
        return {"attempted": True, "ok": False, "method": "USB",
                "msg": f"⚠️ USB Reset ({usb_id}) fallito (rc={r.returncode}): {r.stderr.strip()[:120]}"}

    pin = gpio_pin_config()
    if pin is not None and GPIO_AVAILABLE and (not auto or cfg['settings'].get('gpio_reset_auto', True)):
        logger.info(f"Esecuzione reset GPIO (HAT) sul pin {pin}...")
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, GPIO.LOW)
            time.sleep(0.5)
            GPIO.output(pin, GPIO.HIGH)
            GPIO.cleanup(pin)
            time.sleep(1.5)
            return {"attempted": True, "ok": True, "method": "HAT", "msg": f"🔌 GPIO Pulse (Pin {pin}) inviato!"}
        except Exception as e:
            logger.error(f"Errore GPIO: {e}")
            return {"attempted": True, "ok": False, "method": "HAT", "msg": f"⚠️ Errore GPIO (Pin {pin}): {e}"}

    return {"attempted": False, "ok": False, "method": "",
            "msg": "⚠️ Nessun metodo di reset (USB o GPIO) configurato o disponibile sul nodo."}


# ==========================================
# 7. TELEMETRIA
# ==========================================
def get_cpu_temperature():
    temp = 0.0
    try:
        temps = psutil.sensors_temperatures() if hasattr(psutil, 'sensors_temperatures') else {}
        if temps.get('cpu_thermal'):
            temp = temps['cpu_thermal'][0].current
        elif temps.get('coretemp'):
            temp = temps['coretemp'][0].current
        elif platform.system() == "Linux":
            r = run_cmd(["vcgencmd", "measure_temp"], timeout=5)
            m = re.search(r'temp=([\d.]+)', r.stdout) if r.returncode == 0 else None
            if m:
                temp = float(m.group(1))
    except Exception as e:
        logger.debug(f"Temperature read error: {e}")
    return round(temp, 1)


def get_system_status():
    status = {
        "cpu_usage_percent": psutil.cpu_percent(interval=0.5),
        "cpu_temp": get_cpu_temperature(),
        "memory_usage_percent": psutil.virtual_memory().percent,
        "disk_usage_percent": psutil.disk_usage('/').percent,
        "processes": {},
        "timestamp": time.strftime("%H:%M:%S"),
        "profiles": {k: (v.get('label', f'PROFILE {k}') if isinstance(v, dict) else f'PROFILE {k}')
                     for k, v in cfg['profiles'].items()},
        "update_interval": get_update_interval(),     # il server ne ricava le soglie "agent muto"
        "agent_version": AGENT_VERSION,
    }
    names = read_lines(PROCESS_LIST_PATH)
    if names:
        try:
            running = {(p.info.get('name') or '').lower() for p in psutil.process_iter(['name'])}
            for name in names:
                n = name.strip().lower()
                if n:
                    status["processes"][n] = "online" if n in running else "offline"
        except Exception as e:
            logger.error(f"Process check error: {e}")
    return status


def publish_telemetry():
    status = get_system_status()
    stems = [Path(f).stem for f in read_lines(FILE_LIST_PATH)]
    status["config_files"] = stems
    status["files"] = stems
    # nomi completi con estensione (es. dstargateway.cfg): la dashboard li usa solo per l'etichetta
    status["file_names"] = [Path(f).name for f in read_lines(FILE_LIST_PATH)]
    if connected():
        mqtt_client.publish(TOPIC_SERVICES, json.dumps(status), qos=1)
    return status


# ==========================================
# 8. PUBBLICAZIONE CONFIGURAZIONI (solo se cambiate)
# ==========================================
def parse_ini(text):
    ini_data = {}
    current_section = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';')):
            continue
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
    return ini_data


def mask_sensitive(ini_data):
    """Nasconde Password/Secret/Token nei topic per sezione (NON tocca PassAllTG/PassAllPC, che servono al server)."""
    for payload in ini_data.values():
        for k, v in payload.items():
            if SENSITIVE_KEY_RE.match(k) and str(v).strip():
                payload[k] = "********"
    return ini_data


def profile_only_sources():
    """File usati solo come sorgente di un profilo (es. *_BM.ini): per l'editor basta full_config."""
    sources, targets = set(), set()
    for profile in cfg['profiles'].values():
        for s in (profile.get('services', []) if isinstance(profile, dict) else []):
            if s.get('source'):
                sources.add(s['source'])
            if s.get('target'):
                targets.add(s['target'])
    return sources - targets


def publish_configs(force=False):
    """Pubblica full_config e sezioni INI (retained) SOLO per i file cambiati dall'ultima volta."""
    if not connected():
        return
    skip_sections = profile_only_sources()
    mask = bool(cfg['settings'].get('mask_passwords', True))
    for fp in read_lines(FILE_LIST_PATH):
        try:
            if not os.path.isfile(fp):
                continue
            st = os.stat(fp)
            sig = (st.st_mtime_ns, st.st_size)
            if not force and published_sig.get(fp) == sig:
                continue
            text = Path(fp).read_text(encoding='utf-8', errors='replace')
            stem = Path(fp).stem
            mqtt_client.publish(f"data/{CLIENT_ID}/{stem}/full_config",
                                json.dumps({"raw_text": text}), qos=1, retain=True)
            if fp not in skip_sections:
                ini_data = parse_ini(text)
                if mask:
                    mask_sensitive(ini_data)
                for section, payload in ini_data.items():
                    part = re.sub(r'[/+#]', '_', section)
                    if part:
                        mqtt_client.publish(f"data/{CLIENT_ID}/{stem}/{part}", json.dumps(payload), qos=1, retain=True)
            published_sig[fp] = sig
        except Exception as e:
            logger.error(f"Error publishing config {fp}: {e}")


# ==========================================
# 9. CAMBIO PROFILO
# ==========================================
def find_profile(key):
    k = str(key).strip().lower()
    for pkey, profile in cfg['profiles'].items():
        if str(pkey).lower() == k and isinstance(profile, dict):
            return pkey, profile
    return None, None


def switch_config(key):
    """Ritorna (ok, label_o_errore). Non tocca nessun servizio se i controlli preliminari falliscono;
    se qualcosa va storto ripristina la configurazione precedente e riavvia i servizi."""
    pkey, profile = find_profile(key)
    if not profile:
        return False, f"Profile {key} not found in JSON"
    label = profile.get('label', f"Profile {pkey}")
    services = profile.get('services', [])
    if not services:
        return False, f"No services configured for {pkey}"

    # --- CONTROLLI PRELIMINARI (prima di fermare qualsiasi cosa) ---
    for s in services:
        name, src, dst = s.get('name'), s.get('source'), s.get('target')
        if not (name and src and dst):
            return False, f"Invalid service entry in profile {pkey}"
        if not UNIT_RE.match(str(name)):
            return False, f"Invalid unit name {name!r}"
        if not os.path.isfile(src):
            return False, f"Missing source file {src}"
        if not os.path.isdir(os.path.dirname(str(dst)) or '.'):
            return False, f"Target directory missing for {dst}"

    verify = int_setting('verify_seconds', 8, 0)
    with ops_lock:
        previous = {}
        for s in services:
            dst = s['target']
            try:
                previous[dst] = Path(dst).read_bytes() if os.path.exists(dst) else None
            except Exception as e:
                return False, f"Cannot read target {dst}: {e}"

        error = None
        try:
            for s in services:
                systemctl("stop", s['name'])
            for s in services:
                safe_write_bytes(s['target'], Path(s['source']).read_bytes())
        except Exception as e:
            error = f"Copy failed: {e}"

        if error is None:
            for s in services:
                systemctl("start", s['name'])
            if verify > 0:
                failed = wait_units_active([s['name'] for s in services], verify)
                if failed:
                    error = f"Units not active after switch: {', '.join(failed)}"

        if error:
            logger.error(f"Cambio profilo {pkey} fallito ({error}): ripristino la configurazione precedente.")
            for s in services:
                systemctl("stop", s['name'])
            for dst, data in previous.items():
                if data is not None:
                    try:
                        safe_write_bytes(dst, data)
                    except Exception as e:
                        logger.error(f"Rollback di {dst} fallito: {e}")
            for s in services:
                systemctl("start", s['name'])
            note_ops_done()
            return False, f"{error} - rolled back to previous configuration"

        for s in services:
            stopped_by_admin.discard(norm_name(s['name']))
        save_state()
        save_last_profile(label)
        note_ops_done()
        return True, label


# ==========================================
# 10. SCRITTURA CONFIG DA REMOTO
# ==========================================
def service_for_file(path):
    """Servizio da riavviare dopo la modifica di un file di configurazione (None = nessuno)."""
    stem = Path(path).stem
    unit = resolve_unit(stem)
    if unit:
        return unit
    guess = stem.lower()
    if str(path) not in profile_only_sources() and UNIT_RE.match(guess) and unit_exists(guess):
        return guess
    return None


def apply_config_set(slug, payload):
    try:
        data = json.loads(payload)
    except Exception as e:
        logger.error(f"Config {slug}: JSON non valido ({e})")
        log_to_broker(f"❌ Config {slug}: invalid JSON")
        return
    raw = data.get('raw_text') if isinstance(data, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        logger.error(f"Config {slug}: raw_text mancante o vuoto: rifiutato")
        log_to_broker(f"❌ Config {slug}: empty or missing raw_text, refused")
        return
    raw_bytes = raw.encode('utf-8')
    if len(raw_bytes) > MAX_CONFIG_BYTES:
        logger.error(f"Config {slug}: troppo grande ({len(raw_bytes)} byte)")
        log_to_broker(f"❌ Config {slug}: too large")
        return

    target = None
    for line in read_lines(FILE_LIST_PATH):
        if Path(line).stem.lower() == slug.lower():
            target = Path(line)
            break
    if target is None:
        logger.error(f"Config {slug}: file non presente in file_list")
        log_to_broker(f"❌ Config {slug}: not in file list")
        return

    verify = int_setting('verify_seconds', 8, 0)
    with ops_lock:
        previous = target.read_bytes() if target.exists() else None
        if previous is not None:
            try:
                shutil.copy2(target, str(target) + ".bak")
            except Exception as e:
                logger.warning(f"Backup .bak non creato per {target}: {e}")
        safe_write_bytes(target, raw_bytes)

        unit = service_for_file(target)
        if unit:
            systemctl("restart", unit)
            if verify > 0:
                failed = wait_units_active([unit], verify)
                if failed:
                    logger.error(f"Config {slug}: {unit} non riparte con la nuova configurazione: ripristino.")
                    if previous is not None:
                        safe_write_bytes(target, previous)
                    systemctl("restart", unit)
                    log_to_broker(f"❌ Config {slug}: service did not start, previous configuration restored")
                    send_telegram_message(f"❌ Config {slug.upper()} rejected: service did not start (restored).")
                    note_ops_done()
                    return
        else:
            logger.info(f"Config {slug} salvata: nessun servizio da riavviare.")
        note_ops_done()

    send_telegram_message(f"📝 Config {slug.upper()} updated via Web.")
    logger.info(f"Configuration {slug} updated successfully.")


# ==========================================
# 11. AUTO-HEALING
# ==========================================
def check_auto_healing(status):
    if not cfg['settings'].get('auto_healing', False):
        return
    if time.time() < heal_pause_until:
        return
    if not ops_lock.acquire(blocking=False):   # c'è un'operazione in corso: ci riprovo al prossimo giro
        return
    try:
        _heal(status)
    finally:
        ops_lock.release()


def _heal(status):
    state_changed = False
    for proc_name, state in status["processes"].items():
        norm = norm_name(proc_name)

        if state == "online":
            auto_healing_counter[proc_name] = 0
            if norm in stopped_by_admin:
                stopped_by_admin.discard(norm)
                state_changed = True
            continue

        if norm in stopped_by_admin:
            continue                                  # fermato volontariamente dalla dashboard
        unit = resolve_unit(norm)
        if not unit or not unit_exists(unit) or unit_is_disabled(unit):
            if norm not in _warned_units:
                _warned_units.add(norm)
                logger.info(f"Auto-healing: '{proc_name}' ignorato (unità assente o disabilitata su questo nodo).")
            continue

        attempts = auto_healing_counter.get(proc_name, 0)
        if attempts < 3:
            auto_healing_counter[proc_name] = attempts + 1
            msg = f"🛠 Auto-healing: {proc_name} offline. Restarting {attempts + 1}/3..."
            log_to_broker(msg)
            send_telegram_message(msg)

            if norm == "mmdvmhost":
                res = hardware_reset(auto=True)
                if res["attempted"]:
                    log_to_broker(res["msg"])
                    if not res["ok"]:
                        logger.warning(res["msg"])

            systemctl("restart", unit)
        elif attempts == 3:
            msg = f"🚨 CRITICAL: {proc_name} failed!"
            log_to_broker(msg)
            send_telegram_message(msg, urgent=True)
            auto_healing_counter[proc_name] = 4

    if state_changed:
        save_state()


# ==========================================
# 12. COMANDI (eseguiti da un thread dedicato, mai nella callback MQTT)
# ==========================================
def enqueue(name, fn):
    try:
        cmd_queue.put_nowait((name, fn))
        return True
    except queue.Full:
        logger.error(f"Coda comandi piena: '{name}' scartato")
        log_to_broker(f"❌ Command queue full: {name} discarded")
        return False


def command_worker():
    while True:
        name, fn = cmd_queue.get()
        try:
            fn()
        except Exception as e:
            logger.exception(f"Errore eseguendo il comando '{name}'")
            log_to_broker(f"❌ ERROR ({name}): {redact(e)}"[:300])
            publish_status()


def do_switch_profile(key):
    global current_status
    ok, text = switch_config(key)
    if ok:
        current_status = f"ONLINE - {text}"
        send_telegram_message(f"✅ Multiple switch completed: {text}")
    else:
        logger.error(f"Cambio profilo fallito: {text}")
        log_to_broker(f"❌ {text}")
        send_telegram_message(f"❌ Profile switch failed: {text}")
    publish_status()          # anche in caso di errore: il server esce dallo stato "Sent..."
    publish_telemetry()
    publish_configs()


def do_update_agent():
    log_to_broker("🔄 Avvio aggiornamento OTA (git pull)...")
    logger.info("Ricevuto comando di UPDATE_AGENT. Esecuzione git pull...")
    with ops_lock:
        head = run_cmd(["git", "rev-parse", "HEAD"], timeout=15, cwd=AGENT_DIR)
        old = head.stdout.strip() if head.returncode == 0 else None

        pull = run_cmd(["git", "pull", "--ff-only"], timeout=120, cwd=AGENT_DIR)
        if pull.returncode != 0:
            err = redact(pull.stderr or pull.stdout)[:500]
            log_to_broker(f"❌ Errore aggiornamento:\n{err}")
            logger.error(f"Errore git pull:\n{err}")
            publish_status()
            return

        head = run_cmd(["git", "rev-parse", "HEAD"], timeout=15, cwd=AGENT_DIR)
        new = head.stdout.strip() if head.returncode == 0 else None
        if old and new and old == new:
            log_to_broker("✅ Già aggiornato (nessuna modifica).")
            publish_status()
            return

        # La nuova versione deve almeno essere sintatticamente valida, altrimenti il nodo resterebbe irraggiungibile
        check = run_cmd([sys.executable, "-c",
                         "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())",
                         os.path.abspath(__file__)], timeout=30)
        if check.returncode != 0:
            if old:
                run_cmd(["git", "reset", "--hard", old], timeout=30, cwd=AGENT_DIR)
            msg = redact(check.stderr)[:300]
            log_to_broker(f"❌ Nuova versione non valida: ripristinata la precedente.\n{msg}")
            logger.error(f"Nuova versione non valida, rollback a {old}: {msg}")
            publish_status()
            return

        log_to_broker("✅ Git pull completato. Riavvio in corso...")
        logger.info(f"Aggiornamento completato:\n{pull.stdout}")
        time.sleep(1)
        # Systemd (Restart=always) lo fa ripartire con il nuovo codice
        os._exit(1)


def do_reboot():
    mqtt_client.publish(TOPIC_STAT, f"OFFLINE - Rebooting {CLIENT_ID.upper()}...", qos=1, retain=True)
    logger.info("REBOOT command received. Rebooting system...")
    time.sleep(1)
    r = run_cmd(["sudo", "reboot"], timeout=30)
    if r.returncode != 0:
        logger.error(f"Reboot fallito (rc={r.returncode}): {r.stderr.strip()[:200]}")
        log_to_broker("❌ Reboot failed")
        publish_status()


def do_reset_hat():
    with ops_lock:
        res = hardware_reset(auto=False)
        if not res["attempted"]:
            logger.warning(res["msg"])
            log_to_broker(res["msg"])
            publish_status()
            return
        logger.info("Restarting MMDVMHost...")
        unit = resolve_unit("mmdvmhost") or "mmdvmhost"
        systemctl("restart", unit)
        if res["ok"]:
            mqtt_client.publish(f"fleet/{CLIENT_ID}/status", f"{res['method']} RESET + MMDVM RESTART OK")
            log_to_broker(f"{res['msg']} + MMDVMHost Restarted")
        else:
            mqtt_client.publish(f"fleet/{CLIENT_ID}/status", f"RESET ERROR: {res['msg']}")
            log_to_broker(f"{res['msg']} (MMDVMHost restarted anyway)")
        note_ops_done()
    publish_status()


def do_toggle_telegram(new_state):
    try:
        # si parte dal file su disco (non dalla copia in memoria) per non perdere modifiche fatte a mano
        with open(CONFIG_PATH, 'r') as f:
            disk = json.load(f)
        if not isinstance(disk.get('telegram'), dict):
            disk['telegram'] = {}
        disk['telegram']['enabled'] = new_state
        safe_write_text(CONFIG_PATH, json.dumps(disk, indent=4))
        cfg['telegram']['enabled'] = new_state
        log_to_broker(f"{'🔔' if new_state else '🔇'} Notifications {'ON' if new_state else 'OFF'}")
        if new_state:
            send_telegram_message("Notifications reactivated!")
    except Exception as e:
        logger.error(f"Error saving Telegram status: {e}")
    publish_status()


def do_service_control(action, service):
    unit = resolve_unit(service)
    if not unit:
        logger.warning(f"Servizio non consentito: {service[:40]!r}")
        log_to_broker(f"❌ Service not allowed: {service[:40]}")
        publish_status()
        return
    with ops_lock:
        r = systemctl(action, unit)
        if r.returncode == 0:
            n = norm_name(service)
            if action == "stop":
                stopped_by_admin.add(n)       # l'auto-healing non lo rialzerà
            else:
                stopped_by_admin.discard(n)
            save_state()
            log_to_broker(f"✅ {action.upper()}: {service}")
            logger.info(f"Service command executed: {action.upper()} {service}")
        else:
            err = r.stderr.strip()[:200]
            log_to_broker(f"❌ ERROR: {action} {service}: {err}")
            logger.error(f"Error executing service command {action} {service}: {err}")
        note_ops_done()
    publish_telemetry()
    publish_status()


def do_global_request():
    time.sleep(random.uniform(0, 5))      # evita che tutta la flotta risponda nello stesso istante
    publish_telemetry()
    publish_configs(force=True)
    publish_status()


def initial_publish():
    publish_telemetry()
    publish_configs(force=True)


# ==========================================
# 13. MQTT CALLBACKS (leggere: validano e mettono in coda)
# ==========================================
def handle_command(payload):
    cmd = payload.upper()
    pkey, profile = find_profile(payload)
    if profile:
        enqueue(f"profile {pkey}", lambda: do_switch_profile(pkey))
    elif cmd == "UPDATE_AGENT":
        enqueue("update_agent", do_update_agent)
    elif cmd == "REBOOT":
        enqueue("reboot", do_reboot)
    elif cmd == "RESET_HAT":
        enqueue("reset_hat", do_reset_hat)
    elif cmd in ("TG:OFF", "TG:ON"):
        enqueue("toggle_telegram", lambda: do_toggle_telegram(cmd == "TG:ON"))
    else:
        logger.warning(f"Comando sconosciuto ignorato: {payload[:40]!r}")
        log_to_broker(f"❓ Unknown command: {payload[:40]}")
        publish_status()


def handle_control(payload):
    action, _, service = payload.partition(":")
    action, service = action.strip().lower(), service.strip()
    if action not in ("restart", "stop", "start") or not service:
        logger.warning(f"Comando di controllo non valido: {payload[:60]!r}")
        log_to_broker(f"❌ Invalid control command: {payload[:60]}")
        return
    enqueue(f"{action} {service}", lambda: do_service_control(action, service))


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        logger.info(f"✅ Connected to MQTT broker: {CLIENT_ID.upper()}")
        client.subscribe([
            (TOPIC_CMD, 0),
            (TOPIC_REQUEST, 0),
            (TOPIC_CONTROL, 0),
            (TOPIC_CONFIG_SET + "#", 0)
        ])
        publish_status()                       # a ogni connessione: dopo un blip il LWT ha messo il nodo OFFLINE
        enqueue("initial publish", initial_publish)
    else:
        logger.error(f"❌ MQTT connection error. Code: {reason_code}")


def on_connect_fail(client, userdata):
    logger.warning("MQTT: connessione al broker non riuscita, riprovo...")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    logger.warning(f"⚠️ Disconnesso dal broker MQTT (codice: {reason_code}).")
    if cfg['settings'].get('exit_on_disconnect', False):
        logger.error("exit_on_disconnect attivo: riavvio tramite systemd...")
        os._exit(1)


def on_message(client, userdata, msg):
    try:
        topic = msg.topic
        payload = msg.payload.decode('utf-8', errors='replace').strip()

        if getattr(msg, 'retain', False):
            logger.warning(f"Messaggio retained ignorato su {topic} (i comandi non vanno mai pubblicati retained)")
            return

        if topic == TOPIC_CMD:
            handle_command(payload)
        elif topic == TOPIC_REQUEST:
            if payload.lower() in ("status", "update"):
                logger.info("📥 Received global update command (REQ CONFIG)")
                enqueue("global request", do_global_request)
        elif topic == TOPIC_CONTROL:
            handle_control(payload)
        elif topic.startswith(TOPIC_CONFIG_SET):
            slug = topic[len(TOPIC_CONFIG_SET):]
            if SLUG_RE.match(slug):
                enqueue(f"config {slug}", lambda: apply_config_set(slug, payload))
            else:
                logger.warning(f"config_set con nome non valido: {slug[:40]!r}")
    except Exception:
        logger.exception("Errore in on_message")


# ==========================================
# 14. THREAD DI SERVIZIO: TELEMETRIA E WATCHDOG
# ==========================================
def telemetry_loop():
    while True:
        try:
            if connected():
                status = publish_telemetry()
                publish_configs()
            else:
                status = get_system_status()
            check_auto_healing(status)      # l'auto-healing è locale: funziona anche senza broker
        except Exception as e:
            logger.error(f"❌ Errore critico nel thread telemetria: {e}")
        time.sleep(get_update_interval())


def watchdog_tick(now=None):
    """Se il broker resta irraggiungibile oltre max_offline_seconds il processo esce e systemd lo rilancia."""
    global disconnected_since
    now = time.time() if now is None else now
    if connected():
        if disconnected_since is not None:
            logger.info("✅ Connessione MQTT ripristinata.")
            disconnected_since = None
        return
    if disconnected_since is None:
        disconnected_since = now
        return
    limit = int_setting('max_offline_seconds', 300, 30)
    if now - disconnected_since >= limit:
        logger.error(f"🚨 Broker MQTT irraggiungibile da {int(now - disconnected_since)}s. Riavvio del processo...")
        os._exit(1)


def watchdog_loop():
    while True:
        try:
            watchdog_tick()
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
        time.sleep(10)


def shutdown_handler(signum, frame):
    """systemctl stop/restart: con una disconnessione 'pulita' il LWT non parte, quindi OFFLINE lo pubblichiamo noi."""
    logger.info("Shutdown richiesto: pubblico OFFLINE e chiudo.")
    try:
        info = mqtt_client.publish(TOPIC_STAT, f"OFFLINE - {CLIENT_ID.upper()}", qos=1, retain=True)
        info.wait_for_publish(timeout=3)
        mqtt_client.disconnect()
    except Exception:
        pass
    os._exit(0)


def start_service():
    global current_status, mqtt_client, heal_pause_until
    load_state()
    current_status = get_actual_config_from_disk()
    heal_pause_until = time.time() + int_setting('startup_grace_seconds', 60, 0)

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID.upper())
    mqtt_client = client
    # LWT retained: se il nodo muore il broker sostituisce anche il messaggio "ONLINE" conservato
    client.will_set(TOPIC_STAT, payload=f"OFFLINE - {CLIENT_ID.upper()}", qos=1, retain=True)
    client.username_pw_set(MQTT_CFG['user'], MQTT_CFG['password'])
    if MQTT_CFG.get('tls'):
        client.tls_set(ca_certs=MQTT_CFG.get('ca_cert') or None)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.on_connect = on_connect
    client.on_connect_fail = on_connect_fail
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    for target, name in ((command_worker, "commands"), (telegram_worker, "telegram"),
                         (telemetry_loop, "telemetry"), (watchdog_loop, "watchdog")):
        threading.Thread(target=target, name=name, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    logger.info(f"Node agent v{AGENT_VERSION} avviato ({CLIENT_ID.upper()}). Connessione al broker...")
    # connect_async + loop_start: paho gestisce da solo primo collegamento e riconnessioni
    client.connect_async(MQTT_CFG['broker'], int(MQTT_CFG['port']), int(MQTT_CFG.get('keepalive', 15)))
    client.loop_start()

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    start_service()
