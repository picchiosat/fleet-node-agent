# 🛰️ Fleet Node Agent

🌍 *[Read in English](#english) | 🇮🇹 [Leggi in Italiano](#italiano)*

---

<a name="english"></a>
## 🇬🇧 English

The **Fleet Node Agent** is a lightweight monitoring and control script designed for remote MMDVM nodes. It acts as the bridge between your radio hardware and the Central Console.

> ℹ️ **Note:** This is the Remote Agent repository, intended to be installed on individual repeater nodes. To manage and monitor all your fleet from a single web dashboard, you must also install the [Fleet Control Console (Server)](https://git.arifvg.it/iv3jdv/fleet-control-server) on your central hub. Use the latest console version: older ones do not recognise the `OFFLINE - <ID>` / `ONLINE - <profile>` states sent by this agent, so node push notifications would not fire.

**Contents:** [Features](#en-features) · [How it works](#en-topics) · [Safe operations](#en-safe) · [Profiles](#en-profiles) · [Hardware reset](#en-reset) · [Telegram](#en-telegram) · [Security](#en-security) · [Configuration](#en-config) · [Installation](#en-install) · [Updating](#en-update) · [Troubleshooting](#en-trouble)

<a name="en-features"></a>
### ✨ Key Features
* **Real-Time Telemetry:** CPU, RAM, disk, temperature and the status of the monitored processes, sent via MQTT every `update_interval` seconds (together with the agent version and the interval, so the console can tell when an agent has gone silent).
* **Service Management:** Remote Start/Stop/Restart of system daemons (MMDVMHost, DMRGateway, etc.). Only services that the node knows about can be controlled (see [Security](#en-security)).
* **Remote Configuration:** The Central Server can edit your `.ini` files remotely, with a safety net: empty or oversized content is refused, a `.bak` copy is kept, and if the service does not come back up with the new file the previous version is restored automatically.
* **Dynamic Profiles:** Switch between complete configurations (e.g. BrandMeister ⇄ DMR+) with automatic rollback if something goes wrong.
* **Auto-Healing:** Detects service failures and restarts them (3 attempts, then a CRITICAL alert). It never restarts a service you stopped on purpose from the dashboard.
* **Hardware Reset:** Surgical reset of a stuck modem via USB or GPIO, without rebooting the node.
* **Safe OTA Update:** One-click `git pull` from the console, with a syntax check of the new code and automatic rollback if it is not valid.
* **Telegram Alerts:** Real-time notifications with configurable quiet hours; CRITICAL alerts are always delivered.
* **Resilient MQTT:** Automatic reconnection, retained last-will so that a dead node is never shown as online, and a watchdog for a broker that stays unreachable.
* **Lightweight:** Configuration is published only when files actually change, and passwords are masked in the per-section topics.

> 👥 **On the console:** restart/stop/start of services, the config editor, reboot, HAT reset and the OTA update are reserved to *admin* users. Profile switching and the Telegram toggle are available to operators allowed on that node.

<a name="en-topics"></a>
### 📡 How it works (MQTT topics)
Everything goes through your MQTT broker. `<id>` is your `client_id` in lowercase.

| Topic | Direction | Content |
|---|---|---|
| `servizi/<id>/cmnd` | agent ⬅ console | Commands: a profile key (`A`, `B`, …), `TG:ON`, `TG:OFF`, `REBOOT`, `RESET_HAT`, `UPDATE_AGENT` |
| `servizi/<id>/stat` | agent ➡ console (retained) | `ONLINE - <profile label>` / `OFFLINE - <ID>` (also used as Last Will) |
| `devices/<id>/services` | agent ➡ console | Telemetry JSON (CPU, RAM, disk, temperature, processes, profiles, files, `update_interval`, `agent_version`) |
| `devices/<id>/control` | agent ⬅ console | `start:<service>`, `stop:<service>`, `restart:<service>` |
| `devices/<id>/config_set/<file>` | agent ⬅ console | `{"raw_text": "..."}` – new content of a file from `file_list.txt` |
| `devices/control/request` | agent ⬅ console | `status` / `update`: republish everything (each node answers after a random 0–5 s delay) |
| `devices/<id>/logs` | agent ➡ console | Outcome of commands and auto-healing messages |
| `data/<id>/<file>/full_config` | agent ➡ console (retained) | Full text of each configuration file (used by the editor) |
| `data/<id>/<file>/<Section>` | agent ➡ console (retained) | Each INI section as JSON (passwords masked) |
| `fleet/<id>/status` | agent ➡ console | Result of a `RESET_HAT` |

> The console always uses `servizi/<id>` as the base topic, so leave `mqtt.base_topic` at its default (`servizi/<your id>`).
> The dashboard reads the **Info/General** data from a file named `MMDVMHost.ini` or `MMDVM.ini`, and the DMR networks from `DMRGateway.ini`. Keep these names in `file_list.txt`.

<a name="en-safe"></a>
### 🛡️ Safe operations
Every operation that touches services or files is executed by a **single worker thread**, one at a time, never inside the MQTT callback (so a slow `git pull` or `systemctl` can no longer cause a disconnection).

* **Profile switch:** all source files and units are checked *before* anything is stopped. Then services are stopped, files copied atomically, services started, and (after up to `verify_seconds`) every unit is verified to be active. If anything fails, the previous configuration is restored and the services restarted. The node keeps its previous status and the error is reported to the console and Telegram.
* **Remote config write:** content must be a non-empty text up to 64 KB. The previous file is saved as `<file>.bak`, the new one is written atomically and the *right* service is restarted (the one mapped to that file). If it does not start, the old content is restored. Files that are only a *source* of a profile (e.g. `MMDVMHost_BM.ini`) are saved without restarting anything.
* **OTA update (`UPDATE_AGENT`):** `git pull --ff-only` with a timeout. If the code changed, its syntax is checked; if invalid, the previous commit is restored (`git reset --hard`) and the agent keeps running. If valid, the agent exits and systemd restarts it with the new code. Credentials in git URLs are never published.
* **Auto-healing:** a service reported offline is restarted up to 3 times (with a hardware reset first for `mmdvmhost`, if configured), then a CRITICAL alert is sent once. It is skipped for services you stopped from the dashboard (remembered across restarts in `state_file`), for units that do not exist or are disabled on that node, while another operation is running (and for `heal_grace_seconds` after it), and during the first `startup_grace_seconds` after boot. It works even if the broker is unreachable.
* **Atomic writes:** configuration, last profile and state files are written to a temporary file and then renamed, keeping permissions. If the directory is not writable (e.g. a single file in `/etc` made writable for the agent user) it falls back to an in-place write.

<a name="en-profiles"></a>
### 🔄 Dynamic Profiles (Multiple Configurations)
The agent supports unlimited dynamic profile switching (e.g., switching between a BrandMeister and a DMR+ configuration on the fly).

In your `node_config.json`, define any number of custom profiles under the `"profiles"` key. For each profile, specify a `label` and the list of services/files to swap (`name` of the systemd unit, `source` file to copy, `target` file to overwrite):

```json
"profiles": {
    "A": {
        "label": "Config BM",
        "services": [
            { "name": "mmdvmhost.service", "source": "/etc/MMDVMHost_BM.ini", "target": "/etc/MMDVMHost.ini" },
            { "name": "dmrgateway.service", "source": "/etc/DMRGateway_BM.ini", "target": "/etc/DMRGateway.ini" }
        ]
    }
}
```
The Central Console dashboard reads this configuration and generates a button for each profile. Profile keys are matched case-insensitively. If you don't need profile switching for a node, leave the dictionary empty (`"profiles": {}`) or remove the block: the dashboard hides the controls for that node. The active profile is remembered in `paths.last_profile` and restored at boot.

<a name="en-reset"></a>
### 🔌 Hardware Resets (USB & GPIO)
The agent can reset a stuck modem without rebooting the whole node, either on demand (**Reset HAT** button) or automatically before restarting `mmdvmhost` during auto-healing. Configure it under `"settings"`:
* **USB modems (e.g., STM32 Nucleo):** set `"usb_reset_id"` to the device ID (e.g. `"0483:374b"`). Requires the `usbreset` tool (package `usbutils`) and, if the agent is not root, a sudoers rule.
* **GPIO HATs:** set `"gpio_reset_pin"` to your BCM pin (e.g. `21`). Requires the `RPi.GPIO` library (optional: if it is missing, or cannot be loaded on your board, the GPIO reset is simply disabled and a warning is written to the log). **There is no default pin**: GPIO is used only if the pin is configured. With `"gpio_reset_auto": false` the GPIO is excluded from auto-healing while the manual *Reset HAT* keeps working.

USB has priority: if `"usb_reset_id"` is empty (`""`) the agent falls back to GPIO. The real outcome is always reported (a failed `usbreset` is no longer reported as "sent"). On a node without a HAT, leave `gpio_reset_pin` as `null`.

<a name="en-telegram"></a>
### 🤖 Telegram Bot Integration
Real-time alerts about node status, offline services and critical events on your phone. Configure the `telegram` block:

```json
"telegram": {
    "enabled": true,
    "token": "123456:ABC...",
    "chat_id": "123456789",
    "quiet_hours": { "start": 23, "end": 7 }
}
```
* **Quiet hours** (default 23:00–07:00, local time of the node) silence normal messages; **CRITICAL alerts always get through**. Use `"quiet_hours": false` to disable them.
* Notifications can be turned on or muted for each node from the Fleet Console (`TG:ON` / `TG:OFF`); the choice is saved in `node_config.json`.
* Messages are sent by a dedicated thread, and the bot token never appears in the logs.

<a name="en-security"></a>
### 🔐 Security
The agent executes commands received over MQTT, so protect the channel:
* **Whitelist:** remote service control only works on names the node knows: `process_list.txt`, the services of the profiles and `settings.service_map`. Anything else (including systemctl options) is rejected. If the dashboard cannot control a service, add it to `process_list.txt`.
* **Passwords:** keys such as `Password`, `Pass`, `Secret`, `*Token`, `AuthKey` are masked in the per-section topics (`PassAllTG`/`PassAllPC` are not touched). The `full_config` topic, used by the editor, contains the **full file, passwords included**: restrict who can read `data/#` on the broker. Masking can be disabled with `mask_passwords: false`.
* **Broker:** use one MQTT user per node with ACLs limited to its own topics (`servizi/<id>/#`, `devices/<id>/#`, `data/<id>/#`, `fleet/<id>/#`), a separate user for the server, and TLS (`mqtt.tls`) if the broker is not on a LAN/VPN. MMDVMHost and the gateways use their own credentials, so they need rules too.
* **Retained commands are ignored**, so a stale message can never re-run a reboot.
* **Files:** `chmod 600 node_config.json`, and never commit it (add `node_config.json`, `node_agent.log*` and `*.bak` to `.gitignore`).
* **sudo:** the provided `fleet-agent.service` runs the agent as `root`, so no sudoers rule is needed. If you change `User=` to a non-root user, allow only what the agent needs (check paths with `which systemctl reboot usbreset`, validate with `visudo -c`):
  ```
  # /etc/sudoers.d/fleet-agent   (replace "pi" with the agent user)
  pi ALL=(root) NOPASSWD: /usr/bin/systemctl start *, /usr/bin/systemctl stop *, /usr/bin/systemctl restart *, /usr/sbin/reboot, /usr/bin/usbreset *
  ```

<a name="en-config"></a>
### 🛠️ Configuration
Three files drive the agent:
* **`node_config.json`**: main configuration (MQTT broker, your unique `client_id`, paths, settings, Telegram, profiles). `client_id` and the `mqtt` credentials are **required**; everything else is optional. Use only letters, digits, `_` and `-` in the `client_id` (the console accepts nothing else), and make it **unique**: two nodes with the same ID kick each other off the broker.
* **`process_list.txt`**: names of the processes/services to monitor, one per line (e.g. `mmdvmhost`, `dmrgateway`). They are checked, shown on the dashboard, auto-healed and controllable remotely. Lines starting with `#` are ignored.
* **`file_list.txt`**: absolute paths of the configuration files to expose to the editor (e.g. `/etc/MMDVMHost.ini`), one per line. The topic name is the file name without extension.

Complete example (`node_config.json.example` contains the same keys):
```json
{
    "client_id": "ir3xxx",
    "mqtt": { "broker": "127.0.0.1", "port": 1883, "user": "mmdvm", "password": "password",
              "base_topic": "servizi/ir3xxx", "keepalive": 15, "tls": false },
    "paths": {
        "file_list": "/opt/fleet-node-agent/file_list.txt",
        "process_list": "/opt/fleet-node-agent/process_list.txt",
        "last_profile": "/opt/last_profile.txt",
        "state_file": "/opt/agent_state.json"
    },
    "settings": {
        "auto_healing": true,
        "update_interval": 60,
        "usb_reset_id": "",
        "gpio_reset_pin": null,
        "verify_seconds": 8,
        "max_offline_seconds": 300
    },
    "telegram": { "enabled": false, "token": "TOKEN ID", "chat_id": "CHAT ID", "quiet_hours": { "start": 23, "end": 7 } },
    "profiles": {}
}
```

| Key | Default | Description |
|---|---|---|
| `settings.auto_healing` | `false` | Restart failed services (see above) |
| `settings.update_interval` | `30` | Seconds between telemetry messages (minimum 10) |
| `settings.usb_reset_id` | – | USB ID for `usbreset` (priority over GPIO) |
| `settings.gpio_reset_pin` | none | BCM pin of the HAT reset line. No implicit default |
| `settings.gpio_reset_auto` | `true` | `false`: never pulse the GPIO from auto-healing |
| `settings.verify_seconds` | `8` | How long to wait for services to be active after a profile switch / config write (`0` = don't verify) |
| `settings.max_offline_seconds` | `300` | If the broker stays unreachable this long, the process exits so that systemd restarts it |
| `settings.exit_on_disconnect` | `false` | `true`: exit at *every* disconnection (the old behaviour) |
| `settings.heal_grace_seconds` | `20` | Auto-healing pause after an operation |
| `settings.startup_grace_seconds` | `60` | Auto-healing pause after the agent starts |
| `settings.mask_passwords` | `true` | Mask passwords in per-section topics |
| `settings.service_map` | `{}` | `{"process": "unit.service"}` when the process name differs from the unit name |
| `paths.file_list` / `process_list` | `/opt/file_list.txt` / `/opt/process_list.txt` | Point them to where you created the files |
| `paths.last_profile` | `/opt/last_profile.txt` | Active profile label (must be writable) |
| `paths.state_file` | `/opt/agent_state.json` | Services stopped on purpose (must be writable) |
| `mqtt.keepalive` | `15` | MQTT keepalive in seconds |
| `mqtt.tls` / `mqtt.ca_cert` | `false` / – | Enable TLS (optional CA file) |
| `telegram.quiet_hours` | 23 → 7 | `{"start": h, "end": h}` or `false` |

<a name="en-install"></a>
### 🚀 Installation Guide
**Requirements:** Python 3.8+, `git`, and the packages in `requirements.txt` (`paho-mqtt>=2.0`, `psutil`, `requests`); optionally `RPi.GPIO` (GPIO reset, install it with `pip install RPi.GPIO` inside the venv) and `usbutils` (USB reset).

#### 0. Root Privileges
All installation steps must be executed as the `root` user. Before starting, elevate your privileges by running:
```bash
sudo su
```

#### 1. Clone the Repository
Clone the agent into `/opt` to ensure path consistency:
```bash
git clone https://git.arifvg.it/iv3jdv/fleet-node-agent.git /opt/fleet-node-agent
cd /opt/fleet-node-agent
```

#### 2. Virtual Environment Setup
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### 3. Configuration
1. **Main Config**: `cp node_config.json.example node_config.json` and edit it with your `client_id` and MQTT credentials.
2. **Processes**: `cp process_list.txt.example process_list.txt` and add your service names (one per line).
3. **Files**: `cp file_list.txt.example file_list.txt` and add the absolute paths to your `.ini` files.
4. **Paths**: make sure `paths.file_list` and `paths.process_list` in `node_config.json` point to the files you just created (e.g. `/opt/fleet-node-agent/file_list.txt`).
5. **Permissions**: `chmod 600 node_config.json`.
6. **Telegram** (optional): fill in the `telegram` block (`enabled`, `token`, `chat_id`).

#### 4. Systemd Service (Auto-start)
```bash
cp systemd/fleet-agent.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable fleet-agent
systemctl start fleet-agent
```
> The provided unit runs the agent as `root` with the venv's Python and already has `Restart=always` and `RestartSec=10`. Keep both: the OTA update and the watchdog rely on systemd to restart the agent.

Check that it is running: `journalctl -u fleet-agent -f`. The node should appear online in the console within a few seconds.

<a name="en-update"></a>
### 🔄 Updating
* **From the console:** use the **UPDATE OTA** button of the node (admin only).
* **By hand:** `cd /opt/fleet-node-agent && git pull && systemctl restart fleet-agent`.

The OTA update needs the agent folder to be a git clone where `git pull` works without prompts (public repository or stored credentials). Keep local files (`node_config.json`, logs, `*.bak`) out of git to avoid conflicts.

**Upgrading from 1.x:** your `node_config.json` keeps working unchanged. Three things behave differently: (1) a broker disconnection no longer restarts the process (paho reconnects by itself; see `max_offline_seconds` / `exit_on_disconnect`); (2) the GPIO reset needs `gpio_reset_pin` to be set explicitly; (3) `OFFLINE` is now a retained message and the agent republishes its status at every connection.

<a name="en-trouble"></a>
### 🩺 Troubleshooting
The node card on the console shows a line like `MMDVM 12s · GW 3m · AGENT 3s` with the age of the last message received for each source.
* **AGENT** is a periodic heartbeat: a warning (⚠) means the agent stopped publishing although the node is shown online (stuck connection?).
* **MMDVM** / **GW** arrive only when something happens (a transmission, a mode change…), so on a quiet repeater a long age is normal. A ⚠ on MMDVM means **nothing has ever been received** since the console started: check that MMDVMHost is connected to the broker (its MQTT section: broker, credentials, topic).

| Symptom | Things to check |
|---|---|
| Node always offline | Broker address/port, credentials and ACLs; unique `client_id`; `journalctl -u fleet-agent` |
| A service cannot be controlled from the dashboard | It must be in `process_list.txt` (or in a profile / `service_map`) |
| Profile switch reports an error | The message names the cause (missing file, unit not active…); the previous profile has been restored. Increase `verify_seconds` for slow services |
| Edited config "disappears" | The service failed to start with it and the agent restored the old file (see the log message) |
| Reset HAT does nothing | `gpio_reset_pin` not set, `RPi.GPIO` not installed, or `usb_reset_id`/`usbreset` missing |
| A stopped service is not restarted by auto-healing | By design: it was stopped from the dashboard. Use *start*/*restart* to hand it back to auto-healing |

Logs: `journalctl -u fleet-agent -f` and `node_agent.log` in the agent folder (rotating, 2 MB × 3).

---

<a name="italiano"></a>
## 🇮🇹 Italiano

Il **Fleet Node Agent** è lo script di monitoraggio e controllo per i nodi remoti MMDVM. Funge da ponte tra l'hardware radio e la Console Centrale.

> ℹ️ **Nota:** Questo è il repository dell'Agent Remoto, da installare sui singoli nodi ripetitore. Per gestire e monitorare tutta la flotta da un'unica dashboard web, devi installare anche la [Fleet Control Console (Server)](https://git.arifvg.it/iv3jdv/fleet-control-server) sul tuo hub centrale. Usa la versione più recente della console: le precedenti non riconoscono gli stati `OFFLINE - <ID>` / `ONLINE - <profilo>` inviati da questo agent, quindi le notifiche push dei nodi non partirebbero.

**Indice:** [Funzionalità](#it-features) · [Come funziona](#it-topics) · [Operazioni sicure](#it-safe) · [Profili](#it-profiles) · [Reset hardware](#it-reset) · [Telegram](#it-telegram) · [Sicurezza](#it-security) · [Configurazione](#it-config) · [Installazione](#it-install) · [Aggiornamento](#it-update) · [Risoluzione problemi](#it-trouble)

<a name="it-features"></a>
### ✨ Funzionalità Principali
* **Telemetria Real-Time:** CPU, RAM, disco, temperatura e stato dei processi monitorati, inviati via MQTT ogni `update_interval` secondi (insieme a versione dell'agent e intervallo, così la console capisce quando un agent è diventato muto).
* **Gestione Servizi:** Avvio, arresto o riavvio remoto dei demoni di sistema (MMDVMHost, DMRGateway, ecc.). Si possono controllare solo i servizi che il nodo conosce (vedi [Sicurezza](#it-security)).
* **Configurazione Remota:** Il Server Centrale può modificare i file `.ini` a distanza, con una rete di sicurezza: contenuti vuoti o troppo grandi vengono rifiutati, viene conservata una copia `.bak` e, se il servizio non riparte con il nuovo file, la versione precedente viene ripristinata automaticamente.
* **Profili Dinamici:** Cambio tra configurazioni complete (es. BrandMeister ⇄ DMR+) con rollback automatico se qualcosa va storto.
* **Auto-Healing:** Rileva i crash dei servizi e li riavvia (3 tentativi, poi un allarme CRITICAL). Non riavvia mai un servizio che hai fermato di proposito dalla dashboard.
* **Reset Hardware:** Reset "chirurgico" di un modem bloccato via USB o GPIO, senza riavviare il nodo.
* **Aggiornamento OTA sicuro:** `git pull` con un click dalla console, con controllo di sintassi del nuovo codice e rollback automatico se non è valido.
* **Avvisi Telegram:** Notifiche in tempo reale con ore silenziose configurabili; gli allarmi CRITICAL arrivano sempre.
* **MQTT resiliente:** Riconnessione automatica, last-will retained (un nodo morto non risulta mai online) e watchdog per un broker che resta irraggiungibile.
* **Leggero:** Le configurazioni vengono pubblicate solo quando i file cambiano davvero e le password sono mascherate nei topic per sezione.

> 👥 **Sulla console:** riavvio/stop/start dei servizi, editor di configurazione, reboot, reset HAT e aggiornamento OTA sono riservati agli utenti *admin*. Cambio profilo e interruttore Telegram sono disponibili agli operatori abilitati su quel nodo.

<a name="it-topics"></a>
### 📡 Come funziona (topic MQTT)
Tutto passa dal tuo broker MQTT. `<id>` è il tuo `client_id` in minuscolo.

| Topic | Direzione | Contenuto |
|---|---|---|
| `servizi/<id>/cmnd` | agent ⬅ console | Comandi: chiave di un profilo (`A`, `B`, …), `TG:ON`, `TG:OFF`, `REBOOT`, `RESET_HAT`, `UPDATE_AGENT` |
| `servizi/<id>/stat` | agent ➡ console (retained) | `ONLINE - <etichetta profilo>` / `OFFLINE - <ID>` (usato anche come Last Will) |
| `devices/<id>/services` | agent ➡ console | JSON di telemetria (CPU, RAM, disco, temperatura, processi, profili, file, `update_interval`, `agent_version`) |
| `devices/<id>/control` | agent ⬅ console | `start:<servizio>`, `stop:<servizio>`, `restart:<servizio>` |
| `devices/<id>/config_set/<file>` | agent ⬅ console | `{"raw_text": "..."}` – nuovo contenuto di un file di `file_list.txt` |
| `devices/control/request` | agent ⬅ console | `status` / `update`: ripubblica tutto (ogni nodo risponde dopo un'attesa casuale di 0–5 s) |
| `devices/<id>/logs` | agent ➡ console | Esito dei comandi e messaggi dell'auto-healing |
| `data/<id>/<file>/full_config` | agent ➡ console (retained) | Testo completo di ogni file di configurazione (usato dall'editor) |
| `data/<id>/<file>/<Sezione>` | agent ➡ console (retained) | Ogni sezione INI come JSON (password mascherate) |
| `fleet/<id>/status` | agent ➡ console | Esito di un `RESET_HAT` |

> La console usa sempre `servizi/<id>` come topic base: lascia `mqtt.base_topic` al valore predefinito (`servizi/<tuo id>`).
> La dashboard legge i dati **Info/General** da un file chiamato `MMDVMHost.ini` o `MMDVM.ini`, e le reti DMR da `DMRGateway.ini`. Mantieni questi nomi in `file_list.txt`.

<a name="it-safe"></a>
### 🛡️ Operazioni sicure
Ogni operazione che tocca servizi o file è eseguita da **un unico thread di lavoro**, una alla volta, mai dentro la callback MQTT (così un `git pull` o un `systemctl` lenti non possono più provocare una disconnessione).

* **Cambio profilo:** tutti i file sorgente e le unità vengono controllati *prima* di fermare qualsiasi cosa. Poi si fermano i servizi, si copiano i file in modo atomico, si avviano i servizi e (entro `verify_seconds`) si verifica che ogni unità sia attiva. Se qualcosa fallisce, la configurazione precedente viene ripristinata e i servizi riavviati. Il nodo mantiene lo stato precedente e l'errore viene segnalato alla console e a Telegram.
* **Scrittura config da remoto:** il contenuto deve essere un testo non vuoto fino a 64 KB. Il file precedente viene salvato come `<file>.bak`, il nuovo viene scritto in modo atomico e viene riavviato il servizio *giusto* (quello associato al file). Se non parte, torna il contenuto precedente. I file che sono solo *sorgente* di un profilo (es. `MMDVMHost_BM.ini`) vengono salvati senza riavviare nulla.
* **Aggiornamento OTA (`UPDATE_AGENT`):** `git pull --ff-only` con timeout. Se il codice è cambiato ne viene controllata la sintassi; se non è valido si torna al commit precedente (`git reset --hard`) e l'agent continua a girare. Se è valido, l'agent esce e systemd lo riavvia con il nuovo codice. Le credenziali negli URL di git non vengono mai pubblicate.
* **Auto-healing:** un servizio segnalato offline viene riavviato fino a 3 volte (prima con un reset hardware per `mmdvmhost`, se configurato), poi parte una sola volta un allarme CRITICAL. Viene saltato per i servizi che hai fermato dalla dashboard (ricordati anche dopo un riavvio in `state_file`), per le unità inesistenti o disabilitate su quel nodo, mentre un'altra operazione è in corso (e per `heal_grace_seconds` dopo), e nei primi `startup_grace_seconds` dopo l'avvio. Funziona anche se il broker è irraggiungibile.
* **Scritture atomiche:** configurazione, ultimo profilo e file di stato vengono scritti su un file temporaneo e poi rinominati, mantenendo i permessi. Se la directory non è scrivibile (es. un singolo file in `/etc` reso scrivibile all'utente dell'agent) si ripiega sulla scrittura in-place.

<a name="it-profiles"></a>
### 🔄 Profili Dinamici Multipli (Dynamic Profiles)
L'agent supporta il cambio rapido tra un numero illimitato di configurazioni (es. per passare "al volo" da un setup BrandMeister a uno DMR+).

Nel file `node_config.json` puoi definire quanti profili desideri sotto la chiave `"profiles"`. Per ogni profilo specifica un'etichetta (`label`) e l'elenco di servizi/file da scambiare (`name` dell'unità systemd, file `source` da copiare, file `target` da sovrascrivere):

```json
"profiles": {
    "A": {
        "label": "Config BM",
        "services": [
            { "name": "mmdvmhost.service", "source": "/etc/MMDVMHost_BM.ini", "target": "/etc/MMDVMHost.ini" },
            { "name": "dmrgateway.service", "source": "/etc/DMRGateway_BM.ini", "target": "/etc/DMRGateway.ini" }
        ]
    }
}
```
La dashboard centrale legge questa configurazione e genera un pulsante per ogni profilo. Le chiavi dei profili sono confrontate senza distinguere maiuscole e minuscole. Se un nodo non necessita di questa funzione, lascia il dizionario vuoto (`"profiles": {}`) o rimuovi il blocco: la dashboard nasconde i controlli per quel nodo. Il profilo attivo viene ricordato in `paths.last_profile` e ripristinato all'avvio.

<a name="it-reset"></a>
### 🔌 Reset Hardware (USB & GPIO)
L'agent può resettare un modem bloccato senza riavviare l'intero nodo, su richiesta (pulsante **Reset HAT**) oppure in automatico prima di riavviare `mmdvmhost` durante l'auto-healing. Si configura sotto `"settings"`:
* **Modem USB (es. STM32 Nucleo):** imposta `"usb_reset_id"` con l'ID del dispositivo (es. `"0483:374b"`). Richiede lo strumento `usbreset` (pacchetto `usbutils`) e, se l'agent non è root, una regola sudoers.
* **HAT GPIO:** imposta `"gpio_reset_pin"` con il tuo pin BCM (es. `21`). Richiede la libreria `RPi.GPIO` (facoltativa: se manca, o non può essere caricata sulla tua scheda, il reset GPIO viene semplicemente disattivato e nel log compare un avviso). **Non esiste un pin di default**: il GPIO si usa solo se il pin è configurato. Con `"gpio_reset_auto": false` il GPIO è escluso dall'auto-healing mentre il *Reset HAT* manuale continua a funzionare.

L'USB ha la priorità: se `"usb_reset_id"` è vuoto (`""`) si ripiega sul GPIO. L'esito reale viene sempre riportato (un `usbreset` fallito non viene più segnalato come "inviato"). Su un nodo senza HAT lascia `gpio_reset_pin` a `null`.

<a name="it-telegram"></a>
### 🤖 Telegram Bot Integration
Alert in tempo reale sullo stato dei nodi, sui servizi offline e sugli eventi critici direttamente sullo smartphone. Configura il blocco `telegram`:

```json
"telegram": {
    "enabled": true,
    "token": "123456:ABC...",
    "chat_id": "123456789",
    "quiet_hours": { "start": 23, "end": 7 }
}
```
* Le **ore silenziose** (predefinite 23:00–07:00, ora locale del nodo) silenziano i messaggi normali; gli **allarmi CRITICAL passano sempre**. Con `"quiet_hours": false` si disattivano.
* Le notifiche si possono attivare o silenziare per ogni nodo dalla Fleet Console (`TG:ON` / `TG:OFF`); la scelta viene salvata in `node_config.json`.
* I messaggi partono da un thread dedicato e il token del bot non compare mai nei log.

<a name="it-security"></a>
### 🔐 Sicurezza
L'agent esegue comandi ricevuti via MQTT, quindi proteggi il canale:
* **Whitelist:** il controllo remoto dei servizi funziona solo sui nomi che il nodo conosce: `process_list.txt`, i servizi dei profili e `settings.service_map`. Tutto il resto (comprese le opzioni di systemctl) viene rifiutato. Se la dashboard non riesce a controllare un servizio, aggiungilo a `process_list.txt`.
* **Password:** chiavi come `Password`, `Pass`, `Secret`, `*Token`, `AuthKey` sono mascherate nei topic per sezione (`PassAllTG`/`PassAllPC` non vengono toccate). Il topic `full_config`, usato dall'editor, contiene il **file completo, password comprese**: limita chi può leggere `data/#` sul broker. Il mascheramento si disattiva con `mask_passwords: false`.
* **Broker:** un utente MQTT per nodo con ACL limitate ai suoi topic (`servizi/<id>/#`, `devices/<id>/#`, `data/<id>/#`, `fleet/<id>/#`), un utente separato per il server e TLS (`mqtt.tls`) se il broker non è in LAN/VPN. MMDVMHost e i gateway usano le proprie credenziali, quindi servono regole anche per loro.
* **I comandi retained vengono ignorati**, quindi un messaggio vecchio non può rieseguire un reboot.
* **File:** `chmod 600 node_config.json` e non committarlo mai (aggiungi `node_config.json`, `node_agent.log*` e `*.bak` al `.gitignore`).
* **sudo:** il `fleet-agent.service` fornito fa girare l'agent come `root`, quindi non serve nessuna regola sudoers. Se cambi `User=` con un utente non-root, consenti solo ciò che serve all'agent (verifica i percorsi con `which systemctl reboot usbreset`, controlla con `visudo -c`):
  ```
  # /etc/sudoers.d/fleet-agent   (sostituisci "pi" con l'utente dell'agent)
  pi ALL=(root) NOPASSWD: /usr/bin/systemctl start *, /usr/bin/systemctl stop *, /usr/bin/systemctl restart *, /usr/sbin/reboot, /usr/bin/usbreset *
  ```

<a name="it-config"></a>
### 🛠️ Configurazione
Tre file guidano l'agent:
* **`node_config.json`**: configurazione principale (broker MQTT, `client_id` univoco, percorsi, impostazioni, Telegram, profili). `client_id` e le credenziali `mqtt` sono **obbligatori**; tutto il resto è facoltativo. Nel `client_id` usa solo lettere, numeri, `_` e `-` (la console non accetta altro) e fai in modo che sia **univoco**: due nodi con lo stesso ID si scollegano a vicenda dal broker.
* **`process_list.txt`**: nomi dei processi/servizi da monitorare, uno per riga (es. `mmdvmhost`, `dmrgateway`). Vengono controllati, mostrati in dashboard, riavviati dall'auto-healing e controllabili da remoto. Le righe che iniziano con `#` sono ignorate.
* **`file_list.txt`**: percorsi assoluti dei file di configurazione da esporre all'editor (es. `/etc/MMDVMHost.ini`), uno per riga. Il nome del topic è il nome del file senza estensione.

Esempio completo (`node_config.json.example` contiene le stesse chiavi):
```json
{
    "client_id": "ir3xxx",
    "mqtt": { "broker": "127.0.0.1", "port": 1883, "user": "mmdvm", "password": "password",
              "base_topic": "servizi/ir3xxx", "keepalive": 15, "tls": false },
    "paths": {
        "file_list": "/opt/fleet-node-agent/file_list.txt",
        "process_list": "/opt/fleet-node-agent/process_list.txt",
        "last_profile": "/opt/last_profile.txt",
        "state_file": "/opt/agent_state.json"
    },
    "settings": {
        "auto_healing": true,
        "update_interval": 60,
        "usb_reset_id": "",
        "gpio_reset_pin": null,
        "verify_seconds": 8,
        "max_offline_seconds": 300
    },
    "telegram": { "enabled": false, "token": "TOKEN ID", "chat_id": "CHAT ID", "quiet_hours": { "start": 23, "end": 7 } },
    "profiles": {}
}
```

| Chiave | Default | Descrizione |
|---|---|---|
| `settings.auto_healing` | `false` | Riavvia i servizi caduti (vedi sopra) |
| `settings.update_interval` | `30` | Secondi tra i messaggi di telemetria (minimo 10) |
| `settings.usb_reset_id` | – | ID USB per `usbreset` (priorità sul GPIO) |
| `settings.gpio_reset_pin` | nessuno | Pin BCM della linea di reset dell'HAT. Nessun default implicito |
| `settings.gpio_reset_auto` | `true` | `false`: l'auto-healing non dà mai impulsi sul GPIO |
| `settings.verify_seconds` | `8` | Quanto attendere che i servizi siano attivi dopo un cambio profilo / scrittura config (`0` = non verificare) |
| `settings.max_offline_seconds` | `300` | Se il broker resta irraggiungibile così a lungo, il processo esce e systemd lo riavvia |
| `settings.exit_on_disconnect` | `false` | `true`: esce a *ogni* disconnessione (il vecchio comportamento) |
| `settings.heal_grace_seconds` | `20` | Pausa dell'auto-healing dopo un'operazione |
| `settings.startup_grace_seconds` | `60` | Pausa dell'auto-healing dopo l'avvio dell'agent |
| `settings.mask_passwords` | `true` | Maschera le password nei topic per sezione |
| `settings.service_map` | `{}` | `{"processo": "unita.service"}` quando il nome del processo differisce da quello dell'unità |
| `paths.file_list` / `process_list` | `/opt/file_list.txt` / `/opt/process_list.txt` | Puntali a dove hai creato i file |
| `paths.last_profile` | `/opt/last_profile.txt` | Etichetta del profilo attivo (deve essere scrivibile) |
| `paths.state_file` | `/opt/agent_state.json` | Servizi fermati di proposito (deve essere scrivibile) |
| `mqtt.keepalive` | `15` | Keepalive MQTT in secondi |
| `mqtt.tls` / `mqtt.ca_cert` | `false` / – | Abilita TLS (file CA facoltativo) |
| `telegram.quiet_hours` | 23 → 7 | `{"start": h, "end": h}` oppure `false` |

<a name="it-install"></a>
### 🚀 Guida all'Installazione
**Requisiti:** Python 3.8+, `git` e i pacchetti di `requirements.txt` (`paho-mqtt>=2.0`, `psutil`, `requests`); facoltativi `RPi.GPIO` (reset GPIO, si installa con `pip install RPi.GPIO` dentro il venv) e `usbutils` (reset USB).

#### 0. Privilegi di Root
Tutti i passaggi di installazione devono essere eseguiti come utente `root`. Prima di iniziare, eleva i tuoi privilegi eseguendo:
```bash
sudo su
```

#### 1. Clonazione del Repository
Clona l'agent nella cartella `/opt` per garantire la coerenza con i servizi systemd:
```bash
git clone https://git.arifvg.it/iv3jdv/fleet-node-agent.git /opt/fleet-node-agent
cd /opt/fleet-node-agent
```

#### 2. Setup Ambiente Virtuale (venv)
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

#### 3. Personalizzazione
1. **Config Principale**: `cp node_config.json.example node_config.json` e inserisci il tuo `client_id` e i dati MQTT.
2. **Processi**: `cp process_list.txt.example process_list.txt` e aggiungi i nomi dei tuoi servizi (uno per riga).
3. **File**: `cp file_list.txt.example file_list.txt` e inserisci i percorsi assoluti dei tuoi file `.ini`.
4. **Percorsi**: verifica che `paths.file_list` e `paths.process_list` in `node_config.json` puntino ai file appena creati (es. `/opt/fleet-node-agent/file_list.txt`).
5. **Permessi**: `chmod 600 node_config.json`.
6. **Telegram** (facoltativo): compila il blocco `telegram` (`enabled`, `token`, `chat_id`).

#### 4. Esecuzione come Servizio (systemd)
```bash
cp systemd/fleet-agent.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable fleet-agent
systemctl start fleet-agent
```
> L'unità fornita fa girare l'agent come `root` con il Python del venv e ha già `Restart=always` e `RestartSec=10`. Mantienili entrambi: l'aggiornamento OTA e il watchdog si affidano a systemd per riavviare l'agent.

Verifica che giri: `journalctl -u fleet-agent -f`. Il nodo dovrebbe comparire online nella console entro pochi secondi.

<a name="it-update"></a>
### 🔄 Aggiornamento
* **Dalla console:** usa il pulsante **UPDATE OTA** del nodo (solo admin).
* **A mano:** `cd /opt/fleet-node-agent && git pull && systemctl restart fleet-agent`.

L'aggiornamento OTA richiede che la cartella dell'agent sia un clone git in cui `git pull` funzioni senza richieste interattive (repository pubblico o credenziali salvate). Tieni i file locali (`node_config.json`, log, `*.bak`) fuori da git per evitare conflitti.

**Passando dalla 1.x:** il tuo `node_config.json` continua a funzionare senza modifiche. Tre cose si comportano in modo diverso: (1) una disconnessione dal broker non riavvia più il processo (paho si riconnette da solo; vedi `max_offline_seconds` / `exit_on_disconnect`); (2) il reset GPIO richiede `gpio_reset_pin` impostato esplicitamente; (3) `OFFLINE` ora è un messaggio retained e l'agent ripubblica il proprio stato a ogni connessione.

<a name="it-trouble"></a>
### 🩺 Risoluzione problemi
La card del nodo nella console mostra una riga come `MMDVM 12s · GW 3m · AGENT 3s` con l'età dell'ultimo messaggio ricevuto da ciascuna fonte.
* **AGENT** è un heartbeat periodico: un avviso (⚠) significa che l'agent ha smesso di pubblicare pur risultando online (connessione bloccata?).
* **MMDVM** / **GW** arrivano solo quando succede qualcosa (una trasmissione, un cambio modo…), quindi su un ripetitore tranquillo un'età lunga è normale. Un ⚠ su MMDVM significa che **non è mai arrivato nulla** da quando la console è attiva: controlla che MMDVMHost sia collegato al broker (sezione MQTT: broker, credenziali, topic).

| Sintomo | Cosa controllare |
|---|---|
| Nodo sempre offline | Indirizzo/porta del broker, credenziali e ACL; `client_id` univoco; `journalctl -u fleet-agent` |
| Un servizio non si controlla dalla dashboard | Deve essere in `process_list.txt` (o in un profilo / `service_map`) |
| Il cambio profilo segnala un errore | Il messaggio indica la causa (file mancante, unità non attiva…); il profilo precedente è stato ripristinato. Aumenta `verify_seconds` per i servizi lenti |
| La config modificata "sparisce" | Il servizio non partiva con quella config e l'agent ha ripristinato il file vecchio (vedi il messaggio nel log) |
| Reset HAT non fa nulla | `gpio_reset_pin` non impostato, `RPi.GPIO` non installata, oppure mancano `usb_reset_id`/`usbreset` |
| Un servizio fermato non viene riavviato dall'auto-healing | È voluto: l'hai fermato dalla dashboard. Usa *start*/*restart* per restituirlo all'auto-healing |

Log: `journalctl -u fleet-agent -f` e `node_agent.log` nella cartella dell'agent (a rotazione, 2 MB × 3).

---
*Created by IV3JDV @ ARIFVG - 2026*
