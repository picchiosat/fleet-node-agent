# 🛰️ Fleet Control Agent

🌍 *[Read in English](#english) | 🇮🇹 [Leggi in Italiano](#italiano)*

---

<a name="english"></a>
## 🇬🇧 English

The **Fleet Control Agent** is a lightweight monitoring and control script designed for remote MMDVM nodes. It acts as the bridge between your radio hardware and the Central Console.

### ✨ Key Features
* **Real-Time Telemetry:** Streams CPU, RAM, Disk usage, and Temperature data via MQTT.
* **Service Management:** Allows remote Start/Stop/Restart of system daemons (MMDVMHost, DMRGateway, etc.).
* **Remote Configuration:** Enables the Central Server to edit `.ini` files remotely.
* **Hardware Reset:** Supports physical MMDVM HAT reset via GPIO pins (requires RPi.GPIO).
* **Auto-Healing:** Automatically detects service failures and attempts to restart them.

### 🛠️ Core Configuration Files
To enable full functionality, you must configure these three files:
* **`node_config.json`**: The main configuration. Here you set your MQTT broker, your unique `client_id` (e.g., `IR3XXX`), and the paths for the lists below.
* **`process_list.txt`**: List the names of the system services you want to monitor (e.g., `mmdvmhost`, `dmrgateway`). The agent will check their status and attempt to restart them if they crash (Auto-healing).
* **`file_list.txt`**: List the full absolute paths of the configuration files (e.g., `/etc/MMDVMHost.ini`) that you want to edit remotely from the Dashboard.

### 🚀 Installation Guide

#### 1. Clone the Repository
Clone the agent into `/opt` to ensure path consistency:
```bash
sudo git clone https://git.arifvg.it/iv3jdv/fleet-control-agent.git /opt/fleet-control-agent
cd /opt/fleet-control-agent
```

#### 2. Virtual Environment Setup
```bash
sudo python3 -m venv venv
source venv/bin/activate
sudo pip install -r requirements.txt
```

#### 3. Configuration
1. **Main Config**: `cp node_config.json.example node_config.json` and edit it with your MQTT credentials.
2. **Processes**: `cp process_list.txt.example process_list.txt` and add your service names (one per line).
3. **Files**: `cp file_list.txt.example file_list.txt` and add the absolute paths to your `.ini` files.

#### 4. Systemd Service (Auto-start)
```bash
sudo cp fleet-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable fleet-agent
sudo systemctl start fleet-agent
```

---

<a name="italiano"></a>
## 🇮🇹 Italiano

Il **Fleet Control Agent** è lo script di monitoraggio e controllo per i nodi remoti MMDVM. Funge da ponte tra l'hardware radio e la Console Centrale.

### ✨ Funzionalità Principali
* **Telemetria Real-Time:** Invia dati su CPU, RAM, Disco e Temperatura via MQTT.
* **Gestione Servizi:** Permette l'avvio, l'arresto o il riavvio remoto dei demoni di sistema (MMDVMHost, DMRGateway, ecc.).
* **Configurazione Remota:** Consente al Server Centrale di modificare i file `.ini` a distanza.
* **Reset Hardware:** Supporta il reset fisico della scheda MMDVM HAT tramite pin GPIO (richiede RPi.GPIO).
* **Auto-Healing:** Rileva automaticamente i crash dei servizi e tenta di riavviarli.

### 🛠️ File di Configurazione Chiave
Per il corretto funzionamento, è necessario definire i parametri in questi tre file:
* **`node_config.json`**: La configurazione principale. Qui imposti il broker MQTT, il tuo `client_id` univoco (es. `IR3XXX`) e i percorsi per le liste sottostanti.
* **`process_list.txt`**: Elenca i nomi dei servizi di sistema da monitorare (es. `mmdvmhost`, `dmrgateway`). L'agente ne controllerà lo stato e proverà a riavviarli in caso di crash (Auto-healing).
* **`file_list.txt`**: Elenca i percorsi completi dei file di configurazione (es. `/etc/MMDVMHost.ini`) che desideri poter modificare remotamente dalla Dashboard.

### 🚀 Guida all'Installazione

#### 1. Clonazione del Repository
Clona l'agente nella cartella `/opt` per garantire la coerenza con i servizi systemd:
```bash
sudo git clone [https://git.arifvg.it/iv3jdv/fleet-control-agent.git](https://git.arifvg.it/iv3jdv/fleet-control-agent.git) /opt/fleet-control-agent
cd /opt/fleet-control-agent
```

#### 2. Setup Ambiente Virtuale (venv)
```bash
sudo python3 -m venv venv
source venv/bin/activate
sudo pip install -r requirements.txt
```

#### 3. Personalizzazione
1. **Config Principale**: `cp node_config.json.example node_config.json` e inserisci i dati MQTT.
2. **Processi**: `cp process_list.txt.example process_list.txt` e aggiungi i nomi dei tuoi servizi (uno per riga).
3. **File**: `cp file_list.txt.example file_list.txt` e inserisci i percorsi assoluti dei tuoi file `.ini`.

#### 4. Esecuzione come Servizio (systemd)
```bash
sudo cp fleet-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable fleet-agent
sudo systemctl start fleet-agent
```

---
*Created by IV3JDV @ ARIFVG - 2026*
